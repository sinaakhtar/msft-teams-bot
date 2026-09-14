# Teams bot to Agent Runtime: build summary

**Date**: 2026-09-07. Execution against the settled architecture in `docs/adr/001`-`005`.
No ADR was contradicted. Where a worker's evidence pointed against a brief I gave them,
it is recorded below as a decision for you rather than resolved silently.

The single distinction this document exists to preserve: **"this code exists" is not
"this code was executed successfully."** Every claim below is filed under one or the
other, and nothing is filed under "verified" that I did not personally watch return.

---

## 1. The headline: item 1 is resolved, and the premise it rested on was outdated

Layer 3 was the last known technical risk and it blocked items 4 and 7. It is now
**closed, live**.

`spikes/FINDINGS.md` framed the concern as "`MCPToolset` takes headers at construction
while one agent instance serves all users." That framing is **no longer accurate for
`google-adk` 2.8.0**, which is what actually installs today. `MCPToolset` accepts a
`header_provider` callable invoked **at tool-call time** with the live invocation's
context, and it may be async:

```python
# google/adk/tools/mcp_tool/mcp_tool.py, run_async
dynamic_headers = self._header_provider(ReadonlyContext(tool_context._invocation_context))
```

MCP sessions are additionally pooled on a hash of the merged headers, so distinct
per-user tokens land in distinct pooled sessions. The original worry was the right thing
to worry about; it is simply solved by a mechanism that postdates the note.

### What was actually executed

One `LlmAgent` and one `MCPToolset` instance served every invocation — the production
topology. Two genuinely different live identities were used, chosen so BigQuery itself
reports them differently and a leak would be *observable* rather than argued:

| Identity | `SESSION_USER()` returns |
|---|---|
| `analyst@<TENANT_DOMAIN>` (Entra user, **no Google account**, federated via STS) | `principal://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/subject/<ANALYST_OBJECT_ID>` |
| `admin@<ORG_DOMAIN>` (real Google account, ADC) | `admin@<ORG_DOMAIN>` |

**Concurrency was enforced, not hoped for.** An `asyncio.Barrier` sits inside the
credential-resolution path: no invocation proceeds until every invocation has arrived,
so the tool calls are genuinely in flight together. If the barrier is not met the run
reports `INCONCLUSIVE`, never `PASS` — a test that cannot prove it overlapped has not
tested concurrency. `overlap_confirmed=True` in every run reported here.

**Result: 54 concurrent invocations across 3 approaches and 2 independent runs, at
6-way concurrency. Zero cross-user identity leaks.** Every invocation that reached
BigQuery reached it as the correct person.

9 of 54 invocations returned no identity, all with the same error: `Required parameter
is missing: query`. That is Gemini 2.5 Flash omitting a tool argument — the request was
still authenticated as the correct principal and rejected on argument validation. The
rate is near-uniform across all three approaches (3/18 each), which is what a model
flake looks like and not what an approach-specific bug looks like. The harness computes
its verdict from **identity strings in raw tool responses, not from model prose**,
precisely so a garbled tool call can never be mistaken for an identity defect nor an
identity defect excused as one.

### Recommendation, and the caveat that matters more than the result

**All three approaches are concurrency-safe. Use approach A (contextvar).**

The deciding factor is not concurrency — all three passed — it is **credential
persistence**. Approaches B and C as tested read the token from Agent Runtime Session
state, and Session state is **persisted** by the managed Sessions service. That would
write a live bearer token into durable conversation history. Unacceptable regardless of
concurrency behaviour, and it must not ship in that shape.

**GENUINELY OPEN, and it gates item 4:** approach A needs a hook at the invocation
boundary *inside the deployed agent* to set the contextvar from something the middle
tier sends per invocation. Whether Agent Runtime exposes request-scoped,
**non-persisted** state for this was **not** established. This must be settled before
the agent serves a second user. Fallback if no such channel exists: approach C with the
token passed as an explicit per-invocation tool argument rather than read from state.

Full detail and reproduction: `spikes/layer3/LAYER3_FINDINGS.md`.

---

## 2. Verified live — I watched these return

