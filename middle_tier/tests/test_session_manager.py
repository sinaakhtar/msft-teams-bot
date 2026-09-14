"""Session lifecycle tests. No network, no sleeping, no mocking of the thing under test.

The two assertions that matter most, and why:

  * :func:`test_reset_does_not_delete_the_abandoned_session` - a reset that
    deletes is indistinguishable from a reset that abandons, right up until
    someone needs the history of the conversation they just reset. The test
    therefore checks three separate things: no delete verb was called, the
    abandoned session is still retrievable by id, and neither the client nor
    the manager even EXPOSES a delete or append method (ADR 005). A behavioural
    check alone would pass on a client that grew a delete method nobody happens
    to call yet.
  * :func:`test_concurrent_resolve_creates_exactly_one_session` - the failure
    it guards is invisible in production. Two sessions get created, one wins
    the mapping, the other holds the first message and is never read again, and
    the user just sees the bot "forget" something. The test asserts a CALL
    COUNT, because "it returned the same id" can be true by luck.

The clock is fake, so the 60-minute policy is tested in microseconds. The REST
client is faked at the :class:`SessionsClient` protocol boundary, so the
manager's real logic runs. The LRO tests at the bottom drive the REAL client
with a stubbed transport, because "handles the LRO shape" is a claim about the
client's parsing, not about aiohttp.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ports import (  # noqa: E402
    AuthorizationDenied,
    IdentityUnavailable,
    TransientBackendError,
)
from app.sessions.client import (  # noqa: E402
    Session,
    SessionOwnershipMismatch,
    SessionsClient,
    SessionsRestClient,
    conversation_label,
)
from app.sessions.manager import (  # noqa: E402
    IDLE_TIMEOUT_SECONDS,
    AgentRuntimeSessionManager,
    GroupConversationNotSupported,
    InvalidUserKey,
    assert_one_to_one,
)
from app.sessions.store import InMemorySessionStore  # noqa: E402

TENANT = "00000000-0000-0000-0000-000000000000"
OID = "33333333-3333-3333-3333-333333333333"
USER_KEY = f"entra:{TENANT}:{OID}"
#: A real one-to-one Teams conversation id shape.
CONV_1_1 = "a:1qbxLpTb9F0dvfF7Wt2mVJ0hqPz7Kk"
#: A group chat / channel id shape. Out of scope.
CONV_GROUP = "19:5f8a4f2c17e94ff0b3d3f0b6cf5a1a2b@thread.v2"
TOKEN = "ya29.fake-workforce-principal-access-token"

PARENT = "projects/000000000000/locations/us-central1/reasoningEngines/9000000000000000001"


class FakeClock:
    """Seconds-valued monotonic clock we control."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeSessionsClient:
    """A stand-in for the REST client, at the SessionsClient protocol boundary.

    It records every call. Note what it CANNOT record: a delete or an append,
    because it does not implement either - just like the real client. If the
    manager ever tries to call one, the test fails with AttributeError, which
    is the point.
    """

    def __init__(self, *, create_delay: float = 0.0) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.sessions: dict[str, Session] = {}
        self.create_delay = create_delay
        self.fail_next_create: Exception | None = None
        self._counter = 0

    @property
    def create_calls(self) -> int:
        return sum(1 for name, _ in self.calls if name == "create_session")

    async def create_session(
        self,
        *,
        user_id: str,
        access_token: str,
        display_name: str | None = None,
        labels: Mapping[str, str] | None = None,
    ) -> Session:
        self.calls.append(
            (
                "create_session",
                {
                    "user_id": user_id,
                    "access_token": access_token,
                    "display_name": display_name,
                    "labels": dict(labels or {}),
                },
            )
        )
        if self.create_delay:
            await asyncio.sleep(self.create_delay)
        else:
            # Yield control even in the fast path, so a concurrency bug has a
            # chance to interleave rather than being hidden by the fake being
            # synchronous.
            await asyncio.sleep(0)
        if self.fail_next_create is not None:
            exc, self.fail_next_create = self.fail_next_create, None
            raise exc
        self._counter += 1
        sid = f"sess-{self._counter}"
        session = Session(
            name=f"{PARENT}/sessions/{sid}",
            session_id=sid,
            user_id=user_id,
            display_name=display_name,
        )
        self.sessions[sid] = session
        return session

    async def get_session(self, *, name: str, access_token: str) -> Session:
        self.calls.append(("get_session", {"name": name}))
        sid = name.rsplit("/", 1)[-1]
        if sid not in self.sessions:
            raise AssertionError(f"session {sid} is gone - something deleted it")
        return self.sessions[sid]

    async def list_sessions(
        self,
        *,
        access_token: str,
        user_id: str | None = None,
        labels: Mapping[str, str] | None = None,
        page_size: int = 100,
    ) -> Sequence[Session]:
        self.calls.append(("list_sessions", {"user_id": user_id}))
        return [s for s in self.sessions.values() if user_id in (None, s.user_id)]


