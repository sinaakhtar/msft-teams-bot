# NOTES — Session Manager build log

Written 2026-09-07. Everything below is either something I ran, with its real
output pasted, or something I explicitly could not run, marked **BLOCKED** with
the exact command to run it.

---

## 1. "Exists" vs "executed successfully"

This split is the honest bottom line. Nothing in the right-hand column is
inferred from the left.

| Thing | Exists (code is written) | Executed successfully (I ran it, here, today) |
| --- | --- | --- |
| `client.py` — REST client, create/get/list + read-only `list_events` | yes | **partly.** Its LRO parsing, URL construction, ownership check, error mapping and no-token refusal all ran under test with a stubbed transport. Its aiohttp transport did **not** run against the live API — BLOCKED-1. |
| LRO handling on create (done-inline and poll-until-done) | yes | yes, under test (both paths, plus timeout and error-code mapping). Against the live API: BLOCKED-1. |
| `manager.py` — mapping, `/new` reset, 60-min idle expiry, per-key locking, scope + ADR 003 checks | yes | yes, fully, under test. |
| `store.py` — `SessionStore` protocol + `InMemorySessionStore` | yes | yes, under test (it is the store every manager test runs against). |
| Firestore / Redis store | **no** — documented as recommended options only | n/a |
| `tests/test_session_manager.py` | yes | yes — **44 passed** (was 43; see §4.3), output pasted below, plus two independent rounds of mutation checks. Round 2 found a **real hole** in round 1's coverage and it has been closed. |
| `tests/mutation_check_sessions.py` | yes | yes — executed from the real tree, **4 mutations, 0 survivors**. Self-contained: it makes its own throwaway copy and deletes it. |
| `README.md` | yes | n/a |
| Wiring into `app/routing.py` | **no** — deliberately out of scope for this task, and it needs the interface reconciliation in §6 first | n/a |
| Live `sessions.create` from this workspace | code path exists | **no** — BLOCKED-1. What I did verify live: the endpoint answers `401 UNAUTHENTICATED`, not `404`. |

---

## 2. What exists

```
middle_tier/app/sessions/__init__.py    package docstring: the three-sessions hazard, re-exports
middle_tier/app/sessions/client.py      thin REST client for the sessions subresource
middle_tier/app/sessions/manager.py     AgentRuntimeSessionManager: resolve() / reset()
middle_tier/app/sessions/store.py       SessionStore protocol + InMemorySessionStore
middle_tier/app/sessions/README.md      lifecycle, naming hazard, ADR 005 rationale, multi-instance limit
middle_tier/app/sessions/NOTES.md       this file
middle_tier/tests/test_session_manager.py  43 tests, no network
```

Interface, exactly as contracted — both return a bare Agent Runtime session id,
both raise typed errors, neither ever returns `None`:

```python
async def resolve(*, user_key: str, conversation_id: str, access_token: str) -> str
async def reset(*,   user_key: str, conversation_id: str, access_token: str) -> str
```

Deliberate absences, each with a comment in the source saying why:

* **No event-append anywhere.** `sessions.appendEvent` is a real API method and
  is not wrapped. ADR 005: the runtime writes history, the middle tier reads
  it. Reading is available (`client.list_events`).
* **No delete anywhere.** A `/new` reset abandons a session; it must stay
  retrievable by id. Since nothing else needs to delete, the client simply does
  not offer the verb.

A test asserts both absences as attributes on `SessionsRestClient`,
`AgentRuntimeSessionManager` and the test fake, so adding either method breaks
the build rather than quietly becoming callable.

---

## 3. API version: `v1`, and how I checked

**Two independent confirmations.**

1. *Reported from the earlier live run (not mine):* `sessions.create` on the
   `v1` path in `<GCP_PROJECT_ID>` returned **200** with
   `userId = entra:<ENTRA_TENANT_ID>:<ANALYST_OBJECT_ID>`,
   returning an already-complete LRO wrapping the session.
