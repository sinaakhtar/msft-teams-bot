# NOTES — placeholders, provenance, and dependencies

This page exists so that someone following
[11_azure_bot_service.md](11_azure_bot_service.md) and
[12_demo.md](12_demo.md) under time pressure can tell, for every claim, whether
it came from current documentation read while writing, from verified live
results recorded elsewhere in this repo, or from reasoning that has not been
executed.

**Author's access, stated plainly.** The author had **no Azure or Entra portal
access, no Teams client, and no authenticated Google Cloud credentials.**

- **No portal step in either runbook was performed.**
- **No command in either runbook was executed.**
- **No log entry, resource ID, portal screen, or demo outcome described in
  either runbook was observed.**

What was actually done: Microsoft Learn and Google Cloud documentation pages
were retrieved over HTTPS on **2026-09-07** and quoted; the repository's own
ADRs, middle-tier source, BigQuery scripts and Terraform were read. That is the
entirety of it. Where a runbook says "expect X", that is a prediction to be
falsified in rehearsal, not a report.

Live results referenced in the runbooks (`SESSION_USER()` returning the
analyst's `principal://` URI; a workforce principal driving Agent Runtime) were
verified by **other work in this project** and are recorded in
`spikes/FINDINGS.md` and `bigquery/README.md`. They are reproduced here as
inherited facts, not as things this author ran.

---

## 1. Placeholder checklist — every value a human must supply

### Runbook 11

| Placeholder | What it is | Where you get it | Blocks |
| --- | --- | --- | --- |
| `<APP_A_CLIENT_ID>` | Teams bot app registration's Application (client) ID | `entra/01`, step 2 | Everything |
| `<APP_A_CLIENT_SECRET>` | App A client secret **Value**, shown once | `entra/01`, step 3 | OBO, OAuth connection |
| `<AZURE_SUBSCRIPTION>` | Subscription the bot resource bills to | Your tenant | Step 1a |
| `<AZURE_RESOURCE_GROUP>` | Resource group for the bot resource | Create or reuse | Step 1a |
| `<BOT_HANDLE>` | Globally unique bot handle, 4–42 chars | You choose | Step 1a, all `az bot` commands |
| `<CLOUD_RUN_SERVICE>` | Cloud Run service name for the middle tier | Your deploy | Every log and describe command |
| `<CLOUD_RUN_URL>` | Full HTTPS base URL, no trailing slash | `gcloud run services describe` | Messaging endpoint |
| `<BOT_DOMAIN>` | `<CLOUD_RUN_URL>` minus the scheme | Derived | Teams manifest `validDomains`, App A redirect URI |
| `<REASONING_ENGINE_ID>` | Numeric ID of **our** reasoning engine | Agent Runtime deploy | Env var, audit log check |
| `<OAUTH_CONNECTION_NAME>` | Name of the OAuth connection setting | You choose, step 5a | Sign-in card |

### Runbook 12

| Placeholder | What it is | Where you get it |
| --- | --- | --- |
| `<M365_ADMIN_OBJECT_ID>` | Entra object ID of `m365-admin@<TENANT_DOMAIN>` | `az ad user show --id m365-admin@<TENANT_DOMAIN> --query id -o tsv` |
| `<PRINCIPAL_ADMIN>` | Admin's full `principal://` URI | The analyst's URI with the oid swapped |
| `<ANALYST_TOTAL>` / `<ADMIN_TOTAL>` | The two pipeline numbers | Observe in rehearsal |
| `<ADMIN_ONLY_DEAL_NAME>` | A deal visible only to the admin | `bigquery/03_seed_data.sql` |
| `<OBSERVED_SESSION_METHOD>` | Actual `protoPayload.methodName` for Agent Runtime session create | **Discover in rehearsal** — see §4 |
| Denied resource + triggering prompt | For the failure demo | Choose and confirm in rehearsal |

### Known values — do not retype from memory, do not change

| Value | What |
| --- | --- |
| `<ENTRA_TENANT_ID>` | Entra tenant (`<TENANT_DOMAIN>`) |
| `<FEDERATION_APP_CLIENT_ID>` | **App B**, the federation app Google trusts. Exists. Not the bot. |
| `<ANALYST_OBJECT_ID>` | `analyst@<TENANT_DOMAIN>` object ID |
| `<GCP_PROJECT_ID>` / `<GCP_PROJECT_NUMBER>` | GCP project ID / number |
| `us-central1` | Region |
| `locations/global/workforcePools/teams-bot-demo`, provider `entra` | Workforce pool |
| `organizations/<GCP_ORG_ID>`, customer `<GCP_CUSTOMER_ID>` | GCP org |
| `entra:{tid}:{oid}` | Session `user_id` format (ADR 003) |

### Do-not-touch

`reasoningEngines/<OTHER_ENGINE_ID_1>` — a pre-existing `data_science_agent` in
`<GCP_PROJECT_ID>` belonging to someone else. Not ours. Never set
`REASONING_ENGINE_ID` to it, never modify or delete it, never point a demo
filter at it.

### Secret handling

`<APP_A_CLIENT_SECRET>` belongs in Google Secret Manager (`teams-bot-app-password`)
and, if you configure an OAuth connection setting, in Azure Bot Service's
configuration. Nowhere else — not the repo, not a `.env`, not shell history.
Runbook 11 §5a flags the two-copy problem explicitly because it bites at
rotation time.

---

## 2. Confirmed from current documentation

All URLs retrieved **2026-09-07**. Where the page displays a "last updated"
date, it is given, because several of these surfaces changed recently.

### Microsoft

**M1 — Azure Bot app types, and the retirement notices.**
"Use the Azure portal to Create an Azure Bot resource", last updated
**2026-09-01**.
<https://learn.microsoft.com/en-us/azure/bot-service/abs-quickstart>
Quoted or relied on:
- The three app types and Microsoft's stated fit for each (user-assigned managed
  identity / single tenant / multi-tenant).
