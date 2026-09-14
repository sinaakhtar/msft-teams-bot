# `app.sessions` — Agent Runtime session lifecycle

Maps a Teams one-to-one conversation to an **Agent Runtime Session**, owns
`/new` resets and the 60-minute idle window, and gets out of the way.

```
middle_tier/app/sessions/
  client.py    thin REST client for the sessions subresource (create/get/list + read-only events)
  manager.py   the mapping, /new reset, 60-minute idle expiry, per-key locking, scope enforcement
  store.py     the (user, conversation) -> session mapping. In-memory by default. Read the caveat.
```

## The naming hazard, first

Three unrelated things in this system are called a "session". Sentences like
"the session expired" are unactionable, and have already cost debugging time
elsewhere in this project. Always name which one:

| Name | What it is | Lifetime | Who owns it |
| --- | --- | --- | --- |
| **Agent Runtime Session** | conversational history held by the managed Sessions service on the reasoning engine | service-side expiry, ≥ 24 h; this package additionally abandons it after 60 min idle | **this package** |
| **Workforce pool `sessionDuration`** | how long a federated Google access token stays valid | 3600 s | the identity plane (ADR 002) |
| **Teams Conversation** | a Microsoft-side thread id | Microsoft's business | Teams |

The idle window (3600 s) and the workforce credential lifetime (3600 s) are the
same number **by coincidence**. They are unrelated mechanisms. A turn that
fails because the credential lapsed raises `IdentityUnavailable` from the
identity plane; do not "fix" it by touching the idle window. A fourth thing,
`aiohttp.ClientSession`, is just an HTTP connection pool.

## Lifecycle

```
turn arrives
  ├─ user_key not entra:{tid}:{oid}?      -> InvalidUserKey                (ADR 003, fail closed)
  ├─ group chat or channel?                -> GroupConversationNotSupported (out of scope)
  ├─ no user access token?                 -> IdentityUnavailable           (ADR 002, no SA fallback)
  └─ take the per-(user,conversation) lock
       ├─ mapping exists and idle < 60 min -> reuse it, bump last activity
       ├─ mapping exists and idle ≥ 60 min -> create a new session (old one RETAINED)
       └─ no mapping                       -> create a new session
```

`/new` (Conversation Reset) takes the same path with `force_new=True`: it
**creates a new session and repoints the mapping**.

**The abandoned session is never deleted.** A reset discards *context*; it does
not destroy *history*. The old session stays retrievable by id forever, which
is what makes "what did it tell me an hour ago?" answerable after a reset. This
is enforced structurally, not by discipline: `SessionsRestClient` has no
`delete` method and `AgentRuntimeSessionManager` has no way to reach one. A
test asserts the absence of the attribute, so adding one breaks the build.

The explicit reset and the idle expiry, whichever comes first, are the user's
**only** controls over context length — and therefore over per-turn token cost.
That is why the 60-minute window is enforced here rather than delegated to the
service's own session TTL, whose minimum is 24 hours and would let a chatty
conversation accumulate a day of context before anything trimmed it.

Idle time is measured from the **last activity**, not from creation: fifty
minutes of silence, a turn, then another fifty minutes of silence reuses the
same session, because no single gap reached an hour.

## ADR 005: why the REST subresource, not the agent's methods

The live agent in `<GCP_PROJECT_ID>` exports `create_session` / `get_session` /
`list_sessions` / `delete_session` as class methods, and they work. We do not
use them. Both paths exist; only one may be used, and this middle tier depends
on **platform** surfaces rather than on any given agent's **exported** surface.
The agent will be rewritten during development. When it is, or when it is
swapped for a different agent that exports a different set of methods, the bot
keeps working because the managed Sessions service is unchanged.

The same ADR draws a second line: **the middle tier reads history and never
writes events.** The runtime appends the user content, tool calls, tool results
and model output with the invocation ids the ADK expects. A middle tier that
also appends produces duplicated turns and events nothing issued — history that
silently stops replaying correctly, with the damage surfacing turns later as an
agent that has apparently lost the plot. `sessions.appendEvent` is a real API
method and is deliberately **not** wrapped in `client.py`. The absence is the
enforcement: you cannot call what is not there.

Reading is fine and supported: `SessionsRestClient.list_events`.

## API version

`v1`, on the regional endpoint:

```
POST https://us-central1-aiplatform.googleapis.com/v1/projects/{project}/locations/us-central1/reasoningEngines/{engine}/sessions
```

Confirmed two ways, both recorded in `NOTES.md`: a live `sessions.create` that
returned 200 on this path, and the public discovery document (revision
`20260831`) which lists `create` / `get` / `list` / `delete` / `patch` /
`appendEvent` / `compact` plus `sessions.events.list` for
`projects.locations.reasoningEngines.sessions`. `v1beta1` at the same revision
exposes an identical method set, so nothing in the session lifecycle needs the
beta surface. The version is one constant, `SESSIONS_API_VERSION`.

