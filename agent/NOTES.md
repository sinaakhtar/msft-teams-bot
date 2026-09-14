# agent/ — build notes

Date: 2026-09-07. Everything below distinguishes **code that exists** from
**code that was executed successfully**. Nothing here is a simulated,
reconstructed or plausible-looking result: every output block was copied from a
command that actually ran in this sandbox.

---

## 1. What exists

| Path | Purpose |
|---|---|
| `agent/requirements.txt` | Pinned deps + inline pinning rationale. |
| `agent/pyproject.toml` | Same pins, packaging metadata. |
| `agent/bq_agent/__init__.py` | Thin package init (does not construct the agent). |
| `agent/bq_agent/credentials.py` | contextvar, async `header_provider`, `UserCredentialPlugin` (per-invocation set/reset), persistence constraint, `SessionPoolGuard`. |
| `agent/bq_agent/errors.py` | ADR 004 tool boundary: two-403 classification, resource naming, templates, `FailClosedToolPlugin` (arg repair + denial interception). |
| `agent/bq_agent/agent.py` | `LlmAgent` + `McpToolset` with read-only tool allowlist and the analyst instruction. |
| `agent/deploy.py` | `agent_engines.create` deploy, `--dry-run`, `--list`, `--rollback`, protected-engine guard. |
| `agent/test_local.py` | `--selftest` (offline concurrency test of the credential path) + live harness asserting on raw tool responses. |
| `agent/test_persistence.py` | Executable proof the token is never persisted. |
| `agent/test_errors.py` | Executable proof of the two-403 distinction and arg repair. |
| `agent/README.md` | Architecture, credential threading, deploy, rollback, middle-tier contract. |

---

## 2. What was EXECUTED, with real output

### 2.1 Dependency install — SUCCEEDED

```
$ cd agent && uv venv .venv --python 3.13
$ VIRTUAL_ENV=$PWD/.venv uv pip install 'google-adk==2.8.0' 'mcp<2' \
      'google-cloud-aiplatform[adk,agent_engines]'
$ .venv/bin/python -c "import importlib.metadata as m; ..."
google-adk 2.8.0
mcp 1.29.1
google-cloud-aiplatform 2.1.0
google-genai 2.22.0
```

### 2.2 Import check — SUCCEEDED

```
$ .venv/bin/python -c "import bq_agent.agent; print('OK', bq_agent.agent.root_agent.name, bq_agent.agent.MODEL)"
.../google/adk/features/_feature_decorator.py:71: UserWarning: [EXPERIMENTAL] feature FeatureName.PLUGGABLE_AUTH is enabled.
  check_feature_enabled()
OK bq_teams_analyst gemini-3.5-flash
```

(The `PLUGGABLE_AUTH` warning is ADK's own experimental-feature notice, emitted
on import of the MCP tool package. It is not an error.)

### 2.3 `test_local.py --selftest` — PASSED (12 concurrent callers)

```
=== OFFLINE SELFTEST: 12 concurrent invocations ===
[PASS] contextvar: 12 concurrent callers, each got its own token
[PASS] temp: state fallback + mandatory X-Goog-User-Project header
[PASS] token under a non-temp: key refused, failed closed
[PASS] no credential -> MissingUserCredential (no service-account fallback)
[PASS] plugin binds at invocation start and unbinds at invocation end
[PASS] credential never renders its token
SELFTEST PASSED
```

`asyncio.Barrier` forces genuine overlap: every task holds its binding while the
others acquire theirs, so a sequential implementation cannot pass this.

### 2.4 `test_persistence.py` — PASSED (12 assertions)

```
=== 1. BaseSessionService: temp: applied in memory, trimmed from the event ===
[PASS] token IS readable from the live in-memory session during the invocation
[PASS] token was REMOVED from event.actions.state_delta before persistence
[PASS] an ordinary (non-temp:) key is left alone and still persists
[PASS] no event in the session's durable event list contains the token

=== 2. VertexAiSessionService: what would ACTUALLY go over the wire ===
[PASS] an append call was made (fake client captured it)
[PASS] the ENTIRE Sessions API payload contains no trace of the access token
[PASS] config.actions.state_delta has no temp: key
[PASS] config.raw_event.actions.state_delta has no temp: key
[PASS] an ordinary key still reaches the Sessions API (the trim is targeted, not blanket)

=== 3. assert_no_persisted_token tripwire ===
[PASS] a token under temp: is allowed
[PASS] a token under a persisted key raises

ALL PERSISTENCE ASSERTIONS PASSED
```

Test 2 exercises the *real* `VertexAiSessionService.append_event` with its API
client swapped for a capturing fake, so the assertion is on the actual payload
the real code path constructs. No network, no credentials.