- "Support for user-assigned managed identity and single-tenant app types is
  available in the Bot Framework SDK for C#, JavaScript, and Python. These app
  types are not supported in other SDK languages, Bot Framework Composer,
  **Bot Framework Emulator**, or Dev Tunnels."
- "Multi-tenant bot creation will be deprecated after July 31, 2025. Existing
  multi-tenant bots will continue to function... To ensure continued support,
  use single-tenant or user-assigned managed identity going forward."
- "New Web App Bot and Bot Channels Registration resources can't be created;
  however, any such existing resources that are configured and deployed will
  continue to work."
- Bot handle rules: 4–42 chars, `a-z A-Z 0-9 - _`, must start with letter or
  digit, must be unique.

**M2 — Bot Connector JWT validation requirements.**
"Authentication" (Bot Framework REST).
<https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-authentication>
Relied on for the eight numbered requirements in runbook 11 §3d, the static
metadata URL `https://login.botframework.com/v1/.well-known/openidconfiguration`,
`jwks_uri` `https://login.botframework.com/v1/.well-known/keys`, issuer
`https://api.botframework.com`, `RS256`, 5-minute clock skew, the ≥24h key-cache
refresh guidance, HTTP 403 for a missing channel endorsement, and Microsoft's
warnings that all requirements matter "particularly requirements 4 and 6" and
that implementers "shouldn't expose a way to disable validation".

**M3 — The reply window: 10 to 15 seconds, 504.**
"Long running operations guidance".
<https://learn.microsoft.com/en-us/azure/bot-service/bot-builder-howto-long-operations-guidance>
Exact wording: "If the bot doesn't complete the operation within 10 to 15
seconds, depending on the channel, the Azure AI Bot Service will time out and
report back to the client a 504:GatewayTimeout".
**This is the normative source.** The widely-cited "15 seconds" figure from
Stack Overflow threads about Direct Line is consistent with it but is not
authoritative, and the range matters: design to 10, not 15.

**M4 — Teams channel connection steps.**
"Connect a bot to Microsoft Teams", last updated **2024-10-09**.
<https://learn.microsoft.com/en-us/azure/bot-service/channel-connect-teams>
Relied on for: Channels → Microsoft Teams, terms of service, the Messaging tab
cloud-environment selection, Calling tab is for calling bots, Publish tab is for
the Store, **Get bot embed code** and the `https://teams.microsoft.com/l/chat/0/0?users=28:...`
form, "Adding a bot by GUID, for anything other than testing purposes, isn't
recommended", "Use one bot channel registration per environment", and the
warning that deleting the Teams channel regenerates keys and invalidates stored
`29:xxx` / `a:xxx` IDs.

