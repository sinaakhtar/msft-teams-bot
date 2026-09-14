# Integration pass: wiring the middle tier together

Date: 2026-09-07. Python 3.13, Linux x86_64, `middle_tier/.venv`.

This file records an **integration** pass, not a build. The premise going in was
that every component existed and nothing was connected. That premise was
correct, with one exception recorded below.

Read `NOTES.md` for the original build. Read this for what happened when the
pieces were put together, what turned out to be wrong about the seams, and --
most importantly -- the difference between "assembled and passes against fakes"
and "verified against live services". Those are very far apart and section 8
says exactly how far.

---

## 1. Executive summary

| | |
| --- | --- |
| Tests before | **270 passed, 2 skipped** (real output in §4) |
| Tests after | **302 passed, 2 skipped** (real output in §4) |
| Existing tests broken and left broken | 0 |
| Existing tests deleted, skipped or weakened | 0 |
| New source modules | 3 (`app/runtime/`, `app/composition.py`, `app/streaming/connector.py`) |
| New test modules | 2 (`tests/test_end_to_end_wiring.py`, `tests/test_bot_connector.py`) |
| Collaborators that genuinely did not exist | 1 of 4 (`AgentRuntimeClient`) |
| Contract defects found in `ports.py` | 2 (§3.2, §3.4) -- both would have forced an ADR violation |
| Live network verification of anything built here | **none. Zero.** (§8) |
| BLOCKED items | 3 (§7) |

**The task brief said the suite was at 240 passed, 2 skipped. It was not.** The
measured baseline before any change was **270 passed, 2 skipped**. The brief's
number is stale. Everything below uses the measured figure.

The headline: the service now assembles. A misconfigured deployment refuses to
start instead of coming up green and shrugging at every question -- and that
was verified by actually starting it, not by reading the code (§5). But a human
still cannot talk to this bot in Teams, and the blocker is not any of the four
collaborators. It is the Teams SSO `invoke` handler, which is still a 501 stub,
so no real turn will ever carry the token the whole chain depends on. §9.1.

---

## 2. The survey, done before writing anything

The brief asked for a survey first, and specifically flagged doubt about
whether `AgentRuntimeClient` existed. It does not. Three of the four do.

| Collaborator | Implementation found | Verdict |
| --- | --- | --- |
| `IdentityBroker` | `app/identity/broker.py` -- `ChainedIdentityBroker`, plus `build_identity_broker()`, over `obo.py` / `sts.py` / `cache.py` | **EXISTS**, real and tested. Needed exception translation (§3.1) |
| `SessionManager` | `app/sessions/manager.py` -- `AgentRuntimeSessionManager`, over `SessionsRestClient` and a session store | **EXISTS**, real and tested. Port signature was wrong (§3.2) |
| `AgentRuntimeClient` | nothing, anywhere | **MISSING.** Built (§3.3) |
| `StreamingRenderer` | `app/streaming/renderer.py` -- `TeamsStreamingRenderer`, plus the event parser and the Teams sink | **EXISTS**, real and heavily tested. Wrong shape and wrong lifetime for the port (§3.4) |

How the `AgentRuntimeClient` absence was confirmed rather than assumed: a
tree-wide search for `AgentRuntimeClient`, `stream_query` and
`streaming_agent_run_with_events` returns only the Protocol in `ports.py`, the
import and call site in `routing.py`, prose in the ADRs and NOTES, the agent's
own deploy script, and the vendored `vertexai` SDK. No implementation, no
partial, no `app/runtime/` package.

A fourth thing was also missing and was not on the brief's list:

| Also missing | Why it matters |
| --- | --- |
| Any outbound path to Teams | `ConnectorTeamsSink` takes an injected `send(activity)` callable. Excellent for testing, and nothing in the tree ever supplied the real one. Nothing acquired an outbound credential either: before this pass, the only occurrence of `api.botframework.com` in the source was the *inbound* issuer constant. Without it the renderer cannot be constructed with a working sink, so `Dependencies.renderer` could not honestly be non-`None`. Built as `app/streaming/connector.py` (§3.5) |

---

## 3. What was built, versus what already existed

