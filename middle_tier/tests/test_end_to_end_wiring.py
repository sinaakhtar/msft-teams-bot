"""The test that would have caught it.

Every component in this service was built and individually tested. The suite
was green. And the deployed service answered every single question with the
"temporarily unavailable" template, because ``Dependencies`` has four fields
that default to ``None`` and nothing ever constructed anything into them.

No unit test can catch that, by construction: a unit test supplies its own
collaborators. Only a test that assembles the application the way production
assembles it, and then drives a turn through the assembled thing, can notice
that the assembly is empty. That is this file.

It asserts five things:

1. The assembled application has no ``None`` collaborators. Directly the
   defect.
2. A synthetic Teams activity carrying a valid ``from.aadObjectId`` reaches an
   attempted runtime invocation -- through the real identity adapter, the real
   session manager, the real renderer adapter.
3. The session key is ``entra:{tid}:{oid}`` and the Teams MRI appears nowhere.
   ADR 003.
4. An activity with no ``aadObjectId`` is refused, and is not quietly served
   under the MRI instead. ADR 003, fail closed.
5. A downstream 403 produces the ADR 004 template, byte for byte, rather than
   any text a model could have authored.

WHAT IT DOES NOT PROVE, and the reason it is not enough on its own: there is no
network here. Nothing in this file has spoken to Entra, to Google STS, to the
Agent Runtime, or to the Bot Connector. It proves the parts are connected and
that the wiring carries the right values in the right direction. It does not
prove any remote endpoint accepts what we send. ``INTEGRATION.md`` lists that
gap explicitly, and it is a large one.
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
from typing import Any, AsyncIterator, Mapping, Sequence

import aiohttp
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import errors  # noqa: E402
from app.auth.inbound import AuthenticatedCaller  # noqa: E402
from app.composition import (  # noqa: E402
    CompositionError,
    PortsIdentityBroker,
    PortsSessionManager,
    TeamsRendererFactory,
    TurnRenderer,
    assert_fully_wired,
    build_dependencies,
)
from app.config import Settings  # noqa: E402
from app.identity.errors import OboConsentRequired  # noqa: E402
from app.ports import (  # noqa: E402
    AuthorizationDenied,
    IdentityUnavailable,
    SessionRef,
)
from app.routing import Dependencies, route_activity  # noqa: E402
from app.runtime import DEFAULT_AUTHORIZATION_ID  # noqa: E402
from app.runtime.client import (  # noqa: E402
    ReasoningEngineRuntimeClient,
    RuntimeClientError,
)
from app.sessions.manager import AgentRuntimeSessionManager  # noqa: E402
from app.streaming.renderer import TeamsStreamingRenderer  # noqa: E402
from app.streaming.teams_sink import RecordingTeamsSink  # noqa: E402

# Reuse, do not re-create. `FakeSessionsClient` already exists and already
# matches the real client's protocol; a second copy would be free to drift.
from .test_renderer import envelope, text_event, turn_complete_event  # noqa: E402
from .test_session_manager import PARENT, FakeSessionsClient  # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

TENANT = "00000000-0000-0000-0000-000000000000"
OID = "33333333-3333-3333-3333-333333333333"
#: The Teams MRI. It is here so the tests can assert it is ABSENT. If this
#: string ever appears in a session key, ADR 003 has been reverted.
MRI = "29:1a2b3c4d5e6f7g8h9i0j-teams-mri-not-an-identity"
USER_KEY = f"entra:{TENANT}:{OID}"

CONVERSATION_ID = "a:1p9YyEA-conversation"
ENGINE_ID = "9000000000000000001"
USER_TOKEN = "ya29.a0-fake-user-access-token"
SSO_TOKEN = "eyJ0eXAiOiJKV1QifQ.fake-teams-sso-assertion.sig"


def complete_settings(**overrides: Any) -> Settings:
    """Everything `build_dependencies` needs. No secrets are read."""
    base: dict[str, Any] = dict(
        gcp_project_id="example-project",
        gcp_project_number="000000000000",
        location="us-central1",
        entra_tenant_id=TENANT,
        microsoft_app_id="00000000-1111-2222-3333-444444444444",
        microsoft_app_password="fake-bot-password",
        microsoft_app_type="SingleTenant",
        entra_client_secret="fake-obo-secret",
        reasoning_engine_id=ENGINE_ID,
        federation_app_id="11111111-1111-1111-1111-111111111111",
        workforce_pool_id="teams-bot-demo",
        workforce_provider_id="entra",
        signin_url="https://example.invalid/signin",
        support_contact="the data platform team",
    )
    base.update(overrides)
    return Settings(**base)


def message_activity(*, aad_object_id: str | None = OID, text: str = "how many orders last week?") -> dict[str, Any]:
    """A Teams `message` activity, shaped the way Bot Framework sends one."""
    activity: dict[str, Any] = {
        "type": "message",
        "id": "activity-0001",
        "text": text,
        "channelId": "msteams",
        "serviceUrl": "https://smba.trafficmanager.net/emea/",
        "conversation": {"id": CONVERSATION_ID, "conversationType": "personal"},
        "from": {"id": MRI, "name": "A Person"},
        "recipient": {"id": "28:the-bot", "name": "Data Assistant"},
        "channelData": {"tenant": {"id": TENANT}},
        # The Teams SSO assertion. `_sso_token_from_activity` reads it here.
        "value": {"token": SSO_TOKEN},
    }
    if aad_object_id is not None:
        activity["from"]["aadObjectId"] = aad_object_id
    return activity


def authenticated_caller() -> AuthenticatedCaller:
    """Proof of channel authenticity. Constructed directly on purpose.

    `tests/test_inbound_auth.py` owns proving that only a genuine Bot Framework
    JWT can produce one of these, across 40-odd cases including `alg: none` and
    HS256 confusion. Re-minting a token here would duplicate that suite while
    testing something else. What this file tests starts one step later.
    """
    return AuthenticatedCaller(
        app_id="00000000-1111-2222-3333-444444444444",
        issuer="https://api.botframework.com",
        profile_name="public-cloud",
        service_url="https://smba.trafficmanager.net/emea/",
        claims={"tid": TENANT},
    )


# ==========================================================================
# Fakes. Each one sits at a protocol boundary, and each records.
# ==========================================================================


class FakeInnerBroker:
    """Stands in for `ChainedIdentityBroker`, at its own method boundary.

    Wrapped by the REAL `PortsIdentityBroker`, so the exception translation
    that the router depends on is exercised rather than bypassed.
    """

    def __init__(self, *, token: str = USER_TOKEN, raises: Exception | None = None) -> None:
        self.token = token
        self.raises = raises
        self.calls: list[dict[str, str]] = []
        self.invalidated: list[str] = []

    async def google_access_token(self, *, user_key: str, teams_sso_token: str) -> str:
        self.calls.append({"user_key": user_key, "teams_sso_token": teams_sso_token})
        if self.raises is not None:
            raise self.raises
        return self.token

    async def invalidate(self, user_key: str) -> None:
        self.invalidated.append(user_key)


class NamedFakeSessionsClient(FakeSessionsClient):
    """`FakeSessionsClient` plus the one method the adapter needs.

    `PortsSessionManager` rebuilds a `SessionRef`, which needs the full
    resource name, and the real REST client exposes `session_name` for exactly
    that. The reused fake predates the adapter and does not have it.
    """

    def session_name(self, session_id: str) -> str:
        return f"{PARENT}/sessions/{session_id}"


class RecordingRuntime:
    """Records the invocation, then yields events or raises.

    This is the far edge of the wiring under test: if a call lands here with
    the right session, message and token, the middle tier has done its job.
    """

    def __init__(
        self,
        *,
        events: Sequence[Any] = (),
        raises: Exception | None = None,
    ) -> None:
        self.events = list(events)
        self.raises = raises
        self.invocations: list[dict[str, Any]] = []

    async def stream_query(
        self,
        *,
        session: SessionRef,
        message: str,
        user_access_token: str,
        request_id: str | None = None,
    ) -> AsyncIterator[Any]:
        self.invocations.append(
            {
                "session": session,
                "message": message,
                "user_access_token": user_access_token,
                "request_id": request_id,
            }
        )
        if self.raises is not None:
            raise self.raises
        for event in self.events:
            yield event


class RecordingRendererFactory:
    """Builds a REAL `TurnRenderer` over a REAL renderer and a recording sink.

    Only the network is faked. The push-to-pull adapter, the cumulative
    contract and the renderer's own ADR 004 handling all run for real.
    """

    def __init__(self) -> None:
        self.sinks: list[RecordingTeamsSink] = []
        self.turns: list[TurnRenderer] = []

    def for_turn(
        self,
        conversation_ref: Mapping[str, Any],
        *,
        request_id: str | None = None,
        user_display: str | None = None,
    ) -> TurnRenderer:
        sink = RecordingTeamsSink()
        self.sinks.append(sink)
        turn = TurnRenderer(
            renderer=TeamsStreamingRenderer(
                request_id=request_id, user_display=user_display
            ),
            sink=sink,
            conversation_ref=conversation_ref,
        )
        self.turns.append(turn)
        return turn


def wired_dependencies(
    *,
    runtime: RecordingRuntime,
    inner_broker: FakeInnerBroker | None = None,
) -> tuple[Dependencies, dict[str, Any]]:
    """Assemble the router's collaborators using the REAL adapters."""
    inner_broker = inner_broker or FakeInnerBroker()
    sessions_client = NamedFakeSessionsClient()
    parts: dict[str, Any] = {
        "inner_broker": inner_broker,
        "sessions_client": sessions_client,
        "renderer_factory": RecordingRendererFactory(),
    }
    deps = Dependencies(
        identity_broker=PortsIdentityBroker(
            inner_broker, sts_audience="//iam.googleapis.com/.../teams-bot-demo"
        ),
        sessions=PortsSessionManager(
            AgentRuntimeSessionManager(client=sessions_client),
            client=sessions_client,
        ),
        runtime=runtime,
        renderer=parts["renderer_factory"],
        signin_url="https://example.invalid/signin",
        support_contact="the data platform team",
    )
    return deps, parts