2. *Verified by me, here, today:* fetched the live public discovery document
   and read the method table.

```bash
curl -sS "https://aiplatform.googleapis.com/\$discovery/rest?version=v1" -o /tmp/disc_v1.json
```

Real output of the inspection script:

```
discovery version: v1 20260831 https://aiplatform.googleapis.com/
subresources: ['sandboxEnvironmentSnapshots', 'runtimeRevisions', 'memories', 'sessions', 'sandboxEnvironments', 'sandboxEnvironmentTemplates', 'operations']
session methods: ['compact', 'patch', 'get', 'appendEvent', 'list', 'delete', 'create']
session subresources: ['operations', 'events']
--- create POST v1/{+parent}/sessions
   response: {'$ref': 'GoogleLongrunningOperation'} request: {'$ref': 'GoogleCloudAiplatformV1Session'}
   params: ['parent', 'sessionId']
--- get GET v1/{+name}
   response: {'$ref': 'GoogleCloudAiplatformV1Session'} request: None
--- list GET v1/{+parent}/sessions
   response: {'$ref': 'GoogleCloudAiplatformV1ListSessionsResponse'} request: None
   params: ['parent', 'pageSize', 'orderBy', 'filter', 'pageToken']
events methods: ['list']  ->  v1/{+parent}/events
Session fields: ['labels', 'sessionState', 'updateTime', 'createTime', 'ttl', 'displayName', 'expireTime', 'userId', 'name']
```

And the `v1` vs `v1beta1` comparison, both at revision `20260831`:

```
v1      rev 20260831 methods ['appendEvent', 'compact', 'create', 'delete', 'get', 'list', 'patch']
v1beta1 rev 20260831 methods ['appendEvent', 'compact', 'create', 'delete', 'get', 'list', 'patch']
```

**Decision: `v1`.** The method sets are identical, so nothing in the session
lifecycle needs the beta surface, and `v1` carries the stronger compatibility
guarantee. `SESSIONS_API_VERSION` is a single constant if that ever changes.

Three other facts that came out of the same document and that shaped the code:

* `list` supports filters on `display_name`, `user_id` and `labels.<key>`.
  Exact text: ``Supported fields: * `display_name` * `user_id` * `labels` ``.
  That is what makes the "reconstruct the mapping by listing sessions" store
  option implementable, so the client sets a label
  `teams_conversation=<sha256(conversation_id)[:32]>` on create — conversation
  ids contain `:` and `@` and mixed case, which labels do not allow, hence the
  hash.
* `expireTime` is service-side and its **minimum is 24 hours**
  (`"The minimum value is 24 hours from the time of creation."`). Our 60-minute
  idle window is therefore purely a middle-tier policy; the service knows
  nothing about it. Written into the README so nobody later "simplifies" the
  idle logic by delegating to the service TTL.
* `create` accepts an optional `sessionId`. We do not supply one — a
  server-generated id cannot collide on a retry.

---

## 4. Tests I RAN, with real output

Command:

```bash
cd middle_tier && ./.venv/bin/python -m pytest tests/test_session_manager.py -v
```

Real output (verbatim, trimmed only in the per-test progress column):