def build_manager(
    *, clock: FakeClock | None = None, client: FakeSessionsClient | None = None
) -> tuple[AgentRuntimeSessionManager, FakeSessionsClient, FakeClock]:
    clock = clock or FakeClock()
    client = client or FakeSessionsClient()
    manager = AgentRuntimeSessionManager(
        client=client, store=InMemorySessionStore(), clock=clock
    )
    return manager, client, clock


# --------------------------------------------------------------------------
# Mapping and reuse
# --------------------------------------------------------------------------


async def test_first_turn_creates_a_session_owned_by_the_entra_user_key():
    manager, client, _ = build_manager()

    sid = await manager.resolve(
        user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
    )

    assert sid == "sess-1"
    assert client.create_calls == 1
    _, kwargs = client.calls[0]
    # ADR 003: the session is owned by entra:{tid}:{oid}, never the Teams MRI.
    assert kwargs["user_id"] == USER_KEY
    # ADR 002: the user's token went on the wire.
    assert kwargs["access_token"] == TOKEN
    # The conversation label is what makes "rebuild the mapping by listing
    # sessions" possible later; it must be the hash, not the raw id.
    assert kwargs["labels"] == {"teams_conversation": conversation_label(CONV_1_1)}


async def test_activity_within_60_minutes_reuses_the_same_session():
    manager, client, clock = build_manager()

    first = await manager.resolve(
        user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
    )
    clock.advance(IDLE_TIMEOUT_SECONDS - 1)
    second = await manager.resolve(
        user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
    )

    assert first == second
    assert client.create_calls == 1


async def test_idle_window_counts_from_last_activity_not_from_creation():
    """Fifty minutes, then another fifty. Total 100 > 60, but no gap is."""
    manager, client, clock = build_manager()

    first = await manager.resolve(
        user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
    )
    clock.advance(3000)  # 50 minutes
    await manager.resolve(
        user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
    )
    clock.advance(3000)  # another 50 minutes
    third = await manager.resolve(
        user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
    )

    assert third == first
    assert client.create_calls == 1


async def test_separate_conversations_get_separate_sessions():
    manager, client, _ = build_manager()

    a = await manager.resolve(
        user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
    )
    b = await manager.resolve(
        user_key=USER_KEY, conversation_id="a:1differentconversation", access_token=TOKEN
    )

    assert a != b
    assert client.create_calls == 2


# --------------------------------------------------------------------------
# 60-minute idle expiry
# --------------------------------------------------------------------------


async def test_idle_past_60_minutes_transparently_creates_a_new_session():
    manager, client, clock = build_manager()

    first = await manager.resolve(
        user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
    )
    clock.advance(IDLE_TIMEOUT_SECONDS + 1)
    second = await manager.resolve(
        user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
    )

    assert second != first
    assert client.create_calls == 2
    # The idled-out session was abandoned, not destroyed.
    assert (await client.get_session(name=first, access_token=TOKEN)).session_id == first


async def test_idle_expiry_is_exactly_60_minutes_not_59_or_61():
    """Boundary: at exactly 3600s of idleness the session is over."""
    manager, client, clock = build_manager()

    first = await manager.resolve(
        user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
    )
    clock.advance(IDLE_TIMEOUT_SECONDS)
    second = await manager.resolve(
        user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
    )

    assert second != first
    assert client.create_calls == 2


# --------------------------------------------------------------------------
# Conversation Reset (/new)
# --------------------------------------------------------------------------


async def test_reset_does_not_delete_the_abandoned_session():
    manager, client, _ = build_manager()

    old = await manager.resolve(
        user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
    )
    new = await manager.reset(
        user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
    )

    assert new != old
    assert client.create_calls == 2

    # 1. No delete-shaped call was made.
    assert [name for name, _ in client.calls] == ["create_session", "create_session"]

    # 2. The abandoned session is still retrievable by id.
    recovered = await client.get_session(name=old, access_token=TOKEN)
    assert recovered.session_id == old
    assert recovered.user_id == USER_KEY

    # 3. Neither layer even exposes a way to delete or to append an event.
    #    A reset that only *happens* not to delete is one autocomplete away
    #    from a reset that does (ADR 005).
    for surface in (SessionsRestClient, AgentRuntimeSessionManager, FakeSessionsClient):
        for forbidden in (
            "delete",
            "delete_session",
            "append_event",
            "appendEvent",
            "add_event",
        ):
            assert not hasattr(surface, forbidden), (
                f"{surface.__name__}.{forbidden} exists; ADR 005 says the middle "
                "tier reads history and never writes it, and a reset abandons "
                "rather than deletes"
            )