Nothing that already existed was reimplemented. Where a built component and its
port disagreed, the port was corrected and a thin adapter reconciled the
shapes. All three adapters live in `app/composition.py` and hold no behaviour
of their own.

### 3.1 `PortsIdentityBroker` -- adapter, ~40 lines

`ChainedIdentityBroker` already had the right method names, including a
`get_google_access_token(user_key, sso)` compatibility shim. What it did not
have was the right *exceptions*.

It raises `IdentityAcquisitionError`, which descends from `Exception`. The
router catches `app.ports.PortError` subclasses. **They do not intersect.**
Every identity failure in normal operation -- an expired assertion, a missing
consent, an STS 403 -- would have escaped every handler in `routing.py`, hit
the catch-all in `main.py`, and returned HTTP 500. Azure Bot Service then
retries the activity, so a user with no consent generates a retry loop instead
of a sign-in card. That is a straight ADR 004 violation reached by accident.

The adapter translates: `StsPermissionDenied` becomes `AuthorizationDenied`
naming the workforce pool (a 403 on the pool is not fixed by signing in again,
so a sign-in card would loop the user); anything flagged `retryable` becomes
`TransientBackendError`; everything else becomes `IdentityUnavailable`.

`test_an_identity_failure_becomes_a_signin_prompt_not_a_500` fails if this
adapter is removed.

### 3.2 `PortsSessionManager` -- adapter, ~40 lines, plus a port correction

`AgentRuntimeSessionManager.resolve` takes `user_key`, `conversation_id` and
`access_token`, and returns a session id. The port declared
`get_or_create(user_key) -> SessionRef`.

That is not a cosmetic mismatch. **The port as written could not be satisfied
without violating ADR 002.** It has no parameter for the user's access token,
so any conforming implementation would have had to call the Agent Runtime
sessions subresource under ambient service credentials. ADR 002 exists
precisely to prevent that, and layer 2 of `spikes/FINDINGS.md` already proved
live that a workforce principal can create a session keyed on its own `oid`, so
there was never a reason for the service to do it.

The port was corrected to the implementation's shape rather than the reverse,
because the implementation was right. `routing.py` now passes both extra
arguments. The adapter's only real work is rebuilding a `SessionRef` from the
returned id, since the runtime client needs both the id and the `user_id`.

### 3.3 `ReasoningEngineRuntimeClient` -- **genuinely new**, `app/runtime/client.py`

The one thing that had to be written. `POST` to
`{loc}-aiplatform.googleapis.com/v1/{engine}:streamQuery?alt=sse` with
`class_method: streaming_agent_run_with_events` (ADR 005, not `stream_query`),
the inner request double-encoded as a `request_json` string exactly as
`agent/NOTES.md` §4.4 records, decoding SSE `data:` frames and bare JSON lines
into raw dicts which it yields untouched -- `app/streaming/events.py` owns all
interpretation.

The credential handling is the part that matters, and it follows layer 3 of
`spikes/FINDINGS.md` and the pattern already proven in
`agent/bq_agent/credentials.py`:

- The user's Google access token is the `Authorization` header (Invocation
  Identity: the audit log names the human, not the service).
- The **same** token goes in the `authorizations` map under `bigquery_user`,
  which the runtime surfaces as session state `temp:bigquery_user`. ADK strips
  `temp:`-prefixed keys before persistence.
- It is **not** written to `session_state` or any ordinary key. Layer 3 found
  that approach works perfectly and is still wrong, because session state is
  persisted by the managed Sessions service and readable by session id, which
  would write a live bearer token into durable conversation history.
- `build_request_json` raises if the token is empty. There is no code path that
  invokes without one.

HTTP status mapping is deliberately not collapsed: 401 → `IdentityUnavailable`
(drop the cached token, re-prompt once), 403 and 404 → `AuthorizationDenied`
naming the engine, 429 and 5xx → `TransientBackendError`. ADR 004 only works if
"you are not allowed" and "the backend is unwell" stay distinguishable.

The `bigquery_user` string is a cross-package contract with the agent, and the
two packages cannot import each other.
`test_the_authorization_id_matches_the_agent_side_constant` reads
`agent/bq_agent/credentials.py` and compares, so a one-sided change fails the
build instead of producing an agent that fails closed with no hint why.