```
============================= test session starts ==============================
platform linux -- Python 3.13.15, pytest-9.1.1, pluggy-1.6.0 -- <REPO_ROOT>/middle_tier/.venv/bin/python
cachedir: .pytest_cache
rootdir: <REPO_ROOT>/middle_tier
configfile: pyproject.toml
plugins: asyncio-1.4.0
asyncio: mode=Mode.AUTO
collecting ... collected 43 items

tests/test_session_manager.py::test_first_turn_creates_a_session_owned_by_the_entra_user_key PASSED
tests/test_session_manager.py::test_activity_within_60_minutes_reuses_the_same_session PASSED
tests/test_session_manager.py::test_idle_window_counts_from_last_activity_not_from_creation PASSED
tests/test_session_manager.py::test_separate_conversations_get_separate_sessions PASSED
tests/test_session_manager.py::test_idle_past_60_minutes_transparently_creates_a_new_session PASSED
tests/test_session_manager.py::test_idle_expiry_is_exactly_60_minutes_not_59_or_61 PASSED
tests/test_session_manager.py::test_reset_does_not_delete_the_abandoned_session PASSED
tests/test_session_manager.py::test_turn_after_reset_uses_the_new_session PASSED
tests/test_session_manager.py::test_failed_reset_keeps_the_existing_mapping PASSED
tests/test_session_manager.py::test_concurrent_resolve_creates_exactly_one_session PASSED
tests/test_session_manager.py::test_concurrent_resolves_for_different_conversations_do_not_serialise PASSED
tests/test_session_manager.py::test_concurrent_reset_and_resolve_do_not_interleave PASSED
tests/test_session_manager.py::test_group_conversation_is_rejected PASSED
tests/test_session_manager.py::test_reset_in_a_group_conversation_is_rejected_too PASSED
tests/test_session_manager.py::test_thread_shaped_ids_are_refused_without_a_conversation_type[19:5f8a4f2c17e94ff0b3d3f0b6cf5a1a2b@thread.v2] PASSED
tests/test_session_manager.py::test_thread_shaped_ids_are_refused_without_a_conversation_type[19:meeting_NzJhZDkw@thread.v2] PASSED
tests/test_session_manager.py::test_thread_shaped_ids_are_refused_without_a_conversation_type[19:abcdef@thread.skype] PASSED
tests/test_session_manager.py::test_thread_shaped_ids_are_refused_without_a_conversation_type[19:abcdef@thread.tacv2] PASSED
tests/test_session_manager.py::test_explicit_non_personal_conversation_type_wins_over_the_id_shape[groupChat] PASSED
tests/test_session_manager.py::test_explicit_non_personal_conversation_type_wins_over_the_id_shape[channel] PASSED
tests/test_session_manager.py::test_explicit_non_personal_conversation_type_wins_over_the_id_shape[GROUPCHAT] PASSED
tests/test_session_manager.py::test_explicit_non_personal_conversation_type_wins_over_the_id_shape[] PASSED
tests/test_session_manager.py::test_personal_conversation_type_is_accepted PASSED
tests/test_session_manager.py::test_missing_conversation_id_is_refused PASSED
tests/test_session_manager.py::test_missing_entra_object_id_fails_closed PASSED
tests/test_session_manager.py::test_teams_mri_is_never_accepted_as_a_user_key PASSED
tests/test_session_manager.py::test_malformed_user_keys_are_refused[] PASSED
tests/test_session_manager.py::test_malformed_user_keys_are_refused[entra::] PASSED
tests/test_session_manager.py::test_malformed_user_keys_are_refused[entra:<ANALYST_OBJECT_ID>] PASSED
tests/test_session_manager.py::test_malformed_user_keys_are_refused[<ENTRA_TENANT_ID>:<ANALYST_OBJECT_ID>] PASSED
tests/test_session_manager.py::test_malformed_user_keys_are_refused[entra:not-a-guid:<ANALYST_OBJECT_ID>] PASSED
tests/test_session_manager.py::test_malformed_user_keys_are_refused[entra:<ENTRA_TENANT_ID>:not-a-guid] PASSED
tests/test_session_manager.py::test_malformed_user_keys_are_refused[entra:<ENTRA_TENANT_ID>:<ANALYST_OBJECT_ID>:extra] PASSED
tests/test_session_manager.py::test_missing_access_token_fails_closed_no_service_account_fallback PASSED
tests/test_session_manager.py::test_create_unwraps_an_already_complete_lro PASSED
tests/test_session_manager.py::test_create_polls_an_incomplete_lro_until_done PASSED
tests/test_session_manager.py::test_create_raises_when_the_lro_carries_a_permission_error PASSED
tests/test_session_manager.py::test_create_times_out_rather_than_polling_forever PASSED
tests/test_session_manager.py::test_create_refuses_a_session_owned_by_a_different_user PASSED
tests/test_session_manager.py::test_a_session_name_that_is_actually_an_operation_name_is_rejected PASSED
tests/test_session_manager.py::test_list_sessions_filters_by_user_and_conversation_label PASSED
tests/test_session_manager.py::test_the_client_refuses_to_call_without_a_user_token PASSED
tests/test_session_manager.py::test_conversation_label_is_label_safe_and_stable PASSED

============================== 43 passed in 0.28s ==============================
```

