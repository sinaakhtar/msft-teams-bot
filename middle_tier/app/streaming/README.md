# `app/streaming` - ADK event stream in, Teams streamed message out

Three files. `events.py` parses, `teams_sink.py` speaks Teams, `renderer.py`
joins them. Read the first two sections before changing any of it.

```
Agent Runtime                  events.py            renderer.py        teams_sink.py        Teams
streaming_agent_run_    -->  TextChunk         -->  buffer + loop  --> informative()  -->  typing/informative
with_events (ADR 005)        ToolCallStarted                           content()      -->  typing/streaming
                             ToolCallFinished                          final()        -->  message/final
                             ToolError
                             TurnComplete
```

---

## 1. Teams streamed updates are CUMULATIVE, not deltas

**Every content update must contain all the text you have already streamed.**
Not the newest chunk. All of it, every time.

Microsoft, in
[Stream agent messages](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/streaming-ux):

> While streaming, the agent messages must contain the previous streamed
> content. For example: `A brown.` / `A brown fox.` / `A brown fox jumps over
> the fence.` Non-example: This is an example of a streaming response that
> will return an error: `A brown.` / `Hello.`

> For every response streaming update, the message content should be the
> latest version of the final message [...] Append these tokens to the
> previous message version and then send it to the user.

Send a delta and the user does not see text appended. They see the message
bubble **replaced** by a fragment, and Teams may reject the update.

The direct consequence is that **the renderer buffers the whole response in
middle-tier memory for the duration of the turn.** ADR 005 accepted that
explicitly:

> Buffering cumulative text for Teams means holding partial responses in
> middle tier memory for the duration of a turn.

It is not an oversight, and it is not optimisable away.

This is enforced in three places, deliberately redundantly:

* `ConnectorTeamsSink._assert_cumulative` raises `CumulativeContractViolation`
  in production if an update is not a superset of the last one sent.
* `RecordingTeamsSink` does the same in every test that uses it.
* `test_every_content_update_is_cumulative` asserts the prefix chain across
  **every** emitted update, not just the final text. A delta-sending renderer
  passes a "final text is correct" test and fails users, so that test is the
  one that actually guards the contract.

`TeamsStreamingRenderer._safe_content` swallows transport errors but
**re-raises `CumulativeContractViolation`**, because that exception means a
bug in this directory, not a bad day at the Bot Connector.

### Why coalescing is safe here and would be catastrophic elsewhere

A model emits tokens far faster than Teams' one-per-second throttle. Rather
than block the read loop, `ConnectorTeamsSink.content()` sends when the rate
window is open and otherwise records the newest text as pending. Skipping an
intermediate update is **lossless**, because the next update already contains
everything the skipped one would have said. Under a delta protocol the same
code would silently drop text. That asymmetry is why the cumulative contract
gets shouted about rather than mentioned.

---

## 2. Informative updates on tool start - the reason ADR 005 exists

ADR 005 chose `streaming_agent_run_with_events` over `stream_query`
specifically so tool activity is visible:

> Tool activity is visible, so Teams can show progress during the slow part
> of a turn, which is also the part worth demonstrating.

A BigQuery round trip is the slowest thing in a turn. Without an informative
update the bubble sits empty and the bot reads as hung, which is exactly the
failure `stream_query` would have given us for free. **If you strip the
informative updates to simplify the loop, you have reverted to `stream_query`
with extra steps and thrown away the ADR.**

The copy is a fixed lookup table in `renderer.DEFAULT_TOOL_LABELS`
(`execute_sql_readonly` -> `"Querying BigQuery..."`), with a readable fallback
for tools we have no copy for. It is a table and not a model call because the
Bot Middle Tier contains no prompt logic and makes no model calls. The
renderer formats; it does not reason about content.

Documented behaviour worth knowing:

* Informative updates are **not** cumulative. Each replaces the last.
* Once the first content chunk goes out, informative updates stop displaying.
  `ConnectorTeamsSink` therefore drops them after content starts rather than
  burning throttle budget the content updates need.
* The Teams typing animation is unavailable while a stream is open, so
  informative updates are the *only* progress signal the user gets.
