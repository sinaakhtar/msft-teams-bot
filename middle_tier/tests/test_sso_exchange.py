"""The Teams SSO handshake, end to end through the router.

The bug this covers is not subtle but it was invisible: `_sso_token_from_activity`
only ever read `activity.value.token`, a Teams *message* never carries one, and
the `signin/tokenExchange` invoke that does carry one returned 501. So every
turn refused, in 2ms, for a documented reason, and the logs said the service
was healthy.

These tests drive the real router with fake collaborators, and assert on what
reaches the user rather than on what the router returns, because the two came
apart once already.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, AsyncIterator, Mapping

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import errors  # noqa: E402
from app.auth.inbound import AuthenticatedCaller  # noqa: E402
from app.ports import (  # noqa: E402
    AgentEvent,
    IdentityUnavailable,
    SessionRef,
)
from app.routing import Dependencies, route_activity  # noqa: E402
from app.sso import SsoState, TtlStore  # noqa: E402

from .conftest import BOT_APP_ID, SERVICE_URL  # noqa: E402

TENANT = "00000000-0000-0000-0000-000000000000"
OID = "33333333-3333-3333-3333-333333333333"
USER_KEY = f"entra:{TENANT}:{OID}"
CONNECTION = "test-oauth-connection"
EXCHANGE_URI = f"api://botid-{BOT_APP_ID}"


# ==========================================================================
# Fakes
# ==========================================================================


class FakeBroker:
    """Exchanges an assertion for a Google token, or refuses."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.raises = raises
        self.calls: list[tuple[str, str]] = []
        self.invalidated: list[str] = []

    async def get_google_access_token(self, user_key: str, teams_sso_token: str) -> str:
        self.calls.append((user_key, teams_sso_token))
        if self.raises is not None:
            raise self.raises
        return f"google-token-for-{user_key}"

    async def invalidate(self, user_key: str) -> None:
        self.invalidated.append(user_key)


class FakeSessions:
    async def get_or_create(
        self, user_key: str, *, conversation_id: str, access_token: str
    ) -> SessionRef:
        assert access_token, "ADR 002: no session may be created without a user token"
        return SessionRef(
            name=f"projects/p/locations/l/reasoningEngines/e/sessions/{conversation_id}",
            user_id=user_key,
            session_id=conversation_id,
        )

    async def reset(self, *args: Any, **kwargs: Any) -> SessionRef:  # pragma: no cover
        raise NotImplementedError

    async def list_events(self, *a: Any, **k: Any) -> list[AgentEvent]:  # pragma: no cover
        return []

    async def delete(self, session: SessionRef) -> None:  # pragma: no cover
        return None