# ==========================================================================
# 1. THE DEFECT: nothing may be left None
# ==========================================================================


async def test_the_assembled_app_has_no_none_collaborators():
    """The whole point. Four fields, none of them None, built from Settings.

    `build_dependencies` constructs the concrete implementations only; it makes
    no calls. The aiohttp session below is handed over and never used.
    """
    async with aiohttp.ClientSession() as http:
        deps = build_dependencies(complete_settings(), http=http)

        for name in ("identity_broker", "sessions", "runtime", "renderer"):
            assert getattr(deps, name) is not None, f"{name} was left unwired"

        # And the types are the real implementations, not placeholders.
        assert isinstance(deps.identity_broker, PortsIdentityBroker)
        assert isinstance(deps.sessions, PortsSessionManager)
        assert isinstance(deps.runtime, ReasoningEngineRuntimeClient)
        assert isinstance(deps.renderer, TeamsRendererFactory)

        # The engine addressed is the one ADR 001 says to address, built from
        # config rather than hardcoded.
        assert deps.runtime.engine_name == (
            f"projects/example-project/locations/us-central1/reasoningEngines/{ENGINE_ID}"
        )

        assert_fully_wired(deps)  # does not raise


def test_the_old_default_dependencies_would_now_fail_the_check():
    """`Dependencies()` is exactly what shipped. It must not pass as wired."""
    with pytest.raises(CompositionError) as caught:
        assert_fully_wired(Dependencies())

    message = str(caught.value)
    for name in ("identity_broker", "sessions", "runtime", "renderer"):
        assert name in message