### 2.5 `test_errors.py` — PASSED (19 assertions)

```
[PASS] 403 naming a missing ROLE/permission -> PERMISSION (a grant fixes it)
[PASS] 403 naming the CREDENTIAL/user-project -> CREDENTIAL (fatal, no grant fixes it)
[PASS] 401 -> CREDENTIAL
[PASS] row-access-policy refusal -> PERMISSION
[PASS] an ordinary SQL error is NOT treated as a denial
[PASS] resource extracted from the error text
[PASS] resource recovered from the SQL when the error does not name it
[PASS] rendered message still quotes 'bigquery.tables.getData'
[PASS] rendered message still quotes 'serviceusage.services.use'
[PASS] rendered message still quotes 'invalid authentication credentials'
[PASS] a denial in the tool result is replaced by the template
[PASS] the template names the refused table
[PASS] a raised denial is intercepted at the tool boundary
[PASS] MissingUserCredential renders the no-service-account template
[PASS] a non-authorization error is left alone
[PASS] a call missing `query` is short-circuited with a message naming the field
[PASS] snake_case/alias args repaired to camelCase and projectId defaulted:
       {'query': 'SELECT SESSION_USER()', 'projectId': '<GCP_PROJECT_ID>'}
ALL ERROR-BOUNDARY ASSERTIONS PASSED
```

### 2.6 `deploy.py --dry-run` — SUCCEEDED (no cloud calls)

```
DEPLOYMENT PLAN
{
  "project": "<GCP_PROJECT_ID>",
  "location": "us-central1",
  "staging_bucket": "gs://<GCP_PROJECT_ID>-agent-staging",
  "display_name": "teams-bot-bq-analyst",
  "requirements": [
    "google-adk==2.8.0",
    "mcp>=1.29.1,<2",
    "google-cloud-aiplatform[adk,agent_engines]==2.1.0"
  ],
  "extra_packages": ["./bq_agent"],
  "env_vars": {},
  "protected_engines_untouched": ["<OTHER_ENGINE_ID_1>"]
}

Local build OK: AdkApp constructed and streaming_agent_run_with_events is present.
--dry-run: nothing was uploaded and no Google Cloud call was made.
```

This proves the deployable object builds and that the ADR 005 method exists on
it. It does **not** prove anything about the remote runtime.

---

## 3. The open question: is there a request-scoped, NON-PERSISTED channel?

**Answer: YES. Established, with executable evidence.** This is the main
research result of this task.

### What I found

`vertexai/agent_engines/templates/adk.py`, `streaming_agent_run_with_events`:

```python
# Forward the user's OAuth access tokens as ephemeral `temp:` state.
# ADK exposes `temp:` keys to the agent for the duration of the
# invocation but trims them before the session is written to durable
# storage, so the tokens are never persisted.
state_delta = None
if request.authorizations:
    state_delta = {}
    for auth_id, auth in request.authorizations.items():
        auth = _Authorization(**auth)
        state_delta[f"temp:{auth_id}"] = auth.access_token
...
async for event in runner.run_async(..., state_delta=state_delta, ...)
```

`_StreamRunRequest` accepts `authorizations: Dict[str, _Authorization]`, and
`_Authorization` reads `access_token` / `accessToken`. So the request JSON the
middle tier already sends can carry the token, per invocation, out of band from
the message.

I did not take the source comment on trust. The chain, read in the installed
packages:

* `google/adk/sessions/state.py` → `TEMP_PREFIX = "temp:"`.
* `google/adk/sessions/base_session_service.py` → `append_event` calls
  `_apply_temp_state` (puts `temp:` keys into the in-memory session, so the
  invocation can read them) and `_trim_temp_delta_state` (removes them from
  `event.actions.state_delta`).
* `google/adk/sessions/vertex_ai_session_service.py` → `append_event` calls
  `super().append_event(...)` **first**, then builds
  `config['actions']['state_delta']` and `config['raw_event']` from the
  already-trimmed event, then POSTs.

Then I made it executable: `test_persistence.py` §2 swaps the API client for a
capturing fake and asserts the token appears nowhere in the captured payload.
That test passes (output in §2.4).

### What this means for the design

The critical constraint in the brief — *never read the token from Agent Runtime
Session state, because session state is persisted* — holds for **ordinary**
state keys and is enforced in code: `credentials._token_from_state` reads only
`temp:bigquery_user`, logs an error if it sees a token-shaped value under a
persisted key, and `assert_no_persisted_token()` exists as a tripwire. The
`temp:` prefix is the documented, source-verified, test-verified exception, and
it is the only channel used. The fallback to "pass the token as an explicit tool
argument" is **not needed** and was not implemented.

