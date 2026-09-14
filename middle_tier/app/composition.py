"""The composition root: one place that turns ``Settings`` into ``Dependencies``.

Before this module the service was fully built and fully disconnected. Every
collaborator named in ``app/ports.py`` except one had a real, tested
implementation, and nothing anywhere constructed them. ``Dependencies()`` was
instantiated with its four defaults -- all ``None`` -- so a deployed instance
validated inbound JWTs correctly, extracted the Entra identity correctly, and
then answered every message with the "components not wired" transient
template. It looked healthy from the outside. That is the failure this module
removes.


WHY A COMPOSITION ROOT AND NOT A FACTORY PER PACKAGE
-----------------------------------------------------
There already is a factory per package: ``build_identity_broker`` in
``app/identity/broker.py``. It is a good factory and this module calls it. What
was missing is the single place that knows how the four fit together, which is
also the only place that can answer "is this deployment actually able to serve
a turn". Spread that answer across four packages and no one owns it.


THE ADAPTERS, AND WHY THEY ARE NOT SECOND IMPLEMENTATIONS
----------------------------------------------------------
Three of the four concrete components were built against a slightly different
shape than ``app/ports.py`` declares, because the ports were written first, as
contracts for parallel work, and the implementations learned things. Rather
than fork any of them, this module adapts:

* ``PortsIdentityBroker`` -- ``ChainedIdentityBroker`` already has the right
  method names. What it does not have is the right *exceptions*: it raises
  ``IdentityAcquisitionError``, which descends from ``Exception``, not from
  ``app.ports.PortError``. The router catches the port taxonomy. So without
  translation, every single identity failure -- an expired assertion, a missing
  consent, an STS 403 -- would escape the router's handlers, become an
  unhandled exception, and return HTTP 500. Azure Bot Service would then retry
  the doomed activity. This adapter is the difference between ADR 004
  fail-closed behaviour and an outage.

* ``PortsSessionManager`` -- ``AgentRuntimeSessionManager.resolve`` needs
  ``conversation_id`` and ``access_token``, and returns a bare session id. The
  port's ``get_or_create(user_key)`` has neither parameter, which means the
  port as originally written could not make the sessions call as the user at
  all; it could only have been satisfied by a service-account call, which is
  the exact thing ADR 002 forbids. The port has been corrected to the
  implementation's shape rather than the other way round, and this adapter
  wraps the returned id back into the ``SessionRef`` the runtime client wants.

* ``TeamsRendererFactory`` -- ``TeamsStreamingRenderer`` is pull-style
  (``render(events, sink)``) and is documented as one instance per turn. The
  port is push-style (``begin``/``push``/``finish``) and the router holds one
  ``Dependencies`` for the life of the process. Sharing a single push renderer
  across concurrent turns would interleave two users' answers into one Teams
  bubble. So the dependency is a *factory*, and the per-turn object it hands
  back is a thin push facade over the existing renderer. The renderer's own
  docstring anticipates exactly this adapter and declines to write it; this is
  it, and it adds no rendering logic of its own.


FAIL LOUDLY
-----------
``build_dependencies`` either returns a ``Dependencies`` with four non-``None``
collaborators or it raises. There is no partial success and no silent ``None``.
A deployment missing ``REASONING_ENGINE_ID`` now fails at startup with a
message naming it, instead of starting green and answering every question with
a shrug.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Mapping

import aiohttp

from .config import Settings
from .identity.broker import ChainedIdentityBroker, build_identity_broker
from .identity.errors import (
    IdentityAcquisitionError,
    StsPermissionDenied,
)
from .logging_utils import log_event
from .ports import (
    AuthorizationDenied,
    IdentityUnavailable,
    SessionRef,
    TransientBackendError,
)
from .routing import Dependencies
from .runtime import ReasoningEngineRuntimeClient
from .sessions.client import SessionsRestClient
from .sessions.manager import AgentRuntimeSessionManager
from .streaming.connector import BotConnectorTransport
from .streaming.renderer import TeamsStreamingRenderer
from .streaming.teams_sink import ConnectorTeamsSink

logger = logging.getLogger(__name__)


class CompositionError(RuntimeError):
    """A collaborator could not be constructed. Always fatal at startup."""


# ==========================================================================
# Identity: exception translation
# ==========================================================================


class PortsIdentityBroker:
    """Adapts ``ChainedIdentityBroker`` failures into the port taxonomy.

    Deliberately holds no policy of its own beyond the mapping below, and
    never converts a failure into a credential.
    """

    def __init__(self, inner: ChainedIdentityBroker, *, sts_audience: str) -> None:
        self._inner = inner
        self._sts_audience = sts_audience

    @property
    def inner(self) -> ChainedIdentityBroker:
        return self._inner

    async def get_google_access_token(
        self, user_key: str, teams_sso_token: str
    ) -> str:
        try:
            return await self._inner.google_access_token(
                user_key=user_key, teams_sso_token=teams_sso_token
            )
        except IdentityAcquisitionError as exc:
            raise self._translate(exc) from exc

    async def invalidate(self, user_key: str) -> None:
        await self._inner.invalidate(user_key)

    def _translate(self, exc: IdentityAcquisitionError) -> Exception:
        log_event(
            logger,
            logging.WARNING,
            "identity acquisition failed; translating to the port taxonomy",
            **exc.as_log_fields(),
        )
        if isinstance(exc, StsPermissionDenied):
            # A 403 from STS is not fixed by signing in again: either the
            # workforce principal has no role, or the quota project has not
            # granted serviceusage.serviceUsageConsumer. ADR 004 wants that
            # named, so it becomes a denial with a resource rather than a
            # sign-in card that would send the user round a loop.
            return AuthorizationDenied(self._sts_audience, exc.detail or str(exc))
        if exc.retryable:
            return TransientBackendError(str(exc))
        return IdentityUnavailable(str(exc))


# ==========================================================================
# Sessions: shape + SessionRef
# ==========================================================================


class PortsSessionManager:
    """Adapts ``AgentRuntimeSessionManager`` to the corrected port shape.

    The only real work here is rebuilding a :class:`SessionRef` from the bare
    session id the manager returns, because the runtime client needs both the
    id and the ``user_id`` to construct its request, and ``user_id`` is the ADR
    003 key that must never be a Teams MRI.
    """

    def __init__(
        self, inner: AgentRuntimeSessionManager, *, client: SessionsRestClient
    ) -> None:
        self._inner = inner
        self._client = client

    @property
    def inner(self) -> AgentRuntimeSessionManager:
        return self._inner

    async def get_or_create(
        self, user_key: str, *, conversation_id: str, access_token: str
    ) -> SessionRef:
        session_id = await self._inner.resolve(
            user_key=user_key,
            conversation_id=conversation_id,
            access_token=access_token,
        )
        return self._ref(session_id, user_key)

    async def reset(
        self, user_key: str, *, conversation_id: str, access_token: str
    ) -> SessionRef:
        session_id = await self._inner.reset(
            user_key=user_key,
            conversation_id=conversation_id,
            access_token=access_token,
        )
        return self._ref(session_id, user_key)

    def _ref(self, session_id: str, user_key: str) -> SessionRef:
        return SessionRef(
            name=self._client.session_name(session_id),
            user_id=user_key,
            session_id=session_id,
        )


# ==========================================================================
# Streaming: per-turn push facade over the pull-style renderer
# ==========================================================================


class _StreamClosed:
    """Sentinel: the router pushed its last event."""


@dataclass
class _StreamFailed:
    """Sentinel: the router's upstream raised, and the bubble must terminate."""

    error: BaseException


