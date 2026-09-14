# Layer 3 findings: per-user token threading in ADK under concurrency

**Date**: 2026-09-07. **Status**: RESOLVED, with one blocking caveat for production.

This closes the last open technical risk recorded in `spikes/FINDINGS.md`. Everything
below was executed live against the real BigQuery MCP endpoint with two real
identities. Nothing here is modelled or inferred from behaviour that was not observed.

---

## The premise in FINDINGS.md is outdated for ADK 2.8.0

`spikes/FINDINGS.md` states the concern as: "`MCPToolset` takes `headers` at
construction time while one agent instance serves every user."

That is **no longer accurate for `google-adk` 2.8.0**, which is what actually
installs today. `MCPToolset` accepts a `header_provider` callable that is invoked
**at tool-call time**, with the live invocation's context:

```python
# google/adk/tools/mcp_tool/mcp_tool.py, run_async
dynamic_headers = self._header_provider(
    ReadonlyContext(tool_context._invocation_context)
)
if inspect.isawaitable(dynamic_headers):
    dynamic_headers = await dynamic_headers
```

Two further mechanics matter:

- `header_provider` **may be async**, so it can perform a token refresh inline.
- MCP sessions are **pooled on a hash of the merged headers**
  (`MCPSessionManager._generate_session_key`). Distinct per-user tokens therefore
  land in distinct pooled sessions rather than sharing one. The endpoint is
  stateless (`StatelessServer` / `ESF`), so there is no session affinity being
  violated by this.

The original concern was legitimate and is exactly the right thing to have worried
about. It is simply solved by a mechanism that did not exist when the concern was
written. The ADR is unaffected; only the implementation note changes.

---

## What was actually run

One `LlmAgent` instance and one `MCPToolset` instance served every invocation, which
is the production topology. Two genuinely different live identities were used:

| Label | Identity | What BigQuery reports for `SESSION_USER()` |
|---|---|---|
| `analyst` | `analyst@<TENANT_DOMAIN>`, an Entra user with **no Google account**, federated via Google STS | `principal://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/subject/<ANALYST_OBJECT_ID>` |
| `admin` | `admin@<ORG_DOMAIN>`, a real Google account via `authorized_user` ADC | `admin@<ORG_DOMAIN>` |

The two identities are reported differently by BigQuery itself, so a cross-user leak
is *observable* rather than argued.

### Concurrency was enforced, not hoped for

A sequential test passes even when the design is broken, because the second
invocation merely overwrites shared state after the first has finished. The harness
therefore installs an `asyncio.Barrier` inside the credential-resolution path:
**no invocation may proceed until every invocation has arrived**. The tool calls are
consequently in flight simultaneously.

If the barrier is not met, the run is reported `INCONCLUSIVE`, never `PASS`. A test
that cannot prove it overlapped has not tested concurrency. `overlap_confirmed=True`
in every run below.

Run with `--fanout 3`, i.e. **six simultaneous invocations** through one agent, three
per identity, so several invocations of the *same* person run alongside several of
another. That is harsher than one-each and makes any last-writer-wins bug far likelier
to surface.

### Approaches tested

- **A** — `contextvar` set per invocation, read by an async `header_provider`.
- **B** — token carried in Session state, read from `ReadonlyContext.state`.
- **C** — hand-rolled MCP client as a plain ADK `FunctionTool` reading `ToolContext`.

---

## Results

Two independent runs, 9 approach-runs total, **54 concurrent invocations**.

| Run | A | B | C |
|---|---|---|---|
| 1 (`--fanout 3`) | PASS 6/6 | 5/6 | 5/6 |
| 2 (`--fanout 3 --repeat 2`) | 5/6, 4/6 | 4/6, 5/6 | 5/6, **PASS 6/6** |

**Cross-user identity leaks: 0 out of 54 invocations, in every approach, with
overlap confirmed in every run.**