**M5 — Channels live under Settings.**
<https://learn.microsoft.com/en-us/azure/bot-service/bot-service-manage-channels>
"In the left pane, select **Channels** under **Settings**."

**M6 — OAuth connection settings location and fields.**
<https://learn.microsoft.com/en-us/azure/bot-service/bot-builder-authentication>
"Go to the bot's **Configuration** blade" and "Under **OAuth Connection
Settings** near the bottom of the page, select **Add Setting**."

**M7 — Teams SSO scope limits, consent, and fallback.**
<https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/authentication/bot-sso-overview>
Relied on for: "SSO for a bot app in Teams is supported in one-on-one and group
chat scope, and **not supported in channel scope**"; the token-exchange flow via
the Bot Framework Token Service; first-use consent; and the documented fallback
to a sign-in prompt when consent fails.

**M8 — Application ID URI form for a standalone bot.**
<https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/authentication/bot-sso-register-aad>
"Standalone bot: If you're building a standalone bot, enter the application ID
URI as `api://botid-{YourBotId}`." Also confirms a **client secret** is part of
the required Entra app configuration for bot SSO.

**M9 — OBO requires a confidential client credential.**
<https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow>
"There are two cases depending on whether the client application chooses to be
secured by a shared secret or a certificate", and the request "is made by a
confidential client". Also: "the OBO flow only works for user principals."

**M10 — Teams informative updates during streaming.**
<https://learn.microsoft.com/en-us/microsoftteams/platform/bots/streaming-ux>
"Informative updates appear in the streamed message bubble and inform the user
about the agent's ongoing actions while a response is being generated. The text
remains visible until the next informative update or streamed content replaces
it." Plus: informative messages must be ≤1 KB / 1000 characters; streaming is
supported **only in one-on-one chats**; **one concurrent streaming response per
chat**.

**M11 — Custom app upload policy in Teams admin center.**
<https://learn.microsoft.com/en-us/microsoftteams/teams-custom-app-policies-and-settings>
Teams admin center → **Teams apps → Setup policies** carries the **Upload custom
apps** toggle; there is also an org-wide custom-app setting; per-team settings
interact with both.

### Google

**G1 — `principalSubject` is the field for federated identities.**
<https://cloud.google.com/logging/docs/reference/audit/auditlog/rest/Shared.Types/AuditLog>
`principalEmail`: "For third party identity callers, the `principalSubject`
field is populated instead of this field."
`principalSubject`: "For most identities, the format will be
`principal://iam.googleapis.com/{identity pool name}/subject/{subject}`."
**This is the single most important citation in runbook 12.** It is also exactly
the shape of the string the live `SESSION_USER()` probe returned, which is
corroboration from a second, independent system.

**G2 — Data Access audit logs are off by default, except some BigQuery.**
<https://cloud.google.com/logging/docs/audit/configure-data-access>
"Data Access audit logs are disabled by default for all services but some
BigQuery services." Also: "BigQuery Data Access audit logs can't be disabled",
and DATA_READ / DATA_WRITE "disabled by default and must be enabled".

**G3 — Agent Platform / Vertex AI audited operations.**
<https://cloud.google.com/vertex-ai/docs/general/audit-logging>
Service name `aiplatform.googleapis.com`. `sessions.create`, `sessions.update`,
`sessions.delete`, `sessionEvents.append` are **DATA_WRITE**. `sessions.get`,
`sessions.list`, `sessionEvents.list` are **DATA_READ**.
Note the page is titled around "Agent Platform" — the Vertex AI naming is in
flux, and the doc URL still says `vertex-ai`.

**G4 — BigQuery audited methods.**
<https://cloud.google.com/bigquery/docs/reference/auditlogs>
`google.cloud.bigquery.v2.JobService.InsertJob (LRO)` and
`google.cloud.bigquery.v2.JobService.Query (LRO)` require **ADMIN_WRITE**
permissions, so they produce **Admin Activity** audit logs — which are always
enabled. Service name filter `protoPayload.serviceName="bigquery.googleapis.com"`.

**G5 — Caller identity redaction.**
<https://cloud.google.com/logging/docs/audit>
"Audit logging doesn't redact the caller's principal email address for any
access that succeeds or for any write operation. For read-only operations that
fail with a 'permission denied' error, Audit logging **might redact** the
caller's principal email address unless the caller is a service account."
Also notes BigQuery-specific redaction conditions.
This is why runbook 12 §6.4 tells you to test the *denial* log line in rehearsal
rather than assuming it will name the analyst.