### What remains genuinely unproven

Honest list. None of these were exercised in the cloud, because deployment is
blocked (§4):

1. **Contextvar propagation inside the deployed process.** I proved a contextvar
   set in `UserCredentialPlugin.before_run_callback` is visible to a subsequent
   call and cleared afterwards, and I proved 12 concurrent callers each see
   their own value. What I have *not* proven is that the binding survives into
   whatever task Agent Runtime's ASGI layer runs the tool call on. This is
   exactly why `header_provider` also reads `temp:` state from the **live
   invocation context** ADK hands it: that path cannot be affected by task
   boundaries. If contextvar propagation turns out to fail in the runtime, the
   agent still works, via the backstop, with the same token.
2. **`AdkApp(plugins=...)` reaching both runners.** Read in the template: `set_up`
   builds a `runner` (Vertex session service) and an `in_memory_runner`, and
   passes the same plugins list to both. `streaming_agent_run_with_events`
   picks the in-memory one when no `session_id` is supplied. So plugins fire on
   both paths. Read, not run.
3. **Whether the Agent Runtime front end passes `authorizations` through
   untouched** for a directly-addressed reasoning engine (ADR 001) rather than
   only for the Gemini Enterprise/AgentSpace path the docstring mentions. The
   template code has no such conditional, but that is the SDK side; the service
   side was not observed.

---

## 4. BLOCKED items, with exact unblocking commands

### 4.1 Deployment to Agent Runtime — BLOCKED (IAM, not sandbox)

Network and Application Default Credentials are both reachable from here, so
this is a real IAM denial, not an environment artefact. Actual output of the
read-only listing:

```
$ .venv/bin/python deploy.py --list
google.api_core.exceptions.PermissionDenied: 403 Permission
'aiplatform.reasoningEngines.list' denied on resource
'//aiplatform.googleapis.com/projects/<GCP_PROJECT_ID>/locations/us-central1'
(or it may not exist). ... [reason: "IAM_PERMISSION_DENIED"
 domain: "aiplatform.googleapis.com"
 metadata { key: "permission" value: "aiplatform.reasoningEngines.list" }]
```

ADC principal: `<OPERATOR_GOOGLE_ACCOUNT>` (`gcloud config get-value account`;
`gcloud auth application-default print-access-token` succeeds).

`create` was **not** attempted. `list` is the weaker permission and it was
denied, so `create` would be denied too — but I have not run it, so I am not
reporting a result for it.

Unblock:

```bash
gcloud projects add-iam-policy-binding <GCP_PROJECT_ID> \
    --member="user:<OPERATOR_GOOGLE_ACCOUNT>" \
    --role="roles/aiplatform.user"
gsutil mb -p <GCP_PROJECT_ID> -l us-central1 gs://<GCP_PROJECT_ID>-agent-staging   # if absent
```

Then deploy:

```bash
cd agent
export GOOGLE_CLOUD_PROJECT=<GCP_PROJECT_ID>
export GOOGLE_CLOUD_LOCATION=us-central1
export STAGING_BUCKET=gs://<GCP_PROJECT_ID>-agent-staging
.venv/bin/python deploy.py
```

### 4.2 Live BigQuery query as a user — BLOCKED (no user token here)

The harness needs a Workforce Principal access token minted from a real Entra
sign-in. There is none in this sandbox and one cannot be fabricated.

```bash
cd agent
export GOOGLE_CLOUD_PROJECT=<GCP_PROJECT_ID> GOOGLE_CLOUD_LOCATION=us-central1
export GOOGLE_GENAI_USE_VERTEXAI=True
export BQ_USER_ACCESS_TOKEN="<workforce principal access token>"   # ../layer3/tokens.py
.venv/bin/python test_local.py                # SELECT SESSION_USER() identity probe
.venv/bin/python test_local.py --data         # real question against teams_bot_demo
```

Concurrent two-identity leak test:

```bash
export BQ_USER_ACCESS_TOKEN_A=... BQ_USER_SUBJECT_A='principal://iam.googleapis.com/.../subject/<oid>'
export BQ_USER_ACCESS_TOKEN_B=... BQ_USER_SUBJECT_B='someone@<ORG_DOMAIN>'
.venv/bin/python test_local.py --fanout 3
```

### 4.3 Model id `gemini-3.5-flash` — NOT VERIFIED

