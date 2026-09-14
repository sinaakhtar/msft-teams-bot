"""A thin REST client for the reasoning engine ``sessions`` subresource.

ADR 005 chose the PLATFORM surface over the agent surface. The deployed agent
in ``example-project`` also exports ``create_session`` / ``get_session`` /
``list_sessions`` / ``delete_session`` class methods, and they work. We do not
use them. The middle tier depends on the managed Sessions service, so the bot
keeps working when the agent is rewritten, redeployed or replaced with a
different agent that exports a different set of methods.

WHAT THIS CLIENT DELIBERATELY DOES NOT HAVE
-------------------------------------------
1. **No event-append method.** There is a real REST method for it
   (``sessions.appendEvent``, present in the v1 discovery document) and it is
   deliberately not wrapped here. ADR 005: the middle tier READS history, the
   runtime WRITES it. When the runtime streams a turn it appends the user
   content, the tool calls, the tool results and the model output itself, in
   the order and shape the ADK expects. A middle tier that also appends gets
   duplicated user turns, events with an invocation id the runtime never
   issued, and a history that silently stops replaying correctly. That
   corruption is not visible on the turn that causes it - it shows up later as
   an agent that has apparently lost its mind. The absence of the method is the
   enforcement mechanism: you cannot call what is not here, so a future
   contributor has to consciously add it (and hit this comment) rather than
   reach for an autocomplete suggestion.
2. **No delete method.** ``sessions.delete`` exists in the API. The Conversation
   Reset (``/new``) semantics require that an abandoned session is NOT deleted:
   a reset discards CONTEXT, it does not destroy HISTORY, and the abandoned
   session stays retrievable by id for support and audit. Since nothing else in
   the middle tier has a reason to delete a session, the safest place to hold
   that line is here, by not offering the verb.

API VERSION
-----------
``v1``. Two independent confirmations:

1. A live ``sessions.create`` against the ``v1`` path in ``example-project``
   returned HTTP 200 (executed outside this workspace, response body quoted in
   NOTES.md).
2. The live public discovery document
   (``https://aiplatform.googleapis.com/$discovery/rest?version=v1``, revision
   ``20260831``, fetched here on 2026-09-07) lists the method table for
   ``projects.locations.reasoningEngines.sessions``:

       create  POST v1/{+parent}/sessions   -> GoogleLongrunningOperation
       get     GET  v1/{+name}              -> GoogleCloudAiplatformV1Session
       list    GET  v1/{+parent}/sessions   -> ListSessionsResponse
       events.list GET v1/{+parent}/events  -> ListEventsResponse

``v1beta1`` (same revision) exposes the identical method set for this resource,
so nothing in the session lifecycle requires the beta surface. We take ``v1``
because it carries the stronger compatibility guarantee. The version is a
single constant below; if a future field is beta-only, change it in one place
and say so in NOTES.md.

ENGINE ADDRESSING
-----------------
The engine id is REQUIRED configuration and is never resolved by display name.
``reasoningEngines.list`` in ``us-central1`` currently returns three separate
engines all displaying as ``data_science_agent``, so a name lookup is
ambiguous and would silently attach users to whichever one sorted first.

AUTHORIZATION
-------------
ADR 002: every call is made with the USER's Workforce Principal access token,
passed in per call. There is no ambient credential, no
``google.auth.default()``, and no service-account fallback anywhere in this
module - a fallback would silently re-attribute a user's session to the service
identity, which is the exact failure ADR 002 exists to prevent. The token is a
plain bearer; this client never inspects, caches, logs or refreshes it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

import aiohttp

from ..ports import (
    AuthorizationDenied,
    IdentityUnavailable,
    PortError,
    SessionNotFound,
    TransientBackendError,
)

logger = logging.getLogger(__name__)

#: The Vertex AI API version used for the sessions subresource. See the module
#: docstring for how this was verified.
SESSIONS_API_VERSION = "v1"

#: Sessions live on a regional endpoint. ``global`` has no sessions surface for
#: reasoning engines, so the location is always a real region here.
_ENDPOINT_TEMPLATE = "https://{location}-aiplatform.googleapis.com"

#: How long to keep polling a create LRO that came back not-done, and how long
#: to wait between polls. Creation has been observed to return an
#: already-complete operation, but "observed" is not "guaranteed", so the poll
#: path is implemented rather than assumed away.
_LRO_POLL_TIMEOUT_SECONDS = 30.0
_LRO_POLL_INTERVAL_SECONDS = 0.5

_DEFAULT_TIMEOUT_SECONDS = 30.0

#: Deliberately generic in the project component. The service echoes resource
#: names using the project NUMBER (000000000000) even when the request was
#: addressed with the project ID (example-project), so any equality check between a
#: name we built and a name the API returned would spuriously fail. Compare the
#: location and engine id, never the project token.
_SESSION_NAME_RE = re.compile(
    r"^projects/[^/]+/locations/[^/]+/reasoningEngines/[^/]+/sessions/[^/]+$"
)


class SessionOwnershipMismatch(PortError):
    """The service returned a session whose ``userId`` is not the one we asked for.

    Fail closed rather than hand the caller a session belonging to someone
    else: that would put one person's turns into another person's history.
    Never seen in practice; cheap to assert, catastrophic to miss.
    """


@dataclass(frozen=True)
class Session:
    """One Agent Runtime Session, as returned by the Sessions service.

    :param name: full resource name,
        ``projects/{p}/locations/{l}/reasoningEngines/{e}/sessions/{s}``.
    :param session_id: the last path component. This is what the
        :class:`~app.sessions.manager.AgentRuntimeSessionManager` contract
        returns and what the runtime call takes.
    :param user_id: the owning principal, ``entra:{tid}:{oid}`` per ADR 003.
    :param expire_time: service-side expiry (RFC 3339 string as returned).
        NOTE: this is the managed service's own TTL, minimum 24 hours. It is
        NOT the 60-minute idle policy, which is a middle-tier decision and is
        invisible to the service.
    :param raw: the untouched response body, for logging and for fields we do
        not model yet.
    """

    name: str
    session_id: str
    user_id: str
    create_time: str | None = None
    update_time: str | None = None
    expire_time: str | None = None
    display_name: str | None = None
    raw: Mapping[str, Any] | None = None

    @classmethod
    def from_api(cls, body: Mapping[str, Any]) -> "Session":
        name = str(body.get("name") or "")
        if not name:
            raise TransientBackendError(
                "sessions API returned a session with no resource name"
            )
        if "/operations/" in name or not _SESSION_NAME_RE.match(name):
            # Guard against the single most likely LRO bug: taking the id from
            # the OPERATION name instead of from the session inside
            # ``operation.response``. Both end in an opaque numeric-ish
            # component, so the mistake produces a plausible-looking id that
            # 404s on the next turn.
            raise TransientBackendError(
                f"sessions API returned a name that is not a session: {name!r}"
            )
        return cls(
            name=name,
            session_id=name.rsplit("/", 1)[-1],
            user_id=str(body.get("userId") or ""),
            create_time=body.get("createTime"),
            update_time=body.get("updateTime"),
            expire_time=body.get("expireTime"),
            display_name=body.get("displayName"),
            raw=dict(body),
        )


@runtime_checkable
class SessionsClient(Protocol):
    """The surface the session manager depends on.

    Narrow on purpose: three verbs, all of them safe. Tests substitute a fake
    that implements exactly this, which is also a standing check that the
    manager never grew a dependency on a mutating verb.
    """

    async def create_session(
        self,
        *,
        user_id: str,
        access_token: str,
        display_name: str | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> Session: ...

    async def get_session(self, *, name: str, access_token: str) -> Session: ...

    async def list_sessions(
        self,
        *,
        access_token: str,
        user_id: str | None = None,
        labels: Mapping[str, str] | None = None,
        page_size: int = 100,
    ) -> Sequence[Session]: ...


def conversation_label(conversation_id: str) -> str:
    """Label-safe fingerprint of a Teams conversation id.

    Session labels accept ``[a-z0-9_-]`` up to 63 characters; Teams
    conversation ids contain ``:``, ``@`` and mixed case, so the raw id cannot
    be a label value. A truncated SHA-256 is stable, label-safe, and reversible
    only by someone who already has the conversation id - which is the property
    we want, because it makes ``sessions.list`` filterable by conversation
    without publishing conversation ids into resource metadata.

    This exists to make the "reconstruct the mapping by listing sessions"
    production option in :mod:`.store` actually implementable. It is a hint,
    not a source of truth.
    """
    return hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()[:32]


class SessionsRestClient:
    """REST client for ``projects.locations.reasoningEngines.sessions``.

    One instance per process; it owns an :class:`aiohttp.ClientSession` (a
    fourth unrelated thing called "session" - an HTTP connection pool, nothing
    to do with Agent Runtime Sessions).
    """

    def __init__(
        self,
        *,
        project: str,
        location: str,
        reasoning_engine_id: str,
        quota_project: str | None = None,
        http: aiohttp.ClientSession | None = None,
        api_version: str = SESSIONS_API_VERSION,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        lro_poll_timeout_seconds: float = _LRO_POLL_TIMEOUT_SECONDS,
        lro_poll_interval_seconds: float = _LRO_POLL_INTERVAL_SECONDS,
    ) -> None:
        if not project or not location or not reasoning_engine_id:
            raise ValueError(
                "project, location and reasoning_engine_id are all required; "
                "an unset engine id would make every call target a nonexistent "
                "resource and read as a permissions problem"
            )
        self._project = project
        self._location = location
        self._engine_id = reasoning_engine_id
        # A Workforce Principal has no Google project of its own, so nothing
        # infers a billing/quota project from the credential. Send it
        # explicitly or the call can be refused for quota attribution reasons
        # that read like a permissions error. Defaults to the resource project.
        self._quota_project = quota_project or project
        self._api_version = api_version
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._lro_poll_timeout = lro_poll_timeout_seconds
        self._lro_poll_interval = lro_poll_interval_seconds
        self._http = http
        self._owns_http = http is None

    # -- addressing --------------------------------------------------------

    @property
    def endpoint(self) -> str:
        return _ENDPOINT_TEMPLATE.format(location=self._location)

    @property
    def parent(self) -> str:
        """The reasoning engine resource name that owns the sessions."""
        return (
            f"projects/{self._project}/locations/{self._location}"
            f"/reasoningEngines/{self._engine_id}"
        )

    def session_name(self, session_id: str) -> str:
        return f"{self.parent}/sessions/{session_id}"

    def _url(self, resource: str) -> str:
        return f"{self.endpoint}/{self._api_version}/{resource}"

    # -- lifecycle ---------------------------------------------------------

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None and not self._http.closed:
            await self._http.close()

    async def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
            self._owns_http = True
        return self._http

    # -- verbs -------------------------------------------------------------

    async def create_session(
        self,
        *,
        user_id: str,
        access_token: str,
        display_name: str | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> Session:
        """Create an Agent Runtime Session owned by ``user_id``.

        ``user_id`` is ``entra:{tid}:{oid}`` (ADR 003). A Microsoft user with
        no Google account can own a session under that id: verified live in
        ``example-project`` with
        ``entra:00000000-0000-0000-0000-000000000000:33333333-3333-3333-3333-333333333333``,
        which returned 200 and a session resource carrying that exact
        ``userId``.

        THE LRO. ``sessions.create`` returns a
        ``google.longrunning.Operation``, not a bare Session. In the live call
        it came back already ``done: true`` with the session inline under
        ``response``. We do not rely on that: if ``done`` is false we poll
        ``GET {operation.name}`` until it completes or the poll budget runs
        out. If the operation carries an ``error``, it is raised, mapped by
        code the same way an HTTP status would be. If it completes with no
        ``response`` body (possible in principle), we fall back to a ``get`` on
        the session name from the operation metadata.
        """
        body: dict[str, Any] = {"userId": user_id}
        if display_name:
            body["displayName"] = display_name[:128]
        if labels:
            body["labels"] = dict(labels)

        operation = await self._request(
            "POST",
            self._url(f"{self.parent}/sessions"),
            access_token=access_token,
            json_body=body,
            resource=f"reasoning engine {self._engine_id} (sessions.create)",
        )
        session = await self._session_from_lro(operation, access_token=access_token)
        if session.user_id != user_id:
            # The live create echoes back the exact userId that was sent. If it
            # ever does not, we are about to file one person's turns under
            # another person's history: stop.
            raise SessionOwnershipMismatch(
                f"requested userId {user_id!r} but the service returned "
                f"{session.user_id!r} for {session.name}"
            )
        return session

    async def get_session(self, *, name: str, access_token: str) -> Session:
        """Fetch one session by full resource name or by bare session id."""
        full = name if "/" in name else self.session_name(name)
        if not _SESSION_NAME_RE.match(full):
            raise ValueError(f"not a session resource name: {name!r}")
        body = await self._request(
            "GET",
            self._url(full),
            access_token=access_token,
            resource=f"session {full.rsplit('/', 1)[-1]}",
        )
        return Session.from_api(body)

    async def list_sessions(
        self,
        *,
        access_token: str,
        user_id: str | None = None,
        labels: Mapping[str, str] | None = None,
        page_size: int = 100,
    ) -> Sequence[Session]:
        """List sessions on this engine, optionally filtered.

        The API's list filter supports ``display_name``, ``user_id`` and
        ``labels.<key>`` (checked in the v1 discovery document). Filtering by
        ``user_id`` is what makes the "rebuild the mapping after an instance
        restart" option in :mod:`.store` possible at all, and filtering by the
        conversation label narrows it to one Teams conversation.
        """
        clauses = []
        if user_id:
            clauses.append(f'user_id="{user_id}"')
        for key, value in (labels or {}).items():
            clauses.append(f'labels.{key}="{value}"')

        params: dict[str, str] = {"pageSize": str(page_size)}
        if clauses:
            params["filter"] = " AND ".join(clauses)

        out: list[Session] = []
        page_token: str | None = None
        while True:
            if page_token:
                params["pageToken"] = page_token
            body = await self._request(
                "GET",
                self._url(f"{self.parent}/sessions"),
                access_token=access_token,
                params=params,
                resource=f"reasoning engine {self._engine_id} (sessions.list)",
            )
            out.extend(Session.from_api(s) for s in body.get("sessions") or [])
            page_token = body.get("nextPageToken") or None
            if not page_token:
                return out

    async def list_events(
        self,
        *,
        name: str,
        access_token: str,
        page_size: int = 100,
    ) -> Sequence[Mapping[str, Any]]:
        """READ the events of a session. Read-only, on purpose.

        This is the half of ADR 005 that the middle tier IS allowed to do:
        read history. Returns raw event dicts rather than a modelled type,
        because the ADK event shape is the agent's surface and pinning a
        dataclass to it here would make an agent rewrite a middle-tier change.

        There is no companion write method. See the module docstring.
        """
        full = name if "/" in name else self.session_name(name)
        out: list[Mapping[str, Any]] = []
        params: dict[str, str] = {"pageSize": str(page_size)}
        page_token: str | None = None
        while True:
            if page_token:
                params["pageToken"] = page_token
            body = await self._request(
                "GET",
                self._url(f"{full}/events"),
                access_token=access_token,
                params=params,
                resource=f"events of session {full.rsplit('/', 1)[-1]}",
            )
            out.extend(body.get("sessionEvents") or [])
            page_token = body.get("nextPageToken") or None
            if not page_token:
                return out

    # -- LRO handling ------------------------------------------------------

    async def _session_from_lro(
        self, operation: Mapping[str, Any], *, access_token: str
    ) -> Session:
        deadline = asyncio.get_running_loop().time() + self._lro_poll_timeout
        polls = 0

        while True:
            if operation.get("error"):
                self._raise_for_operation_error(operation["error"])

            if operation.get("done"):
                response = operation.get("response")
                if isinstance(response, Mapping) and response.get("name"):
                    if polls:
                        logger.info(
                            "sessions.create LRO completed after %d poll(s)", polls
                        )
                    return Session.from_api(response)
                # Done, but no inline resource. Recover the name from the
                # operation name: it is the session's own operations
                # collection, i.e. .../sessions/{sid}/operations/{opid}.
                name = str(operation.get("name") or "")
                if "/sessions/" in name:
                    session_name = name.split("/operations/", 1)[0]
                    return await self.get_session(
                        name=session_name, access_token=access_token
                    )
                raise TransientBackendError(
                    "sessions.create returned a completed operation with no "
                    f"session resource and no recoverable name: {name!r}"
                )

            op_name = str(operation.get("name") or "")
            if not op_name:
                raise TransientBackendError(
                    "sessions.create returned an incomplete operation with no name"
                )
            if asyncio.get_running_loop().time() >= deadline:
                raise TransientBackendError(
                    f"sessions.create operation {op_name} still running after "
                    f"{self._lro_poll_timeout:.0f}s"
                )
            await asyncio.sleep(self._lro_poll_interval)
            polls += 1
            operation = await self._request(
                "GET",
                self._url(op_name),
                access_token=access_token,
                resource=f"operation {op_name.rsplit('/', 1)[-1]}",
            )

    @staticmethod
    def _raise_for_operation_error(error: Mapping[str, Any]) -> None:
        code = error.get("code")
        message = str(error.get("message") or "operation failed")
        if code in (7, 16):  # PERMISSION_DENIED, UNAUTHENTICATED
            raise AuthorizationDenied("Agent Runtime session", message)
        raise TransientBackendError(f"sessions.create failed: {message}")

    # -- transport ---------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        *,
        access_token: str,
        resource: str,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> Mapping[str, Any]:
        if not access_token:
            # ADR 002. No token, no call - and specifically no fallback to an
            # ambient service-account credential.
            raise IdentityUnavailable(
                "no user access token supplied for a sessions call"
            )
        http = await self._session()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=utf-8",
            # Quota/billing attribution for a principal that owns no project.
            "x-goog-user-project": self._quota_project,
        }
        try:
            async with http.request(
                method,
                url,
                headers=headers,
                json=dict(json_body) if json_body is not None else None,
                params=dict(params) if params else None,
                timeout=self._timeout,
            ) as response:
                text = await response.text()
                if response.status >= 400:
                    self._raise_for_status(response.status, text, resource=resource)
                if not text:
                    return {}
                try:
                    return json.loads(text)
                except json.JSONDecodeError as exc:
                    raise TransientBackendError(
                        f"sessions API returned non-JSON body ({exc})"
                    ) from exc
        except asyncio.TimeoutError as exc:
            raise TransientBackendError(f"sessions API timeout on {resource}") from exc
        except aiohttp.ClientError as exc:
            raise TransientBackendError(
                f"sessions API transport error on {resource}: {type(exc).__name__}"
            ) from exc

    @staticmethod
    def _raise_for_status(status: int, text: str, *, resource: str) -> None:
        """Map HTTP status onto the shared ADR 004 error taxonomy.

        The distinction that matters: "you are not allowed" (terminal, render a
        denial that names the resource) versus "the backend is having a bad
        day" (retryable). Collapsing them into one exception breaks ADR 004.
        The raw body is passed as ``detail`` for logs only; ADR 004 forbids
        handing it to the model.
        """
        detail = text[:400]
        if status == 401:
            raise IdentityUnavailable(
                f"sessions API rejected the user credential (401) on {resource}"
            )
        if status == 403:
            raise AuthorizationDenied(resource, detail)
        if status == 404:
            raise SessionNotFound(f"{resource} not found (404)")
        if status == 409:
            # Session id already exists. The manager never supplies a session
            # id, so this is a genuine conflict rather than a retry artifact.
            raise TransientBackendError(f"conflict on {resource} (409): {detail}")
        if status == 429 or status >= 500:
            raise TransientBackendError(f"{resource} failed with {status}: {detail}")
        raise TransientBackendError(f"{resource} failed with {status}: {detail}")


__all__ = [
    "SESSIONS_API_VERSION",
    "Session",
    "SessionsClient",
    "SessionsRestClient",
    "conversation_label",
]