### 3.4 `TeamsRendererFactory` + `TurnRenderer` -- adapter, ~90 lines, plus a port addition

Two mismatches, one of them a concurrency bug waiting to happen.

**Shape.** `TeamsStreamingRenderer` is pull-style: `render(events, sink)`. The
port is push-style: `begin` / `push` / `finish`. The renderer's own docstring
anticipates this adapter and explicitly declines to write it. `TurnRenderer` is
it: pushed events go on an `asyncio.Queue`, and the existing renderer consumes
that queue as the async iterator it already expects. It adds no rendering
logic, and a stream failure is re-raised *inside* the renderer's own loop so
that the renderer's existing ADR 004 handling runs rather than being duplicated
in a second place.

**Lifetime.** This is the more serious one. A `StreamingRenderer` is inherently
single-turn: one bubble, one sequence counter, one accumulating buffer. But
`Dependencies` is process-scoped and shared by every concurrent turn. A single
renderer there would interleave two users' answers into one Teams bubble under
any real load, intermittently, in a way no single-user test would ever show. So
`Dependencies.renderer` is now a **factory** (`ports.StreamingRendererFactory`,
added) and the router asks for a renderer per turn.

### 3.5 `BotConnectorTransport` -- **new**, `app/streaming/connector.py`

Acquires a client-credentials token from Entra for
`https://api.botframework.com/.default` (own tenant for SingleTenant, the
shared `botframework.com` authority for MultiTenant -- getting that backwards
yields an `AADSTS700016` that reads like a wrong secret), caches it with a
5-minute skew, and POSTs activities to
`{serviceUrl}/v3/conversations/{id}/activities`.

This is the one credential in the system that is not the user's, and that is
correct: it authenticates *the bot to Microsoft* so a message lands in the right
conversation. It never reaches Google and is not substitutable for the user's
token anywhere.

It refuses any `serviceUrl` that is not HTTPS on a recognised Bot Framework
host. `app/auth/inbound.py` already binds `serviceUrl` to the verified JWT, so
this is a second independent check on the one path that would hand a bot
credential to an attacker-controlled host. Cheap, and the failure mode of
getting it wrong is silent.

### 3.6 `build_dependencies` -- the composition root, `app/composition.py`

One function. Takes `Settings` and a shared `aiohttp.ClientSession`, returns a
fully populated `Dependencies`, or raises `CompositionError` naming the missing
configuration. There is no partial success and no silent `None`:
`assert_fully_wired` runs before it returns, and again in the smoke test.

Wired into `main.py` as an `on_startup` hook, because assembly needs a running
event loop for the shared session. An exception there aborts startup, which is
the entire point. `create_app(deps=...)` still injects, so every existing test
is unaffected.

### 3.7 Configuration added

`Settings` gained `federation_app_id`, `workforce_pool_id`,
`workforce_provider_id` and `runtime_authorization_id`. Defaults come from
`terraform/variables.tf` and `app/identity/README.md`
(`<FEDERATION_APP_CLIENT_ID>`, `teams-bot-demo`, `entra`, `bigquery_user`). They are
identifiers, not secrets, and a wrong value fails loudly at the STS exchange.
No new secrets and no new secret-loading path.

### 3.8 Stale markers corrected

`app/ports.py` lines 13-16 claimed all four Protocols were "NOT built". Three
of those four claims were false when written. The table now names each
implementing module, and carries a note saying the table used to lie and that
anyone about to implement one of these should check `app/composition.py` first.
That note is there because the failure mode of a stale "NOT built" marker is
someone writing a second implementation, which is worse than none.

### 3.9 `app/errors.py` -- deliberately untouched

Left exactly as found, per the brief. It is dead code: `app/errors/` (the
package) shadows `app/errors.py` (the module), so `from . import errors`
resolves to the package and nothing imports the module. Verified untouched --
`md5sum bbe9c4c824b9add62aa8facdf14eb94c`, mtime unchanged from before this
pass. Its fate is still the user's call. If it is kept, it should carry a
comment saying it is shadowed, because right now it reads like live code.