class TurnRenderer:
    """One turn's worth of rendering. Push in, Teams activities out.

    Wraps :class:`TeamsStreamingRenderer` without reimplementing any of it: the
    events pushed by the router are put on a queue, and the existing renderer
    consumes that queue as the async iterator it already expects.
    """

    def __init__(
        self,
        *,
        renderer: TeamsStreamingRenderer,
        sink: Any,
        conversation_ref: Mapping[str, Any],
        log: logging.Logger | None = None,
    ) -> None:
        self._renderer = renderer
        self._sink = sink
        self._conversation_ref = conversation_ref
        self._log = log or logger
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._task: asyncio.Task[str] | None = None
        self._final_text: str = ""

    @property
    def final_text(self) -> str:
        return self._final_text

    async def begin(self, conversation_ref: Mapping[str, Any]) -> None:
        if self._task is not None:  # pragma: no cover - router calls once
            return
        self._task = asyncio.create_task(
            self._renderer.render(self._events(), self._sink)
        )

    async def push(self, event: Any) -> None:
        if self._task is None:
            # push without begin is a router bug, not a user condition. Do not
            # silently start rendering; that would hide the ordering error.
            raise RuntimeError("TurnRenderer.push called before begin")
        await self._queue.put(event)

    async def finish(self, *, error: Exception | None = None) -> None:
        if self._task is None:
            return
        await self._queue.put(
            _StreamFailed(error) if error is not None else _StreamClosed()
        )
        try:
            self._final_text = await self._task
        except Exception:
            # The renderer already logs and already tries to terminate the
            # Teams bubble. Re-raising here would replace whatever the router
            # was about to tell the user with a 500.
            self._log.exception("renderer failed while finishing the turn")
        finally:
            self._task = None

    async def _events(self) -> AsyncIterator[Any]:
        while True:
            item = await self._queue.get()
            if isinstance(item, _StreamClosed):
                return
            if isinstance(item, _StreamFailed):
                # Re-raise inside the renderer's own loop, which is where its
                # ADR 004 denial handling lives. Translating the failure here
                # would duplicate that logic in a second place.
                raise item.error
            yield item


