"""Agent Runtime invocation: the one collaborator that did not exist.

ADR 001 says the bot addresses the Agent Runtime directly, as a Reasoning
Engine resource, rather than the Gemini Enterprise assistant. ADR 005 says the
streaming method is ``streaming_agent_run_with_events`` and not
``stream_query``, because the latter hides tool activity and tool activity is
the part worth showing.

So one call, to one URL::

    POST https://{loc}-aiplatform.googleapis.com/v1/{engine}:streamQuery?alt=sse

with a body that wraps the real request in a JSON string::

    {"class_method": "streaming_agent_run_with_events",
     "input": {"request_json": "{\\"message\\": ..., \\"user_id\\": ...}"}}

The double encoding is not a mistake in this file. ``AdkApp`` exposes
``streaming_agent_run_with_events(request_json: str)``, so the reasoning
engine's generic ``:streamQuery`` shim passes the string through verbatim and
the agent parses it. ``agent/NOTES.md`` section 4.4 records the same shape as
a working ``curl``.


THE CREDENTIAL RULE
-------------------
Two credentials are in play and ADR 002 exists because they get conflated:

* **Invocation Identity** authorizes calling the reasoning engine at all. Here
  that is the user's own workforce-federated token, in the ``Authorization``
  header. Layer 2 of ``spikes/FINDINGS.md`` proved a workforce principal can
  reach ``reasoningEngines.get`` and ``sessions.create``, so there is no reason
  to fall back to the service identity, and every reason not to: the audit log
  should name the human.

* **Tool Identity** is the same user's Google access token, forwarded so that
  BigQuery MCP acts as the human. It goes in the ``authorizations`` map, NOT in
  ``session_state``.

That second point is the whole reason this class is careful. Layer 3 of
``spikes/FINDINGS.md`` found that approaches writing the token into ordinary
Agent Runtime session state work perfectly and are still wrong: session state
is persisted by the managed Sessions service and readable by session id, so
that writes a live bearer token into durable conversation history. The runtime
turns ``authorizations["bigquery_user"]`` into the session-state key
``temp:bigquery_user``, and ADK strips ``temp:``-prefixed keys before
persistence. ``agent/bq_agent/credentials.py`` reads exactly that key, and its
``AUTHORIZATION_ID`` constant is the contract this file must match.

There is no code path here that invokes without a user token. A missing token
raises before the request is built.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Mapping

import aiohttp

from ..logging_utils import log_event
from ..ports import (
    AuthorizationDenied,
    IdentityUnavailable,
    SessionRef,
    TransientBackendError,
)

logger = logging.getLogger(__name__)

#: Must equal ``AUTHORIZATION_ID`` in ``agent/bq_agent/credentials.py``. The
#: runtime maps ``authorizations[<this>]`` to session state ``temp:<this>``,
#: which is what the agent's header provider reads. Changing this string on one
#: side only produces an agent that fails closed with "no user credential" and
#: gives no hint as to why, so both sides are asserted in the smoke test.
DEFAULT_AUTHORIZATION_ID = "bigquery_user"

#: ADR 005. The other option, ``stream_query``, is deliberately not offered as
#: a toggle: a config flag that silently downgrades the event stream to plain
#: text would remove tool visibility without removing the code that expects it.
CLASS_METHOD = "streaming_agent_run_with_events"

#: Server-sent-event data prefix. The runtime also emits bare JSON lines
#: depending on the ``alt`` parameter, so both are handled.
_SSE_DATA_PREFIX = "data:"


class RuntimeClientError(RuntimeError):
    """A programming/configuration error in this client. Never a user fault."""


class ReasoningEngineRuntimeClient:
    """Concrete :class:`app.ports.AgentRuntimeClient`.

    :param project: the project that owns the reasoning engine. May be the
        project *number*; the resource name accepts either.
    :param location: e.g. ``us-central1``. ``global`` returns an empty agent
        list in this project (``spikes/FINDINGS.md``), so it is not a default.
    :param reasoning_engine_id: numeric engine id.
    :param authorization_id: the key in the ``authorizations`` map. Must match
        the agent's ``AUTHORIZATION_ID``.
    :param quota_project: workforce principals have no project of their own, so
        the API cannot infer one to bill and refuses the call with a message
        that reads like a permissions error. Sent as ``X-Goog-User-Project``.
    :param http: shared session, owned by the application. One per process.
    """

    def __init__(
        self,
        *,
        project: str,
        location: str,
        reasoning_engine_id: str,
        authorization_id: str = DEFAULT_AUTHORIZATION_ID,
        quota_project: str | None = None,
        http: aiohttp.ClientSession | None = None,
        api_version: str = "v1",
        timeout_seconds: float = 300.0,
    ) -> None:
        if not project:
            raise RuntimeClientError("project is required")
        if not location:
            raise RuntimeClientError("location is required")
        if not reasoning_engine_id:
            raise RuntimeClientError("reasoning_engine_id is required")
        if not authorization_id:
            raise RuntimeClientError("authorization_id is required")

        self._project = project
        self._location = location
        self._engine_id = str(reasoning_engine_id)
        self._authorization_id = authorization_id
        self._quota_project = quota_project or project
        self._http = http
        self._owns_http = http is None
        self._api_version = api_version
        # A generous total budget: a turn that runs several BigQuery queries is
        # legitimately slow. sock_read is the one that matters for a stream --
        # it bounds the gap BETWEEN chunks, so a hung upstream is caught
        # without killing a long but healthy answer.
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds, sock_read=120.0)

    # -- addressing ---------------------------------------------------------

    @property
    def engine_name(self) -> str:
        return (
            f"projects/{self._project}/locations/{self._location}"
            f"/reasoningEngines/{self._engine_id}"
        )

    @property
    def endpoint(self) -> str:
        return f"https://{self._location}-aiplatform.googleapis.com"

    @property
    def stream_url(self) -> str:
        return f"{self.endpoint}/{self._api_version}/{self.engine_name}:streamQuery?alt=sse"

    @property
    def authorization_id(self) -> str:
        return self._authorization_id

    # -- lifecycle ----------------------------------------------------------

    async def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
            self._owns_http = True
        return self._http

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None and not self._http.closed:
            await self._http.close()

    # -- the request --------------------------------------------------------

    def build_request_json(
        self,
        *,
        session: SessionRef,
        message: str,
        user_access_token: str,
    ) -> str:
        """The inner, string-encoded request. Separated so it is testable.

        The user's token appears here exactly once, under ``authorizations``.
        Anything that adds a second copy under ``session_state`` is reverting
        the layer 3 finding.
        """
        if not user_access_token:
            # ADR 002. An invocation without the user token would still work --
            # the agent would fall back to nothing and fail, or worse, to
            # ambient credentials -- so this is checked here rather than trusted
            # to the callers.
            raise RuntimeClientError(
                "refusing to invoke the Agent Runtime without a user access token; "
                "ADR 002 requires Tool Identity to be the signed-in human"
            )
        payload: dict[str, Any] = {
            "message": {"role": "user", "parts": [{"text": message}]},
            "user_id": session.user_id,
            "session_id": session.session_id,
            "authorizations": {
                self._authorization_id: {"access_token": user_access_token}
            },
        }
        return json.dumps(payload)

    async def stream_query(
        self,
        *,
        session: SessionRef,
        message: str,
        user_access_token: str,
        request_id: str | None = None,
    ) -> AsyncIterator[Mapping[str, Any]]:
        """Invoke the agent and yield raw ADK event payloads as they arrive.

        Yields the runtime's dicts untouched. ``app.streaming.events`` owns all
        interpretation of them, and it accepts the envelope, a bare event, a
        list and a JSON string, so nothing is normalised here. The middle tier
        relays; it does not read content.
        """
        body = {
            "class_method": CLASS_METHOD,
            "input": {
                "request_json": self.build_request_json(
                    session=session,
                    message=message,
                    user_access_token=user_access_token,
                )
            },
        }

        headers = {
            # Invocation Identity: the user, not the service. ADR 002.
            "Authorization": f"Bearer {user_access_token}",
            "Content-Type": "application/json",
            "X-Goog-User-Project": self._quota_project,
        }
        if request_id:
            headers["X-Request-Id"] = request_id

        http = await self._session()
        log_event(
            logger,
            logging.INFO,
            "agent runtime invocation starting",
            engine=self.engine_name,
            session_id=session.session_id,
            user_key=session.user_id,
            request_id=request_id,
        )

        try:
            async with http.post(
                self.stream_url,
                json=body,
                headers=headers,
                timeout=self._timeout,
            ) as resp:
                if resp.status >= 400:
                    detail = await resp.text()
                    self._raise_for_status(resp.status, detail)

                async for chunk in self._iter_events(resp):
                    yield chunk

        except asyncio.TimeoutError as exc:
            raise TransientBackendError(
                f"agent runtime timed out on {self.engine_name}"
            ) from exc
        except aiohttp.ClientError as exc:
            raise TransientBackendError(
                f"agent runtime transport error on {self.engine_name}: "
                f"{type(exc).__name__}"
            ) from exc

    # -- stream decoding ----------------------------------------------------

    async def _iter_events(
        self, resp: aiohttp.ClientResponse
    ) -> AsyncIterator[Mapping[str, Any]]:
        """Decode SSE ``data:`` lines, or bare JSON lines, into dicts.

        Undecodable lines are logged and skipped rather than raising. A single
        malformed frame should not lose an answer that is otherwise arriving,
        and the alternative -- raising -- turns a cosmetic upstream glitch into
        a failed turn. Genuine mid-stream failures still surface, because the
        transport error path above is separate from this one.
        """
        async for raw in resp.content:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if line.startswith(_SSE_DATA_PREFIX):
                line = line[len(_SSE_DATA_PREFIX) :].strip()
            if not line or line == "[DONE]":
                continue
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError:
                log_event(
                    logger,
                    logging.WARNING,
                    "undecodable frame from the agent runtime; skipping",
                    engine=self.engine_name,
                    frame_length=len(line),
                )
                continue
            if isinstance(decoded, list):
                for item in decoded:
                    if isinstance(item, Mapping):
                        yield item
                continue
            if isinstance(decoded, Mapping):
                yield decoded

    # -- failure classification ---------------------------------------------

    def _raise_for_status(self, status: int, detail: str) -> None:
        """Map HTTP status onto the shared ADR 004 taxonomy.

        The split that matters is "the user is not allowed" (terminal, gets a
        templated denial naming the resource) versus "the backend is having a
        bad day" (retryable, gets the transient message). Collapsing them would
        let a permanent denial masquerade as a blip and be retried forever.
        """
        snippet = detail[:600]
        if status in (401,):
            # The bearer token was rejected outright. The middle tier drops the
            # cached token and re-prompts sign-in once.
            raise IdentityUnavailable(
                f"agent runtime rejected the user token: {snippet}"
            )
        if status == 403:
            raise AuthorizationDenied(self.engine_name, snippet)
        if status == 404:
            # Not retryable and not a permissions problem, but from the user's
            # point of view the engine is simply not there. Naming it is more
            # useful than a generic failure.
            raise AuthorizationDenied(self.engine_name, f"not found: {snippet}")
        if status == 429 or status >= 500:
            raise TransientBackendError(
                f"agent runtime {self.engine_name} failed with {status}: {snippet}"
            )
        raise TransientBackendError(
            f"agent runtime {self.engine_name} failed with {status}: {snippet}"
        )


__all__ = [
    "CLASS_METHOD",
    "DEFAULT_AUTHORIZATION_ID",
    "ReasoningEngineRuntimeClient",
    "RuntimeClientError",
]