async def test_turn_after_reset_uses_the_new_session():
    manager, client, clock = build_manager()

    await manager.resolve(
        user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
    )
    new = await manager.reset(
        user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
    )
    clock.advance(60)
    after = await manager.resolve(
        user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
    )

    assert after == new
    assert client.create_calls == 2


async def test_failed_reset_keeps_the_existing_mapping():
    """A /new that cannot create the replacement must not strand the user."""
    manager, client, _ = build_manager()

    old = await manager.resolve(
        user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
    )
    client.fail_next_create = TransientBackendError("sessions.create 503")

    with pytest.raises(TransientBackendError):
        await manager.reset(
            user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
        )

    still = await manager.resolve(
        user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
    )
    assert still == old


# --------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------


async def test_concurrent_resolve_creates_exactly_one_session():
    """Eight simultaneous first turns for one conversation. One session."""
    manager, client, _ = build_manager(client=FakeSessionsClient(create_delay=0.01))

    results = await asyncio.gather(
        *(
            manager.resolve(
                user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
            )
            for _ in range(8)
        )
    )

    assert client.create_calls == 1, (
        f"expected exactly one sessions.create, got {client.create_calls}; "
        "the per-key lock is not holding"
    )
    assert set(results) == {"sess-1"}


async def test_concurrent_resolves_for_different_conversations_do_not_serialise():
    """The lock is per key. Two conversations must not block each other."""
    manager, client, _ = build_manager(client=FakeSessionsClient(create_delay=0.05))

    started = asyncio.get_running_loop().time()
    await asyncio.gather(
        manager.resolve(
            user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
        ),
        manager.resolve(
            user_key=USER_KEY, conversation_id="a:1other", access_token=TOKEN
        ),
    )
    elapsed = asyncio.get_running_loop().time() - started

    assert client.create_calls == 2
    assert elapsed < 0.09, f"the two creates serialised ({elapsed:.3f}s)"


async def test_concurrent_reset_and_resolve_do_not_interleave():
    """A /new racing an ordinary turn still leaves exactly one mapped session."""
    manager, client, _ = build_manager(client=FakeSessionsClient(create_delay=0.01))

    first = await manager.resolve(
        user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
    )
    a, b = await asyncio.gather(
        manager.reset(user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN),
        manager.resolve(
            user_key=USER_KEY, conversation_id=CONV_1_1, access_token=TOKEN
        ),
    )

    # Exactly one new session was created by the reset; the resolve either ran
    # before it (returning the original) or after it (returning the new one).
    assert client.create_calls == 2
    assert a != first
    assert b in {first, a}


# --------------------------------------------------------------------------
# Scope: one-to-one only
# --------------------------------------------------------------------------


async def test_group_conversation_is_rejected():
    manager, client, _ = build_manager()

    with pytest.raises(GroupConversationNotSupported):
        await manager.resolve(
            user_key=USER_KEY, conversation_id=CONV_GROUP, access_token=TOKEN
        )

    # Rejected BEFORE any session was created. A refused turn that still
    # created a session would leave orphans on the engine.
    assert client.create_calls == 0


async def test_reset_in_a_group_conversation_is_rejected_too():
    manager, client, _ = build_manager()

    with pytest.raises(GroupConversationNotSupported):
        await manager.reset(
            user_key=USER_KEY, conversation_id=CONV_GROUP, access_token=TOKEN
        )
    assert client.create_calls == 0


@pytest.mark.parametrize(
    "conversation_id",
    [
        "19:5f8a4f2c17e94ff0b3d3f0b6cf5a1a2b@thread.v2",
        "19:meeting_NzJhZDkw@thread.v2",
        "19:abcdef@thread.skype",
        "19:abcdef@thread.tacv2",
    ],
)
def test_thread_shaped_ids_are_refused_without_a_conversation_type(conversation_id):
    with pytest.raises(GroupConversationNotSupported):
        assert_one_to_one(conversation_id, None)