async def test_incomplete_configuration_fails_loudly_at_assembly():
    """A missing engine id must stop startup, not degrade it.

    The failure this replaces: the service came up, both probes went green, and
    every turn returned the transient template. Green dashboard, broken bot.
    """
    async with aiohttp.ClientSession() as http:
        with pytest.raises(CompositionError) as caught:
            build_dependencies(complete_settings(reasoning_engine_id=""), http=http)

    assert "REASONING_ENGINE_ID" in str(caught.value)


async def test_assembly_refuses_a_missing_federation_app():
    """Every collaborator's config is checked, not only the obvious one."""
    async with aiohttp.ClientSession() as http:
        with pytest.raises(CompositionError) as caught:
            build_dependencies(complete_settings(federation_app_id=""), http=http)

    assert "FEDERATION_APP_ID" in str(caught.value)


# ==========================================================================
# 2 + 3. A real activity reaches the runtime, keyed on the Entra object id
# ==========================================================================


async def test_message_activity_flows_through_to_a_runtime_invocation():
    """End to end, through the real adapters, to an attempted invocation."""
    runtime = RecordingRuntime(
        events=[
            envelope(text_event("You had ")),
            envelope(text_event("1,204 orders.")),
            envelope(turn_complete_event()),
        ]
    )
    deps, parts = wired_dependencies(runtime=runtime)

    result = await route_activity(
        message_activity(),
        caller=authenticated_caller(),
        deps=deps,
        expected_tenant_id=TENANT,
    )

    # The turn succeeded and the answer went out over the renderer, so there is
    # deliberately no body: a body here would double-post the reply.
    assert result.status == 200
    assert result.body is None

    # The invocation happened, exactly once, carrying the user's text.
    assert len(runtime.invocations) == 1, runtime.invocations
    invocation = runtime.invocations[0]
    assert invocation["message"] == "how many orders last week?"

    # Tool Identity reached the runtime, and it is the user's token.
    assert invocation["user_access_token"] == USER_TOKEN
    assert parts["inner_broker"].calls == [
        {"user_key": USER_KEY, "teams_sso_token": SSO_TOKEN}
    ]

    # The answer was rendered to Teams, cumulatively.
    sink = parts["renderer_factory"].sinks[0]
    assert sink.final_text == "You had 1,204 orders."