class FakeRuntime:
    def __init__(self, *, answer: str = "you are you") -> None:
        self.answer = answer
        self.invocations: list[dict[str, Any]] = []

    async def stream_query(
        self,
        *,
        session: SessionRef,
        message: str,
        user_access_token: str,
        request_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        assert user_access_token, "ADR 002: the agent is called AS THE USER"
        self.invocations.append(
            {"message": message, "token": user_access_token, "session": session.name}
        )
        yield AgentEvent(author="agent", payload={"text": self.answer})


class RecordingRenderer:
    def __init__(self, bucket: list[str]) -> None:
        self._bucket = bucket
        self.finished = False
        self.error: Exception | None = None

    async def begin(self, conversation_ref: Mapping[str, Any]) -> None:
        return None

    async def push(self, event: AgentEvent) -> None:
        text = (event.payload or {}).get("text")
        if text:
            self._bucket.append(text)

    async def finish(self, *, error: Exception | None = None) -> None:
        self.finished = True
        self.error = error


class RecordingRendererFactory:
    def __init__(self) -> None:
        self.rendered: list[str] = []
        self.turns: list[RecordingRenderer] = []

    def for_turn(
        self,
        conversation_ref: Mapping[str, Any],
        *,
        request_id: str | None = None,
        user_display: str | None = None,
    ) -> RecordingRenderer:
        turn = RecordingRenderer(self.rendered)
        self.turns.append(turn)
        return turn


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def __call__(self, activity, conversation_ref):
        self.sent.append(dict(activity))
        return {"id": f"posted-{len(self.sent)}"}


def build_deps(
    *,
    broker: FakeBroker | None = None,
    runtime: FakeRuntime | None = None,
    state: SsoState | None = None,
    connection_name: str = CONNECTION,
) -> tuple[Dependencies, dict[str, Any]]:
    broker = broker or FakeBroker()
    runtime = runtime or FakeRuntime()
    sender = RecordingSender()
    renderer = RecordingRendererFactory()
    parts = {
        "broker": broker,
        "runtime": runtime,
        "sender": sender,
        "renderer": renderer,
        "state": state or SsoState(),
    }
    deps = Dependencies(
        identity_broker=broker,
        sessions=FakeSessions(),
        runtime=runtime,
        renderer=renderer,
        reply_sender=sender,
        sso_state=parts["state"],
        oauth_connection_name=connection_name,
        token_exchange_uri=EXCHANGE_URI,
        signin_url="https://example.invalid/signin",
        support_contact="the data platform team",
    )
    return deps, parts


def caller() -> AuthenticatedCaller:
    return AuthenticatedCaller(
        app_id=BOT_APP_ID,
        issuer="https://api.botframework.com",
        profile_name="bot_connector",
        service_url=SERVICE_URL,
    )


def message(text: str = "who am I?") -> dict[str, Any]:
    return {
        "type": "message",
        "id": "1700000000000",
        "channelId": "msteams",
        "serviceUrl": SERVICE_URL,
        "text": text,
        "from": {"id": "29:abc", "name": "A Person", "aadObjectId": OID},
        "recipient": {"id": f"28:{BOT_APP_ID}"},
        "conversation": {"id": "a:1abcdef", "tenantId": TENANT},
        "channelData": {"tenant": {"id": TENANT}},
    }


def exchange(token: str = "teams-assertion", exchange_id: str = "ex-1") -> dict[str, Any]:
    return {
        **message(),
        "type": "invoke",
        "name": "signin/tokenExchange",
        "value": {"id": exchange_id, "connectionName": CONNECTION, "token": token},
    }


async def run(activity, deps):
    return await route_activity(
        activity, caller=caller(), deps=deps, expected_tenant_id=TENANT
    )


# ==========================================================================
# The store
# ==========================================================================


def test_ttl_store_expires_on_wall_time():
    now = [1000.0]
    store: TtlStore[str] = TtlStore(ttl_seconds=10, clock=lambda: now[0])
    store.put("k", "v")
    assert store.get("k") == "v"
    now[0] += 11
    assert store.get("k") is None


def test_parked_turn_is_handed_out_exactly_once():
    """A replay loop is worse than a dropped message."""
    state = SsoState()
    state.park_turn(USER_KEY, message("first question"))
    assert state.take_parked_turn(USER_KEY)["text"] == "first question"
    assert state.take_parked_turn(USER_KEY) is None


def test_only_one_caller_can_claim_an_exchange():
    """Teams fans the same exchange to every active client; the assertion is
    single use, so the losers must not try to redeem it."""
    state = SsoState()
    assert state.claim_exchange("ex-1") is True
    assert state.claim_exchange("ex-1") is False


def test_an_exchange_with_no_id_is_not_deduplicated():
    """Better to redeem twice than to refuse a turn over a missing field."""
    state = SsoState()
    assert state.claim_exchange("") is True
    assert state.claim_exchange("") is True


def test_an_assertion_is_never_keyed_on_an_empty_user():
    with pytest.raises(ValueError):
        SsoState().remember_assertion("", "an-assertion")


# ==========================================================================
# First contact: no assertion yet
# ==========================================================================


async def test_a_turn_with_no_assertion_sends_an_oauth_card_not_a_refusal():
    """This is the activity that makes Teams start the silent exchange.

    A plain signin card here is the difference between SSO working and the
    user being asked to sign in forever: Teams only enters the silent path
    when it sees an OAuthCard carrying a tokenExchangeResource.
    """
    deps, parts = build_deps()

    result = await run(message(), deps)

    assert result.status == 200
    delivered = parts["sender"].sent
    assert len(delivered) == 1

    attachment = delivered[0]["attachments"][0]
    assert attachment["contentType"] == errors.OAUTH_CARD_CONTENT_TYPE
    assert attachment["content"]["connectionName"] == CONNECTION
    assert attachment["content"]["tokenExchangeResource"]["uri"] == EXCHANGE_URI

    # Nothing downstream was touched: no token, no turn.
    assert parts["runtime"].invocations == []


async def test_the_prompt_carries_no_text_so_a_silent_sign_in_shows_nothing():
    """Teams intercepts the OAuthCard but still renders message text.

    With text, a fully silent, fully successful sign-in printed a sentence
    telling the user to approve a prompt that never appeared. The card carries
    its own title and button, so the visible fallback loses nothing.
    """
    deps, parts = build_deps()

    await run(message(), deps)

    delivered = parts["sender"].sent[0]
    assert "text" not in delivered
    assert delivered["attachments"][0]["contentType"] == errors.OAUTH_CARD_CONTENT_TYPE


async def test_the_users_question_is_parked_not_lost():
    """Teams does not resend the message after a sign-in. If we do not keep
    it, the first thing anybody says to the bot vanishes."""
    deps, parts = build_deps()

    await run(message("what were last month's sales?"), deps)

    parked = parts["state"].take_parked_turn(USER_KEY)
    assert parked is not None
    assert parked["text"] == "what were last month's sales?"


async def test_without_a_connection_name_it_degrades_to_the_adr_004_refusal():
    """Honest, and terminal. Worth asserting so the degraded mode stays
    visibly different from the working one."""
    deps, parts = build_deps(connection_name="")

    await run(message(), deps)

    delivered = parts["sender"].sent[0]
    types = [a["contentType"] for a in delivered.get("attachments", [])]
    assert errors.OAUTH_CARD_CONTENT_TYPE not in types


# ==========================================================================
# The exchange
# ==========================================================================


async def test_a_successful_exchange_returns_200_with_an_empty_body():
    deps, parts = build_deps()

    result = await run(exchange(), deps)

    assert result.status == 200
    assert result.body is None
    assert parts["broker"].calls == [(USER_KEY, "teams-assertion")]


async def test_the_assertion_is_kept_for_the_turns_that_follow():
    deps, parts = build_deps()

    await run(exchange(), deps)

    assert parts["state"].assertion_for(USER_KEY) == "teams-assertion"


async def test_a_duplicate_exchange_is_not_redeemed_twice():
    deps, parts = build_deps()

    first = await run(exchange(exchange_id="ex-dup"), deps)
    second = await run(exchange(exchange_id="ex-dup"), deps)

    assert first.status == 200
    # 200, not 412: the exchange IS being handled, just not by this call.
    assert second.status == 200
    assert len(parts["broker"].calls) == 1


async def test_a_refused_exchange_is_412_so_teams_shows_the_card():
    deps, parts = build_deps(
        broker=FakeBroker(raises=IdentityUnavailable("consent required"))
    )

    result = await run(exchange(), deps)

    assert result.status == 412
    assert result.body["connectionName"] == CONNECTION
    # Nothing is stored from a failed exchange.
    assert parts["state"].assertion_for(USER_KEY) is None


async def test_a_failed_exchange_never_leaks_the_assertion_or_upstream_text():
    deps, _ = build_deps(
        broker=FakeBroker(
            raises=IdentityUnavailable("AADSTS65001: user has not consented")
        )
    )

    result = await run(exchange(token="super-secret-assertion"), deps)

    rendered = str(result.body)
    assert "super-secret-assertion" not in rendered
    assert "AADSTS65001" not in rendered


# ==========================================================================
# The replay: the half that makes it feel like it worked
# ==========================================================================


async def test_the_parked_question_is_answered_after_sign_in():
    """The whole point. Without this the user signs in and nothing happens."""
    deps, parts = build_deps(runtime=FakeRuntime(answer="you are A Person"))

    await run(message("who am I?"), deps)  # parks, sends the card
    await run(exchange(), deps)  # redeems, replays

    assert [i["message"] for i in parts["runtime"].invocations] == ["who am I?"]
    assert parts["renderer"].rendered == ["you are A Person"]


async def test_the_replayed_turn_runs_as_the_user():
    deps, parts = build_deps()

    await run(message(), deps)
    await run(exchange(), deps)

    assert parts["runtime"].invocations[0]["token"] == f"google-token-for-{USER_KEY}"


async def test_a_failed_replay_does_not_turn_a_good_exchange_into_a_412():
    """The assertion has already been redeemed and is single use. Reporting
    412 here would send Teams back round a loop it cannot win."""

    class ExplodingRuntime(FakeRuntime):
        async def stream_query(self, **kwargs):
            raise RuntimeError("the runtime fell over")
            yield  # pragma: no cover

    deps, _ = build_deps(runtime=ExplodingRuntime())

    await run(message(), deps)
    result = await run(exchange(), deps)

    assert result.status == 200


async def test_an_exchange_with_nothing_parked_is_still_a_success():
    """Users can sign in without a pending question."""
    deps, parts = build_deps()

    result = await run(exchange(), deps)

    assert result.status == 200
    assert parts["runtime"].invocations == []


# ==========================================================================
# Steady state, and recovery
# ==========================================================================


async def test_a_later_turn_uses_the_stored_assertion_and_does_not_re_prompt():
    deps, parts = build_deps()
    await run(exchange(), deps)
    parts["sender"].sent.clear()

    await run(message("and last month?"), deps)

    assert [i["message"] for i in parts["runtime"].invocations] == ["and last month?"]
    assert parts["sender"].sent == [], "re-prompting a signed-in user is the loop"


async def test_a_rejected_assertion_is_dropped_so_the_next_turn_can_recover():
    """Otherwise the user is stuck: every turn replays the same dead token and
    nothing ever triggers a fresh exchange."""
    state = SsoState()
    state.remember_assertion(USER_KEY, "stale-assertion")
    deps, parts = build_deps(
        broker=FakeBroker(raises=IdentityUnavailable("token rejected")), state=state
    )

    await run(message(), deps)

    assert parts["state"].assertion_for(USER_KEY) is None
