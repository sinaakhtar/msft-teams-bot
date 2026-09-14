# NOTES - Streaming Renderer

Working notes for the `middle_tier/app/streaming/` component. Written to be
read by whoever picks this up next, including the parts that are not
finished.

Date of the test runs below: 2026-09-07.

---

## 1. "Exists" vs "executed successfully"

The distinction the rest of this document keeps.

| Thing | Exists | Executed successfully | Notes |
|---|---|---|---|
| `app/streaming/events.py` | yes | yes | imported and exercised by 34 passing tests |
| `app/streaming/teams_sink.py` | yes | yes (against a fake transport) | the HTTP transport is injected and was never given a real Bot Connector |
| `app/streaming/renderer.py` | yes | yes | driven end to end by the tests |
| `tests/test_renderer.py` | yes | **yes - 34 passed, real output pasted in section 3** | |
| `app/streaming/README.md` | yes | n/a | |
| Parsing of **synthetic** ADK events | yes | yes | |
| Parsing of **real `google.adk` `Event` objects**, serialised exactly as the runtime serialises them | yes | yes | `test_real_adk_event_dump_shape`, `test_end_to_end_with_real_adk_serialised_events` |
| Parsing of a **live** `streaming_agent_run_with_events` response | yes (code path) | **NO - never run** | BLOCKED, section 5 |
| Sending a real Teams streaming activity to the Bot Connector | yes (code path) | **NO - never run** | BLOCKED, section 5 |
| Rate limiter honouring the real 1 rps Teams limit | yes | partially - the *mechanism* was tested with a 0.05 s interval and a fake clock; the 1.0 s default has never been exercised against Teams | |
| `ports.StreamingRenderer` (push-style) adapter | **no** | n/a | deliberate, section 6 |

Blunt version: **the renderer has never seen a byte from a live Agent Runtime
and has never sent a byte to Teams.** Everything below that says "verified" is
verified against the installed libraries and the published documentation, not
against a running system.

---

## 2. What exists

```
middle_tier/app/streaming/
  __init__.py       re-exports the vocabulary, the sinks and the renderer
  events.py         defensive ADK -> internal vocabulary parser
  teams_sink.py     Teams wire protocol, cumulative contract, rate limiting
  renderer.py       the loop
  README.md         the contract, the ADR rationale, the upgrade runbook
  NOTES.md          this file
middle_tier/tests/
  test_renderer.py  34 tests
```

**Internal vocabulary** (`events.py`): `TextChunk`, `ToolCallStarted(name, call_id, args)`,
`ToolCallFinished(name, ok, call_id)`, `ToolError(name, status, resource, authorization, detail)`,
`TurnComplete(reason)`. Unknown events are counted, logged at DEBUG, and dropped.
`AdkEventParser.feed()` cannot raise - it is wrapped in a bare `except`.

**Interface**, exactly as specified:

```python
async def render(self, events: AsyncIterator[Any], sink: TeamsSink) -> str
```

`TeamsSink` is three verbs: `informative(text)`, `content(cumulative_text)`,
`final(text)`. Two implementations ship: `ConnectorTeamsSink` (real, takes an
injected async `send(activity) -> response`) and `RecordingTeamsSink` (records,
no network, enforces the cumulative contract).

---

## 3. Tests I ran, and their real output

### 3a. The renderer suite, in the venv that has `google-adk` installed