async def test_the_session_key_is_the_entra_object_id_and_never_the_mri():
    """ADR 003. The session `user_id` is `entra:{tid}:{oid}`.

    Asserted in three places, because there are three chances to get it wrong:
    what the middle tier asked the sessions API to create, what the runtime was
    handed, and whether the MRI leaked anywhere at all.
    """
    runtime = RecordingRuntime(events=[envelope(turn_complete_event())])
    deps, parts = wired_dependencies(runtime=runtime)

    await route_activity(
        message_activity(),
        caller=authenticated_caller(),
        deps=deps,
        expected_tenant_id=TENANT,
    )

    # (a) what was created against the sessions subresource
    created = [
        payload
        for name, payload in parts["sessions_client"].calls
        if name == "create_session"
    ]
    assert created, parts["sessions_client"].calls
    assert created[0]["user_id"] == USER_KEY

    # (b) what the runtime was given as the session owner
    session: SessionRef = runtime.invocations[0]["session"]
    assert session.user_id == USER_KEY
    assert session.name.startswith(PARENT + "/sessions/")

    # (c) the MRI is nowhere. Not as a key, not as a fallback, not in the ref.
    haystack = json.dumps(
        {
            "calls": [
                {"name": name, "payload": _stringify(payload)}
                for name, payload in parts["sessions_client"].calls
            ],
            "session": {
                "name": session.name,
                "user_id": session.user_id,
                "session_id": session.session_id,
            },
        }
    )
    assert MRI not in haystack, haystack
    assert "29:" not in haystack, haystack


def _stringify(payload: Any) -> Any:
    try:
        json.dumps(payload)
        return payload
    except TypeError:
        return str(payload)


# ==========================================================================
# 4. No aadObjectId: fail closed
# ==========================================================================