---

## 4. Test counts: real output, both runs

### Before (baseline, before any change)

Command, from `middle_tier/`:

```
.venv/bin/python -m pytest tests/ -q
```

Tail of the real output:

```
...................................................... [ 79%]
..........ss............................................                 [100%]
270 passed, 2 skipped in 8.81s
```

**270 passed, 2 skipped.** Not the 240 the brief stated.

### After

Same command, same venv:

```
..........................................ss............................ [ 94%]
................                                                         [100%]
302 passed, 2 skipped in 8.39s
```

**302 passed, 2 skipped.** +32, being 15 in `test_end_to_end_wiring.py` and 17
in `test_bot_connector.py`. The 2 skips are the same 2 as before.

The suite was run after every individual change, not only at the end. It
returned 270 after the config change, after the `ports.py` change, after the
`routing.py` surgery and after the `main.py` change; the count moved only when
tests were added.

### One test failed during development, and was fixed

Not hidden. On first run of the new suite:

```
E       TypeError: IdentityAcquisitionError.__init__() got an unexpected keyword argument 'stage'
tests/test_end_to_end_wiring.py:575: TypeError
=========================== short test summary item ============================
FAILED tests/test_end_to_end_wiring.py::test_an_identity_failure_becomes_a_signin_prompt_not_a_500
1 failed, 14 passed in 0.42s
```

My error, not the code's: `stage` is a class attribute on those exceptions, not
a constructor argument. Fixed in the test; it now passes.

### Mutation check still holds

`tests/mutation_check.py` breaks the validator on purpose to prove the suite
notices. Real output after the integration pass:

```
=== RESTORED; re-running baseline ===
exit=0  285 passed, 2 skipped in 8.42s

=== SUMMARY ===
CAUGHT      M1: accept alg:none (remove the unsecured-JWS guard)
CAUGHT      M2: stop verifying the audience
CAUGHT      M3: stop verifying the signature
CAUGHT      M4: stop verifying expiry
CAUGHT      M5: downgrade serviceUrl mismatch to a warning (the SDK's behaviour)
CAUGHT      M6: ADR 003 violation - fall back to the Teams MRI
            7 failed, 278 passed, 2 skipped in 8.60s
```

6 of 6 still caught. M6 now fails 7 tests rather than 6, because the new
end-to-end test also catches an MRI fallback. (That run predates
`test_bot_connector.py`, hence 285 rather than 302.)

---

## 5. The fail-loud claim, actually executed

"It fails loudly at startup" is the kind of claim that is easy to write and
easy to be wrong about, so it was run rather than asserted. A script sets dev
mode and the required secrets, then calls `create_app` and `AppRunner.setup()`
twice, once without `REASONING_ENGINE_ID` and once with it. Real output:

```
no REASONING_ENGINE_ID: REFUSED TO START -> CompositionError: cannot assemble the middle tier; missing configuration: REASONING_ENGINE_ID. Refusing to start with unwired collaborators, because a service that starts and then declines every turn is harder to diagnose than one that does not start.
with REASONING_ENGINE_ID: STARTED
```

Before this pass, both cases would have started, both would have served
`/healthz` and `/readyz` as green, and both would have answered every message
with the transient-failure template.

---

## 6. What the smoke test proves, and what it deliberately does not

`middle_tier/tests/test_end_to_end_wiring.py`, 15 tests, picked up by a plain
`pytest tests/`. No network, no marks, no opt-in.

**Proves:**

1. **No `None` collaborators.** `build_dependencies` on a complete `Settings`
   yields four non-`None` fields of the expected concrete types, and the engine
   name is built from config. Separately, `assert_fully_wired(Dependencies())`
   -- exactly what shipped -- raises and names all four fields.
2. **Fails loudly.** Missing `REASONING_ENGINE_ID` or `FEDERATION_APP_ID`
   raises `CompositionError` naming the setting.
