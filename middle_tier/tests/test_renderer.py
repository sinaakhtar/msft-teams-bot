"""Real tests for the streaming renderer. No network, no live Agent Runtime.

WHAT IS AND IS NOT BEING FAKED HERE
-----------------------------------
The ADK event stream is this component's INPUT. Feeding it a synthetic input
to exercise our own logic is ordinary unit testing. What would be dishonest
is claiming these runs prove anything about a LIVE Agent Runtime - they do
not, and NOTES.md says so in as many words. The renderer has NOT been
validated against real streamed output from a deployed engine.

Two things reduce that risk without a network:

* ``test_real_adk_event_dump_shape`` builds genuine ``google.adk`` ``Event``
  objects and serialises them with the exact one-liner the Agent Runtime
  uses (``json.loads(event.model_dump_json(exclude_none=True))`` from
  ``vertexai/agent_engines/_utils.py``), so at least the SHAPE is the real
  library's and not our imagination. It skips if google-adk is not installed
  in the test venv.
* Every other test deliberately feeds shapes that are WRONG - camelCase,
  missing keys, junk types - because the point of the parser is surviving
  exactly the drift we cannot predict.

THE ONE TEST THAT MATTERS MOST
------------------------------
``test_every_content_update_is_cumulative``. Teams streamed updates are
cumulative, not deltas. A renderer that sends only the newest chunk passes a
"final text is correct" test and fails users. So the invariant is asserted
on EVERY emitted update, not just the last one.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ports import AuthorizationDenied  # noqa: E402
from app.streaming.events import (  # noqa: E402
    AdkEventParser,
    TextChunk,
    ToolCallFinished,
    ToolCallStarted,
    ToolError,
    TurnComplete,
    classify_authorization_failure,
    extract_resource,
)
from app.streaming.renderer import TeamsStreamingRenderer  # noqa: E402
from app.streaming.teams_sink import (  # noqa: E402
    STREAM_TYPE_FINAL,
    STREAM_TYPE_INFORMATIVE,
    STREAM_TYPE_STREAMING,
    ConnectorTeamsSink,
    CumulativeContractViolation,
    RecordingTeamsSink,
    build_activity,
)

# --------------------------------------------------------------------------
# Synthetic ADK events, in the shape the runtime really emits
# --------------------------------------------------------------------------
#
# snake_case and sparse (keys ABSENT rather than null), because the runtime
# dumps with `model_dump_json(exclude_none=True)` and WITHOUT `by_alias=True`.
# Verified by reading google-adk 2.8.0 / vertexai agent_engines source.


def text_event(text: str, *, partial: bool = True, author: str = "agent") -> dict[str, Any]:
    event: dict[str, Any] = {
        "content": {"parts": [{"text": text}], "role": "model"},
        "invocation_id": "inv-1",
        "author": author,
        "actions": {"state_delta": {}, "artifact_delta": {}},
        "id": f"ev-{abs(hash(text)) % 10_000}",
        "timestamp": 1788789686.324771,
    }
    if partial:
        event["partial"] = True
    return event


def tool_call_event(name: str, call_id: str = "fc-1", **args: Any) -> dict[str, Any]:
    return {
        "content": {
            "parts": [{"function_call": {"id": call_id, "name": name, "args": args or {}}}],
            "role": "model",
        },
        "invocation_id": "inv-1",
        "author": "agent",
        "actions": {"state_delta": {}, "artifact_delta": {}},
        "id": f"ev-call-{call_id}",
    }


def tool_response_event(
    name: str, response: Any, call_id: str = "fc-1"
) -> dict[str, Any]:
    return {
        "content": {
            "parts": [
                {"function_response": {"id": call_id, "name": name, "response": response}}
            ],
            "role": "user",
        },
        "invocation_id": "inv-1",
        "author": "agent",
        "actions": {"state_delta": {}, "artifact_delta": {}},
        "id": f"ev-resp-{call_id}",
    }


def turn_complete_event() -> dict[str, Any]:
    return {
        "invocation_id": "inv-1",
        "author": "agent",
        "turn_complete": True,
        "actions": {"state_delta": {}, "artifact_delta": {}},
        "id": "ev-done",
    }


def envelope(*events: dict[str, Any]) -> dict[str, Any]:
    """The `_StreamingRunResponse.dump()` wrapper the runtime actually yields."""
    return {"events": list(events), "session_id": "sess-abc"}


async def stream(*items: Any) -> AsyncIterator[Any]:
    for item in items:
        yield item
        await asyncio.sleep(0)


BQ_403 = (
    "403 Access Denied: Table example-project:sales.orders: User does not have "
    "permission to query table example-project:sales.orders, or perhaps it does "
    "not exist. Required permission: bigquery.tables.getData"
)


# ==========================================================================
# 1. THE CUMULATIVE INVARIANT - the most important test in this file
# ==========================================================================


async def test_every_content_update_is_cumulative():
    """Each update must CONTAIN every update before it.

    Teams: "While streaming, the agent messages must contain the previous
    streamed content." A delta-sending renderer replaces the bubble with a
    fragment. Asserting only on the final text would not catch that.
    """
    chunks = ["A brown", " fox", " jumps", " over", " the fence."]
    sink = RecordingTeamsSink()
    renderer = TeamsStreamingRenderer()

    events = stream(*(envelope(text_event(c)) for c in chunks))
    final = await renderer.render(events, sink)

    assert len(sink.content_updates) == len(chunks), sink.content_updates

    # (a) strict prefix chain: update N contains update N-1, entirely.
    for earlier, later in zip(sink.content_updates, sink.content_updates[1:]):
        assert later.startswith(earlier), (
            f"cumulative contract broken: {later!r} does not contain {earlier!r}"
        )
        assert len(later) > len(earlier), "an update must never shrink"

    # (b) EVERY update is a prefix of the final text - no update ever
    #     contained something the final message does not.
    for update in sink.content_updates:
        assert final.startswith(update), f"{update!r} is not a prefix of the final text"

    # (c) the updates really are growing towards the whole answer.
    assert sink.content_updates[0] == "A brown"
    assert sink.content_updates[-1] == "A brown fox jumps over the fence."
    assert final == "A brown fox jumps over the fence."
    assert sink.final_text == final


async def test_returned_final_text_equals_full_concatenation():
    chunks = ["Revenue ", "was ", "$4.2M ", "in Q3."]
    sink = RecordingTeamsSink()
    final = await TeamsStreamingRenderer().render(
        stream(*(envelope(text_event(c)) for c in chunks)), sink
    )
    assert final == "".join(chunks)
    assert sink.final_text == "".join(chunks)


async def test_non_cumulative_update_is_rejected_by_the_sink():
    """The guard itself works: a delta-style update raises, loudly."""
    sink = RecordingTeamsSink()
    await sink.content("A brown")
    with pytest.raises(CumulativeContractViolation):
        await sink.content(" fox")  # the classic bug: sending only the delta


async def test_connector_sink_also_guards_the_cumulative_contract():
    sent: list[dict[str, Any]] = []

    async def send(activity):
        sent.append(dict(activity))
        return {"id": "stream-1"}

    sink = ConnectorTeamsSink(send, min_interval=0.0)
    await sink.content("Hello")
    with pytest.raises(CumulativeContractViolation):
        await sink.content("Goodbye")


# ==========================================================================
# 2. INFORMATIVE UPDATES ON TOOL START - the ADR 005 payoff
# ==========================================================================


async def test_informative_update_emitted_on_tool_start():
    """ADR 005 exists for this. Without it the slow part of a turn looks hung."""
    sink = RecordingTeamsSink()
    renderer = TeamsStreamingRenderer()

    events = stream(
        envelope(tool_call_event("execute_sql_readonly", query="SELECT 1")),
        envelope(tool_response_event("execute_sql_readonly", {"rows": [{"n": 1}]})),
        envelope(text_event("There is ")),
        envelope(text_event("1 row.")),
        envelope(turn_complete_event()),
    )
    final = await renderer.render(events, sink)

    assert "Querying BigQuery..." in sink.informative_updates, sink.informative_updates

    # It must arrive BEFORE any content, or it is useless: informative
    # updates stop displaying once content streaming starts.
    kinds = [kind for kind, _ in sink.calls]
    assert kinds.index("informative") < kinds.index("content")
    assert final == "There is 1 row."


async def test_unknown_tool_still_gets_a_readable_informative_update():
    sink = RecordingTeamsSink()
    await TeamsStreamingRenderer().render(
        stream(envelope(tool_call_event("some_future_tool", call_id="fc-9"))), sink
    )
    assert "Running some future tool..." in sink.informative_updates


async def test_informative_updates_are_not_cumulative():
    """They are status, not content: each replaces the last."""
    sink = RecordingTeamsSink()
    await TeamsStreamingRenderer().render(
        stream(
            envelope(tool_call_event("list_table_ids", call_id="fc-1")),
            envelope(tool_response_event("list_table_ids", {"tables": ["orders"]}, "fc-1")),
            envelope(tool_call_event("execute_sql_readonly", call_id="fc-2")),
        ),
        sink,
    )
    assert "Looking up BigQuery tables..." in sink.informative_updates
    assert "Querying BigQuery..." in sink.informative_updates
    # No update is a superset of a previous one - they are independent lines.
    assert not sink.informative_updates[-1].startswith(sink.informative_updates[0])


# ==========================================================================
# 3. ADR 004 - a tool 403 renders the TEMPLATE, never model prose
# ==========================================================================


async def test_tool_403_renders_templated_denial_and_discards_model_prose():
    model_prose = (
        "It looks like that table may have been renamed or archived recently, "
        "so I could not read it."
    )
    sink = RecordingTeamsSink()
    renderer = TeamsStreamingRenderer()

    events = stream(
        envelope(tool_call_event("execute_sql_readonly", query="SELECT * FROM sales.orders")),
        envelope(
            tool_response_event(
                "execute_sql_readonly",
                {"status": "ERROR", "error_details": BQ_403},
            )
        ),
        envelope(text_event(model_prose)),
        envelope(turn_complete_event()),
    )
    final = await renderer.render(events, sink)

    # The template, naming the refused resource (ADR 004's whole point).
    assert "Access denied" in final
    assert "example-project:sales.orders" in final
    assert "service account" in final  # "I did not retry under a service account."

    # And NOT the model's fluent, confident, fabricated explanation.
    assert model_prose not in final
    assert "renamed or archived" not in final

    assert sink.final_text == final


async def test_tool_401_is_also_treated_as_an_authorization_denial():
    sink = RecordingTeamsSink()
    final = await TeamsStreamingRenderer().render(
        stream(
            envelope(tool_call_event("execute_sql_readonly")),
            envelope(
                tool_response_event(
                    "execute_sql_readonly",
                    {"error": "401 UNAUTHENTICATED: invalid authentication credentials"},
                )
            ),
        ),
        sink,
    )
    assert "Access denied" in final


async def test_denial_names_a_resource_even_when_the_error_has_none():
    """ADR 004 forbids an unattributed denial, and the template raises on one."""
    sink = RecordingTeamsSink()
    final = await TeamsStreamingRenderer().render(
        stream(
            envelope(tool_call_event("execute_sql_readonly")),
            envelope(
                tool_response_event(
                    "execute_sql_readonly", {"status": "ERROR", "error_details": "403 Forbidden"}
                )
            ),
        ),
        sink,
    )
    assert "Access denied" in final
    assert "execute_sql_readonly" in final  # fallback resource name, not a crash


async def test_authorization_denied_raised_by_the_stream_also_renders_the_template():
    async def failing() -> AsyncIterator[Any]:
        yield envelope(text_event("Let me check that"))
        raise AuthorizationDenied("example-project.sales.orders", "missing bigquery.tables.getData")

    sink = RecordingTeamsSink()
    final = await TeamsStreamingRenderer().render(failing(), sink)
    assert "Access denied" in final
    assert "example-project.sales.orders" in final
    assert "Let me check that" not in final


async def test_non_authorization_tool_failure_does_not_produce_a_denial():
    """A 500 is not a permissions problem and must not be dressed as one."""
    sink = RecordingTeamsSink()
    final = await TeamsStreamingRenderer().render(
        stream(
            envelope(tool_call_event("execute_sql_readonly")),
            envelope(
                tool_response_event(
                    "execute_sql_readonly",
                    {"status": "ERROR", "error_details": "500 Internal Error: backend timeout"},
                )
            ),
        ),
        sink,
    )
    assert "Access denied" not in final
    assert "not a permissions problem" in final


# ==========================================================================
# 4. SHAPE DRIFT - unknown events must never kill a turn
# ==========================================================================


async def test_unknown_event_types_do_not_crash_the_turn():
    """The explicitly-recorded most-likely upgrade breakage. Survive it."""

    class Hostile:
        def __getattr__(self, name):
            raise RuntimeError(f"no attribute {name}, and I am angry about it")

    junk: list[Any] = [
        None,
        42,
        "this is not json at all",
        b"\x00\x01binary",
        [],
        {},
        {"type": "something_adk_3_invented"},
        {"events": None},
        {"events": [{"weird_new_field": {"nested": True}}]},
        {"content": {"parts": [{"inline_data": {"mime_type": "image/png", "data": "x"}}]}},
        {"content": {"parts": [{"executable_code": {"code": "print(1)"}}]}},
        {"content": "not a dict at all"},
        {"content": {"parts": "also not a list"}},
        Hostile(),
        object(),
    ]

    sink = RecordingTeamsSink()
    renderer = TeamsStreamingRenderer()

    events = stream(
        junk[0],
        envelope(text_event("The answer ")),
        *junk[1:8],
        envelope(text_event("is ")),
        *junk[8:],
        envelope(text_event("42.")),
        envelope(turn_complete_event()),
    )
    final = await renderer.render(events, sink)

    assert final == "The answer is 42."
    assert sink.final_text == "The answer is 42."
    for earlier, later in zip(sink.content_updates, sink.content_updates[1:]):
        assert later.startswith(earlier)


async def test_camelcase_event_shape_parses_too():
    """Today the runtime dumps snake_case. One `by_alias=True` flips it."""
    parser = AdkEventParser()
    parsed = parser.feed(
        {
            "events": [
                {
                    "content": {"parts": [{"functionCall": {"name": "execute_sql_readonly"}}]},
                    "invocationId": "inv-1",
                    "author": "agent",
                }
            ]
        }
    )
    assert [type(p) for p in parsed] == [ToolCallStarted]
    assert parsed[0].name == "execute_sql_readonly"

    parsed = parser.feed({"turnComplete": True, "author": "agent"})
    assert any(isinstance(p, TurnComplete) for p in parsed)


async def test_bare_event_without_the_envelope_parses():
    """Envelope today; someone will hand us bare events tomorrow."""
    sink = RecordingTeamsSink()
    final = await TeamsStreamingRenderer().render(
        stream(text_event("bare "), text_event("events")), sink
    )
    assert final == "bare events"


async def test_partial_run_then_aggregate_is_not_double_counted():
    """ADK streams deltas as `partial`, then repeats the whole thing once."""
    sink = RecordingTeamsSink()
    final = await TeamsStreamingRenderer().render(
        stream(
            envelope(text_event("Hello ", partial=True)),
            envelope(text_event("world", partial=True)),
            envelope(text_event("Hello world", partial=False)),  # the aggregate
            envelope(turn_complete_event()),
        ),
        sink,
    )
    assert final == "Hello world", f"double-counted: {final!r}"


async def test_aggregate_that_extends_the_partial_run_keeps_the_remainder():
    sink = RecordingTeamsSink()
    final = await TeamsStreamingRenderer().render(
        stream(
            envelope(text_event("Hello ", partial=True)),
            envelope(text_event("Hello world!", partial=False)),
        ),
        sink,
    )
    assert final == "Hello world!"


async def test_parser_counts_what_it_could_not_understand():
    parser = AdkEventParser()
    parser.feed({"totally": "unknown"})
    parser.feed("not json")
    assert parser.unknown_event_count == 2


async def test_empty_stream_still_terminates_the_teams_message():
    """An empty bubble that never resolves is the worst outcome available."""
    sink = RecordingTeamsSink()
    final = await TeamsStreamingRenderer().render(stream(), sink)
    assert sink.final_text is not None
    assert final


# ==========================================================================
# 5. RATE LIMITING - documented as 1 request per second
# ==========================================================================


async def test_rate_limiter_spaces_updates_in_real_time():
    """Real wall clock, small interval. Sends must be >= interval apart."""
    interval = 0.05
    sent_at: list[float] = []

    async def send(activity):
        sent_at.append(time.monotonic())
        return {"id": "stream-1"}

    sink = ConnectorTeamsSink(send, min_interval=interval)

    # 40 rapid cumulative updates, far faster than the window allows.
    text = ""
    for i in range(40):
        text += f"{i} "
        await sink.content(text)
        await asyncio.sleep(0.005)
    await sink.final(text)

    assert len(sent_at) >= 2, "expected at least an opening send and a final"
    gaps = [b - a for a, b in zip(sent_at, sent_at[1:])]
    for gap in gaps:
        assert gap >= interval * 0.9, f"updates {gap:.4f}s apart, below the {interval}s limit"

    # Coalescing did its job: we did not send one request per chunk.
    assert len(sent_at) < 40, f"no coalescing happened: {len(sent_at)} sends for 40 chunks"


async def test_rate_limiter_coalesces_deterministically_with_a_fake_clock():
    now = [1000.0]
    sent: list[dict[str, Any]] = []

    async def send(activity):
        sent.append(dict(activity))
        return {"id": "stream-1"}

    sink = ConnectorTeamsSink(send, min_interval=1.0, clock=lambda: now[0])

    await sink.content("A")            # window open -> sends
    await sink.content("A brown")      # throttled -> coalesced
    await sink.content("A brown fox")  # throttled -> coalesced
    assert [a["text"] for a in sent] == ["A"]

    now[0] += 1.0                      # window reopens
    await sink.content("A brown fox jumps")
    assert [a["text"] for a in sent] == ["A", "A brown fox jumps"]

    # Coalescing skipped two updates and lost nothing: the update that DID
    # go out contains everything the skipped ones would have said.
    assert sent[-1]["text"].startswith(sent[0]["text"])


async def test_final_message_waits_out_the_throttle_rather_than_being_dropped():
    now = [1000.0]
    sent: list[dict[str, Any]] = []
    slept: list[float] = []

    async def send(activity):
        sent.append(dict(activity))
        return {"id": "stream-1"}

    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        slept.append(seconds)
        now[0] += seconds
        await real_sleep(0)

    sink = ConnectorTeamsSink(send, min_interval=1.0, clock=lambda: now[0])
    await sink.content("partial")

    import app.streaming.teams_sink as teams_sink_module

    original = teams_sink_module.asyncio.sleep
    teams_sink_module.asyncio.sleep = fake_sleep
    try:
        await sink.final("partial answer")
    finally:
        teams_sink_module.asyncio.sleep = original

    assert slept and slept[0] == pytest.approx(1.0)
    assert sent[-1]["type"] == "message"
    assert sent[-1]["text"] == "partial answer"


# ==========================================================================
# 6. THE TEAMS WIRE FORMAT
# ==========================================================================


async def test_stream_id_and_sequence_bookkeeping():
    sent: list[dict[str, Any]] = []

    async def send(activity):
        sent.append(dict(activity))
        return {"id": "stream-xyz"}  # Teams hands back the streamId here

    sink = ConnectorTeamsSink(send, min_interval=0.0)
    await sink.informative("Querying BigQuery...")
    await sink.content("Revenue ")
    await sink.content("Revenue was $4.2M.")
    await sink.final("Revenue was $4.2M.")

    def info(activity):
        return next(e for e in activity["entities"] if e["type"] == "streaminfo")

    # First activity opens the stream: no streamId yet, sequence starts at 1,
    # and it MUST carry text or Teams answers 400.
    assert "streamId" not in info(sent[0])
    assert info(sent[0])["streamSequence"] == 1
    assert info(sent[0])["streamType"] == STREAM_TYPE_INFORMATIVE
    assert sent[0]["type"] == "typing"
    assert sent[0]["text"]

    # Everything after carries the streamId and increments by exactly 1.
    assert [info(a).get("streamSequence") for a in sent[:-1]] == [1, 2, 3]
    for activity in sent[1:]:
        assert info(activity)["streamId"] == "stream-xyz"

    assert info(sent[1])["streamType"] == STREAM_TYPE_STREAMING
    assert sent[1]["type"] == "typing"

    # The terminating activity: type `message`, streamType `final`, and NO
    # streamSequence ("Don't set streamSequence for the final message").
    last = sent[-1]
    assert last["type"] == "message"
    assert info(last)["streamType"] == STREAM_TYPE_FINAL
    assert "streamSequence" not in info(last)
    assert info(last)["streamId"] == "stream-xyz"


async def test_channel_data_mirrors_the_streaminfo_entity():
    activity = build_activity(
        text="hi", stream_type=STREAM_TYPE_STREAMING, stream_id="s-1", sequence=3
    )
    entity = activity["entities"][0]
    assert entity == {
        "type": "streaminfo",
        "streamType": "streaming",
        "streamId": "s-1",
        "streamSequence": 3,
    }
    assert activity["channelData"] == {
        "streamType": "streaming",
        "streamId": "s-1",
        "streamSequence": 3,
    }


async def test_informative_text_is_truncated_to_the_documented_limit():
    sent: list[dict[str, Any]] = []

    async def send(activity):
        sent.append(dict(activity))
        return {"id": "s"}

    sink = ConnectorTeamsSink(send, min_interval=0.0)
    await sink.informative("x" * 5000)
    assert len(sent[0]["text"]) == 1000


async def test_informative_updates_stop_once_content_starts():
    """Documented: after the first content chunk they no longer display."""
    sent: list[dict[str, Any]] = []

    async def send(activity):
        sent.append(dict(activity))
        return {"id": "s"}

    sink = ConnectorTeamsSink(send, min_interval=0.0)
    await sink.content("answering")
    await sink.informative("Querying BigQuery...")
    assert [a["text"] for a in sent] == ["answering"]


async def test_transport_failure_on_a_progress_update_does_not_kill_the_turn():
    calls = {"n": 0}

    async def flaky(activity):
        calls["n"] += 1
        if activity["entities"][0]["streamType"] != STREAM_TYPE_FINAL:
            raise RuntimeError("connector 502")
        return {"id": "s"}

    sink = ConnectorTeamsSink(flaky, min_interval=0.0)
    final = await TeamsStreamingRenderer().render(
        stream(envelope(text_event("still ")), envelope(text_event("works"))), sink
    )
    assert final == "still works"
    assert calls["n"] >= 3


# ==========================================================================
# 7. THE PARSER, DIRECTLY
# ==========================================================================


def test_authorization_classifier():
    assert classify_authorization_failure(BQ_403) == "403"
    assert classify_authorization_failure("PERMISSION_DENIED") == "PERMISSION_DENIED"
    assert classify_authorization_failure("401 Unauthorized") == "401"
    assert classify_authorization_failure("500 backend timeout") is None
    assert classify_authorization_failure("") is None


def test_resource_extraction():
    assert extract_resource(BQ_403, fallback="tool") == "example-project:sales.orders"
    assert (
        extract_resource("Permission denied on `example-project.sales.orders`", fallback="tool")
        == "example-project.sales.orders"
    )
    assert (
        extract_resource(
            "denied: projects/example-project/datasets/sales", fallback="tool"
        )
        == "projects/example-project/datasets/sales"
    )
    assert extract_resource("nothing useful here", fallback="execute_sql_readonly") == (
        "execute_sql_readonly"
    )


def test_parser_vocabulary_for_a_whole_happy_turn():
    parser = AdkEventParser()
    seen: list[Any] = []
    for raw in (
        envelope(tool_call_event("execute_sql_readonly", query="SELECT 1")),
        envelope(tool_response_event("execute_sql_readonly", {"rows": []})),
        envelope(text_event("No rows.")),
        envelope(turn_complete_event()),
    ):
        seen.extend(parser.feed(raw))

    assert [type(s) for s in seen] == [
        ToolCallStarted,
        ToolCallFinished,
        TextChunk,
        TurnComplete,
    ]
    assert seen[0].args == {"query": "SELECT 1"}
    assert seen[1].ok is True


def test_parser_emits_toolerror_then_toolcallfinished_for_a_denial():
    parser = AdkEventParser()
    parser.feed(envelope(tool_call_event("execute_sql_readonly")))
    out = parser.feed(
        envelope(
            tool_response_event(
                "execute_sql_readonly", {"status": "ERROR", "error_details": BQ_403}
            )
        )
    )
    assert [type(o) for o in out] == [ToolError, ToolCallFinished]
    assert out[0].authorization is True
    assert out[0].status == "403"
    assert out[0].resource == "example-project:sales.orders"
    assert out[1].ok is False


def test_parser_reads_an_event_level_error_code():
    out = AdkEventParser().feed(
        {"author": "agent", "error_code": "403", "error_message": "PERMISSION_DENIED on sales"}
    )
    assert isinstance(out[0], ToolError)
    assert out[0].authorization is True


# ==========================================================================
# 8. THE REAL google-adk EVENT SHAPE (skipped if the lib is absent)
# ==========================================================================


def test_real_adk_event_dump_shape():
    """Build real ``Event`` objects and dump them the way the runtime does.

    This is still not a live Agent Runtime - it is the real library's
    serialisation of real objects, which is the closest we can get without
    a deployed engine and a user token.
    """
    adk_events = pytest.importorskip(
        "google.adk.events.event", reason="google-adk not installed in this venv"
    )
    from google.genai import types  # noqa: PLC0415

    Event = adk_events.Event

    def runtime_dump(event):
        # vertexai/agent_engines/_utils.py::dump_event_for_json, verbatim.
        return json.loads(event.model_dump_json(exclude_none=True))

    text = runtime_dump(
        Event(
            author="agent",
            invocation_id="inv-1",
            partial=True,
            content=types.Content(role="model", parts=[types.Part(text="Hello ")]),
        )
    )
    call = runtime_dump(
        Event(
            author="agent",
            invocation_id="inv-1",
            content=types.Content(
                role="model",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(
                            id="fc-1", name="execute_sql_readonly", args={"query": "SELECT 1"}
                        )
                    )
                ],
            ),
        )
    )
    denial = runtime_dump(
        Event(
            author="agent",
            invocation_id="inv-1",
            content=types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id="fc-1",
                            name="execute_sql_readonly",
                            response={"status": "ERROR", "error_details": BQ_403},
                        )
                    )
                ],
            ),
        )
    )

    # The findings this component is built on, asserted rather than assumed:
    # snake_case keys, and absent-not-null for unset fields.
    assert "invocation_id" in text and "invocationId" not in text
    assert "function_call" in call["content"]["parts"][0]
    assert "partial" not in call  # exclude_none=True drops it entirely

    parser = AdkEventParser()
    out = parser.feed({"events": [text], "session_id": "s"})
    assert out == [TextChunk("Hello ")]

    out = parser.feed({"events": [call], "session_id": "s"})
    assert isinstance(out[0], ToolCallStarted)
    assert out[0].name == "execute_sql_readonly"

    out = parser.feed({"events": [denial], "session_id": "s"})
    assert isinstance(out[0], ToolError)
    assert out[0].authorization is True
    assert out[0].resource == "example-project:sales.orders"
    assert parser.unknown_event_count == 0


async def test_end_to_end_with_real_adk_serialised_events():
    adk_events = pytest.importorskip(
        "google.adk.events.event", reason="google-adk not installed in this venv"
    )
    from google.genai import types  # noqa: PLC0415

    Event = adk_events.Event

    def dumped(**kwargs):
        return {
            "events": [json.loads(Event(**kwargs).model_dump_json(exclude_none=True))],
            "session_id": "s",
        }

    def part_text(t):
        return dumped(
            author="agent",
            invocation_id="i",
            partial=True,
            content=types.Content(role="model", parts=[types.Part(text=t)]),
        )

    sink = RecordingTeamsSink()
    final = await TeamsStreamingRenderer().render(
        stream(
            dumped(
                author="agent",
                invocation_id="i",
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                id="fc-1", name="execute_sql_readonly", args={"q": "SELECT 1"}
                            )
                        )
                    ],
                ),
            ),
            dumped(
                author="agent",
                invocation_id="i",
                content=types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                id="fc-1",
                                name="execute_sql_readonly",
                                response={"rows": [{"total": 42}]},
                            )
                        )
                    ],
                ),
            ),
            part_text("The total "),
            part_text("is 42."),
            dumped(author="agent", invocation_id="i", turn_complete=True),
        ),
        sink,
    )

    assert final == "The total is 42."
    assert "Querying BigQuery..." in sink.informative_updates
    for earlier, later in zip(sink.content_updates, sink.content_updates[1:]):
        assert later.startswith(earlier)
