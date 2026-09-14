# `agent/` — BigQuery analyst that runs as the signed-in Microsoft user

An ADK `LlmAgent` deployed to Vertex AI Agent Runtime. It answers questions
about `<GCP_PROJECT_ID>.teams_bot_demo` by calling the hosted BigQuery MCP endpoint
**with the Teams user's own Workforce Principal token**, never with the
runtime's service identity.

```
Teams ──► Bot Middle Tier ──► Agent Runtime (this agent) ──► BigQuery MCP ──► BigQuery
          (OBO + STS:            streaming_agent_run_with_events        (row-access policies
           mints the user's        authorizations{bigquery_user}         decide what is visible)
           Google token)
```

---

## 1. The problem this design solves

One deployed agent instance serves every user. If a per-user credential is
captured once — at construction, in a module global, in session state — then
user B's question is answered with user A's authority. That is a cross-user
data leak, not a bug. ADR 002 states the rule: **a Tool Identity that outlives
the turn that created it is a leak**, and ambient/default credentials are never
a valid Tool Identity for user-scoped data.

So the credential has to be:

1. supplied fresh on every request,
2. visible to the tool call for exactly that invocation,
3. **never written to durable storage**, and
4. never substituted with a service account when it is absent.

---

## 2. How the credential is threaded

### The channel: `authorizations` → `temp:` state

`streaming_agent_run_with_events` (the method ADR 005 commits the middle tier
to) accepts an `authorizations` map on its request JSON. The Agent Engine
template turns each entry into `state_delta["temp:<auth_id>"] = access_token`.

ADK reserves the `temp:` prefix for single-invocation values:
`BaseSessionService.append_event` **applies** `temp:` keys to the in-memory
session (so the invocation can read them) and **strips** them from
`event.actions.state_delta` before `VertexAiSessionService` builds the Sessions
API payload. The token is therefore readable during the turn and absent from
conversation history.

This is asserted executably, not documented and hoped for:
`test_persistence.py` drives the real `VertexAiSessionService.append_event`
against a capturing fake API client and asserts the token appears **nowhere**
in the bytes that would have gone to the Sessions service — neither in
`actions.state_delta` nor in the newer `raw_event` blob.

> Any state key **without** the `temp:` prefix is persisted. `credentials.py`
> refuses to read a token from one, logs loudly if it sees a token-shaped value
> under one, and ships `assert_no_persisted_token()` as a tripwire.

### The mechanism: contextvar, read by an async `header_provider`

```
before_run_callback (UserCredentialPlugin)
    read temp:bigquery_user  ──►  ContextVar.set(UserCredential)
        │
        ├── LLM turn ─► tool call ─► McpToolset ─► header_provider(ReadonlyContext)
        │                                              │
        │                                    1. ContextVar  (primary)
        │                                    2. temp: state on the LIVE
        │                                       invocation context (backstop)
        │                                    3. …nothing else. Raises.
        │
after_run_callback  ──►  ContextVar.reset() + drop the temp: key
```

In `google-adk` 2.8.0, `McpToolset(header_provider=...)` is invoked **at
tool-call time** with `ReadonlyContext(tool_context._invocation_context)`, and
it may be async. Headers are not frozen at construction, which is what makes a
single shared toolset safe. ADK pools MCP sessions on a hash of the merged
headers, so distinct user tokens get distinct pooled sessions.

The Layer 3 spike verified this live: 54 concurrent invocations across three
approaches through one shared agent instance, `asyncio.Barrier` forcing genuine
overlap, two real identities, **zero cross-user identity leaks**. The contextvar
approach passed 6/6 under 6-way concurrency and is what is implemented here.

Why keep the `temp:`-state backstop as well? The contextvar is set inside the
deployed process by a plugin callback; whether that binding always propagates
into the task that executes the tool has not been exercised in the cloud. The
backstop reads the *live invocation context ADK hands the provider*, which is
per-request by construction, so the identity is correct either way. Both paths
are the same token; there is no precedence hazard.

### Fail closed

If neither source yields a token, `header_provider` raises
`MissingUserCredential`. There is deliberately **no** service-account branch in
that function. The tool-boundary plugin turns the exception into a templated
"sign-in required" message that says, in as many words, that the bot will not
fall back to a service account.

---

## 3. Files

| File | What it is |
|---|---|
| `bq_agent/credentials.py` | The contextvar, the async `header_provider`, the invocation-boundary plugin, the persistence constraint, the session-pool watchdog. |
| `bq_agent/errors.py` | ADR 004 tool boundary: classify 401/403, name the refused resource, render a template, never let a raw IAM error reach the model. Also repairs the model's malformed tool calls. |
| `bq_agent/agent.py` | The `LlmAgent`, the `McpToolset`, the read-only tool allowlist, the instruction. |
| `deploy.py` | Create / list / roll back on Agent Runtime. `--dry-run` builds locally and prints the plan. |
| `test_local.py` | `--selftest` (offline credential threading under concurrency) and a live harness that asserts identity from **raw tool responses**. |
| `test_persistence.py` | Proves the token is not persisted. Offline. |
| `test_errors.py` | Proves the two-403 distinction and the arg repair. Offline. |
| `requirements.txt` | Pinned, with the rationale inline. |
| `NOTES.md` | What was executed, what is blocked, what remains open. |