async def test_activity_without_aad_object_id_is_refused_and_never_invokes():
    """ADR 003. A turn we cannot attribute is refused, not served under the MRI.

    The tempting bug is a fallback to `from.id`, which is always present. It
    would mint a second, unfederated identity for a person who already has one,
    silently, and only under the exact conditions nobody tests.
    """
    runtime = RecordingRuntime(events=[envelope(turn_complete_event())])
    deps, parts = wired_dependencies(runtime=runtime)

    result = await route_activity(
        message_activity(aad_object_id=None),
        caller=authenticated_caller(),
        deps=deps,
        expected_tenant_id=TENANT,
    )

    # Refused before anything downstream was touched.
    assert runtime.invocations == []
    assert parts["sessions_client"].calls == []
    assert parts["inner_broker"].calls == []

    # 200, because a refusal delivered is a successful delivery of a refusal.
    # A non-2xx would make Azure Bot Service retry the same doomed activity.
    assert result.status == 200
    assert result.body is not None

    body = json.dumps(result.body)
    assert MRI not in body, "the MRI leaked into the refusal message"


# ==========================================================================
# 5. Downstream 403: the ADR 004 template, not a model's explanation
# ==========================================================================


def real_denial_for_a_403() -> AuthorizationDenied:
    """The exception the REAL runtime client raises for a 403.

    Produced by calling the real classifier rather than by hand, so that if the
    mapping ever changes, this test changes with it instead of quietly
    testing a fiction.
    """
    client = ReasoningEngineRuntimeClient(
        project="example-project", location="us-central1", reasoning_engine_id=ENGINE_ID
    )
    try:
        client._raise_for_status(
            403,
            "Permission 'aiplatform.reasoningEngines.streamQuery' denied on "
            "resource. The caller does not have permission.",
        )
    except AuthorizationDenied as exc:
        return exc
    raise AssertionError("a 403 must classify as AuthorizationDenied")


def test_the_runtime_client_maps_403_to_a_denial_that_names_the_engine():
    denial = real_denial_for_a_403()
    assert isinstance(denial, AuthorizationDenied)
    assert denial.resource == (
        f"projects/example-project/locations/us-central1/reasoningEngines/{ENGINE_ID}"
    )


async def test_a_downstream_403_produces_the_templated_denial():
    """ADR 004. Intercepted at the boundary and rendered from a template.

    The assertion is byte equality with the template function's own output for
    the same inputs. That is what rules out a model-authored explanation: there
    is no room for generated prose in a string that must match exactly.
    """
    denial = real_denial_for_a_403()
    runtime = RecordingRuntime(raises=denial)
    deps, parts = wired_dependencies(runtime=runtime)

    activity = message_activity(text="show me the payroll table")
    result = await route_activity(
        activity,
        caller=authenticated_caller(),
        deps=deps,
        expected_tenant_id=TENANT,
    )

    assert result.status == 200
    assert result.body is not None

    expected = errors.downstream_denial(
        resource=denial.resource,
        user_display="A Person",
        request_id=activity["id"],
    )
    assert result.body == expected, result.body

    # The refused resource is named, which is the actionable half of ADR 004.
    assert ENGINE_ID in json.dumps(result.body)

    # And the turn was not retried under anything else.
    assert len(runtime.invocations) == 1


async def test_an_identity_failure_becomes_a_signin_prompt_not_a_500():
    """The translation the composition adapter exists for.

    `ChainedIdentityBroker` raises `IdentityAcquisitionError`, which is not a
    `PortError`. Without `PortsIdentityBroker` translating it, this exception
    escapes every handler in the router and becomes an HTTP 500 -- and Azure
    Bot Service then retries the activity. This test fails if the adapter is
    removed.
    """
    inner = FakeInnerBroker(
        raises=OboConsentRequired(
            "the user has not consented",
            upstream_code="AADSTS65001",
        )
    )
    runtime = RecordingRuntime()
    deps, parts = wired_dependencies(runtime=runtime, inner_broker=inner)

    result = await route_activity(
        message_activity(),
        caller=authenticated_caller(),
        deps=deps,
        expected_tenant_id=TENANT,
    )

    assert result.status == 200
    assert result.body is not None
    assert runtime.invocations == []

    expected = errors.identity_failure(
        signin_url="https://example.invalid/signin",
        reason_code="token_exchange_failed",
        support_contact="the data platform team",
    )
    assert result.body == expected, result.body


