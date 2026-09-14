# Spike findings: BigQuery MCP server and caller identity

Each entry records what was actually run and what actually came back. Assumptions that
have not been executed are listed as open, not as expected results.

---

## Layer 1: does the managed MCP server authorize per caller? PASSED

**Date**: 2026-09-07
**Environment**: GCP project `<GCP_PROJECT_ID>`, org domain `<ORG_DOMAIN>`
**Credential**: `authorized_user` ADC (a real Google account, not a service account)
**Command**:

```
.venv/bin/python spikes/mcp_identity_spike.py \
  --project <GCP_PROJECT_ID> \
  --token-source adc \
  --adc-file ~/.config/gcloud/application_default_credentials.json \
  --sql "SELECT 1 AS ok, SESSION_USER() AS whoami"
```

**Result**: `SESSION_USER()` returned `admin@<ORG_DOMAIN>`.

The query ran inside BigQuery as the bearer token's identity. No intermediary service
identity was substituted. This is the property the entire design depends on and it is
now demonstrated rather than assumed.

**Corollary**: `SESSION_USER()` is the demo probe. Two users asking the identical
question through the bot will get two different answers to it, which is provable on
screen without contriving a dataset.

### Facts established about the endpoint

- URL `https://bigquery.googleapis.com/mcp` responds to streamable HTTP JSON-RPC.
- Negotiated protocol version `2025-06-18`.
- `serverInfo` reports `StatelessServer` / `ESF`. It is stateless, so there is no
  meaningful MCP session to keep alive between calls. Each call must carry its own
  credential, which suits per-user token threading rather than fighting it.
- Six tools advertised, matching documentation: `list_dataset_ids`, `get_dataset_info`,
  `list_table_ids`, `get_table_info`, `execute_sql_readonly`, `execute_sql`.
- `execute_sql_readonly` requires camelCase `projectId` and `query`. An earlier guess of
  `project_id` / `statement` returned a bare `Request contains an invalid argument`
  with no indication of which argument, so read `tools/list` rather than guessing.

---

## Environment facts (verified 2026-09-07, read-only calls)

- Organization: `organizations/<GCP_ORG_ID>`, display name `<ORG_DOMAIN>`,
  directory customer ID `<GCP_CUSTOMER_ID>`, created 2022-03-04.
- Because the org predates 2024-05-03, it is not subject to the default-on
  domain-restricted sharing rule that applies to newer organizations. The earlier
  assumption that DRS would need editing was wrong for this org.
- **Domain-restricted sharing is already configured to permit workforce principals.**
  The effective `constraints/iam.allowedPolicyMemberDomains` policy allows
  `<GCP_CUSTOMER_ID>`, `<OTHER_ALLOWED_CUSTOMER_ID>`, and crucially
  `is:principalSet://iam.googleapis.com/organizations/<GCP_ORG_ID>`. That last value is
  the organization principal set, which is exactly what permits workforce identity pool
  principals to receive IAM roles. No org policy change is required.
- `constraints/iam.workloadIdentityPoolProviders` is `allValues: ALLOW`, so provider
  configuration is unrestricted.
- `constraints/iam.workforcePoolProviders` does not exist as a constraint ID. There is
  no equivalent restriction to clear.

### Blocker: the current credential cannot administer workforce pools

`iam.workforcePools.list` is denied for `admin@<ORG_DOMAIN>` on
`organizations/<GCP_ORG_ID>`. Org-level Workforce Identity Pool administration requires
a role grant (`roles/iam.workforcePoolAdmin`, or equivalent) before layer 2 can begin.
Being project Owner on `<GCP_PROJECT_ID>` is not sufficient, because workforce pools are
organization-level resources.

---

## Layer 2: does it accept a workforce-federated token? PASSED

**Date**: 2026-09-07. **Verdict**: ADR 002 holds. An Entra ID user with no Google
account queried BigQuery as themselves, end to end.

**Result**: `SELECT SESSION_USER()` executed through the MCP server returned

```
principal://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/subject/<ANALYST_OBJECT_ID>
```

where `<ANALYST_OBJECT_ID>` is the Entra `oid` of
`analyst@<TENANT_DOMAIN>`, a plain tenant user with no admin rights and no Google
identity of any kind. The subject BigQuery sees is exactly the string ADR 003 fixed as
the session key, so session ownership and IAM identity are one value by construction,
as intended rather than by coincidence.