Whole middle-tier suite, to confirm nothing else broke
(`./.venv/bin/python -m pytest`), run at 16:12 CEST:

```
collected 223 items

tests/test_app_smoke.py ...............................                  [ 13%]
tests/test_cache.py ..................                                   [ 21%]
tests/test_identity.py .....................                             [ 31%]
tests/test_inbound_auth.py ..........................................    [ 50%]
tests/test_no_service_account_fallback.py ........................       [ 60%]
tests/test_obo_audience.py ..........                                    [ 65%]
tests/test_renderer.py ................................ss                [ 80%]
tests/test_session_manager.py .......................................... [ 99%]
.                                                                        [100%]

======================== 221 passed, 2 skipped in 1.31s ========================
```

Note on that number: an earlier run of the same command 20 minutes before
(16:07 CEST) collected **155** items and reported `155 passed`. The suite grew
between the two runs because other work is landing in this shared tree
concurrently — `test_no_service_account_fallback.py`, `test_obo_audience.py`
and `test_renderer.py` are not mine and did not exist at the first run. The two
skips are in `test_renderer.py`, also not mine. My own file was 43 passed in
both runs and in every run in between. I am recording both numbers rather than
just the newer one, because a total that silently changes between a build and
its write-up is exactly the kind of thing that later reads as a fabricated
figure.

### 4.1 Mutation checks — proof the tests are not vacuous

43 green on the first run is exactly the situation where a test file is
worthless and looks fine. So I copied the tree to `/tmp`, broke one behaviour at
a time **in the copy** (the real tree was never modified), and re-ran. Real
output, trimmed to the summary lines:

**Mutation 1 — remove the per-key lock** (`async with await self._lock_for(key)`
→ `if True:`):

```
E       AssertionError: expected exactly one sessions.create, got 8; the per-key lock is not holding
E       assert 8 == 1
FAILED tests/test_session_manager.py::test_concurrent_resolve_creates_exactly_one_session
================== 1 failed, 2 passed, 40 deselected in 0.43s ==================
```

**Mutation 2 — disable idle expiry** (`if idle < self._idle_timeout:` → `if True:`):

```
FAILED tests/test_session_manager.py::test_idle_past_60_minutes_transparently_creates_a_new_session
FAILED tests/test_session_manager.py::test_idle_expiry_is_exactly_60_minutes_not_59_or_61
========================= 2 failed, 41 passed in 0.53s =========================
```

**Mutation 3 — make `/new` reuse the existing session** (drop `and not force_new`):

```
FAILED tests/test_session_manager.py::test_reset_does_not_delete_the_abandoned_session
FAILED tests/test_session_manager.py::test_turn_after_reset_uses_the_new_session
FAILED tests/test_session_manager.py::test_failed_reset_keeps_the_existing_mapping
FAILED tests/test_session_manager.py::test_concurrent_reset_and_resolve_do_not_interleave
========================= 4 failed, 39 passed in 0.55s =========================
```

**Mutation 4 — weaken the ADR 003 user-key check** (`if not _USER_KEY_RE.match(...)`
→ `if False:`):