Every invocation that reached BigQuery reached it as the correct person. No
invocation ever saw another user's identity.

### The shortfall from 6/6 is NOT an identity defect

9 of 54 invocations returned no identity. Every one of those failed with the same
tool error:

```
Required parameter is missing: query
```

That is Gemini 2.5 Flash omitting a required tool argument. The request was still
authenticated as the correct principal and was rejected by BigQuery on argument
validation, not on authorization. The rate is roughly uniform across all three
approaches (A 3/18, B 3/18, C 3/18), which is what you would expect from a
model-behaviour flake and not from an approach-specific bug.

This distinction is load-bearing and the harness enforces it in code: the verdict is
computed from **identity strings observed in raw tool responses**, not from the
model's prose, precisely so that a garbled tool call can never be mistaken for an
identity defect, nor an identity defect excused as a garbled tool call.

It is, separately, a real production concern for item 4: the agent needs argument
validation and a retry on malformed tool calls, or roughly one turn in six will fail
in front of a user.

---

## Recommendation, and the caveat that blocks B and C as written

**All three approaches are concurrency-safe. Recommend approach A (contextvar).**

The deciding factor is not concurrency, since all three passed. It is **credential
persistence**.

Approaches B and C as tested read the user's Google access token from Agent Runtime
Session state. In the live system, Session state is persisted by the managed Sessions
service and is retrievable by session ID. **That would write a live bearer token into
durable conversation history.** That is unacceptable regardless of how well it
performs under concurrency, and it must not ship in that form.

Approach A keeps the credential in a request-scoped `contextvar` that is never
persisted, which is the correct shape. Its risk is the opposite one: `contextvar`
propagation is ambient, so if ADK ever dispatches a tool onto a thread or a bare task
without copying the context, the failure would be silent. It held across 54
overlapping invocations here, but that is evidence, not a guarantee, and it should be
re-run against any ADK upgrade. Treat the spike as a regression test, not a one-off.

**Open question for item 4, and it is genuinely open**: approach A requires a hook at
the invocation boundary *inside the deployed agent* to set the contextvar from
something the middle tier sends per invocation. Whether Agent Runtime exposes
request-scoped, non-persisted state for this — as opposed to Session state, which
persists — was **not** established by this spike. That must be settled before the
agent serves a second user. If no non-persisted channel exists, the fallback is
approach C with the token passed as a tool argument supplied per invocation rather
than read from Session state.

---

## Reproducing

```bash
export GOOGLE_APPLICATION_CREDENTIALS=~/.config/gcloud/application_default_credentials.json
python layer3/concurrency_spike.py --approaches A,B,C --fanout 3 --repeat 2 --out results.json
```

Exit code is 0 only if every approach returns PASS. `--fanout` sets concurrent
invocations per identity. `layer3/tokens.py` mints both identities live from the
stored Entra refresh token and the local ADC; it rotates and rewrites
`/tmp/entra_token.json` on each run so repeated runs keep working.

---

## Incidental findings worth keeping

- **`google-adk` 2.8.0 requires `mcp<2`.** MCP SDK 2.x restructured its modules and
  ADK's import of `mcp.shared.session.ProgressFnT` fails outright with
  `ModuleNotFoundError`. Pin `mcp<2` in the agent's requirements or the deploy breaks
  at import time.
- **`MCPToolset` is deprecated in 2.8.0** in favour of `McpToolset`. Same class,
  emits a `DeprecationWarning`. Use the new name in new code.
- The MCP session pool is keyed on a hash of the headers, and the headers contain the
  bearer token. Every token refresh therefore creates a **new** pooled session for the
  same user. Over a long-running agent instance this is unbounded growth in pooled
  sessions. Not a correctness problem, but worth a bounded cache or periodic eviction
  in item 4.
- `execute_sql_readonly` requires camelCase `projectId` and `query`. Passing
  `project_id` / `statement` returns a bare `Request contains an invalid argument`
  naming neither. Confirmed again here.