### The verified chain

1. Entra device-code sign-in produces an ID token with `aud` = the federation app's
   client ID and `iss` = `https://login.microsoftonline.com/{tid}/v2.0`.
2. Google STS token exchange at `https://sts.googleapis.com/v1/token`, with
   `audience` = `//iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/providers/entra`,
   `subject_token_type` = `urn:ietf:params:oauth:token-type:id_token`, and
   `options` = `{"userProject": "<GCP_PROJECT_ID>"}`. Returns a Google access token.
3. That token is presented as a plain bearer to `https://bigquery.googleapis.com/mcp`.

### Configuration that mattered

- Workforce pool `teams-bot-demo`, provider `entra`, both org-level under
  `organizations/<GCP_ORG_ID>`.
- `attributeMapping`: `google.subject` = `assertion.oid`. This is the line that makes
  the whole design cohere.
- `webSsoConfig.assertionClaimsBehavior` must be `ONLY_ID_TOKEN_CLAIMS` when
  `responseType` is `ID_TOKEN`. Using `MERGE_USER_INFO_OVER_ID_TOKEN_CLAIMS` is
  rejected with a bare `Invalid OIDC WebSsoConfig AssertionClaimsBehavior`, which does
  not tell you the two fields are coupled.
- IAM roles granted to
  `principalSet://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/*`
  on `<GCP_PROJECT_ID>`.

### Non-obvious requirement: serviceUsageConsumer

The first attempt failed with a 403 that had nothing to do with BigQuery:

> Caller does not have required permission to use project <GCP_PROJECT_ID>. Grant the caller
> the roles/serviceusage.serviceUsageConsumer role...

This is caused by sending `userProject` / `X-Goog-User-Project`, which is itself
required for workforce principals because they have no project of their own to bill.
So the quota project is mandatory, and the quota project then demands its own role. The
full required set is therefore four roles, not the three the BigQuery MCP documentation
lists:

- `roles/mcp.toolUser`
- `roles/bigquery.jobUser`
- `roles/bigquery.dataViewer`
- `roles/serviceusage.serviceUsageConsumer`

Allow around 30 to 60 seconds for the grant to propagate before retrying.

### Distinguishing the two 403s

Both failures in this spike were HTTP 403 and they meant completely different things.
The text is the only signal. A 403 naming a missing role is a permission fix. A 403
naming the credential or principal type would have been fatal. Read the message.

---

## Layer 2b: can a workforce principal drive the Agent Runtime? PASSED

**Date**: 2026-09-07. Same federated token as layer 2, after granting the pool
`roles/aiplatform.user` on `<GCP_PROJECT_ID>`.

This is a different API surface from the BigQuery MCP endpoint and was a separate
untested assumption. It underpins the Invocation Identity plane of ADR 002.

- `reasoningEngines.list` in `us-central1`: **200**. The federated user can enumerate
  deployed agents.
- `reasoningEngines.get`: **200**.
- `sessions.create` with
  `userId = entra:<ENTRA_TENANT_ID>:<ANALYST_OBJECT_ID>`: **200**, returning an already-complete
  LRO and a session resource carrying that exact `userId`.

So a Microsoft user with no Google account can create and own an Agent Runtime Session
keyed by their Entra object ID. ADR 002's first plane and ADR 003's key format are both
confirmed against the live API.

### Incidental discoveries

- `<GCP_PROJECT_ID>` already contains a deployed agent, `data_science_agent`
  (`reasoningEngines/<OTHER_ENGINE_ID_1>`, us-central1, Python 3.12, pickle-based
  package spec). Useful as a test target; it is not ours and should not be assumed
  stable.
- The `global` location returns an empty list. Agents here live in `us-central1`.
- Exposed class methods include both `stream_query` and
  `streaming_agent_run_with_events`, plus sync and async session CRUD, plus
  `async_add_session_to_memory` and `async_search_memory`. The two streaming methods
  are not equivalent and the choice between them is a design decision, not a detail.
- Sessions are reachable two ways: the REST `sessions` subresource on the reasoning
  engine, and the agent's own `create_session` class method. Both exist; only one
  should be used.