```
$ cd middle_tier && ../agent/.venv/bin/python -m pytest tests/test_renderer.py -v
============================= test session starts ==============================
platform linux -- Python 3.13.15, pytest-9.1.1, pluggy-1.6.0 -- <REPO_ROOT>/middle_tier/../agent/.venv/bin/python
cachedir: .pytest_cache
rootdir: <REPO_ROOT>/middle_tier
configfile: pyproject.toml
plugins: anyio-4.15.1, asyncio-1.4.0
asyncio: mode=Mode.AUTO, debug=False, asyncio_default_fixture_loop_scope=None, asyncio_default_test_loop_scope=function
collecting ... collected 34 items

tests/test_renderer.py::test_every_content_update_is_cumulative PASSED   [  2%]
tests/test_renderer.py::test_returned_final_text_equals_full_concatenation PASSED [  5%]
tests/test_renderer.py::test_non_cumulative_update_is_rejected_by_the_sink PASSED [  8%]
tests/test_renderer.py::test_connector_sink_also_guards_the_cumulative_contract PASSED [ 11%]
tests/test_renderer.py::test_informative_update_emitted_on_tool_start PASSED [ 14%]
tests/test_renderer.py::test_unknown_tool_still_gets_a_readable_informative_update PASSED [ 17%]
tests/test_renderer.py::test_informative_updates_are_not_cumulative PASSED [ 20%]
tests/test_renderer.py::test_tool_403_renders_templated_denial_and_discards_model_prose PASSED [ 23%]
tests/test_renderer.py::test_tool_401_is_also_treated_as_an_authorization_denial PASSED [ 26%]
tests/test_renderer.py::test_denial_names_a_resource_even_when_the_error_has_none PASSED [ 29%]
tests/test_renderer.py::test_authorization_denied_raised_by_the_stream_also_renders_the_template PASSED [ 32%]
tests/test_renderer.py::test_non_authorization_tool_failure_does_not_produce_a_denial PASSED [ 35%]
tests/test_renderer.py::test_unknown_event_types_do_not_crash_the_turn PASSED [ 38%]
tests/test_renderer.py::test_camelcase_event_shape_parses_too PASSED     [ 41%]
tests/test_renderer.py::test_bare_event_without_the_envelope_parses PASSED [ 44%]
tests/test_renderer.py::test_partial_run_then_aggregate_is_not_double_counted PASSED [ 47%]
tests/test_renderer.py::test_aggregate_that_extends_the_partial_run_keeps_the_remainder PASSED [ 50%]
tests/test_renderer.py::test_parser_counts_what_it_could_not_understand PASSED [ 52%]
tests/test_renderer.py::test_empty_stream_still_terminates_the_teams_message PASSED [ 55%]
tests/test_renderer.py::test_rate_limiter_spaces_updates_in_real_time PASSED [ 58%]
tests/test_renderer.py::test_rate_limiter_coalesces_deterministically_with_a_fake_clock PASSED [ 61%]
tests/test_renderer.py::test_final_message_waits_out_the_throttle_rather_than_being_dropped PASSED [ 64%]
tests/test_renderer.py::test_stream_id_and_sequence_bookkeeping PASSED   [ 67%]
tests/test_renderer.py::test_channel_data_mirrors_the_streaminfo_entity PASSED [ 70%]
tests/test_renderer.py::test_informative_text_is_truncated_to_the_documented_limit PASSED [ 73%]
tests/test_renderer.py::test_informative_updates_stop_once_content_starts PASSED [ 76%]
tests/test_renderer.py::test_transport_failure_on_a_progress_update_does_not_kill_the_turn PASSED [ 79%]
tests/test_renderer.py::test_authorization_classifier PASSED             [ 82%]
tests/test_renderer.py::test_resource_extraction PASSED                  [ 85%]
tests/test_renderer.py::test_parser_vocabulary_for_a_whole_happy_turn PASSED [ 88%]
tests/test_renderer.py::test_parser_emits_toolerror_then_toolcallfinished_for_a_denial PASSED [ 91%]
tests/test_renderer.py::test_parser_reads_an_event_level_error_code PASSED [ 94%]
tests/test_renderer.py::test_real_adk_event_dump_shape PASSED            [ 97%]
tests/test_renderer.py::test_end_to_end_with_real_adk_serialised_events PASSED [100%]

============================== 34 passed in 1.16s ==============================
```

### 3b. The same suite in the middle tier's own venv

`middle_tier/.venv` does not have `google-adk`, so the two tests that build
real `Event` objects skip. Recorded here so nobody mistakes the skips for
passes:

```
$ cd middle_tier && .venv/bin/python -m pytest tests/test_renderer.py -q
................................ss                                       [100%]
32 passed, 2 skipped in 0.45s
```