```
FAILED tests/test_session_manager.py::test_malformed_user_keys_are_refused[<ENTRA_TENANT_ID>:<ANALYST_OBJECT_ID>]
FAILED tests/test_session_manager.py::test_malformed_user_keys_are_refused[entra:not-a-guid:<ANALYST_OBJECT_ID>]
FAILED tests/test_session_manager.py::test_malformed_user_keys_are_refused[entra:<ENTRA_TENANT_ID>:not-a-guid]
FAILED tests/test_session_manager.py::test_malformed_user_keys_are_refused[entra:<ENTRA_TENANT_ID>:<ANALYST_OBJECT_ID>:extra]
========================= 8 failed, 35 passed in 0.61s =========================
```

All four mutations were caught. The `/tmp` copies were deleted afterwards.

### 4.1.1 Correction: those four mutations missed the highest-risk behaviour

The four mutations above are real and they were all killed. But re-reading what
they actually cover: the per-key lock, the idle comparison, the `/new` force
flag, and the ADR 003 key regex. **None of them touches the LRO unwrapping** —
which is the one thing the brief singles out as "the obvious mistake" with this
API. Round 1 proved the tests were not vacuous about concurrency and identity.
It proved nothing either way about where the session id comes from.

Round 2 (§4.3) tested exactly that, and the suite failed to catch it.

### 4.2 What the tests cover, in the terms the task asked for

| Required case | Test |
| --- | --- |
| reset creates a new session and does **not** delete the old | `test_reset_does_not_delete_the_abandoned_session` — asserts a new id, `create` called twice, **no delete-shaped call in the recorded call log**, the old session still retrievable by id, and that no layer even *exposes* a delete/append attribute |
| idle past 60 minutes creates a new session | `test_idle_past_60_minutes_transparently_creates_a_new_session`, plus the exact-boundary case |
| activity within 60 minutes reuses it | `test_activity_within_60_minutes_reuses_the_same_session`, plus `..._counts_from_last_activity_not_from_creation` (50 min + 50 min still reuses) |
| concurrent resolve creates exactly ONE session (call count asserted) | `test_concurrent_resolve_creates_exactly_one_session` — 8 concurrent `resolve`s, asserts `create_calls == 1` |
| a group conversation is rejected | `test_group_conversation_is_rejected` (+ reset, + 4 thread-id shapes, + explicit `conversationType`) — and asserts **zero** sessions were created, so a refused turn leaves no orphan |
| a missing Entra oid fails closed | `test_missing_entra_object_id_fails_closed`, `test_teams_mri_is_never_accepted_as_a_user_key` (also asserts the MRI does not leak into the error text), + 7 malformed-key cases |

---

### 4.3 Round 2 mutation check — one mutation SURVIVED, and what that exposed

Run `2026-09-07`, via `tests/mutation_check_sessions.py`. Four mutations, this
time aimed at the LRO path. **MS1 survived the entire 43-test suite.**

MS1 replaces the correct `return Session.from_api(response)` with code that
derives the session id from the LRO envelope's *own* name by stripping
`/operations/...` off it. That is precisely the mistake the brief warns about.
Real output from the first run:

```
MS1: LRO -- take the session id from the OPERATION name instead of response.name
  file: app/sessions/client.py
  result: 43 passed in 0.27s
  VERDICT: SURVIVED -- the tests do NOT catch this. Bad.
```

**Root cause of the blind spot.** Every LRO fixture in the suite embeds the
*same* session id in both places — the operation is
`.../sessions/777/operations/42` and the response is `.../sessions/777`. Both
the right implementation and the wrong one therefore return `777`. The existing
assertions (`session_id == "5000000000000000001"`, `"operations" not in name`)
pass under both. The test was agreeing with the code, not constraining it.

**Fix.** Added `test_session_id_comes_from_response_name_when_the_two_disagree`,
which forces the two apart: the operation is filed under a decoy id
`1111111111111111111` while `response.name` carries the real id
`2222222222222222222`. Only an implementation that reads `response.name` passes.