`sessions.create` returns a `google.longrunning.Operation`, not a bare Session.
In the live call it came back already `done: true` with the session inline
under `response`. The client does not assume that: if `done` is false it polls
`GET {operation.name}` on a bounded budget, and it takes the session id from
`operation.response.name` — never from the operation name, which ends in a
similar-looking opaque component and would produce a plausible id that 404s on
the next turn. A parse-time guard rejects an operation name given as a session
name.

## The multi-instance limitation — read this before deploying

`InMemorySessionStore` is a process-local dict. **Cloud Run scales to N
instances, and with more than one instance this mapping breaks.** Concretely:

* Turn 1 lands on instance A and creates session S. Turn 2 lands on instance B,
  which has never heard of S, so it creates T. The user's follow-up is answered
  with no memory of the previous question — intermittently, and looking like
  the model forgetting rather than like a bug.
* Any redeploy or cold start begins with an empty map, so every live
  conversation silently starts a new session. Nothing is lost (nothing here
  deletes), but context is dropped without the user asking.

Options, and the recommendation:

1. **Firestore (Native mode)** — one document per `(user_key, conversation_id)`
   holding the session id and last-activity timestamp. Serverless, no VPC
   connector, no capacity to size, and a TTL policy expires stale rows
   automatically. One read and one write per turn, which is noise next to a
   model call.
2. **Redis (Memorystore)** — fastest, and `SET key value NX` gives a
   distributed lock, i.e. the multi-instance version of the per-key async lock
   in `manager.py`. Costs an always-on instance plus a Serverless VPC Access
   connector: real money and real setup for a bot with bursty, small traffic.
3. **Reconstruct by listing sessions** — keep nothing; each turn call
   `sessions.list` filtered by `user_id="entra:{tid}:{oid}"` and
   `labels.teams_conversation="<sha256 prefix>"` (the client sets that label on
   create precisely to keep this option open) and take the most recently
   updated. No storage, self-heals across restarts. But it adds a list call to
   the front of every turn, it cannot express "this was reset" without a second
   signal, and two simultaneous first turns both list nothing and both create.

**Recommended: Firestore.** Option 3 is an appealing "no state" story until you
try to express a reset in it; option 2 buys latency we do not need at the price
of infrastructure we would not otherwise run. Firestore is the only one that is
both durable and free of standing cost. Keep option 3 as a repair path for a
mapping Firestore has lost, and take option 2 only if real concurrent traffic
proves a distributed lock is needed.

**Until a durable store lands, deploy with `--min-instances=1
--max-instances=1`, written into the deploy config rather than remembered.**

Note also that the default clock is `time.monotonic`, which is per-process and
cannot be persisted. A durable store implementation must switch to UTC wall
timestamps (`clock=lambda: datetime.now(UTC).timestamp()`); monotonic is the
right default only while the store is in-memory.

## Scope: one-to-one chats only

Group chats and channels are refused with `GroupConversationNotSupported`,
before any session is created. Not for effort reasons: a group conversation has
**one** conversation id and **many** users, so a single mapped session would
collect several people's turns under one `user_id`. That is a privacy failure —
A's question becomes part of the context B's answer is generated from — and an
ADR 003 violation, since the `user_id` would be whoever happened to speak
first. Per-user sessions inside a shared thread are a coherent design, but they
need a product decision about what "the conversation" means when the bot can
see other people's messages.

Callers **should** pass `conversation_type` from
`activity.conversation.conversationType`, which is authoritative. Absent that,
`assert_one_to_one` falls back to the id shape (`19:…` / `…@thread.…` is a
group or channel), which is a documented heuristic, not a guarantee.

## Using it

```python
from app.sessions import AgentRuntimeSessionManager, SessionsRestClient

client = SessionsRestClient(
    project=settings.gcp_project_id,
    location=settings.location,               # us-central1
    reasoning_engine_id=settings.reasoning_engine_id,
)
sessions = AgentRuntimeSessionManager(client=client)   # in-memory store: see above

session_id = await sessions.resolve(
    user_key=identity.user_key,               # entra:{tid}:{oid}
    conversation_id=activity["conversation"]["id"],
    access_token=user_access_token,           # the USER's workforce token (ADR 002)
)

# /new
session_id = await sessions.reset(user_key=..., conversation_id=..., access_token=...)
```

Both return a bare session id and never return `None`. Errors are typed and
map onto the ADR 004 taxonomy already used across the middle tier:
`InvalidUserKey` and `GroupConversationNotSupported` (both
`PortError` subclasses, both terminal), `IdentityUnavailable`,
`AuthorizationDenied`, `SessionNotFound`, `TransientBackendError` (retryable).

The engine id is required configuration and is never resolved by display name:
`reasoningEngines.list` in `us-central1` currently returns three engines all
displaying as `data_science_agent`, so a name lookup would silently attach
users to whichever sorted first.

## Tests

`middle_tier/tests/test_session_manager.py` — 43 tests, no network, fake clock,
fake client at the `SessionsClient` protocol boundary, plus the real client
driven through a stubbed transport for the LRO paths. Run:

```bash
cd middle_tier && ./.venv/bin/python -m pytest tests/test_session_manager.py -v
```

Real output and the mutation checks that prove the tests are not vacuous are in
`NOTES.md`.