| What | Evidence |
|---|---|
| Layer 3 concurrency, 3 approaches | 54 invocations, 0 leaks, barrier-enforced overlap |
| BigQuery MCP authorizes per caller | Two identities, two different `SESSION_USER()` values |
| Entra → STS → Workforce Principal | Fresh ID token minted from stored refresh token; STS returned an access token, `expires_in` 3598 |
| Workforce pool + provider config | Read live: `ACTIVE`, `sessionDuration 3600s`, `google.subject = assertion.oid`, `ID_TOKEN` + `ONLY_ID_TOKEN_CLAIMS` |
| All five IAM roles on `<GCP_PROJECT_ID>` | `mcp.toolUser`, `bigquery.jobUser`, `bigquery.dataViewer`, `serviceusage.serviceUsageConsumer`, `aiplatform.user`, all on the pool principalSet |
| `reasoningEngines.list` as workforce principal | HTTP 200 |
| `sessions.create` as workforce principal | HTTP 200, LRO already `done`, `userId` echoed back as `entra:{tid}:{oid}` |
| Vertex Gemini 2.5 Flash in `<GCP_PROJECT_ID>` | HTTP 200 |
| Middle tier test suite | **270 passed, 2 skipped** |
| Agent test suite | **7 passed** |
| `app/errors` re-export claim | Verified by AST diff of the old module's public surface — nothing missing |
| Terraform `init` + `validate` + `plan` | Ran and passed (worker), provider 7.46.1 downloaded |

Two environment facts worth carrying forward:

- **There are now THREE reasoning engines named `data_science_agent`** in us-central1
  (`<OTHER_ENGINE_ID_1>`, `<OTHER_ENGINE_ID_2>`, `<OTHER_ENGINE_ID_3>`), not the one
  recorded in FINDINGS. None are ours. **Never resolve the target engine by display
  name** — require an explicit engine ID in config.
- **`google-adk` 2.8.0 requires `mcp<2`.** MCP SDK 2.x restructured its modules and
  ADK's `from mcp.shared.session import ProgressFnT` fails at import. Pin it or the
  deploy breaks on startup. (`MCPToolset` is also deprecated in favour of `McpToolset`.)

---

## 3. Written but NOT executed

Everything here is code or documentation that exists and is internally consistent, but
has never run against the real thing. Do not read any of it as working.

| Item | State | Why not executed |
|---|---|---|
| 2. Terraform | Written, `validate`/`plan` pass | `apply`/`import` never run, deliberately |
| 3. BigQuery dataset + row-level policies | SQL + `apply.sh` written | Never executed against BigQuery |
| 5. Entra app registration, SSO, OBO grant | Click-by-click runbook + manifest | Requires a human in the Azure portal |
| 4. ADK agent | Code written, imports clean, 7 tests pass | Never deployed to Agent Runtime |
| 7. Identity broker | Code + cache tests pass | **The OBO hop has never run** — see risk 1 |
| 8. Session manager | Code + tests pass | Never run against the live sessions API by the worker (though I verified the underlying hop myself — see §5) |
| 9. Streaming renderer | Code + tests pass against a **synthetic** event stream | Never seen real ADK output or a real Teams client |
| 10. Error handling | Code + tests pass, fallback scanner runs | Templates never rendered in a real Teams client |
| 11. Azure Bot Service wiring | Runbook written | Requires human action in Azure |
| 12. Demo runbook | Written | Depends on everything above |
| Container image | `Dockerfile` written | No Docker daemon in the sandbox; **never built** |

---

## 4. The risks that actually threaten this build

**1. The OBO audience mismatch — the likeliest single point of failure.**
Google's STS validates `aud` against the federation app's client ID, and the hop is
verified working with an Entra **ID token** carrying the bare client ID. An OBO exchange
typically returns an **access token** whose `aud` may be the target's Application ID URI
(`api://…`) instead. If so, the chain breaks at STS and the entire identity story stops.
This has **never been executed** — there is no Teams SSO token and no client secret
available. `entra/04_verification.md` is written to localise exactly this, hop by hop.
**Run that before building anything else on top.**

**2. Approach A's per-invocation channel is unresolved** (see §1). Gates item 4.

**3. The conversation→session map is in-memory.** Cloud Run scales to N instances and
the mapping will break above one. Documented with options in
`middle_tier/app/sessions/store.py`; needs Firestore or equivalent before real use.

**4. ADK event shapes drift across versions** and the renderer has only been tested
against a synthetic stream. ADR 005 already names this the most likely place an upgrade
breaks the bot. Treat `spikes/layer3/concurrency_spike.py` as a regression test to re-run
on every ADK bump.

---

## 5. Conflicts and corrections — surfaced, not silently resolved

**Three worker-reported BLOCKED items are actually unblocked.** The workers lacked
credentials; I had them and ran the calls. Where we disagree, my live result stands and
theirs is a permissions artefact, not a finding:

- Terraform: *"cannot read or import the workforce pool and provider"* → I read both
  live; they are ACTIVE and match the config.
- Terraform: *"cannot confirm the five IAM bindings exist"* → all five confirmed live.
- Sessions BLOCKED-1: *"live `sessions.create` as a Workforce Principal"* → executed,
  HTTP 200, `userId` echoed correctly.

**A live correction the Terraform worker had missed.** The provider carries **two**
attribute mappings, not one: `google.subject = assertion.oid` **and**
`google.display_name = assertion.preferred_username`. I sent the correction; it did not
land before the worker's harness session ended, so **`terraform/workforce_pool.tf` is
still missing `google.display_name`**. Left as-is rather than hand-patched, because an
`apply` without it would silently remove the mapping from live infrastructure. **Add it
before any apply.**

**A deviation from my brief, with evidence, that needs your call.** I told the middle
tier worker to prefer the Bot Framework SDK over hand-rolled JWT validation. It came
back with `botbuilder-python` being **archived/EOL (support ended 2025-12-31)**, and its
successor shipping without the `cryptography` extra — meaning RS256 is unavailable —
plus a `serviceUrl` check that warns and continues. It hand-rolled on `PyJWT[crypto]`
instead, bounded by an RSA-only allow-list and a mutation suite (6/6 mutations caught).
**I think it was right and the brief was wrong, but this is a security boundary and it
is your decision.**

**Two namespace collisions, both resolved cleanly, one leftover.**
`app/identity.py` was renamed `app/caller_identity.py` because another worker claimed
`app/identity/` as a package. `app/errors.py` is superseded by the `app/errors/` package,
which re-exports the old surface identically — I verified that by AST diff rather than
taking it on trust. **`app/errors.py` is now unreachable dead code and should be
deleted**; I left it in place rather than delete work unprompted.

---

## 6. To unblock, in order

```bash
# 1. THE CRITICAL ONE. Prove the OBO hop end to end before anything else.
#    Follow entra/04_verification.md; it isolates each hop so a failure localises.
#    Decisive check: decode the OBO output and confirm aud == <FEDERATION_APP_CLIENT_ID>
#    (the bare client ID, NOT api://...). If it is not, the chain breaks at STS.

# 2. Terraform: add the missing google.display_name mapping, then adopt existing infra.
#    NEVER apply before importing - a fresh apply would try to recreate a working pool.
cd terraform && terraform init && cat IMPORT.md   # follow the import commands, then:
terraform plan    # expect NO destructive changes; if you see any, stop.

# 3. Demo data.
bash bigquery/apply.sh          # needs the m365-admin Entra oid filled in first

# 4. Deploy the agent (settle the approach-A channel question first).
python agent/deploy.py

# 5. Build and deploy the middle tier.
gcloud builds submit middle_tier/ \
  --tag us-central1-docker.pkg.dev/<GCP_PROJECT_ID>/bot/middle-tier:v0 --project <GCP_PROJECT_ID>

# 6. Azure: follow entra/01..03 then runbook/11_azure_bot_service.md (human, in-portal).
# 7. Rehearse with runbook/12_demo.md.
```

Human actions that no automation can replace: the Entra app registrations and admin
consent (item 5), and wiring Azure Bot Service to the Cloud Run URL (item 11).

One thing to keep in view when wiring Cloud Run: the service must allow unauthenticated
invocations at the network layer, because Bot Framework authenticates with its own JWT
rather than a Google identity token. That makes the inbound JWT validation the only
thing between the internet and a forged activity claiming any `aadObjectId`. It is the
most load-bearing code in the repository, which is why it carries a mutation suite.

---

## 7. Layout

```
terraform/     item 2   pool, provider, 5 IAM roles, APIs, Cloud Run SA + secrets, IMPORT.md
bigquery/      item 3   dataset, tables, seed, row access policies, apply.sh
entra/         item 5   app registrations, OBO grant, Teams manifest, per-hop verification
agent/         item 4   ADK agent, per-user credential threading, deploy script
middle_tier/   items 6-10  JWT validation, routing, identity broker, sessions, streaming, errors
spikes/        item 1   layer3/ concurrency harness, findings, raw result JSON
runbook/       items 11-12  Azure wiring, two-user demo with audit-log evidence
docs/          CONTEXT.md and ADRs 001-005, carried forward unchanged
```

Each component carries its own `NOTES.md` with that worker's own account of what it ran
and what it could not. Where those disagree with this file, §5 says which I verified.