After the fix, all four mutations are killed:

```
MS1: LRO -- take the session id from the OPERATION name instead of response.name
  result: 1 failed, 43 passed        VERDICT: KILLED by 1 test(s)
    FAILED ...::test_session_id_comes_from_response_name_when_the_two_disagree
MS2: LRO -- drop the guard that refuses an operation name as a session
  result: 1 failed, 43 passed        VERDICT: KILLED by 1 test(s)
    FAILED ...::test_a_session_name_that_is_actually_an_operation_name_is_rejected
MS3: ADR 003 -- stop validating the user key
  result: 8 failed, 36 passed        VERDICT: KILLED by 8 test(s)
MS4: idle expiry -- 60-minute boundary off by one (>= becomes >)
  result: 1 failed, 43 passed        VERDICT: KILLED by 1 test(s)
    FAILED ...::test_idle_expiry_is_exactly_60_minutes_not_59_or_61

AFTER RESTORE: 44 passed in 0.24s
mutations: 4, survivors: 0
```

**Caveat on what this does and does not prove.** It proves the suite now
constrains *where the id is read from*. It does not prove what the live API
actually returns; that is still BLOCKED-1. If the real operation name never
embeds a divergent id, MS1's bug would have been harmless in production — but
the code's own fallback branch already assumes `.../sessions/{sid}/operations/`
holds, and nothing verified that assumption against the live service either.

---

## 5. BLOCKED items

### BLOCKED-1 — live `sessions.create` as a Workforce Principal

**Reason:** I have no Workforce Principal access token, and I am not permitted
to mint one here (and a service-account token would violate ADR 002, so
substituting one would not be a valid test of anything). The token has to come
from the workforce federation flow for a real Entra user.

What I *did* run live, to at least confirm the path resolves and that the only
missing piece is the credential:

```bash
curl -sS --max-time 20 -o /tmp/noauth.json -w "http=%{http_code}\n" \
  "https://us-central1-aiplatform.googleapis.com/v1/projects/<GCP_PROJECT_ID>/locations/us-central1/reasoningEngines/<OTHER_ENGINE_ID_1>/sessions"
```

Real output:

```
http=401
{
  "error": {
    "code": 401,
    "message": "Request is missing required authentication credential. Expected OAuth 2 access token, login cookie or other valid authentication credential. ...",
    "status": "UNAUTHENTICATED",
```

Same URL with a deliberately bogus bearer returns `401 ... "Request had invalid
authentication credentials"`. **401, not 404** — the resource path is right and
only the credential is absent. That is the whole of what this proves; it is not
evidence that a create would succeed.

**Exact commands to run when a workforce token is available** (`$WF_TOKEN` is a
Workforce Principal access token; the pool needs `roles/aiplatform.user` on
`<GCP_PROJECT_ID>`):