The two `s` are `test_real_adk_event_dump_shape` and
`test_end_to_end_with_real_adk_serialised_events`. **They passed in 3a.** To
run them in the middle tier venv you would need `uv pip install google-adk`
there, which I did not do because `pyproject.toml` deliberately pins the
middle tier's dependency set and google-adk is not one of them.

### 3c. The whole existing middle tier suite, to confirm nothing regressed

```
$ cd middle_tier && .venv/bin/python -m pytest -q
........................................................................ [ 33%]
........................................................................ [ 67%]
........................ss...........................................    [100%]
211 passed, 2 skipped in 1.22s
```

### 3d. What I installed to make 3a possible

```
$ VIRTUAL_ENV=$PWD/agent/.venv uv pip install "pytest==9.1.1" pytest-asyncio
Installed 5 packages in 1.73s
 + iniconfig==2.3.0
 + pluggy==1.6.0
 + pygments==2.21.0
 + pytest==9.1.1
 + pytest-asyncio==1.4.0
```

`agent/.venv` already had `google-adk 2.8.0`, `google-genai`, `aiohttp`,
`cryptography` and `PyJWT`; it was missing only pytest. Note this modified
`agent/.venv`, which is a side effect on someone else's venv - revert it if
that is unwelcome.

### 3e. What the tests actually assert

Everything asked for, plus a few things that turned out to matter:

* **The cumulative invariant, on every update, not just the last.**
  `test_every_content_update_is_cumulative` asserts the full prefix chain
  (`update[n].startswith(update[n-1])`, strictly growing) *and* that every
  emitted update is a prefix of the returned final text. Asserting only the
  final text would let a delta-sending renderer pass.
* Informative update on tool start, **and that it arrives before any content**
  (it is invisible afterwards, so ordering is the whole point).
* A tool 403 produces the ADR 004 template naming `<GCP_PROJECT_ID>:sales.orders`,
  and the model's prose ("It looks like that table may have been renamed or
  archived recently") is asserted **absent** from the final text.
* Unknown event types do not crash the turn: the junk stream includes `None`,
  `42`, a non-JSON string, raw bytes, `{}`, `{"events": None}`, an
  `inline_data` part, an `executable_code` part, `{"content": "not a dict"}`,
  a `Hostile()` object whose `__getattr__` raises on every access, and a bare
  `object()`. The correct answer still comes out the other end.
* The returned final text equals the full concatenation.
* The rate limiter spaces updates: 40 rapid updates through a 0.05 s window,
  asserting every real wall-clock gap is >= the interval **and** that fewer
  than 40 requests went out (i.e. coalescing actually happened). Plus a
  deterministic fake-clock test of the coalescing, and a test that the final
  message *waits out* the throttle rather than being dropped.
* `streamId` / `streamSequence` bookkeeping: first activity has no `streamId`
  and sequence 1, later ones echo the id and increment by 1, the final
  activity is `type: message` / `streamType: final` with **no**
  `streamSequence`.
* The partial/aggregate double-count trap (section 4).

---

## 4. What I learned about the real ADK event shape, from the installed source

`google-adk 2.8.0`, read at
`agent/.venv/lib/python3.13/site-packages/google/adk/` and
`.../vertexai/agent_engines/`. Not guessed - the file and line are given for
each claim, and the JSON below is real output from
`agent/.venv/bin/python`, not an illustration.

**a. `Event` is `LlmResponse` plus a handful of fields.**
`google/adk/events/event.py:Event(LlmResponse)`. Its own fields:
`invocation_id`, `author`, `actions` (`EventActions`), `output`, `node_info`
(`NodeInfo`), `long_running_tool_ids`, `branch`, `isolation_scope`, `id`,
`timestamp`. Inherited from `google/adk/models/llm_response.py`: `content`,
`partial`, `turn_complete`, `turn_complete_reason`, `finish_reason`,
`error_code`, `error_message`, `interrupted`, `custom_metadata`,
`usage_metadata`, `grounding_metadata`, `interaction_status` and more.