**G6 — Cloud Run public access.**
<https://cloud.google.com/run/docs/authenticating/public>
Two supported approaches: disable the Cloud Run Invoker IAM check
(Google's recommended option), or grant `roles/run.invoker` to `allUsers`.

**G7 — Cloud Run request timeout.**
<https://cloud.google.com/run/docs/configuring/request-timeout>
"The timeout is set by default to 5 minutes." Set with
`gcloud run services update SERVICE --timeout=TIMEOUT`.

**G8 — `gcloud logging read` flags.**
<https://cloud.google.com/sdk/gcloud/reference/logging/read>
`--freshness` defaults to `1d` and works only with DESC ordering; `--order`
defaults to `desc`; `--limit`, `--project`, `--format` as used.

**G9 — STS requirements for the workforce provider** (inherited from
`entra/NOTES.md`, retrieved the same day):
<https://cloud.google.com/iam/docs/reference/sts/rest/v1/TopLevel/token>
`aud` must match the provider's client ID; `alg` must be `RS256` or `ES256`.
Relevant to why the App A app type and the single-issuer trust matter.

---

## 3. Inherited from verified work elsewhere in this repo

Not verified by this author; recorded as live results by the work that produced
them. Treat as facts, cite the source if challenged.

| Fact | Source |
| --- | --- |
| `SELECT SESSION_USER()` as the federated analyst returns `principal://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/subject/<ANALYST_OBJECT_ID>` | `spikes/FINDINGS.md`, `bigquery/README.md` |
| The managed BigQuery MCP server runs queries as the bearer token's identity, with no service-identity substitution | `spikes/FINDINGS.md` (Layer 1, 2026-09-07) |
| A workforce principal can drive Agent Runtime: `reasoningEngines.list`, `.get`, `sessions.create` all returned 200; `sessions.create` accepted `userId = entra:{tid}:{oid}` | Task brief / Agent Runtime spike |
| Domain-restricted sharing in org `<GCP_ORG_ID>` already permits workforce principals; no org policy change needed | `spikes/FINDINGS.md` |
| `reasoningEngines/<OTHER_ENGINE_ID_1>` is a pre-existing, unrelated `data_science_agent` | Task brief |

---

## 4. Inferred, reasoned, or explicitly unverified

Everything in this section is a claim the runbooks make that is **not** backed
by a retrieved document or a recorded live result. Check each in rehearsal.

**I1 — "A managed-identity bot cannot perform the OBO exchange."**
Reasoning, not a quoted sentence. Built from: OBO requires a confidential client
authenticating with a shared secret or certificate (M9); a user-assigned managed
identity does not hand you such a credential to present from a non-Azure
process; and the middle tier runs on Cloud Run, where there is no Azure Instance
Metadata Service to acquire a managed-identity token from at all. The conclusion
is sound but Microsoft does not state it in one place. If someone challenges it,
argue from M9, not from a doc that says "managed identity bots can't do OBO".

**I2 — "Recreate the bot resource rather than change its app type."**
Reasoning. Microsoft documents the app type as a creation-time choice and the
`az bot` surface does not present it as an ordinary update. The advice is
conservative on purpose: a partially-changed registration produces intermittent
401s that cost more to diagnose than a recreate costs to perform. Not a
documented prohibition.

**I3 — Exact portal blade paths.**
"Settings → Configuration" and "Settings → Channels" match the wording in M5 and
M6 as of retrieval. Microsoft moves these. Both runbooks give the stable ARM
property (`properties.endpoint`, `properties.msaAppType`) so you can navigate by
`az bot show`/`az bot update` if the label has moved again.

**I4 — The `az bot show --query` JMESPath expressions.**
Written against the documented `Microsoft.BotService/botServices` property
names. **Not executed.** If a key comes back `null`, run
`az bot show -o json` and read the actual shape rather than assuming the
resource is misconfigured.

**I5 — The Agent Runtime audit `methodName`.**
**Unknown and deliberately left as a placeholder.** G3 gives the *permission*
names (`sessions.create` and so on), which are not the same strings as the
`protoPayload.methodName` values that appear in log entries. Runbook 12 §1.10
tells you to discover them with a broad `serviceName`-only query in rehearsal
and write them down. Do not guess a
`google.cloud.aiplatform.v1beta1.SessionService.CreateSession`-shaped string on
stage.

**I6 — The Agent Runtime session list/filter REST call in runbook 12 §5.**
The URL shape and `filter=user_id="..."` syntax are **unverified**. The runbook
says so at the point of use and tells you to confirm or cut. It also flags that
running it with `gcloud auth print-access-token` uses the operator's identity,
not the analyst's, which must be said aloud if shown.

**I7 — Whether `principalSubject` is redacted on permission-denied reads.**
G5 documents possible redaction of `principalEmail` for failed read-only
operations. Whether an equivalent applies to `principalSubject` for a workforce
principal is **not documented and not tested**. Runbook 12 §6.4 makes this a
rehearsal check with a defined fallback.

**I8 — Audit log delivery latency.**
No published guarantee was found. The runbook therefore gives no number, uses a
generous `--freshness=30m`, sequences the demo so several minutes elapse before
the log query, and tells the presenter to pre-announce the lag. If you want a
number, measure it in rehearsal and write it on the crib sheet.

**I9 — The 8-second latency threshold for "demo risk".**
An engineering judgement derived from M3's 10–15 second range, not a documented
figure. It is a margin, not a limit.

**I10 — Which specific denied resource to use for the failure demo.**
Not chosen here, because choosing it requires reading the actual IAM state of
`<GCP_PROJECT_ID>`. Runbook 12 §6.1 makes it a rehearsal task with an explicit warning
about the difference between a row-level empty result and a genuine 403.

**I11 — Expected reply text and behaviour in Teams.**
Every "what you should see" in both runbooks is a prediction from reading the
middle tier's templates and router, not an observation. The template opening
line quoted in runbook 12 §6.2 — "**Access denied — I stopped here rather than
working around it.**" — is read from `middle_tier/app/errors.py` as it stands
today. If the templates change, the runbook is stale.

**I12 — Whether the analyst's Teams tenant policy actually permits custom app
upload.** M11 documents the toggles; their current state in this tenant was not
inspected.

---

## 5. Dependencies on work items not yet complete

Both runbooks assume things that, as of writing, are not finished. Each is a
place where a presenter could follow the instructions perfectly and still fail.
They are ordered by how badly they break the demo.

**D1 — App A does not exist yet.**
`entra/01_bot_app_registration.md` is a runbook, not a record; its own NOTES
state that no portal step in it was performed. Runbook 11 Step 1 cannot start
until App A exists with the exposed `access_as_user` scope, the
`api://botid-<APP_A_CLIENT_ID>` Application ID URI, the pre-authorized Teams
client IDs, and a client secret in Secret Manager.
*Blocks:* all of runbook 11.

**D2 — The Cloud Run service is not created by Terraform.**
`terraform/NOTES.md` records that the plan produces "No Cloud Run service,
because `var.bot_image`" — the Cloud Run resource is behind a `count = 0` until
an image exists. So `<CLOUD_RUN_SERVICE>` and `<CLOUD_RUN_URL>` do not exist
until someone builds and deploys the middle-tier container.
*Blocks:* runbook 11 Steps 0, 2, 3, 6, 7, 8; all Cloud Run log commands in
runbook 12.

**D3 — The middle tier's collaborators may not be wired.**
`middle_tier/app/routing.py` declares `Dependencies` with `identity_broker`,
`sessions`, `runtime` and `renderer` **all optional and defaulting to `None`**,
each owned by a different worker. Where an implementation is absent the router
degrades to a clearly-labelled no-op and returns `transient_failure` with
`request_id="components-not-wired"` rather than fabricating an answer. That is
correct behaviour and a useless demo.
*Check before demoing:* send one message and confirm you do **not** see
`components-not-wired` in the reply or the logs.
*Blocks:* every answer in runbook 12.

**D4 — The streaming renderer is explicitly "NOT built".**
`middle_tier/app/ports.py` marks `StreamingRenderer` — "Teams streaming / card
rendering" — as **NOT built** in its ownership map. The "Querying BigQuery…"
informative update in runbook 12 §3.1 depends on it.
*Consequence if it is still unbuilt on the day:* the answer arrives with no
intermediate update. The demo still works; you lose the most visually
convincing evidence that a tool call really happened, and you lose the cover for
the query latency. Rehearse the narration both ways.
*Also affected:* the "acknowledge fast, update later" mitigation for the 10–15
second reply window (runbook 11 §8). Without a renderer there is no
"later" — the whole turn must fit inside the window.

**D5 — The BigQuery demo dataset is being built in parallel, and its admin half
is placeholder-blocked. Confirmed unfilled as of 2026-09-07.**
`bigquery/03_seed_data.sql` and `04_row_access_policies.sql` still carry the
literal token `M365_ADMIN_OID` inside the principal URIs — this was checked in
the working tree, not inferred. `apply.sh` refuses to run until it is filled
(`ALLOW_PLACEHOLDER=1` applies the analyst half only). Note the token is the
bare string `M365_ADMIN_OID`, with no angle brackets, and the intended fix is a
`sed -i` across both files.
*Consequence:* version B of the question ("what is our total pipeline?") is not
available, or is available for one user only. Version A, the `SESSION_USER()`
probe, needs none of this and is why it is the default.
*Also:* `CREATE OR REPLACE TABLE` in `02_tables.sql` silently drops row access
policies, so `04` must always be re-run after `02`. Runbook 12 §1.6 checks for
this because the failure mode — both users seeing all rows — looks like success.

**D6 — The admin's end-to-end federation path is unverified.**
Only `analyst@<TENANT_DOMAIN>` has a recorded live `SESSION_USER()`
result. `<M365_ADMIN_OBJECT_ID>` is not even known yet. Nothing about the analyst's
success implies the admin's will work: different user, possibly different
consent state, and the row access policies for the admin have never been
exercised.
*This is the most likely single point of failure on the day.* Runbook 12 §1.5
makes it a pre-flight item.

**D7 — Data Access audit logs for `aiplatform.googleapis.com` are probably off.**
Per G2 they are disabled by default. Nobody has confirmed they are enabled on
`<GCP_PROJECT_ID>`. Enabling them is not retroactive, so a demo-morning fix produces
an empty table for anything that happened before the fix.
*Blocks:* runbook 12 §4.6. The BigQuery half (§4.3–4.5) is unaffected because
those logs cannot be disabled (G2, G4).

**D8 — The Teams app package and its manifest.**
`entra/03_teams_app_manifest.md` plus icons. Without an installed app package
there is no Teams SSO, so runbook 11 §7b and all of runbook 12 fall back to the
identity-failure path. The embed-code chat (runbook 11 §4a) proves the wire but
not the identity.

**D9 — The OBO exchange itself.**
`entra/02_federation_app_obo.md`. Runbook 11 configures the plumbing that
carries the SSO token to the middle tier; it does not implement or verify the
exchange from that token to App B to Google STS.

**D10 — Which reasoning engine is "ours".**
`<REASONING_ENGINE_ID>` is unfilled because our agent has not been deployed, or
its ID has not been recorded anywhere in this repo. The only reasoning engine
whose ID is known is the one nobody may touch. This is a small thing that will
cost someone twenty minutes on the day if it is not written down first.

---

## 6. Things most likely to be wrong by the time you read this

Ranked by probability, so a reader in a hurry knows where to spend their
scepticism.

1. **Azure portal labels.** Microsoft renamed Azure AD to Entra ID, "Azure Bot
   Service" to "Azure AI Bot Service", moved settings under a Settings group,
   and is currently pushing the Microsoft 365 Agents SDK as the successor to the
   Bot Framework SDK. Any of the paths in runbook 11 could have moved. The
   stable ARM property names will not have.
2. **The Agent Runtime / Vertex AI surface.** Docs are mid-rename to "Agent
   Platform", the session API is `v1beta1`, and method names in audit logs are
   the least stable thing in either runbook. This is why I5 and I6 are left as
   rehearsal tasks.
3. **The middle tier's templates and log field names.** Runbook 11's log
   filters match on `jsonPayload.message` strings like
   `"inbound activity rejected"` and `"middle tier initialised"`. Those are
   source-level strings, and a refactor renames them without breaking a test.
4. **Multi-tenant availability.** M1's deprecation date has passed. Whether the
   portal still offers the option in this subscription is unknown; the runbook
   tells you not to pick it either way.
5. **The BigQuery demo dataset.** It is being built in parallel with this
   runbook. Check `bigquery/NOTES.md` for what was actually executed before
   trusting §1.6 and §1.7.