### Why `execute_sql` is excluded

The toolset allowlists five read-only tools and deliberately omits
`execute_sql`:

* The demo is read-only. Its point is that row-level access policies decide
  what a user can *see*; write capability adds nothing and adds a way to mutate
  fixture data mid-demo.
* `execute_sql` accepts DDL and DML. An instruction planted in a queried row
  could reach it. A read-only tool cannot be turned into a write by any prompt.
* Defence in depth: IAM should also withhold write from the workforce
  principals. The allowlist means a slip in that grant is not immediately
  exploitable.

Adding write needs its own ADR, not an edit to the tuple.

### Two known runtime hazards, handled

* **Malformed tool calls.** Gemini 2.5 Flash omitted the required `query`
  argument in ~17% of calls in the spike, and the MCP server's reply to a
  malformed call names no field, so the model cannot self-correct.
  `FailClosedToolPlugin.before_tool_callback` renames known aliases to
  camelCase (`project_id`→`projectId`, `statement`→`query`), defaults
  `projectId`, and otherwise short-circuits with a message naming the missing
  field — which gives the model another turn, i.e. the retry.
* **Session-pool growth.** The pool key contains the token, so every refresh
  mints a new entry. ADK 2.8.0 already sweeps sessions idle for >900 s and caps
  the tools/list cache at 64 entries; `SessionPoolGuard` tracks distinct
  identities (as a salted digest, never the token) and warns when churn looks
  like it is outrunning that sweep. It deliberately does not reach into ADK's
  private pool — closing a transport ADK believes is live is worse than the
  growth it would fix.

---

## 4. Running it

```bash
cd agent
python -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python -c "import bq_agent.agent"     # import check
.venv/bin/python test_local.py --selftest       # credential threading, offline
.venv/bin/python test_persistence.py            # token never persisted, offline
.venv/bin/python test_errors.py                 # ADR 004 boundary, offline
```

Live, against real BigQuery (needs a real user token):

```bash
export GOOGLE_CLOUD_PROJECT=<GCP_PROJECT_ID> GOOGLE_CLOUD_LOCATION=us-central1
export GOOGLE_GENAI_USE_VERTEXAI=True
export BQ_USER_ACCESS_TOKEN="$(python ../layer3/tokens.py --print-workforce-token)"
.venv/bin/python test_local.py                  # SELECT SESSION_USER() probe
.venv/bin/python test_local.py --fanout 3       # concurrent two-identity leak test
```

---

## 5. Deploying

```bash
cd agent
.venv/bin/python deploy.py --dry-run            # no cloud calls at all

export GOOGLE_CLOUD_PROJECT=<GCP_PROJECT_ID>
export GOOGLE_CLOUD_LOCATION=us-central1        # NOT global; global lists empty
export STAGING_BUCKET=gs://<GCP_PROJECT_ID>-agent-staging
.venv/bin/python deploy.py
```

`deploy.py` reads `requirements.txt` so the deployed environment cannot drift
from the tested one, and refuses to run if the `mcp` pin has gone missing.

**The `mcp<2` pin is not optional.** `google-adk` 2.8.0 imports
`mcp.shared.session.ProgressFnT`; MCP SDK 2.x restructured its modules and that
import raises `ModuleNotFoundError`. Unpinned, the deploy resolves to 2.x and
the agent dies at import, in the runtime, after a successful-looking build.

### Rollback

```bash
.venv/bin/python deploy.py --list
.venv/bin/python deploy.py --rollback projects/<GCP_PROJECT_ID>/locations/us-central1/reasoningEngines/<OUR_ID> --yes
```

`deploy.py` only ever **creates**. It never updates an existing engine, and
`--rollback` hard-refuses `reasoningEngines/<OTHER_ENGINE_ID_1>`
("data_science_agent") — that engine is **not ours** and must not be touched.
If a deploy is bad, delete the engine you created and repoint the middle tier
at the previous id; there is no in-place mutation to undo.

---

## 6. Contract with the middle tier

Per invocation, `streaming_agent_run_with_events` must be given:

```json
{
  "message":   {"role": "user", "parts": [{"text": "..."}]},
  "user_id":   "<Entra object id>",
  "session_id": "<session resource id>",
  "authorizations": {
    "bigquery_user": {"access_token": "<user's Google access token>"}
  }
}
```

* The key **`bigquery_user`** is the contract (`AUTHORIZATION_ID`). Change it in
  one place and both sides break.
* The token must be sent on **every** request. It is not remembered, by design.
* Do not put the token anywhere else in the request. Any other state key is
  persisted, and this agent will refuse to read it.