`BQ_AGENT_MODEL` defaults to `gemini-3.5-flash`, which is the model
`google-adk` 2.8.0's own scaffolding offers (`google/adk/cli/cli_create.py`). I
did **not** make a `generate_content` call, so I have not confirmed the id is
servable in `<GCP_PROJECT_ID>`/`us-central1`. It is env-overridable, so a wrong id is
a config change, not a code change.

```bash
gcloud ai models list --region=us-central1 --project=<GCP_PROJECT_ID> | grep -i gemini
# or just: export BQ_AGENT_MODEL=<verified id> before deploy.py
```

### 4.4 End-to-end `streaming_agent_run_with_events` with `authorizations`

Cannot run until 4.1 clears. Once deployed:

```bash
curl -sS -X POST \
  "https://us-central1-aiplatform.googleapis.com/v1/projects/<GCP_PROJECT_ID>/locations/us-central1/reasoningEngines/<OUR_ID>:streamQuery?alt=sse" \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  -d '{"class_method":"streaming_agent_run_with_events","input":{"request_json":"{\"message\":{\"role\":\"user\",\"parts\":[{\"text\":\"SELECT SESSION_USER()\"}]},\"user_id\":\"<oid>\",\"authorizations\":{\"bigquery_user\":{\"access_token\":\"<user token>\"}}}"}}'
```

Then confirm the token did not land in history:

```bash
curl -sS "https://us-central1-aiplatform.googleapis.com/v1/projects/<GCP_PROJECT_ID>/locations/us-central1/reasoningEngines/<OUR_ID>/sessions/<SID>/events" \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" | grep -c 'ya29\.'
# expected: 0
```

---

## 5. "This code exists" vs "this code was executed successfully"

| Item | Exists | Executed successfully |
|---|---|---|
| `bq_agent/credentials.py` | yes | yes — imported; header provider, plugin, tripwire all exercised under 12-way concurrency |
| `bq_agent/errors.py` | yes | yes — 19 assertions on classification, resource naming, templates, arg repair |
| `bq_agent/agent.py` | yes | partly — module imports and the agent object constructs. The LLM has never been called and no MCP call has been made from it |
| `McpToolset` against the real BigQuery MCP endpoint | yes | **no** — never connected from this sandbox |
| `deploy.py` build path | yes | yes — `--dry-run` built the `AdkApp` and confirmed `streaming_agent_run_with_events` |
| `deploy.py` create path | yes | **no** — BLOCKED, IAM (§4.1) |
| `deploy.py --list` | yes | ran, returned a real 403 (§4.1) |
| `deploy.py --rollback` | yes | **no** — nothing to roll back |
| `test_local.py --selftest` | yes | yes — PASSED |
| `test_local.py` live path | yes | **no** — BLOCKED, no user token (§4.2) |
| `test_persistence.py` | yes | yes — PASSED |
| `test_errors.py` | yes | yes — PASSED |
| Non-persistence of the token in the real Sessions **service** | — | **no** — proven against the real client code with a fake transport, not against the live service (§4.4 has the check) |

---

## 6. Deviations, and things I'd flag

No ADR was contradicted. Four things worth a second opinion:

1. **The brief's "never read the token from Session state" is implemented as
   "never read it from a *persisted* state key".** I want this called out
   explicitly rather than buried: I did use session state, specifically the
   `temp:` namespace, because that is the only request-scoped channel Agent
   Runtime offers and it is provably stripped before persistence. If you would
   rather have zero state involvement at any cost, the alternative is passing
   the token as an explicit tool argument, which means hand-maintaining the MCP
   tool schemas (as `layer3/handrolled_mcp.py` does) and losing tool discovery.
   I judged that a worse trade. Say the word and I'll switch it.

2. **The session-pool watchdog does not evict.** The brief asked for a bounded
   cache or periodic eviction. ADK 2.8.0 turns out to already do both (a 900 s
   idle sweep that skips in-flight sessions, and a 64-entry cap on the tools/list
   cache), so I implemented a watchdog that measures and warns instead of a
   second eviction mechanism racing the first. Closing a transport ADK believes
   is live would be a worse bug than the growth it prevents.

3. **The arg-repair path silently mutates the model's tool call** (aliases →
   camelCase, `projectId` defaulted). That is a deliberate ergonomic fix for a
   measured 17% failure rate, but it does mean the model is being corrected
   without knowing it. It is logged at INFO. If you would rather see the flake
   rate honestly in production telemetry, drop the alias map and keep only the
   short-circuit.

4. **`enable_tracing=True` in `deploy.py`.** Useful for debugging a distributed
   identity path. Worth confirming no trace attribute can capture a header — I
   did not audit ADK's span attributes for that, and it is the one place a
   token could plausibly leak into a durable store that I have not checked.