**b. The serialisation is one line, and it decides everything.**
`vertexai/agent_engines/_utils.py:680`:

```python
def dump_event_for_json(event: BaseModel) -> Dict[str, Any]:
    return json.loads(event.model_dump_json(exclude_none=True))
```

Called from `_StreamingRunResponse.dump()` in
`vertexai/agent_engines/templates/adk.py:245`, which is what
`streaming_agent_run_with_events` yields
(`.../templates/adk.py:1341` and `:1447`).

**c. Therefore: snake_case on the wire, not camelCase.** `Event.model_config`
sets `alias_generator=alias_generators.to_camel` and `populate_by_name=True`
(`events/event.py:98-103`), *but* `model_dump_json` is called **without**
`by_alias=True`. So today's wire is `invocation_id`, `function_call`,
`error_code`, `turn_complete`. One keyword argument upstream flips the entire
stream to camelCase. **The parser accepts both spellings for every field**,
and `test_camelcase_event_shape_parses_too` covers the flip.

**d. Therefore: `exclude_none=True` means keys are ABSENT, not null.**
`event["partial"]` raises `KeyError` on most events. Every access in
`events.py` goes through `_get(obj, *names, default=...)`.

**e. The envelope.** Per streamed chunk:
`{"events": [ {...} ], "session_id": "...", "artifacts": [...]}`. One event
per chunk in practice - `_convert_response_events(..., events=[event], ...)`
is called with a single-element list inside the `async for`. The parser
accepts the envelope, a bare event, a list, and a JSON string.

**f. Real dumped output** (from `agent/.venv/bin/python`, dumping constructed
`Event` objects with the exact `dump_event_for_json` one-liner):

```json
{
  "content": {"parts": [{"text": "Hello "}], "role": "model"},
  "partial": true,
  "invocation_id": "inv-1",
  "author": "agent",
  "actions": {"state_delta": {}, "artifact_delta": {},
              "requested_auth_configs": {}, "requested_tool_confirmations": {}},
  "node_info": {"path": ""},
  "id": "ab5b0826-d3eb-452f-94d3-47f1503e4a62",
  "timestamp": 1788789686.324771
}
```

```json
{
  "content": {"parts": [{"function_call": {"id": "fc-1",
      "args": {"query": "SELECT 1"}, "name": "execute_sql_readonly"}}],
   "role": "model"},
  "invocation_id": "inv-1", "author": "agent", ...
}
```

Note there is **no `partial` key at all** on the function-call event.

**g. `Event.model_config` has `extra='ignore'`** while its parent
`LlmResponse` has `extra='forbid'` (`event.py:99`, `llm_response.py:53`).
So an `Event` tolerates unknown fields but the base class does not - a detail
that will matter if anyone tries to round-trip these dicts back through
pydantic. We do not: we read them as plain dicts.

**h. The partial/aggregate double-count trap.** ADK emits `partial: true`
events carrying deltas, then a non-partial event for the same response
carrying the whole text again. Naively appending doubles the answer.
`AdkEventParser` is stateful for exactly this and emits only the genuine
remainder. This is a real behaviour of ADK streaming, not a hypothetical -
but note I confirmed it from the `is_final_response()` / `partial` semantics
in the source and from how `Runner.run_async` yields, **not** by watching a
live stream. It is therefore the most likely of my claims to be wrong in
detail, and `test_partial_run_then_aggregate_is_not_double_counted` is the
place to re-check once live output is available.

**i. `is_final_response()`** (`event.py`) returns True when there are no
function calls, no function responses, not `partial`, and no trailing code
execution result. I chose **not** to depend on it: it is a semantic judgement
that has changed across versions, and `turn_complete` plus stream exhaustion
is a cruder but more stable signal.

---

## 5. Teams streaming: confirmed vs inferred

Single source for everything marked confirmed:
<https://learn.microsoft.com/en-us/microsoftteams/platform/bots/streaming-ux>
("Stream agent messages", page dated 27 August 2026).