3. **A real activity reaches an attempted invocation.** A synthetic Teams
   `message` with a valid `from.aadObjectId` goes through the *real* identity
   adapter, the *real* `AgentRuntimeSessionManager`, the *real* `TurnRenderer`
   and the *real* `TeamsStreamingRenderer`, and arrives at the runtime boundary
   with the right text and the user's token. The rendered answer comes back out
   of a recording sink.
4. **ADR 003.** The `user_id` sent to `create_session`, and the `SessionRef`
   handed to the runtime, are both `entra:{tid}:{oid}`. The Teams MRI appears
   nowhere -- asserted by serialising every recorded call and the session ref
   and checking that neither the MRI nor the substring `29:` occurs.
5. **Fail closed.** An activity with no `aadObjectId` is refused with no
   session created, no token exchanged and no invocation attempted, and the MRI
   does not appear in the refusal.
6. **ADR 004.** A downstream 403 produces a body **byte-identical** to
   `errors.downstream_denial(...)` for the same inputs. Byte equality is what
   rules out a model-authored explanation: there is no room for generated prose
   in a string that must match exactly. The denial exception is produced by
   calling the *real* client's classifier, so if that mapping changes the test
   changes with it rather than testing a fiction.
7. **The credential contract.** The user token appears under
   `authorizations[bigquery_user]` and nowhere else in the request; there is no
   `session_state` or `state` key; and the authorization id matches the agent's
   `AUTHORIZATION_ID`, checked by reading the agent's source.
8. **ADR 002 and ADR 005 invariants.** The client refuses to build a request
   with an empty token; the class method is `streaming_agent_run_with_events`.

**Deliberately does not prove:**

- **Anything about the network.** Nothing in the file has spoken to Entra,
  Google STS, the Agent Runtime, the Sessions API or the Bot Connector. It
  proves the parts are connected and carry the right values in the right
  direction. It does not prove any remote endpoint accepts what we send.
- **Inbound JWT validation.** It constructs an `AuthenticatedCaller` directly.
  `tests/test_inbound_auth.py` owns that, across 40-odd cases including
  `alg: none` and HS256 confusion, and re-minting tokens here would duplicate
  that suite while testing something else.
- **Real ADK event shapes.** It replays the event fixtures from
  `tests/test_renderer.py`, which were derived from the installed
  `google-adk` 2.8.0 source. No live event stream has ever been parsed.
- **Concurrency.** Every test drives one turn. The per-turn renderer factory
  exists *because* sharing one renderer across concurrent turns is a bug, and
  that bug is now structurally impossible, but no test drives two turns at once.
- **The OBO and STS exchanges.** The identity broker's inner chain is faked at
  `ChainedIdentityBroker`'s own method boundary. The real OBO and STS logic is
  covered by `test_identity.py` / `test_obo_audience.py`, also offline.

---

## 7. BLOCKED

Stated as blocked rather than worked around.

**B1. No live call to the Agent Runtime.** `ReasoningEngineRuntimeClient` has
never made a request. The request shape follows `agent/NOTES.md` §4.4, which is
itself recorded as *not yet executed*. So the wire format is derived from
documentation and SDK source, not from a 200.

To unblock, against a deployed engine, with a real user Workforce Principal
token (**not** a service account -- ADR 002):

```bash
curl -N -sS -X POST \
  "https://us-central1-aiplatform.googleapis.com/v1/projects/<GCP_PROJECT_ID>/locations/us-central1/reasoningEngines/<REASONING_ENGINE_ID>:streamQuery?alt=sse" \
  -H "Authorization: Bearer $USER_TOKEN" \
  -H "X-Goog-User-Project: <GCP_PROJECT_ID>" \
  -H "Content-Type: application/json" \
  -d '{"class_method":"streaming_agent_run_with_events","input":{"request_json":"{\"message\":{\"role\":\"user\",\"parts\":[{\"text\":\"SELECT SESSION_USER()\"}]},\"user_id\":\"entra:<TID>:<OID>\",\"session_id\":\"<SID>\",\"authorizations\":{\"bigquery_user\":{\"access_token\":\"<USER_TOKEN>\"}}}"}}'
```

Then the check that matters -- that the token did not land in durable history:

```bash
curl -sS "https://us-central1-aiplatform.googleapis.com/v1/projects/<GCP_PROJECT_ID>/locations/us-central1/reasoningEngines/<REASONING_ENGINE_ID>/sessions/<SID>/events" \
  -H "Authorization: Bearer $USER_TOKEN" | grep -c 'ya29\.'
# expected: 0
```

Note the brief's constraint: do not use the pre-existing
`reasoningEngines/<OTHER_ENGINE_ID_1>` as a write target. It is not ours.

**B2. No live call to the Bot Connector.** `BotConnectorTransport` has never
acquired a token or posted an activity. Its tests assert what *would* go on the
wire -- URL, addressing, headers, credential -- against a recording session.
Whether Microsoft accepts it is untested and cannot be tested from here.
Unblocking needs a registered bot and a real conversation.

**B3. No live OBO or STS exchange from the assembled service.** Covered
offline; never executed through `build_dependencies`.

None of these were attempted. No `terraform apply` was run, the workforce pool,
the BigQuery dataset and the pre-existing reasoning engine were not touched, and
no `git` command was run.

---

## 8. Assembled and passing against fakes ≠ verified against live services

The distinction the brief asked to be explicit about, stated plainly.

**What is now true:** the four collaborators are constructed from `Settings` by
one function; a misconfigured deployment refuses to start; a synthetic activity
traverses the real identity adapter, the real session manager, the real
renderer and the real event pipeline to the runtime boundary; the session key
is the Entra object id; a missing `aadObjectId` is refused; a 403 yields the
templated denial; the user's token travels only in the `authorizations` map;
302 tests pass and 6 of 6 security mutations are still caught.

**What is not true, and is not implied by any of the above:** that this bot
works. Every external boundary is unexercised. Specifically, all of the
following are assumptions with zero live evidence behind them from this pass:

- that the Agent Runtime accepts the request body built by `build_request_json`;
- that a workforce-federated token is accepted as the `Authorization` header on
  `:streamQuery` (layer 2 proved `sessions.create` and `reasoningEngines.get`,
  not this);
- that the streamed response is SSE `data:` frames in the shape the parser
  expects;
- that `authorizations` really does become `temp:bigquery_user` on the live
  runtime, and that ADK really does strip it before persisting;
- that Entra issues the bot a Connector token with these parameters;
- that Teams renders the streamed activities as intended.

A green suite here means the parts are connected correctly *to each other*. It
says nothing about whether they are connected correctly *to the outside world*.
The first live turn will find things. Expect the ADK event shape and the
streaming response framing to be where it breaks first, since both are decoded
from documentation rather than from a captured response.

---

## 9. Every remaining gap between this and a bot a human can talk to

Ordered by what blocks a working demo.

### 9.1 The Teams SSO `invoke` handler is still a 501 stub — **the actual blocker**

This is the one that stops everything, and it is not any of the four
collaborators.

`_handle_agent_turn` gets the user's Teams SSO assertion from
`_sso_token_from_activity`, which only reads `activity["value"]["token"]`. In
the real Teams flow that token arrives on an **`invoke`** activity
(`signin/tokenExchange`), not on a `message`, and must be held in per-user state
between the two. `_handle_invoke` returns **HTTP 501** and has done since the
original build.

Consequence: in real Teams, no `message` activity carries a token, so
`_acquire_user_token` refuses every turn and every user gets the sign-in card
forever. **The bot is not usable end to end until this is built**, no matter how
well the rest is wired. It needs: handling `signin/tokenExchange`, per-user
storage of the assertion keyed on `entra:{tid}:{oid}`, dedup for the multiple
concurrent exchange requests Teams sends across a user's clients, and HTTP 412
on failure so Teams retries the sign-in.

### 9.2 On a failure the user may get two messages

When a renderer is wired and the stream fails, `TurnRenderer.finish(error=...)`
lets the renderer terminate the Teams bubble with its ADR 004 template, *and*
the router additionally returns a denial body. Two denials for one failure.

Left as found rather than silently changed, because it is a behaviour decision
and the router's error returns predate this pass. The fix is one line per error
path in `_handle_agent_turn`: return `body=None` when `renderer is not None`,
since the renderer has already delivered the message. On the success path this
is already correct (`body=None`).