* Cap: 1 KB / 1000 characters. The sink truncates.

---

## 3. ADR 004 - the model never explains a refusal

When a tool event carries a 401/403, the renderer emits the **fixed template
naming the refused resource** and **discards the model's prose**. ADR 004:

> A language model asked to explain an authorization error has no way to
> distinguish a missing role from a missing dataset from a network fault, and
> will produce a fluent, confident, and possibly fabricated reason.

Implementation notes:

* The template is `app.errors.downstream_denial`. One source of truth; this
  directory does not have its own copy of the wording.
* `events.extract_resource` pulls the resource name out of the error prose and
  falls back to the tool name if it cannot find one, because
  `downstream_denial` **raises** on an empty resource and an unhandled crash
  is a worse ADR 004 outcome than a slightly vague denial.
* `replace_text_on_denial=True` (default) means the template *replaces* the
  buffered model text in the final message. Appending would leave the model's
  explanation on screen, which is the thing the ADR forbids.
* Non-authorization tool failures (a 500, a timeout) get the *transient*
  template instead, and only when the model produced no answer around them. A
  network blip rendered as a security message is its own kind of wrong.
* There is never a service-account retry. Not here, not anywhere.

**Known honest limitation.** If the model streams prose *before* the denying
tool result arrives, that prose was briefly visible in the bubble; only the
final message replaces it. In the normal ordering the tool call precedes the
answer text, so this does not arise, but a multi-step tool turn can produce
it. Fixing it properly would mean not streaming text until the turn is known
to be clean, which costs the streaming UX entirely.

---

## 4. Event shapes are the fragile part. Start here when an upgrade breaks it

ADR 005 named this in advance:

> Event parsing is more code than forwarding text, and ADK event shapes are a
> moving target across versions. **This is the most likely place for an
> upgrade to break the bot.**

### What the wire actually carries (verified against google-adk 2.8.0)

`vertexai/agent_engines/templates/adk.py` yields, per event, the dump of
`_StreamingRunResponse`, and `vertexai/agent_engines/_utils.py` defines the
per-event serialisation as exactly:

```python
def dump_event_for_json(event: BaseModel) -> Dict[str, Any]:
    return json.loads(event.model_dump_json(exclude_none=True))
```

Two consequences fall out of that one line, and both are load-bearing:

1. **snake_case.** `model_dump_json` is called *without* `by_alias=True`, even
   though `Event.model_config` sets `alias_generator=to_camel`. So today the
   wire carries `invocation_id`, `function_call`, `error_code`,
   `turn_complete`. Adding one keyword argument upstream would flip the whole
   stream to camelCase, so **every lookup in `events.py` accepts both.**
2. **`exclude_none=True` means keys are ABSENT, not null.** Never assume a
   field exists. Most events have no `partial` key at all.

Envelope per streamed chunk:

```json
{"events": [ {"...one event..."} ], "session_id": "...", "artifacts": [...]}
```

A real event, captured by dumping a constructed `Event` the way the runtime
dumps it:

```json
{
  "content": {"parts": [{"text": "Hello "}], "role": "model"},
  "partial": true,
  "invocation_id": "inv-1",
  "author": "agent",
  "actions": {"state_delta": {}, "artifact_delta": {},
              "requested_auth_configs": {}, "requested_tool_confirmations": {}},
  "node_info": {"path": ""},
  "id": "ab5b0826-...",
  "timestamp": 1788789686.324771
}
```

Tool call: a part with `function_call: {id, name, args}`.
Tool result: a part with `function_response: {id, name, response}`.

### The partial/aggregate double-count trap

ADK emits a run of `partial: true` events carrying **deltas**, then a
non-partial event for the same response carrying the **whole text again**.
Appending everything doubles the answer. `AdkEventParser` is stateful
precisely to absorb this: it tracks the partial run and, when the aggregate
arrives, emits only the genuine remainder (usually nothing).
`test_partial_run_then_aggregate_is_not_double_counted` guards it.

### When an ADK upgrade breaks the bot, look here, in this order