### Confirmed, quoted from the doc

| Fact | Evidence |
|---|---|
| Updates are cumulative | "While streaming, the agent messages must contain the previous streamed content. For example: `A brown.` / `A brown fox.` / `A brown fox jumps over the fence.` Non-example [...] `A brown.` / `Hello.`" |
| Cumulative, restated | "For every response streaming update, the message content should be the latest version of the final message [...] Append these tokens to the previous message version and then send it to the user." |
| **Rate limit: 1 request per second** | "The throttling limit is 1 request per second. You must ensure that the agent sends the request within this limit. The agent may send requests at a slower rate, as needed." |
| Serialise requests | "ensure to call the next streaming API only after receiving a successful response from the initial API call" |
| Activity types | "Supported values are either `typing` or `message`. `typing`: Use when streaming the message. `message`: Use for the final streamed message." |
| `streamType` values | "either `informative`, `streaming`, or `final`. The default value is `streaming`. `final` is used only in the final message." |
| `streamSequence` rules | "For REST APIs, `streamSequence` must start at 1 and increment by 1 for each subsequent streaming request. Don't set `streamSequence` for the final message." |
| Fields live in a `streaminfo` entity | The doc's REST sample: `"entities":[{ "type": "streaminfo", "streamId": "a-0000l", "streamType": "informative", "streamSequence": 2 }]` |
| First activity must have text | "A start streaming activity must include `text`. If `text` is missing, the request fails with a 400 BadRequest response and the error message `Start streaming activities should include text`." |
| `streamId` comes from the first response | "The response includes the `streamId`, which is important for executing subsequent calls." |
| Informative vs content, and the handover | "Informative updates appear in the streamed message bubble [...] The text remains visible until the next informative update or streamed content replaces it." / "After the first call to `Stream.Emit`, informative updates will no longer be shown and `Stream.Update` will have no effect." |
| Informative size cap | "Informative messages must not be more than 1 kb or 1000 characters." |
| No typing animation during a stream | "The Teams typing animation isn't available while a stream is open." |

### Inferred, not confirmed - treat as a live-test checklist

1. **`channelData` mirroring.** The task brief and various SDK samples put
   `streamType` / `streamId` / `streamSequence` on `channelData`; the Learn
   REST doc puts them in the `streaminfo` **entity**. I could not find a
   current doc page that specifies `channelData` for this, so
   `build_activity` emits **both** and the extra keys should be inert. If a
   live test shows Teams rejecting the duplicate, drop the `channelData`
   block - `entities` is the documented one.
2. **The exact key carrying `streamId` in the response.**
   `StreamState.adopt_stream_id` reads `streamId` first, then falls back to
   `id`, because the Connector's `ResourceResponse` returns `id` and the SDK
   samples reuse it. Documented only as "the response includes the streamId".
3. **Coalescing intermediate updates is acceptable to Teams.** It follows from
   updates being cumulative and from "the agent may send requests at a slower
   rate, as needed", but skipping a `streamSequence` value is not something
   the doc explicitly blesses. Note that this implementation does **not** skip
   sequence numbers - it only skips *sends*, and each send takes the next
   sequential number - so the sequence stays contiguous. Still worth watching.
4. **Whether a 1000-character truncation of an informative update is the right
   behaviour** rather than splitting. The doc gives the cap, not the remedy.

---

## 6. BLOCKED items, with exact commands

### BLOCKED: live streaming against the deployed Agent Runtime

**Reason:** requires a deployed reasoning engine ID in `<GCP_PROJECT_ID>` /
`us-central1` **and** a user Workforce Principal bearer token (ADR 002 -
never a service account). I have neither. I found no engine ID committed
anywhere in the repo (`grep` for `reasoningEngines/`, `reasoning_engine`,
`engine_id` across all `.md`/`.py`/`.tf`/`.toml`/`.txt`: no matches).

**Evidence it is genuinely unauthenticated rather than unreachable** - the
endpoint answers, it just refuses us:

```
$ curl -s -o /dev/null -w "%{http_code}\n" --max-time 20 \
    https://us-central1-aiplatform.googleapis.com/v1/projects/<GCP_PROJECT_ID>/locations/us-central1/reasoningEngines
401
```

**Exact command to run once you have a token and an engine ID:**

```bash
export PROJECT=<GCP_PROJECT_ID>
export LOCATION=us-central1
export ENGINE_ID=<the deployed reasoning engine numeric id>
# ADR 002: this MUST be the user's Workforce Principal token, NOT a service
# account. Do not substitute `gcloud auth print-access-token` from a service
# account impersonation here; that is the exact anti-pattern ADR 004 records
# as rejected.
export USER_TOKEN=<user Workforce Principal bearer token>

curl -N -sS \
  -H "Authorization: Bearer ${USER_TOKEN}" \
  -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: ${PROJECT}" \
  "https://${LOCATION}-aiplatform.googleapis.com/v1/projects/${PROJECT}/locations/${LOCATION}/reasoningEngines/${ENGINE_ID}:streamQuery?alt=sse" \
  -d '{
        "class_method": "streaming_agent_run_with_events",
        "input": {
          "request_json": "{\"user_id\":\"<entra-object-id>\",\"session_id\":\"<session>\",\"message\":{\"role\":\"user\",\"parts\":[{\"text\":\"how many orders last week\"}]}}"
        }
      }' \
  | tee /tmp/live_adk_stream.jsonl
```

Then replay the captured stream through the parser - **this is the check that
closes the biggest gap in this component:**

```bash
cd middle_tier
../agent/.venv/bin/python - <<'PY'
import json, logging, asyncio
from app.streaming.events import AdkEventParser
logging.basicConfig(level=logging.DEBUG)   # unknown events log here
p = AdkEventParser()
for line in open("/tmp/live_adk_stream.jsonl"):
    line = line.strip().removeprefix("data: ")
    if not line:
        continue
    for item in p.feed(json.loads(line)):
        print(type(item).__name__, item)
print("UNPARSED EVENTS:", p.unknown_event_count)   # must be 0
PY
```

`UNPARSED EVENTS: 0` and a sensible reassembled answer is the pass condition.

### BLOCKED: live Teams streaming against the Bot Connector

**Reason:** requires a real `serviceUrl` from an inbound Teams activity, a
live conversation id, and a Bot Framework app credential to mint a Connector
token. None available here.

**Exact command once you have them:**

```bash
export SERVICE_URL="https://smba.trafficmanager.net/emea/"   # from the inbound activity
export CONVERSATION_ID="<conversation id from the inbound activity>"
export BOT_TOKEN=$(curl -sS -X POST \
  "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token" \
  -d "grant_type=client_credentials&client_id=${BOT_APP_ID}&client_secret=${BOT_APP_SECRET}&scope=https%3A%2F%2Fapi.botframework.com%2F.default" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

# 1. start the stream (MUST carry text, or 400 BadRequest)
curl -sS -X POST "${SERVICE_URL}v3/conversations/${CONVERSATION_ID}/activities" \
  -H "Authorization: Bearer ${BOT_TOKEN}" -H "Content-Type: application/json" \
  -d '{"type":"typing","text":"Querying BigQuery...",
       "entities":[{"type":"streaminfo","streamType":"informative","streamSequence":1}]}'
# -> capture the returned id; that is the streamId

# 2. a cumulative content update
curl -sS -X POST "${SERVICE_URL}v3/conversations/${CONVERSATION_ID}/activities" \
  -H "Authorization: Bearer ${BOT_TOKEN}" -H "Content-Type: application/json" \
  -d '{"type":"typing","text":"There were 1,204 orders",
       "entities":[{"type":"streaminfo","streamId":"<STREAM_ID>","streamType":"streaming","streamSequence":2}]}'

# 3. the terminating message - note NO streamSequence
curl -sS -X POST "${SERVICE_URL}v3/conversations/${CONVERSATION_ID}/activities" \
  -H "Authorization: Bearer ${BOT_TOKEN}" -H "Content-Type: application/json" \
  -d '{"type":"message","text":"There were 1,204 orders last week.",
       "entities":[{"type":"streaminfo","streamId":"<STREAM_ID>","streamType":"final"}]}'
```