# ==========================================================================
# The credential contract with the agent
# ==========================================================================


def test_the_user_token_travels_in_authorizations_not_in_session_state():
    """Layer 3 of `spikes/FINDINGS.md`, encoded.

    Session state is persisted by the managed Sessions service and readable by
    session id. A live bearer token written to an ordinary state key becomes
    part of durable conversation history. The runtime maps `authorizations[x]`
    to `temp:x`, and ADK strips `temp:` before persisting, which is the only
    reason this is safe.
    """
    client = ReasoningEngineRuntimeClient(
        project="example-project", location="us-central1", reasoning_engine_id=ENGINE_ID
    )
    payload = json.loads(
        client.build_request_json(
            session=SessionRef(
                name=f"{PARENT}/sessions/s-1", user_id=USER_KEY, session_id="s-1"
            ),
            message="hello",
            user_access_token=USER_TOKEN,
        )
    )

    # Present, exactly where the agent looks for it.
    assert payload["authorizations"][DEFAULT_AUTHORIZATION_ID] == {
        "access_token": USER_TOKEN
    }

    # Absent everywhere else. In particular there is no `session_state`.
    assert "session_state" not in payload
    assert "state" not in payload
    leaked = [
        key
        for key, value in payload.items()
        if key != "authorizations" and USER_TOKEN in json.dumps(value)
    ]
    assert leaked == [], f"the user token also appears under {leaked}"

    # The session is owned by the Entra object id, not the MRI.
    assert payload["user_id"] == USER_KEY


def test_the_authorization_id_matches_the_agent_side_constant():
    """A contract between two packages that cannot import each other.

    The middle tier writes `authorizations[<id>]`; the agent reads
    `temp:<id>`. They agree only by convention, and disagreeing produces an
    agent that fails closed with "no user credential" and no hint why. So the
    agent's source is read and compared.
    """
    source = (REPO_ROOT / "agent" / "bq_agent" / "credentials.py").read_text()
    match = re.search(
        r"AUTHORIZATION_ID\s*=\s*os\.environ\.get\(\s*"
        r"[\"']BQ_AGENT_AUTHORIZATION_ID[\"']\s*,\s*[\"']([^\"']+)[\"']",
        source,
    )
    assert match, "could not find AUTHORIZATION_ID in agent/bq_agent/credentials.py"
    assert match.group(1) == DEFAULT_AUTHORIZATION_ID

    # And the agent must still be reading it from a temp:-prefixed key.
    assert 'TEMP_STATE_KEY = f"temp:{AUTHORIZATION_ID}"' in source


def test_the_runtime_refuses_to_invoke_without_a_user_token():
    """ADR 002. No Tool Identity, no invocation. Not even an empty string."""
    client = ReasoningEngineRuntimeClient(
        project="example-project", location="us-central1", reasoning_engine_id=ENGINE_ID
    )
    with pytest.raises(RuntimeClientError):
        client.build_request_json(
            session=SessionRef(
                name=f"{PARENT}/sessions/s-1", user_id=USER_KEY, session_id="s-1"
            ),
            message="hello",
            user_access_token="",
        )


def test_the_runtime_uses_the_adr_005_streaming_method():
    """ADR 005 chose `streaming_agent_run_with_events` over `stream_query`.

    The distinction is not cosmetic: `stream_query` hides tool activity, and
    the progress indicators the renderer emits during the slow part of a turn
    are built entirely from tool events.
    """
    from app.runtime.client import CLASS_METHOD

    assert CLASS_METHOD == "streaming_agent_run_with_events"


def test_the_runtime_client_maps_401_to_an_identity_failure():
    """401 and 403 are different turns. Collapsing them breaks ADR 004."""
    client = ReasoningEngineRuntimeClient(
        project="example-project", location="us-central1", reasoning_engine_id=ENGINE_ID
    )
    with pytest.raises(IdentityUnavailable):
        client._raise_for_status(401, "invalid authentication credentials")