```bash
ENGINE=<OTHER_ENGINE_ID_1>          # pre-existing test engine. DO NOT modify or delete it.
BASE="https://us-central1-aiplatform.googleapis.com/v1/projects/<GCP_PROJECT_ID>/locations/us-central1/reasoningEngines/$ENGINE"
USER_ID='entra:<ENTRA_TENANT_ID>:<ANALYST_OBJECT_ID>'

# 1. create — expect 200 and a google.longrunning.Operation
curl -sS -X POST "$BASE/sessions" \
  -H "Authorization: Bearer $WF_TOKEN" \
  -H "x-goog-user-project: <GCP_PROJECT_ID>" \
  -H "Content-Type: application/json" \
  -d "{\"userId\":\"$USER_ID\",\"displayName\":\"teams:a:1qbxLpTb9F0dvfF7Wt2mVJ0hqPz7Kk\",\"labels\":{\"teams_conversation\":\"$(printf %s 'a:1qbxLpTb9F0dvfF7Wt2mVJ0hqPz7Kk' | sha256sum | cut -c1-32)\"}}" \
  -w '\nhttp=%{http_code}\n'
# Read the session id from .response.name (NOT from .name, which is the operation).
# If .done is false, poll:  curl -sS -H "Authorization: Bearer $WF_TOKEN" \
#   "https://us-central1-aiplatform.googleapis.com/v1/<operation .name>"

# 2. get it back
SESSION_ID=<from .response.name>
curl -sS "$BASE/sessions/$SESSION_ID" \
  -H "Authorization: Bearer $WF_TOKEN" -H "x-goog-user-project: <GCP_PROJECT_ID>" \
  -w '\nhttp=%{http_code}\n'

# 3. list this user's sessions (the reconstruct-by-listing store option)
curl -sS -G "$BASE/sessions" \
  --data-urlencode "filter=user_id=\"$USER_ID\"" \
  -H "Authorization: Bearer $WF_TOKEN" -H "x-goog-user-project: <GCP_PROJECT_ID>" \
  -w '\nhttp=%{http_code}\n'

# 4. reset semantics: create a SECOND session, then confirm the FIRST is still
#    there. This is the one that matters — it is the live version of
#    test_reset_does_not_delete_the_abandoned_session.
curl -sS "$BASE/sessions/$SESSION_ID" \
  -H "Authorization: Bearer $WF_TOKEN" -H "x-goog-user-project: <GCP_PROJECT_ID>" \
  -w '\nhttp=%{http_code}\n'      # expect 200, still

# 5. read history (allowed). Expect 200 and an empty sessionEvents on a fresh session.
curl -sS "$BASE/sessions/$SESSION_ID/events" \
  -H "Authorization: Bearer $WF_TOKEN" -H "x-goog-user-project: <GCP_PROJECT_ID>" \
  -w '\nhttp=%{http_code}\n'
```

The Python equivalent, exercising the actual client rather than curl:

```bash
cd middle_tier && ./.venv/bin/python - <<'PY'
import asyncio, os
from app.sessions import SessionsRestClient, AgentRuntimeSessionManager
async def main():
    c = SessionsRestClient(project="<GCP_PROJECT_ID>", location="us-central1",
                           reasoning_engine_id="<OTHER_ENGINE_ID_1>")
    m = AgentRuntimeSessionManager(client=c)
    tok = os.environ["WF_TOKEN"]
    uk = "entra:<ENTRA_TENANT_ID>:<ANALYST_OBJECT_ID>"
    conv = "a:1qbxLpTb9F0dvfF7Wt2mVJ0hqPz7Kk"
    a = await m.resolve(user_key=uk, conversation_id=conv, access_token=tok)
    b = await m.resolve(user_key=uk, conversation_id=conv, access_token=tok)
    n = await m.reset(user_key=uk,   conversation_id=conv, access_token=tok)
    print("resolve twice ->", a, b, "reused:", a == b)
    print("after /new    ->", n, "new session:", n != a)
    print("old still there ->", (await c.get_session(name=a, access_token=tok)).name)
    await c.aclose()
asyncio.run(main())
PY
```

Expected, if the code is right: the two `resolve`s return the same id, `/new`
returns a different one, and the old id still fetches 200. **I have not run
this. Do not treat the expectation as a result.**

### BLOCKED-2 — the 60-minute idle window against the live service

The unit tests prove the *policy* with a fake clock. They cannot prove the
*service* still returns the session after an hour, or that nothing else on the
platform reaps it. Verifying that needs a session created, left alone for
61 minutes, then re-fetched — an hour of wall time plus a workforce token.

```bash
# with $WF_TOKEN and $BASE as above, and $SESSION_ID from a create an hour earlier:
curl -sS "$BASE/sessions/$SESSION_ID" \
  -H "Authorization: Bearer $WF_TOKEN" -H "x-goog-user-project: <GCP_PROJECT_ID>" \
  -w '\nhttp=%{http_code}\n'   # expect 200: our idle policy abandons, the service does not delete
```