### 9.3 Group conversations degrade to a generic message

`AgentRuntimeSessionManager` raises `GroupConversationNotSupported` for a
channel or group chat. It is a `PortError` but not one the router names, so it
now lands in the new catch-all and produces the generic transient template.
Honest, but unhelpful: the user is told to try again later when the real answer
is "this bot only works in a 1:1 chat". It needs its own template.

### 9.4 `readyz` does not check the collaborators

`/readyz` still checks config and JWKS only. Since assembly now happens at
startup and aborts on failure, an instance that is serving is an instance that
assembled -- so this is much less dangerous than it was. But it does not verify
that the reasoning engine is reachable or that the bot can get a Connector
token.

### 9.5 No concurrency test

The renderer factory makes cross-turn interleaving structurally impossible, and
the identity cache is documented as single-flight, but nothing drives two
concurrent turns for two identities through the assembled application. Layer 3
of `spikes/FINDINGS.md` set the bar for this kind of check -- a barrier, real
overlap, verdicts from observed identity strings, INCONCLUSIVE rather than PASS
when overlap cannot be proven -- and the equivalent has not been done here.

### 9.6 The runtime client has no retry or backoff

`TransientBackendError` is raised and the turn ends. The port allows a retry
with backoff; none is implemented. Also relevant: `agent/NOTES.md` records that
roughly one turn in six failed on a malformed tool call during the layer 3
spike. If that persists, users will see intermittent failures the middle tier
cannot distinguish from a backend problem.

### 9.7 Session store is in-memory

`AgentRuntimeSessionManager` defaults to `InMemorySessionStore`, and the
composition root does not override it. On Cloud Run with more than one instance,
or across a revision, the (user, conversation) → session mapping is lost and the
user silently starts a new conversation. Fine for a single-instance demo;
not fine for anything else.

### 9.8 Untested paths in the new code

Honest inventory of what was written here and is *not* covered:
`_iter_events` (SSE decoding) has no test -- the stream decode path is exercised
only through the fake runtime, which yields already-decoded dicts;
`ReasoningEngineRuntimeClient.stream_query`'s HTTP handling is untested end to
end; `TurnRenderer`'s behaviour when `push` is called after `finish` is
undefined. `_raise_for_status` and `build_request_json` are covered.

### 9.9 Container never built

Unchanged from `NOTES.md` B1. No container runtime available here, so the
`Dockerfile` is still unbuilt, and it does not yet know about `app/runtime/`
(it copies `app/`, so this is probably fine, but it is unverified).

---

## 10. Files touched

**New:**

| Path | Lines | What |
| --- | --- | --- |
| `app/runtime/__init__.py` | 25 | package surface |
| `app/runtime/client.py` | 360 | `ReasoningEngineRuntimeClient` -- the missing collaborator |
| `app/composition.py` | 472 | composition root + the three adapters |
| `app/streaming/connector.py` | 311 | outbound Bot Connector transport |
| `tests/test_end_to_end_wiring.py` | 702 | 15 tests; the test that would have caught it |
| `tests/test_bot_connector.py` | 275 | 17 tests for the new transport |

**Modified:**

| Path | Change |
| --- | --- |
| `app/ports.py` | corrected the "NOT built" table (§3.8); corrected `SessionManager.get_or_create`/`reset` signatures (§3.2); added `StreamingRendererFactory` (§3.4) |
| `app/routing.py` | extracted `_acquire_user_token` so `/new` acquires a token too; session calls pass `conversation_id` and `access_token`; per-turn renderer from the factory; added `_conversation_id`; added `PortError` catch-alls so a seam failure is not a 500-and-retry |
| `app/main.py` | assembles dependencies in an `on_startup` hook and aborts startup on failure; owns and closes the shared `aiohttp` session |
| `app/config.py` | four new `Settings` fields with defaults from terraform (§3.7) |

**Deliberately not touched:** `app/errors.py` (§3.9), `app/auth/inbound.py`,
`app/caller_identity.py`, everything under `app/identity/`, `app/sessions/`,
`app/errors/`, and `app/streaming/{events,renderer,teams_sink}.py`.