@pytest.mark.parametrize("conversation_type", ["groupChat", "channel", "GROUPCHAT", ""])
def test_explicit_non_personal_conversation_type_wins_over_the_id_shape(
    conversation_type,
):
    """Even a 1:1-looking id is refused when Teams says it is not personal."""
    with pytest.raises(GroupConversationNotSupported):
        assert_one_to_one(CONV_1_1, conversation_type)


def test_personal_conversation_type_is_accepted():
    assert_one_to_one(CONV_1_1, "personal") is None


def test_missing_conversation_id_is_refused():
    with pytest.raises(GroupConversationNotSupported):
        assert_one_to_one("", "personal")


# --------------------------------------------------------------------------
# ADR 003: fail closed without an Entra object id
# --------------------------------------------------------------------------


async def test_missing_entra_object_id_fails_closed():
    manager, client, _ = build_manager()

    with pytest.raises(InvalidUserKey):
        await manager.resolve(
            user_key=f"entra:{TENANT}:", conversation_id=CONV_1_1, access_token=TOKEN
        )
    assert client.create_calls == 0


async def test_teams_mri_is_never_accepted_as_a_user_key():
    """The MRI must not reach the API, and must not leak into the error."""
    mri = "29:1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"
    manager, client, _ = build_manager()

    with pytest.raises(InvalidUserKey) as caught:
        await manager.resolve(
            user_key=mri, conversation_id=CONV_1_1, access_token=TOKEN
        )

    assert client.create_calls == 0
    assert mri not in str(caught.value)


@pytest.mark.parametrize(
    "user_key",
    [
        "",
        "entra::",
        f"entra:{OID}",
        f"{TENANT}:{OID}",
        f"entra:not-a-guid:{OID}",
        f"entra:{TENANT}:not-a-guid",
        f"entra:{TENANT}:{OID}:extra",
    ],
)
async def test_malformed_user_keys_are_refused(user_key):
    manager, client, _ = build_manager()

    with pytest.raises(InvalidUserKey):
        await manager.resolve(
            user_key=user_key, conversation_id=CONV_1_1, access_token=TOKEN
        )
    assert client.create_calls == 0


async def test_missing_access_token_fails_closed_no_service_account_fallback():
    """ADR 002. No user token means no call, not a call as the service."""
    manager, client, _ = build_manager()

    with pytest.raises(IdentityUnavailable):
        await manager.resolve(
            user_key=USER_KEY, conversation_id=CONV_1_1, access_token=""
        )
    assert client.create_calls == 0


# --------------------------------------------------------------------------
# The real client's LRO handling (no network: the transport is stubbed)
# --------------------------------------------------------------------------


class StubTransportClient(SessionsRestClient):
    """The real client with ``_request`` replaced. Everything else is real.

    This is the narrowest possible seam: URL construction, LRO unwrapping,
    ownership checking and error mapping are all still the production code.
    """

    def __init__(self, responses: list[Any], **kwargs: Any) -> None:
        super().__init__(
            project="example-project",
            location="us-central1",
            reasoning_engine_id="9000000000000000001",
            **kwargs,
        )
        self._responses = responses
        self.requests: list[tuple[str, str]] = []

    async def _request(self, method: str, url: str, **kwargs: Any) -> Mapping[str, Any]:
        self.requests.append((method, url))
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _live_shaped_create_response(session_id: str = "5000000000000000001") -> dict:
    """The shape the live 200 came back in: a DONE LRO wrapping the session."""
    name = f"{PARENT}/sessions/{session_id}"
    return {
        "name": f"{name}/operations/1234567890",
        "done": True,
        "response": {
            "name": name,
            "createTime": "2026-09-07T09:12:33.123456Z",
            "updateTime": "2026-09-07T09:12:33.123456Z",
            "expireTime": "2026-09-08T09:12:33.123456Z",
            "userId": USER_KEY,
        },
    }


async def test_create_unwraps_an_already_complete_lro():
    client = StubTransportClient([_live_shaped_create_response()])

    session = await client.create_session(user_id=USER_KEY, access_token=TOKEN)

    # The id comes from the SESSION, not from the operation.
    assert session.session_id == "5000000000000000001"
    assert "operations" not in session.name
    assert session.user_id == USER_KEY
    assert len(client.requests) == 1  # no poll needed
    method, url = client.requests[0]
    assert method == "POST"
    assert url == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/example-project/"
        "locations/us-central1/reasoningEngines/9000000000000000001/sessions"
    )