1. **`AdkEventParser.unknown_event_count`, and the DEBUG log.** Every event
   the parser could not understand is logged at DEBUG with its keys and
   counted. Turn on DEBUG for `app.streaming.events` and re-run one turn. If
   the count is non-zero and the answer is empty, the shape moved.
2. **`events._get`** - the accessor that tries snake_case, camelCase, mapping
   keys and attributes in turn. A renamed field is a one-line fix here.
3. **`events._parse_event`** - the three things it looks for: an event-level
   `error_code`, `content.parts`, and `turn_complete`. If ADK moves function
   calls out of `content.parts`, this is the function that changes.
4. **`events._response_looks_failed`** - how a tool result is judged to have
   failed. The BigQuery MCP server's error payload shape is not contractual
   and has the most freedom to drift.
5. **`test_real_adk_event_dump_shape`** in `tests/test_renderer.py` - it
   builds genuine `google.adk` `Event` objects and asserts the snake_case /
   absent-not-null findings above. **If that test fails after an upgrade, the
   wire shape changed and the failure message tells you how.** It skips
   silently if `google-adk` is not installed in the venv running pytest, so
   check for a skip before concluding it passed.

The design principle throughout: **an unrecognised event is logged at DEBUG
and dropped, never fatal.** A bot that dies on an unknown event type is
strictly worse than one that ignores it. `AdkEventParser.feed` cannot raise;
`TeamsStreamingRenderer.render` still sends a terminating message even if the
stream dies mid-turn, because leaving a stream open leaves the user staring
at a half-written bubble forever.

---

## 5. The Teams wire format, in one place

Source for all of it:
<https://learn.microsoft.com/en-us/microsoftteams/platform/bots/streaming-ux>

| Stage | Activity `type` | `streamType` | `streamSequence` |
|---|---|---|---|
| Informative update | `typing` | `informative` | yes, 1-based |
| Content update | `typing` | `streaming` (default) | yes, +1 each |
| Final message | `message` | `final` | **must not be set** |

Fields travel in an entity:

```json
"entities": [{
  "type": "streaminfo",
  "streamId": "<from the response to the FIRST activity>",
  "streamType": "informative" | "streaming" | "final",
  "streamSequence": 1
}]
```

Rules that bite:

* The **first** activity must include `text`, or Teams answers
  `400 BadRequest: Start streaming activities should include text`.
* `streamId` comes back on the first response and must be echoed on every
  subsequent activity.
* **Throttle: 1 request per second.** Exceed it and updates are dropped.
  `TEAMS_MIN_UPDATE_INTERVAL_SECONDS = 1.0`.
* Send serially - "call the next streaming API only after receiving a
  successful response from the initial API call". `ConnectorTeamsSink` holds
  an `asyncio.Lock` for exactly this.

**`entities` vs `channelData`:** the Learn REST docs put these fields in the
`streaminfo` entity; SDK samples and internal notes describe them on
`channelData`. `build_activity` emits **both**, mirrored. The extra keys are
inert if unused. See NOTES.md for which of those is doc-confirmed and which is
belt-and-braces.

---

## 6. Interface

```python
class StreamingRenderer(Protocol):
    async def render(self, events: AsyncIterator[Any], sink: "TeamsSink") -> str: ...
```

`TeamsSink` is three verbs - `informative(text)`, `content(cumulative_text)`,
`final(text)` - so the whole renderer can be driven in a test with
`RecordingTeamsSink` and no network at all. `RecordingTeamsSink` lives in
production code rather than the test file on purpose: three divergent copies
of a test double is how a fake stops matching the thing it fakes.

Note that `app/ports.py` declares an older push-style `StreamingRenderer`
(`begin` / `push` / `finish`). The pull-style `render` above is what this
component was specified against and what other components consume. The two are
reconcilable with a thin adapter, which is deliberately not written yet. See
NOTES.md.

---

## 7. Status

Tested against a **synthetic** event stream and against real `google.adk`
`Event` objects serialised the way the runtime serialises them. **Not yet
validated against live output from a deployed Agent Runtime** - that needs a
deployed engine and a user Workforce Principal token. That gap is the known
risk and is written up, with the exact blocked command, in `NOTES.md`.