class TeamsRendererFactory:
    """Builds one :class:`TurnRenderer` per turn, bound to one conversation."""

    def __init__(
        self,
        *,
        transport: BotConnectorTransport,
        tool_labels: Mapping[str, str] | None = None,
    ) -> None:
        self._transport = transport
        self._tool_labels = tool_labels

    @property
    def transport(self) -> BotConnectorTransport:
        return self._transport

    def for_turn(
        self,
        conversation_ref: Mapping[str, Any],
        *,
        request_id: str | None = None,
        user_display: str | None = None,
    ) -> TurnRenderer:
        send = self._transport.sender_for(conversation_ref)
        sink = ConnectorTeamsSink(send)
        renderer = TeamsStreamingRenderer(
            tool_labels=self._tool_labels,
            request_id=request_id,
            user_display=user_display,
        )
        return TurnRenderer(
            renderer=renderer, sink=sink, conversation_ref=conversation_ref
        )


# ==========================================================================
# The root
# ==========================================================================


def build_dependencies(
    settings: Settings, *, http: aiohttp.ClientSession
) -> Dependencies:
    """Construct every collaborator, or raise.

    :param http: one shared aiohttp session for the process, owned by the
        application and closed on shutdown. Not one per request: the OBO/STS
        and Bot Connector token caches are only useful if connections and
        state are shared.
    :raises CompositionError: any collaborator that cannot be built. The
        message names the setting that is missing, because "the bot does not
        answer" is not a debuggable symptom.
    """
    missing = [
        name
        for name, value in (
            ("REASONING_ENGINE_ID", settings.reasoning_engine_id),
            ("GCP_PROJECT_ID", settings.gcp_project_id),
            ("GCP_LOCATION", settings.location),
            ("ENTRA_TENANT_ID", settings.entra_tenant_id),
            ("MICROSOFT_APP_ID", settings.microsoft_app_id),
            ("MICROSOFT_APP_PASSWORD", settings.microsoft_app_password),
            ("ENTRA_CLIENT_SECRET", settings.entra_client_secret),
            ("FEDERATION_APP_ID", settings.federation_app_id),
            ("WORKFORCE_POOL_ID", settings.workforce_pool_id),
            ("WORKFORCE_PROVIDER_ID", settings.workforce_provider_id),
        )
        if not value
    ]
    if missing:
        raise CompositionError(
            "cannot assemble the middle tier; missing configuration: "
            + ", ".join(missing)
            + ". Refusing to start with unwired collaborators, because a "
            "service that starts and then declines every turn is harder to "
            "diagnose than one that does not start."
        )

    # --- Tool Identity (ADR 002) -------------------------------------------
    broker = build_identity_broker(
        session=http,
        tenant_id=settings.entra_tenant_id,
        bot_client_id=settings.microsoft_app_id,
        bot_client_secret=settings.entra_client_secret,
        federation_app_id=settings.federation_app_id,
        workforce_pool_id=settings.workforce_pool_id,
        workforce_provider_id=settings.workforce_provider_id,
        user_project=settings.gcp_project_id,
    )
    sts_audience = (
        f"//iam.googleapis.com/locations/global/workforcePools/"
        f"{settings.workforce_pool_id}/providers/{settings.workforce_provider_id}"
    )
    identity_broker = PortsIdentityBroker(broker, sts_audience=sts_audience)

    # --- Sessions (ADR 005) ------------------------------------------------
    sessions_client = SessionsRestClient(
        project=settings.gcp_project_id,
        location=settings.location,
        reasoning_engine_id=settings.reasoning_engine_id,
        quota_project=settings.gcp_project_id,
        http=http,
    )
    sessions = PortsSessionManager(
        AgentRuntimeSessionManager(client=sessions_client),
        client=sessions_client,
    )

    # --- Invocation (ADR 001) ----------------------------------------------
    runtime = ReasoningEngineRuntimeClient(
        project=settings.gcp_project_id,
        location=settings.location,
        reasoning_engine_id=settings.reasoning_engine_id,
        authorization_id=settings.runtime_authorization_id,
        quota_project=settings.gcp_project_id,
        http=http,
    )

    # --- Reply path --------------------------------------------------------
    transport = BotConnectorTransport(
        app_id=settings.microsoft_app_id,
        app_password=settings.microsoft_app_password,
        tenant_id=settings.entra_tenant_id,
        single_tenant=settings.microsoft_app_type != "MultiTenant",
        http=http,
    )
    renderer = TeamsRendererFactory(transport=transport)

    deps = Dependencies(
        identity_broker=identity_broker,
        sessions=sessions,
        runtime=runtime,
        renderer=renderer,
        signin_url=settings.signin_url,
        support_contact=settings.support_contact,
    )
    assert_fully_wired(deps)

    log_event(
        logger,
        logging.INFO,
        "middle tier assembled",
        engine=runtime.engine_name,
        authorization_id=runtime.authorization_id,
        workforce_pool=settings.workforce_pool_id,
    )
    return deps


def assert_fully_wired(deps: Dependencies) -> None:
    """Refuse a ``Dependencies`` with any collaborator left ``None``.

    Called by :func:`build_dependencies` and, more importantly, by the
    end-to-end wiring test. The whole defect this module exists to fix was four
    fields quietly defaulting to ``None``, so the assertion that they are not
    belongs in the code and not only in a test.
    """
    unwired = [
        name
        for name in ("identity_broker", "sessions", "runtime", "renderer")
        if getattr(deps, name) is None
    ]
    if unwired:
        raise CompositionError(
            "Dependencies left unwired: " + ", ".join(unwired)
        )


__all__ = [
    "CompositionError",
    "PortsIdentityBroker",
    "PortsSessionManager",
    "TeamsRendererFactory",
    "TurnRenderer",
    "assert_fully_wired",
    "build_dependencies",
]