---

## Layer 3: per-user token threading in ADK. RESOLVED

**Date**: 2026-09-07. The last open technical risk is closed. Executed live against the
real BigQuery MCP endpoint with two real identities, not modelled.

**The premise recorded below was outdated.** `google-adk` 2.8.0 gives `MCPToolset` a
`header_provider` callable invoked at *tool-call* time with the live invocation's
context, and it may be async so it can refresh a token inline. MCP sessions are pooled
on a hash of the merged headers, so distinct per-user tokens land in distinct pooled
sessions rather than sharing one. The worry was the right thing to worry about; it is
simply solved by a mechanism that did not exist when it was written. No ADR changes.

**Concurrency was enforced rather than hoped for.** An `asyncio.Barrier` inside the
credential-resolution path prevents any invocation proceeding until all have arrived,
so tool calls are genuinely in flight together. A run that cannot prove it overlapped
reports INCONCLUSIVE, never PASS. Verdicts are computed from identity strings observed
in raw tool responses, never from the model's prose. One agent instance and one
toolset served every invocation, which is the production topology. 54 overlapping
invocations across two identities that BigQuery itself reports differently, so a leak
would be observable rather than argued.

**All three approaches were concurrency-safe. Approach A, a request-scoped
contextvar, is the one to use.** The deciding factor was not concurrency but
credential persistence: approaches B and C read the user's Google access token from
Agent Runtime Session state, and Session state is persisted by the managed Sessions
service and retrievable by session ID. That would write a live bearer token into
durable conversation history. It must not ship in that form regardless of how well it
performs.

**Correction, later the same day, verified by execution.** The prohibition above is
too broad as written. It holds for *ordinary* session state keys. Agent Runtime does
provide a first-class request-scoped credential channel:
`streaming_agent_run_with_events` accepts an `authorizations` map and surfaces each
access token as `temp:`-prefixed session state, and ADK's `BaseSessionService` strips
`temp:` keys from `state_delta` before persistence. This was proven rather than read:
a test captures the payload that would actually go over the wire to the Sessions API
and confirms it contains no trace of the token, while an ordinary key in the same
delta still persists, which shows the trim is targeted rather than a blanket wipe that
would pass for the wrong reason.

So there are two safe mechanisms, not one. The contextvar remains primary because it
depends on nothing the platform might change; `temp:` state is the supported fallback.
What remains forbidden is putting a credential under an ordinary, persisted key.

**The residual risk on approach A is the opposite one.** Contextvar propagation is
ambient, so if ADK ever dispatches a tool onto a thread or bare task without copying
the context, the failure is silent. It held across 54 overlapping invocations, but
that is evidence, not a guarantee. Re-run the spike against every ADK upgrade and
treat it as a regression test rather than a one-off.

**Separate finding, affects the agent build**: roughly one turn in six failed on a
malformed tool call. The agent needs argument validation and a retry, or that failure
rate reaches users.

---

## Original layer 3 concern, retained for context

Deferred at the user's request on 2026-09-07 in order to validate the rest of the
architecture first. This is a conscious deferral of the last known technical risk, not
a resolution of it. The concern stands: `MCPToolset` takes headers at construction time
while one agent instance serves every user, so a token captured at construction is a
cross-user data leak. Nothing has been built on top of this yet, so deferring costs
nothing today, but it must be settled before the agent serves a second user.

Not yet run. It was deliberately held back until layer 2 passed, which it now has, so
this is next and it is the last thing standing between the design and implementation.

The concern is that `MCPToolset` takes `headers` at construction time while one agent
instance in the Agent Runtime serves every user. A token captured at construction is a
cross-user data leak. The endpoint being stateless helps, since there is no session
affinity to preserve.

Approaches to try, in order:
1. A contextvar set per invocation, resolved by a custom httpx auth hook. The thing to
   prove is that the contextvar survives ADK's async execution intact.
2. A hand-rolled MCP client as a plain ADK tool, reading the token from `ToolContext`.
   Loses the toolset ergonomics, gains complete control over the credential.

The acceptance test is concurrency, not correctness: two simultaneous invocations with
different tokens must each reach BigQuery as the right person. A sequential test will
pass even when the design is broken.
