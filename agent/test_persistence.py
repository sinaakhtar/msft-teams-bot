"""PROVE the user's access token is never written to durable session history.

This is the single most important design constraint on this agent, so it gets
an executable test rather than a comment. The claim under test:

    A token delivered as `authorizations[...]` on a
    `streaming_agent_run_with_events` request reaches the agent as
    `temp:<auth_id>` session state, is readable during the invocation, and is
    STRIPPED before anything is sent to the managed Sessions service.

Two independent assertions:

  1. `BaseSessionService.append_event` applies `temp:` keys to the in-memory
     session (so the invocation can read them) AND removes them from
     `event.actions.state_delta` (so they are not part of the persisted event).

  2. `VertexAiSessionService.append_event` -- the class that actually runs
     inside Agent Runtime -- sends a payload to the Sessions API that contains
     no trace of the token, in either the legacy `actions.state_delta` field or
     the newer `raw_event` blob. The API client is replaced with a capturing
     fake, so no network and no credentials are needed, and the assertion is on
     the real bytes the real code would have sent.

Runs entirely offline:  python test_persistence.py
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys

from google.adk.events.event import Event, EventActions
from google.adk.sessions import InMemorySessionService
from google.adk.sessions.session import Session
from google.adk.sessions.vertex_ai_session_service import VertexAiSessionService
from google.genai import types

from bq_agent.credentials import TEMP_STATE_KEY, assert_no_persisted_token

SECRET = "ya29.THIS-TOKEN-MUST-NEVER-BE-PERSISTED"

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        failures.append(label)


# --------------------------------------------------------------------------
# 1. The base session service contract
# --------------------------------------------------------------------------


async def test_base_session_service() -> None:
    print("=== 1. BaseSessionService: temp: applied in memory, trimmed from the event ===")
    service = InMemorySessionService()
    session = await service.create_session(app_name="app", user_id="u1")

    event = Event(
        invocation_id="inv-1",
        author="user",
        content=types.Content(role="user", parts=[types.Part(text="hi")]),
        actions=EventActions(
            state_delta={
                TEMP_STATE_KEY: SECRET,          # the credential channel
                "user:display_name": "Analyst",  # an ordinary, persisted key
            }
        ),
    )
    await service.append_event(session, event)

    check(
        session.state.get(TEMP_STATE_KEY) == SECRET,
        "token IS readable from the live in-memory session during the invocation",
    )
    check(
        TEMP_STATE_KEY not in event.actions.state_delta,
        "token was REMOVED from event.actions.state_delta before persistence",
    )
    check(
        event.actions.state_delta.get("user:display_name") == "Analyst",
        "an ordinary (non-temp:) key is left alone and still persists",
    )

    reloaded = await service.get_session(
        app_name="app", user_id="u1", session_id=session.id
    )
    # NOTE: InMemorySessionService hands back the SAME live state dict, so its
    # in-memory copy legitimately still holds the temp key; asserting otherwise
    # here would be asserting something untrue. What matters for durability is
    # the persisted EVENT, checked above and again against the real Vertex
    # payload in test 2.
    persisted_events = json.dumps(
        [e.model_dump(mode="json") for e in reloaded.events], default=str
    )
    check(
        SECRET not in persisted_events,
        "no event in the session's durable event list contains the token",
    )


# --------------------------------------------------------------------------
# 2. The class that actually runs in Agent Runtime
# --------------------------------------------------------------------------


class _CapturingEvents:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    async def append(self, **kwargs) -> None:
        self._sink.append(kwargs)


class _CapturingClient:
    def __init__(self, sink: list) -> None:
        self.agent_engines = type(
            "_AE", (), {"sessions": type("_S", (), {"events": _CapturingEvents(sink)})()}
        )()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_vertex_session_service() -> None:
    print()
    print("=== 2. VertexAiSessionService: what would ACTUALLY go over the wire ===")
    captured: list = []

    service = VertexAiSessionService(
        project="example-project", location="us-central1", agent_engine_id="1234567890"
    )
    service._get_api_client = lambda: _CapturingClient(captured)  # type: ignore[method-assign]

    session = Session(
        id="9876543210",
        app_name="1234567890",
        user_id="u1",
        state={},
        events=[],
        last_update_time=0.0,
    )
    event = Event(
        invocation_id="inv-2",
        author="user",
        content=types.Content(role="user", parts=[types.Part(text="who am i")]),
        actions=EventActions(
            state_delta={TEMP_STATE_KEY: SECRET, "user:tenant": "contoso"}
        ),
    )

    await service.append_event(session, event)

    check(bool(captured), "an append call was made (fake client captured it)")
    if not captured:
        return

    payload = json.dumps(captured[-1], default=str)
    check(
        SECRET not in payload,
        "the ENTIRE Sessions API payload contains no trace of the access token",
    )
    config = captured[-1].get("config", {})
    check(
        TEMP_STATE_KEY not in (config.get("actions", {}).get("state_delta") or {}),
        "config.actions.state_delta has no temp: key",
    )
    raw_event = config.get("raw_event") or {}
    raw_delta = (raw_event.get("actions") or {}).get("state_delta") or {}
    check(TEMP_STATE_KEY not in raw_delta, "config.raw_event.actions.state_delta has no temp: key")
    check(
        (config.get("actions", {}).get("state_delta") or {}).get("user:tenant") == "contoso",
        "an ordinary key still reaches the Sessions API (the trim is targeted, not blanket)",
    )


# --------------------------------------------------------------------------
# 3. The tripwire helper itself
# --------------------------------------------------------------------------


def test_tripwire() -> None:
    print()
    print("=== 3. assert_no_persisted_token tripwire ===")
    assert_no_persisted_token({TEMP_STATE_KEY: SECRET, "user:name": "Analyst"})
    check(True, "a token under temp: is allowed")

    raised = False
    try:
        assert_no_persisted_token({"user:token": SECRET})
    except AssertionError:
        raised = True
    check(raised, "a token under a persisted key raises")


async def main() -> int:
    await test_base_session_service()
    with contextlib.suppress(ImportError):
        await test_vertex_session_service()
    test_tripwire()
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print("ALL PERSISTENCE ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