Expected 200 on the documented grounds that `expireTime` has a 24-hour minimum
(quoted in §3). Not run.

### BLOCKED-3 — multi-instance behaviour on Cloud Run

The in-memory store's failure mode is only observable with ≥ 2 instances
serving one conversation. Not reproducible in a single process, and not worth
reproducing: it is a known, documented limitation, not a hypothesis. It goes
away when a Firestore store lands.

---

## 6. Things I want on the record

**6.1 `app/ports.py` already declares a different `SessionManager`, and the two
do not match.** The existing Protocol is:

```python
async def get_or_create(self, user_key: str) -> SessionRef
async def reset(self, user_key: str) -> SessionRef
async def list_events(self, session: SessionRef, ...) -> Sequence[AgentEvent]
async def delete(self, session: SessionRef) -> None
```

The contract I was given is `resolve/reset(*, user_key, conversation_id,
access_token) -> str`. The differences are not cosmetic:

* **`conversation_id` is absent from `ports`.** Keying on `user_key` alone
  merges every conversation a person has with the bot into one session.
* **`access_token` is absent from `ports`.** That implies the manager obtains
  credentials itself, which is the shape that invites a service-account
  fallback (ADR 002).
* **`ports` has `delete`.** The reset semantics say abandoned sessions are
  retained. I have not implemented `delete`, deliberately.
* `ports` returns a `SessionRef`; the contract returns a bare `str`.

I implemented the contract I was given and did **not** edit `ports.py` — it is a
shared seam and `routing.py` currently calls `deps.sessions.get_or_create(...)`
against it. **Someone must reconcile the two before wiring this in**, and my
recommendation is to amend `ports.SessionManager` to match the new contract
(add `conversation_id` and `access_token`, drop `delete`), because every
difference above is a place where the `ports` version is the more dangerous
one. Until that happens, `routing.py` will not type-check against this class,
which is the correct outcome: a silent adapter would hide the `conversation_id`
merge.

**6.2 Group/channel detection is only as good as its input.** With
`conversation_type` supplied from the activity it is exact. Without it, the id
shape is a heuristic. `routing.py` has the activity in hand and should pass
`activity.conversation.conversationType`; the internal `_resolve` already
accepts it. I did not add it to the public two-method contract because the
contract was specified as exact and other components are coding against it —
flagging it here instead of quietly widening the signature.

**6.3 The per-key lock is per process.** It makes concurrent turns safe on one
instance. It does nothing across instances, which is the same limitation as the
in-memory store and is fixed by the same change (a Firestore transaction or a
Redis `SETNX`). Stated so that "we have a lock" is not mistaken for "concurrent
creation is impossible".

**6.4 The clock default is `time.monotonic`.** Correct while the store is
in-memory (it cannot jump backwards on an NTP correction), and wrong the moment
timestamps are persisted, since monotonic values are meaningless across
processes. A durable store must inject a UTC wall clock. Noted in both the
store and manager docstrings.

**6.5 I added two things the task did not ask for, both small and both
defensible.** (a) `client.list_events`, read-only, because ADR 005 explicitly
permits reading history and the manager's consumers will need it; there is no
write counterpart. (b) A `teams_conversation` label (a SHA-256 prefix of the
conversation id) set on create, purely so the "reconstruct by listing sessions"
store option stays implementable. Neither changes the lifecycle. If either is
unwanted, both are one-line removals.

**6.6 What I did not verify at all.** That the `<GCP_PROJECT_ID>` workforce pool
actually still has `roles/aiplatform.user`; that engine `<OTHER_ENGINE_ID_1>`
is still present; that a real Teams activity's `conversation.id` for a 1:1 chat
matches the `a:1…` shape my heuristic assumes (I took that from Bot Framework
documentation, not from a captured activity). The first two were reported as
verified earlier by someone else and I took them as given; the third would be
settled by one logged inbound activity.