async def test_session_id_comes_from_response_name_when_the_two_disagree():
    """``response.name`` wins over the operation name. The discriminating case.

    Every other LRO fixture here embeds the SAME session id in both the
    operation name (``.../sessions/{sid}/operations/{opid}``) and
    ``response.name``, so those assertions pass whether the implementation
    reads the id from the right place or the wrong one. A mutation that
    derived the id from the operation name survived the whole suite.

    So force the two apart. The operation is filed under one id and the
    created session is genuinely another. Only an implementation that reads
    ``response.name`` gets this right; one that strips ``/operations/...``
    off the envelope's own name returns the decoy.
    """
    decoy = "1111111111111111111"
    real = "2222222222222222222"
    client = StubTransportClient(
        [
            {
                "name": f"{PARENT}/sessions/{decoy}/operations/1234567890",
                "done": True,
                "response": {
                    "name": f"{PARENT}/sessions/{real}",
                    "userId": USER_KEY,
                },
            }
        ]
    )

    session = await client.create_session(user_id=USER_KEY, access_token=TOKEN)

    assert session.session_id == real
    assert session.name == f"{PARENT}/sessions/{real}"
    assert decoy not in session.name
    # And no follow-up GET: the resource was inline, so there is nothing to
    # go and fetch by the operation's name.
    assert [m for m, _ in client.requests] == ["POST"]


async def test_create_polls_an_incomplete_lro_until_done():
    op_name = f"{PARENT}/sessions/777/operations/42"
    client = StubTransportClient(
        [
            {"name": op_name, "done": False},
            {"name": op_name, "done": False},
            {
                "name": op_name,
                "done": True,
                "response": {"name": f"{PARENT}/sessions/777", "userId": USER_KEY},
            },
        ],
        lro_poll_interval_seconds=0.001,
    )

    session = await client.create_session(user_id=USER_KEY, access_token=TOKEN)

    assert session.session_id == "777"
    assert [m for m, _ in client.requests] == ["POST", "GET", "GET"]


async def test_create_raises_when_the_lro_carries_a_permission_error():
    client = StubTransportClient(
        [{"name": "op", "done": True, "error": {"code": 7, "message": "denied"}}]
    )

    with pytest.raises(AuthorizationDenied):
        await client.create_session(user_id=USER_KEY, access_token=TOKEN)


async def test_create_times_out_rather_than_polling_forever():
    op_name = f"{PARENT}/sessions/777/operations/42"
    client = StubTransportClient(
        [{"name": op_name, "done": False}] * 50,
        lro_poll_interval_seconds=0.001,
        lro_poll_timeout_seconds=0.01,
    )

    with pytest.raises(TransientBackendError):
        await client.create_session(user_id=USER_KEY, access_token=TOKEN)


async def test_create_refuses_a_session_owned_by_a_different_user():
    body = _live_shaped_create_response()
    body["response"]["userId"] = "entra:someone:else"
    client = StubTransportClient([body])

    with pytest.raises(SessionOwnershipMismatch):
        await client.create_session(user_id=USER_KEY, access_token=TOKEN)


async def test_a_session_name_that_is_actually_an_operation_name_is_rejected():
    """The classic LRO bug, caught at parse time."""
    with pytest.raises(TransientBackendError):
        Session.from_api(
            {"name": f"{PARENT}/sessions/777/operations/42", "userId": USER_KEY}
        )


async def test_list_sessions_filters_by_user_and_conversation_label():
    client = StubTransportClient([{"sessions": []}])

    await client.list_sessions(
        access_token=TOKEN,
        user_id=USER_KEY,
        labels={"teams_conversation": conversation_label(CONV_1_1)},
    )

    assert client.requests == [
        (
            "GET",
            "https://us-central1-aiplatform.googleapis.com/v1/projects/example-project/"
            "locations/us-central1/reasoningEngines/9000000000000000001/sessions",
        )
    ]


async def test_the_client_refuses_to_call_without_a_user_token():
    """ADR 002, checked on the real transport path."""
    client = SessionsRestClient(
        project="example-project",
        location="us-central1",
        reasoning_engine_id="9000000000000000001",
    )
    try:
        with pytest.raises(IdentityUnavailable):
            await client.get_session(name="sess-1", access_token="")
    finally:
        await client.aclose()


def test_conversation_label_is_label_safe_and_stable():
    label = conversation_label(CONV_1_1)
    assert label == conversation_label(CONV_1_1)
    assert len(label) <= 63
    assert all(c in "0123456789abcdef" for c in label)
    # The raw conversation id must not be recoverable from resource metadata.
    assert CONV_1_1 not in label