What to watch for on that run, in priority order:

1. Does step 3 render as a normal persisted message, or does the bubble keep
   spinning? (Wrong `streamType` on the final activity is the usual cause.)
2. Does Teams accept the duplicated `channelData` block, or complain? This
   settles inferred item 1 in section 5.
3. Does the returned id from step 1 actually work as the `streamId`? This
   settles inferred item 2.
4. Drive it from `ConnectorTeamsSink` at the real 1.0 s default and confirm no
   updates are dropped.

### BLOCKED: end-to-end (Teams user message -> BigQuery -> streamed answer)

Needs both of the above plus the inbound auth path, the identity broker and
the session manager wired together. Out of scope for this component; nothing
here has been run in that configuration.

---

## 7. Decisions I made that someone may want to reverse

1. **`render()` vs `ports.StreamingRenderer`.** `app/ports.py` declares a
   push-style protocol (`begin` / `push` / `finish`). The brief specified, and
   other components consume, the pull-style
   `render(events, sink) -> str`. I implemented the specified one and did
   **not** write the adapter, rather than quietly changing `ports.py` or
   quietly implementing a different interface. Someone needs to reconcile
   these two - it is a small adapter, but it is a real inconsistency and I did
   not want to hide it.

2. **A denial REPLACES the buffered model text** (`replace_text_on_denial`,
   default `True`). Appending the template after the model's explanation would
   leave the model's explanation on screen, which is what ADR 004 forbids.
   Honest limitation: if the model streamed prose *before* the denying tool
   result, that prose was briefly visible in the live bubble; only the final
   message replaces it. In the normal ordering (tool call, then answer) this
   does not arise. Fixing it properly means not streaming until the turn is
   known clean, which costs the streaming UX entirely.

3. **Authorization sniffing is regex over error prose.** The BigQuery MCP
   server's error payload is not a contract, so `classify_authorization_failure`
   pattern-matches for `403`, `401`, `PERMISSION_DENIED`, "access denied",
   "does not have permission", "forbidden", `UNAUTHENTICATED`, "unauthorized".
   Deliberately conservative: anything it does not recognise is treated as a
   **non**-authorization failure, because rendering a security message for a
   network blip is its own kind of wrong. The cost is that an unusual denial
   phrasing would slip through as a generic failure. If the MCP server ever
   returns a structured status code, use it and delete the regexes.

4. **`extract_resource` falls back to the tool name.**
   `errors.downstream_denial` *raises* on an empty resource (correctly - ADR
   004 forbids an unattributed denial). Without a fallback, a denial whose
   error text has no parseable resource would turn a handled denial into an
   unhandled crash. A slightly vague denial beats a stack trace.

5. **`RecordingTeamsSink` lives in production code**, not the test file. The
   turn handler and anyone debugging a stream want the same fake, and three
   divergent copies of a test double is how a fake stops matching the thing it
   fakes.

6. **Sequence numbers count informative and content updates together**, per
   "incremental integer for each request". Skipped *sends* do not skip
   *numbers* - each actual send takes the next value, so the sequence stays
   contiguous.

7. **I installed pytest into `agent/.venv`** (section 3d) to get the two
   google-adk shape tests actually running instead of skipping. That is a side
   effect on a venv this component does not own. Revert with
   `VIRTUAL_ENV=agent/.venv uv pip uninstall pytest pytest-asyncio pluggy iniconfig pygments`
   if unwelcome.

---

## 8. The one-line summary

The renderer is written, documented and covered by 34 real passing tests
against a synthetic event stream and against genuinely-serialised
`google-adk 2.8.0` events. **It has never been run against a live Agent
Runtime or a live Bot Connector.** That is the known risk, the commands to
close it are in section 6, and the first thing to check when it does break is
`AdkEventParser.unknown_event_count` with DEBUG logging on
`app.streaming.events`.
