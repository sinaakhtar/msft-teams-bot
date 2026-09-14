# 12 — The two-user demo

**The claim you are making, in one sentence:** two people ask this bot the
identical question in Microsoft Teams, get different answers, and Google Cloud's
audit log names each of them individually — because the bot never has an
identity of its own to fall back on.

**The proof is not the answer on screen. It is the audit log entry.** The
answers can be faked by any competent prompt engineer. A Cloud Audit Log entry
whose `authenticationInfo.principalSubject` resolves to a specific Entra object
ID cannot. Budget your stage time accordingly: the log is the payoff, not the
epilogue.

**Nothing on this page has been executed.** No demo run described here was
performed, no log entry quoted here was observed. Field names and filter syntax
come from current documentation, cited inline and listed in
[NOTES.md](NOTES.md). Everything else is a plan you must rehearse.

**Rehearse this end to end at least once, the day before, in the room if you
can.** Not "check the pieces work" — run the actual script, including the audit
log commands, including the failure demo. The first time you run
`gcloud logging read` with a filter that returns nothing should not be in front
of an audience.

---

## Cast and constants

| Who | Identity | Google account? |
| --- | --- | --- |
| **Analyst** | `analyst@<TENANT_DOMAIN>`, Entra oid `<ANALYST_OBJECT_ID>` | **None.** No Google account of any kind. This is the point. |
| **Admin** | `m365-admin@<TENANT_DOMAIN>`, Entra oid `<M365_ADMIN_OBJECT_ID>` | Not required |

The analyst's federated Google principal, **verified live** through the managed
BigQuery MCP server:

```
principal://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/subject/<ANALYST_OBJECT_ID>
```

That string is the whole demo in one line. It is a Google Cloud principal, it
came from a Microsoft identity, and no Google account exists behind it.

| Constant | Value |
| --- | --- |
| GCP project | `<GCP_PROJECT_ID>` (number `<GCP_PROJECT_NUMBER>`) |
| Region | `us-central1` |
| Workforce pool | `locations/global/workforcePools/teams-bot-demo`, provider `entra` |
| Entra tenant | `<ENTRA_TENANT_ID>` |
| Demo dataset | `<GCP_PROJECT_ID>.teams_bot_demo` |
| Session `user_id` format | `entra:{tid}:{oid}` (ADR 003) |

Placeholders to fill before the demo: `<M365_ADMIN_OBJECT_ID>`,
`<CLOUD_RUN_SERVICE>`, `<CLOUD_RUN_URL>`, `<REASONING_ENGINE_ID>`,
`<PRINCIPAL_ADMIN>` (the admin's full `principal://` URI, which is the analyst's
with the oid swapped).

> `reasoningEngines/<OTHER_ENGINE_ID_1>` is a pre-existing `data_science_agent`
> that belongs to someone else. It is not ours. Do not point the demo at it and
> do not touch it — including in a log filter you paste on stage.

---

## 1. Pre-flight checklist

Run this list the day before **and** again 30 minutes before. Every line has a
check and a failure mode. Nothing here is optional; each one has a plausible way
of silently breaking between rehearsal and showtime.

### 1.0 The bot's collaborators are actually wired

```bash
# Send one message from Teams first, then:
gcloud logging read \
  'resource.type="cloud_run_revision"
   resource.labels.service_name="<CLOUD_RUN_SERVICE>"
   jsonPayload.request_id:"not-wired"' \
  --project <GCP_PROJECT_ID> --freshness=15m --limit=5 \
  --format='table(timestamp, jsonPayload.message, jsonPayload.request_id)'
```
**Pass:** no rows.
**Fail:** any row. `components-not-wired` means the identity broker, session
manager or runtime client is missing; `session-manager-not-wired` means `/new`
will not work.
**Failure mode if you skip it:** the bot replies politely to everything and
answers nothing, and you will spend the first two minutes of the demo thinking
it is a Teams problem.
**This is the first check for a reason.** As of writing, all four collaborator
implementations are marked NOT built in the middle tier's ownership map — see
[NOTES.md](NOTES.md) D3. If that is still true, **this runbook is not yet
runnable** and no amount of Azure configuration will change that.

### 1.1 The bot answers at all

```bash
curl -sS <CLOUD_RUN_URL>/readyz
```
**Pass:** `{"status": "ready", ...}`.
**Fail:** 503 with a `checks` object, or no response.
**Failure mode if you skip it:** you find out during the opening line.
**Fix:** [11_azure_bot_service.md](11_azure_bot_service.md), Step 0.

### 1.2 The instance is warm and will stay warm

```bash
gcloud run services describe <CLOUD_RUN_SERVICE> --project <GCP_PROJECT_ID> \
  --region us-central1 \
  --format='value(spec.template.metadata.annotations["autoscaling.knative.dev/minScale"])'
```
**Pass:** `1` or more.
**Fail:** empty or `0`.
**Failure mode:** the first message of the demo hits a cold start and blows the
channel's 10–15 second reply window, so your opening turn is the one that
errors. See runbook 11's timeout section.
**Fix:** `gcloud run services update <CLOUD_RUN_SERVICE> --project <GCP_PROJECT_ID> --region us-central1 --min-instances=1`

### 1.3 Both users have consented, and neither will see a consent prompt on stage

Send one throwaway message as **each** user, from the client they will use on
the day. Teams SSO requires per-user consent on first use.
**Failure mode:** a consent dialog appears mid-demo. Not fatal, but it derails
the narrative into "so you do have to log in" — which is exactly the objection
you are trying to pre-empt.

### 1.4 Both users are in a **one-to-one** chat with the bot, not a channel

Teams SSO for bots is supported in one-on-one and group chat scope and **is not
supported in channel scope**
(<https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/authentication/bot-sso-overview>).
**Failure mode:** every turn hits the ADR 004 identity-failure path and you
demo the failure case by accident, before you have framed it.

### 1.5 The federation chain works for each user, independently

The analyst has been verified live. The admin has **not** — that verification is
still outstanding and it is the single most likely thing to be broken on the
day, because nothing in the analyst's success implies it. Ask each user to send
the identity probe question (section 2) and confirm two different answers come
back. Do this from the actual Teams clients, not a script.

### 1.6 The demo dataset and its row access policies exist

```bash
bq --project_id=<GCP_PROJECT_ID> ls teams_bot_demo
bq --project_id=<GCP_PROJECT_ID> query --nouse_legacy_sql \
  'SELECT * FROM `<GCP_PROJECT_ID>.teams_bot_demo.INFORMATION_SCHEMA.ROW_ACCESS_POLICIES`'
```
**Pass:** the tables from `bigquery/02_tables.sql` are listed and at least one
row access policy exists on `sales_opportunities`.
**Fail / empty:** the richer version of the demo is not available. Fall back to
the `SESSION_USER()` probe, which needs no dataset at all.
**Watch out:** `CREATE OR REPLACE TABLE` silently drops every row access policy
on the table. If anyone re-ran `02_tables.sql` without re-running
`04_row_access_policies.sql`, the policies are gone and **both users will see
all rows** — a demo that appears to work and proves the opposite of your claim.
Verify the policies, not just the tables.

### 1.7 The row access policies compare `principal://` URIs, not email addresses

Read `04_row_access_policies.sql` and confirm the predicate compares against the
full `principal://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/subject/{oid}`
form.
**Failure mode:** a predicate written against `analyst@<TENANT_DOMAIN>`
matches zero rows, so the analyst sees an empty result and you conclude the
identity is broken when in fact it is working perfectly. This is documented in
`bigquery/README.md` as the single most common way to get this wrong.

### 1.8 `<M365_ADMIN_OBJECT_ID>` is filled in everywhere

```bash
grep -rn 'M365_ADMIN_OID\|<M365_ADMIN_OBJECT_ID>' bigquery/ || echo 'clean'
```
**Pass:** `clean`.
**Fail:** the seed data and/or policies still carry the placeholder, in which
case the admin half of the demo has no rows behind it. `apply.sh` refuses to run
with the placeholder unfilled, so an unfilled placeholder usually means the
scripts were never fully applied.

Get the value with:
```bash
az ad user show --id m365-admin@<TENANT_DOMAIN> --query id -o tsv
```

### 1.9 Audit logging is actually capturing what you plan to show

Two different situations, and only one of them is safe by default.

**BigQuery: safe.** BigQuery's Data Access audit logs cannot be disabled
(<https://cloud.google.com/logging/docs/audit/configure-data-access>), and job
submission (`google.cloud.bigquery.v2.JobService.InsertJob`,
`google.cloud.bigquery.v2.JobService.Query`) requires an `ADMIN_WRITE`
permission, so it lands in **Admin Activity** logs, which are always on
(<https://cloud.google.com/bigquery/docs/reference/auditlogs>).

**Agent Runtime: not safe by default.** Agent Platform / Vertex AI
`sessions.create`, `sessions.update`, `sessions.delete` and
`sessionEvents.append` are **DATA_WRITE**, and `sessions.get`, `sessions.list`,
`sessionEvents.list` are **DATA_READ**
(<https://cloud.google.com/vertex-ai/docs/general/audit-logging>). Data Access
audit logs are **disabled by default for all services but some BigQuery
services** (<https://cloud.google.com/logging/docs/audit/configure-data-access>).
So unless someone has explicitly enabled them for
`aiplatform.googleapis.com`, **there will be no Agent Runtime entries to show.**

Check:
```bash
gcloud projects get-iam-policy <GCP_PROJECT_ID> --format='yaml(auditConfigs)'
```
**Pass:** an entry for `aiplatform.googleapis.com` with `DATA_READ` and
`DATA_WRITE` log types (or an `allServices` entry covering them).
**Fail:** no such entry.
**Fix:** enable them in the console at **IAM & Admin → Audit Logs**, select
**Vertex AI API** / Agent Platform, tick **Data Read** and **Data Write**, save.
Do this well in advance: enabling data access logs takes effect going forward,
not retroactively, and you cannot show a call made before you turned it on.

Prefer the console for this. `gcloud projects set-iam-policy` replaces the
entire policy from a file and is an easy way to remove someone's bindings by
accident the day before a demo.

**Cost note before you enable it:** Data Access logs on a busy project generate
volume and Cloud Logging charges for ingestion above the free allotment.
`<GCP_PROJECT_ID>` is a dev project so this is unlikely to matter, but say so to
whoever owns the billing rather than surprising them.

### 1.10 The audit log commands you will run actually return rows

This is the check people skip, and it is the one that ruins the demo. Send a
message as the analyst, wait a minute, then run the exact `gcloud logging read`
commands from section 4 — copy-pasted from this file, not retyped. If they
return nothing, debug it now while it costs you fifteen minutes rather than
your credibility.

Confirm in particular that the **method name** your filter matches is the one
Agent Runtime actually emits. Discover it rather than assuming:

```bash
gcloud logging read \
  'protoPayload.serviceName="aiplatform.googleapis.com"' \
  --project <GCP_PROJECT_ID> --freshness=1h --limit=20 \
  --format='table(timestamp, protoPayload.methodName, protoPayload.authenticationInfo.principalSubject, protoPayload.authenticationInfo.principalEmail)'
```

Write the observed `methodName` values into your crib sheet. The exact string
for session creation on Agent Runtime is **not** asserted in this runbook — see
[NOTES.md](NOTES.md).

### 1.11 Your terminal is legible from the back row

Font size up, prompt shortened, `clear` between commands, colours that survive
a projector. Have every command pre-typed in a scratch file so you paste rather
than type. A 200-character `gcloud logging read` typed live is dead air, and a
typo in a filter returns zero rows with no error, which looks like the system
failing.

### 1.12 Two screens, or a rehearsed alt-tab

You need Teams and a terminal visible. If you have one screen, rehearse the
switch. Do not screen-share only Teams and then narrate the logs from memory.

---

## 2. The question

Pick one. Have the other ready as a fallback.

### Version A — the identity probe. Zero setup, cannot break.

> **"Who am I to BigQuery?"**

The agent runs `SELECT SESSION_USER()`. Each user gets back their own federated
principal URI.

- Analyst sees:
  `principal://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/subject/<ANALYST_OBJECT_ID>`
- Admin sees the same string with `<M365_ADMIN_OBJECT_ID>` in the subject position.

**Why this version is good:** it needs no dataset, no row access policies, no
seed data. It is the version that survives someone re-running `02_tables.sql`
the night before. And the answer is *self-evidently* not something the model
could have invented — it is a Google Cloud resource identifier containing a
Microsoft object ID.

**Why it is not enough on its own:** a hostile audience will say "so it prints a
string, so what". You need version B, or the audit log, or both, to land the
business point.

**Verified:** this probe was confirmed live through the managed BigQuery MCP
server for the analyst. It returned exactly the URI above.

### Version B — the business question. Unmistakable on screen.

> **"What is our total pipeline?"**

Identical question, identical SQL, two different numbers, because BigQuery
row-level security filters rows against the caller's federated principal before
the agent ever sees them. The seed data in `bigquery/03_seed_data.sql` is
deliberately lopsided so the difference is a glance, not a squint.

**Say the number difference out loud.** "The analyst sees 
`<ANALYST_TOTAL>`. The admin sees `<ADMIN_TOTAL>`. Same question, same bot,
same SQL." Fill those in during rehearsal; do not read them off the screen for
the first time on stage.

### Version B+ — the follow-up that lands with a security audience

Have the analyst ask for one of the admin's deals **by name**:

> **"Tell me about the `<ADMIN_ONLY_DEAL_NAME>` deal."**

They get an empty result. Not an error. Not a refusal. The row simply does not
exist as far as their session is concerned.

This is the moment worth spending thirty seconds on: **there is nothing for a
prompt injection to override, because the model was never given the hidden rows
in the first place.** The filtering happens in BigQuery, below the agent. Every
"ignore your previous instructions" attack in the room's imagination operates on
a context window that does not contain the data.

### What NOT to ask

- Anything whose answer depends on the model's general knowledge. If the bot
  could plausibly have answered from training data, you have proved nothing.
- Anything requiring a long chain of tool calls. Every extra second is a second
  closer to the channel timeout.
- Anything ambiguous enough that the two answers might differ for reasons of
  phrasing rather than authorization. Ask the *same* question, character for
  character. Have it in your paste buffer.

---

## 3. Run of show

Total: roughly 12 minutes at a comfortable pace. Section 7 has the cut-down.

### 3.0 Frame it before you touch anything (60 seconds)

Say the claim before you demonstrate it, so the audience knows what to watch
for:

> "This is a Teams bot talking to an agent running on Google Cloud. The person
> using it has a Microsoft account and no Google account at all. I'm going to
> show you two people asking the same question and getting different answers,
> and then I'm going to show you Google's own audit log naming each of them."

Then show the analyst is a Microsoft-only identity. One line, on screen:

```bash
gcloud projects get-iam-policy <GCP_PROJECT_ID> \
  --flatten="bindings[].members" \
  --filter="bindings.members~teams-bot-demo" \
  --format='table(bindings.role, bindings.members)'
```

Point at the `principal://` and `principalSet://` members. "No user accounts.
No service account impersonating anybody. Those are Microsoft identities holding
Google Cloud IAM roles directly."

### 3.1 User 1 — the analyst (2 minutes)

1. Analyst opens their 1:1 Teams chat with the bot. Show the Teams avatar in the
   corner so the audience can see who is signed in. This matters — half the
   scepticism in the room is "how do I know that's a different user".
2. Paste the question. Send.
3. **Watch for the informative update.** The streamed message bubble should show
   something like *"Querying BigQuery…"* while the tool call runs, then be
   replaced by the answer. Teams calls these **informative updates**: they
   "appear in the streamed message bubble and inform the user about the agent's
   ongoing actions while a response is being generated", and remain visible
   until the next update or the streamed content replaces them
   (<https://learn.microsoft.com/en-us/microsoftteams/platform/bots/streaming-ux>).

   **Call it out.** That update is the visible proof that tool activity is real
   and is happening now — the difference between an agent that queried a
   database and a chatbot that produced a plausible number. It is also your
   cover for the two or three seconds the query takes.

   Two constraints to know: streaming agent messages are supported **only in
   one-on-one chats**, and Teams supports **one concurrent streaming response
   per chat**. Both are satisfied by this script; neither survives you demoing
   in a channel.
4. Read the answer out. Leave it on screen.

### 3.2 User 2 — the admin (2 minutes)

1. Switch to the admin's Teams client. **Show the avatar again.** Say the name.
2. Paste the identical question. Emphasise that it is identical — if you have it
   in a paste buffer, say so.
3. Same informative update, different answer.
4. Put the two answers side by side if your screen layout allows. If not, say
   the analyst's number again from memory as you read the admin's.

### 3.3 The follow-up, if you are running version B (1 minute)

Analyst asks for the admin's deal by name. Empty result. Deliver the line about
there being nothing for a prompt injection to override.

### 3.4 The audit log (4 minutes) — section 4

### 3.5 `/new` conversation reset (1 minute) — section 5

### 3.6 The failure demo (2 minutes) — section 6

---

## 4. The audit log evidence

This is the payoff. Slow down here.

### 4.1 The field, and why getting it wrong flattens the demo

For a federated workforce identity, **`authenticationInfo.principalEmail` is not
the field you want.** Google's `AuditLog` reference is explicit: `principalEmail`
is "the email address of the authenticated user (or service account on behalf of
third party principal) making the request. **For third party identity callers,
the `principalSubject` field is populated instead of this field.**" And
`principalSubject` is "a string representing the principalSubject associated
with the identity. For most identities, the format will be
`principal://iam.googleapis.com/{identity pool name}/subject/{subject}`"
(<https://cloud.google.com/logging/docs/reference/audit/auditlog/rest/Shared.Types/AuditLog>).

That format is exactly what came back from `SESSION_USER()` in the live probe.
Same identity, same string, two independent systems. That correspondence is
worth pointing at.

If you filter on `principalEmail` you will get zero rows, conclude the audit
trail is missing, and lose the room. If you `--format=json` without selecting
fields you will get a wall of text nobody can read. Select the fields.

### 4.2 Set the two principals as shell variables first

Do this **before** you present, in the terminal you will use. It keeps the
on-screen commands short and readable.

```bash
POOL="principal://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/subject"
ANALYST="$POOL/<ANALYST_OBJECT_ID>"
ADMIN="$POOL/<M365_ADMIN_OBJECT_ID>"
PROJ=<GCP_PROJECT_ID>
```

### 4.3 BigQuery: the analyst's query, attributed to the analyst

```bash
gcloud logging read \
  "protoPayload.serviceName=\"bigquery.googleapis.com\"
   protoPayload.authenticationInfo.principalSubject=\"$ANALYST\"" \
  --project "$PROJ" --freshness=30m --limit=5 \
  --format='table(
    timestamp,
    protoPayload.methodName,
    protoPayload.authenticationInfo.principalSubject,
    protoPayload.resourceName)'
```

**What to point at on screen:** the `principalSubject` column. Not the
timestamp, not the method. Say it plainly:

> "That is Google Cloud's audit log. The identity that ran this query is a
> Microsoft Entra object ID. There is no Google account here to point at,
> because there isn't one."

### 4.4 The same query, run as the admin

```bash
gcloud logging read \
  "protoPayload.serviceName=\"bigquery.googleapis.com\"
   protoPayload.authenticationInfo.principalSubject=\"$ADMIN\"" \
  --project "$PROJ" --freshness=30m --limit=5 \
  --format='table(
    timestamp,
    protoPayload.methodName,
    protoPayload.authenticationInfo.principalSubject,
    protoPayload.resourceName)'
```

Two commands, two different subjects, same `methodName`. That is the shape of
the evidence.

### 4.5 Both at once — the money shot

If you only run one log command, run this one. It puts both humans in one
table.

```bash
gcloud logging read \
  "protoPayload.serviceName=\"bigquery.googleapis.com\"
   protoPayload.authenticationInfo.principalSubject:\"workforcePools/teams-bot-demo\"" \
  --project "$PROJ" --freshness=30m --limit=20 \
  --format='table(
    timestamp.date("%H:%M:%S"),
    protoPayload.authenticationInfo.principalSubject,
    protoPayload.methodName)'
```

Note the `:` operator — substring match, not equality — so it catches every
subject in the pool. Two distinct values in that column, interleaved in time,
is the entire argument.

If the subject column is too wide for the projector, add a `sed` to trim the
common prefix:

```bash
... | sed 's|principal://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/subject/|oid:|'
```

Practise that. A one-line prefix strip turns an unreadable column into a
readable one.

### 4.6 Agent Runtime: the session call, attributed to the same human

Session operations on Agent Platform are audited: `sessions.create`,
`sessions.update`, `sessions.delete`, `sessionEvents.append` under **DATA_WRITE**
and `sessions.get`, `sessions.list`, `sessionEvents.list` under **DATA_READ**
(<https://cloud.google.com/vertex-ai/docs/general/audit-logging>), with service
name `aiplatform.googleapis.com`.

Start broad, because you should confirm the exact `methodName` string during
rehearsal rather than trusting a guess:

```bash
gcloud logging read \
  "protoPayload.serviceName=\"aiplatform.googleapis.com\"
   protoPayload.authenticationInfo.principalSubject=\"$ANALYST\"" \
  --project "$PROJ" --freshness=30m --limit=10 \
  --format='table(
    timestamp,
    protoPayload.methodName,
    protoPayload.authenticationInfo.principalSubject,
    protoPayload.resourceName)'
```

Once you know the method string from rehearsal, narrow it for the stage:

```bash
gcloud logging read \
  "protoPayload.serviceName=\"aiplatform.googleapis.com\"
   protoPayload.methodName:\"<OBSERVED_SESSION_METHOD>\"
   protoPayload.authenticationInfo.principalSubject:\"workforcePools/teams-bot-demo\"" \
  --project "$PROJ" --freshness=30m --limit=10 \
  --format='table(timestamp, protoPayload.methodName, protoPayload.authenticationInfo.principalSubject)'
```

**What this proves that the BigQuery log does not:** the user's identity is not
merely reaching the data layer, it is reaching the *agent* layer. The session
itself was created by the human, not by a service account acting on their
behalf. `resourceName` should contain your reasoning engine — check it is
`<REASONING_ENGINE_ID>` and not the unrelated `<OTHER_ENGINE_ID_1>`.

Verified separately, and worth saying: a workforce principal can drive Agent
Runtime — `reasoningEngines.list`, `reasoningEngines.get` and `sessions.create`
all returned 200, with `sessions.create` accepting `userId = entra:{tid}:{oid}`.

### 4.7 Log delivery lag — say it before someone notices

Audit log entries are not instantaneous. Cloud Logging does not publish a
delivery-latency guarantee for audit logs, and in practice entries usually
appear within seconds but can take longer under load.

Handle it like this:

- **Say it out loud in advance:** "these take a few seconds to land, so I'm
  going to run the query for the messages we sent a minute ago, not the one I
  just sent." Pre-empting it costs you one sentence. Being surprised by it costs
  you the room's confidence.
- **Structure the demo so the lag is absorbed:** do the two user turns, then the
  `/new` reset, *then* the logs. By the time you get to section 4 the entries
  from section 3 have had two or three minutes to arrive.
- **Use a generous `--freshness`.** `30m` above is deliberate. There is no prize
  for `--freshness=1m` and it is a great way to show an empty table.
- **Have a fallback.** Keep a Logs Explorer tab open, already filtered, from
  rehearsal. If the CLI returns empty, switch to it and keep talking. Do not
  re-run the same command three times while the room watches.

If a filter genuinely returns nothing, the diagnosis order is: (1) freshness too
short, (2) Data Access logs not enabled for that service — section 1.9, (3)
wrong field (`principalEmail` instead of `principalSubject`), (4) a typo in the
subject URI. Check them in that order.

---

## 5. `/new` — reset without destroying history

A small moment, thirty to sixty seconds, but it answers a question a governance
audience always asks: *if the user can clear the context, what happens to the
record?*

### The script

1. Analyst asks a question that establishes context. Something the next question
   depends on — e.g. ask about a specific deal, then ask "and what stage is it
   at?" so the audience sees the follow-up work.
2. Analyst types **`/new`**.
3. The bot replies with the conversation-reset template. Context is gone.
4. Analyst asks the dependent follow-up again — *"and what stage is it at?"* —
   with no antecedent. The bot no longer knows what "it" is.
5. Now the point: **the previous session was not deleted.** It is abandoned, and
   it remains retrievable by ID.

### What to say

> "That reset the conversation. It did not delete anything. The old session is
> still there, addressable by its ID, with every event in it. The user can clear
> their screen; they cannot clear the record."

### Showing it, if you want to go one level deeper

Per ADR 005, the middle tier creates and resolves sessions and never writes
events; the runtime appends them. So the old session is a live Agent Runtime
resource. Retrieve it by ID:

```bash
# List sessions for this user on our reasoning engine.
# user_id is entra:{tid}:{oid} per ADR 003.
curl -sS -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/<GCP_PROJECT_NUMBER>/locations/us-central1/reasoningEngines/<REASONING_ENGINE_ID>/sessions?filter=user_id=%22entra:<ENTRA_TENANT_ID>:<ANALYST_OBJECT_ID>%22"
```

Two honest caveats before you put this on screen:

- The exact list/filter syntax for Agent Runtime sessions is **not verified** in
  this runbook. Confirm it in rehearsal and paste the working command into your
  crib sheet, or cut this sub-step. See [NOTES.md](NOTES.md).
- Running it with `gcloud auth print-access-token` uses **your** credentials,
  not the analyst's — which is a different identity plane from the one you just
  spent five minutes demonstrating. If you show this, say so explicitly:
  "I'm listing this as an operator, not as the analyst." Do not let the audience
  think a Google admin credential is somehow part of the user flow. If that
  distinction is too fiddly to make cleanly in the time available, describe the
  behaviour and skip the command. Silence is better than a muddled claim.

Also worth noting: `sessions.list` and `sessionEvents.list` are audited as
DATA_READ, so your own retrieval of the abandoned session is itself in the audit
log. That is a nice closing beat if you have time — the operator who reads the
record leaves a record.

---

## 6. The failure demo

**This is the most persuasive part of the whole session.** Do not cut it. If you
are short on time, cut section 5 and keep this.

Everyone in the room has seen a demo where the agent answers. Nobody has seen a
demo where the agent *refuses correctly*. The refusal is what distinguishes a
system with real authorization from a system with a well-written prompt.

### 6.1 The setup

Have a user ask for something they are genuinely not entitled to. Pick the
resource during rehearsal and confirm the denial is a **403 from the downstream
system**, not a row-level filter.

The distinction matters and you should understand it before you present:

| What happens | What the user sees | Which mechanism |
| --- | --- | --- |
| Analyst asks for the admin's deal by name (section 2, version B+) | Empty result | Row-level security. The row was filtered before the agent saw it. |
| Analyst asks about a table or dataset they have no IAM access to | **Templated denial naming the refused resource** | ADR 004 downstream denial |

Both are worth showing, but the second is the one to spend time on. The first
looks like "no results found". The second looks like a system saying no.

Suggested: a dataset or table in `<GCP_PROJECT_ID>` that the workforce principal has
no `bigquery.dataViewer` on. Pick it in rehearsal, note the exact prompt that
reliably triggers it, and write both into your crib sheet.

### 6.2 What the audience sees

The bot replies with a fixed template, opening with:

> **Access denied — I stopped here rather than working around it.**

and naming the refused resource. Per ADR 004 the error is intercepted at the
tool boundary and rendered from a template; the model is informed only that
access was denied and to which resource.

### 6.3 What to say, and why it is the strongest slide in the deck

Make three points, in this order.

**1. This is a template, not a paraphrase.**

> "That message is a fixed template. The model did not write it. The model was
> told 'access was denied to this resource' and nothing else."

Why that matters: a language model asked to explain an authorization error has
no way to distinguish a missing IAM role from a missing dataset from a network
fault, and will produce a fluent, confident, and possibly fabricated reason.
Every one of those explanations sounds equally plausible, and one of them is
true. In a system whose entire value proposition is that authorization is real,
a fabricated explanation of an authorization failure is worse than no
explanation.

**2. There is no fallback identity. There is nowhere for this to degrade to.**

> "There is no service account behind this bot that could have answered. If we
> cannot establish who is asking, or the answer is no, the bot stops."

ADR 004 records "fall back to a service account" as an **explicitly rejected**
option — recorded as rejected rather than omitted, because it is the obvious
expedient fix and someone will propose it again. Say that out loud. It is the
sentence that separates this architecture from every "we'll fix it later"
integration in the room.

Falling back would mean the bot answers under an identity that is not the
user's. The data would be right. The audit log would be wrong. And the audit log
is the only thing anyone will have in twelve months when they need to know who
saw what.

**3. A bot that visibly stops working is the correct behaviour.**

This is the counter-intuitive point, so say it directly:

> "A bot that quietly widens its own privileges so the conversation can continue
> is not a working bot. It is a broken bot that has stopped telling you it is
> broken. This one fails closed, in front of you, and names the thing it was
> refused. That is the feature."

Then connect it to their world: the failure they just watched is the same code
path that runs at 3am when someone leaves the company and their Entra account is
disabled. The bot stops for them, immediately, without anyone having to
remember to revoke a separate Google account — because there never was one.

### 6.4 Do not rehearse this into a lie

Confirm during rehearsal that the denial really is a denial. If it turns out the
"denied" resource is actually accessible and the empty answer comes from
somewhere else, you are about to narrate a mechanism that did not fire. Check
the audit log for the denial too:

```bash
gcloud logging read \
  "protoPayload.serviceName=\"bigquery.googleapis.com\"
   protoPayload.authenticationInfo.principalSubject=\"$ANALYST\"
   protoPayload.status.code!=0" \
  --project "$PROJ" --freshness=30m --limit=5 \
  --format='table(timestamp, protoPayload.methodName, protoPayload.status.code, protoPayload.status.message, protoPayload.authenticationInfo.principalSubject)'
```

A non-zero `status.code` with a permission-denied message, attributed to the
analyst's `principalSubject`, is the denial as Google recorded it. Showing the
refusal in the log immediately after showing it in Teams is a strong pairing.

**Caveat to check in rehearsal:** Google's audit logging documentation notes
that for read-only operations failing with permission denied, "audit logging
might redact the caller's principal email address unless the caller is a service
account" (<https://cloud.google.com/logging/docs/audit>). That statement is
about `principalEmail`; whether any equivalent redaction applies to
`principalSubject` for a workforce principal is **not** something this runbook
can tell you. Run the command above during rehearsal. If the subject is
redacted, drop this sub-step and rely on the Teams-side denial plus the
successful-call log entries.

---

## 7. Recovery: what to do if federation breaks mid-demo

Be honest with yourself about this before you start: **ADR 004 means there is no
graceful degradation.** If the identity chain breaks, the bot stops. There is no
mode where it keeps answering with reduced fidelity, because that mode would be
a service account answering on the user's behalf, and that is precisely the
thing this system exists to not do.

So the recovery options are genuinely limited, and pretending otherwise on stage
will be obvious.

### What breaking looks like

Every turn returns the ADR 004 identity-failure message with a sign-in card. Not
an error, not a hang — a clear message saying identity could not be established.

### The only in-demo mitigation: the sign-in card

If you configured an OAuth connection setting (runbook 11, step 5a), the card is
clickable. Have the user click it, complete the sign-in, and retry the question.
This recovers a **consent or token-expiry** failure, which is the most common
transient cause. It does **not** recover a misconfigured provider, a revoked
secret, or an STS trust problem.

If you did not configure the connection, the card is decorative and there is no
in-demo recovery. Know which situation you are in before you walk on stage.

### Triage, in the order that finds it fastest

Thirty seconds, one command, while you keep talking:

```bash
gcloud logging read \
  'resource.type="cloud_run_revision"
   resource.labels.service_name="<CLOUD_RUN_SERVICE>"
   severity>=WARNING' \
  --project <GCP_PROJECT_ID> --freshness=5m --limit=10 \
  --format='table(timestamp, jsonPayload.message, jsonPayload.reason, jsonPayload.detail)'
```

| What you see | Cause | Recoverable live? |
| --- | --- | --- |
| `inbound activity rejected` with an auth `reason` | Bot Framework JWT validation, not federation at all | Sometimes — see runbook 11's 401 table |
| Identity failure logged at the OBO step | App A secret expired or revoked, or consent withdrawn | Only if consent: click the card. An expired secret is not a live fix. |
| Identity failure at the Google STS step | Provider config, audience, or attribute mapping | No. Not a live fix. |
| `transient_failure` with `components-not-wired` | A dependency was deployed without its collaborator | No. Wrong revision is live. |
| Nothing in the logs at all | Activities are not arriving | Runbook 11, "endpoint is not reachable" |

### If it is not recoverable in under a minute, stop trying

Have this decided in advance. Fumbling at a terminal for five minutes is worse
than any of these alternatives:

1. **Switch to the recorded fallback.** Record a screen capture of a successful
   rehearsal run — both users, both answers, the audit log commands. Two
   minutes of video. Say plainly: "the live environment is having an identity
   problem, here is the run from yesterday." An audience forgives a recording.
   They do not forgive ten minutes of debugging.
2. **Pivot to the architecture.** You still have the ADRs, the audit log entries
   from rehearsal (which are still in Cloud Logging and still queryable, subject
   to retention), and the `SESSION_USER()` result. You can show yesterday's log
   entries live — they are real, they are timestamped, and they name real
   humans.
3. **Make it the demo.** This is a genuine option and it is not a consolation
   prize. "You're watching the fail-closed behaviour, unrehearsed. The bot has
   lost its ability to establish who I am, and it has stopped. It has not
   guessed, it has not fallen back to a service account, and it has not answered
   from a cache. That is the behaviour we designed for and you are seeing it
   under real conditions." Then walk section 6's three points.

Option 3 is stronger than it sounds, but only if you say it *immediately* and
with conviction. It does not work as a rescue after four minutes of visible
panic. Decide in the first thirty seconds.

### Prevention beats recovery

- Do not change anything on demo day. Not a revision, not an IAM binding, not a
  secret, not a policy.
- Check the App A client secret expiry date **a week out**. An expired secret is
  the classic silent killer and there is no live fix.
- Send one throwaway message as each user 15 minutes before you start. This also
  warms the instance.
- Keep the rehearsal recording on the presenting machine, not in the cloud.

---

## 8. Timing

| Section | Minutes | Cuttable? |
| --- | --- | --- |
| 3.0 Framing + IAM policy showing Microsoft principals | 1.5 | Trim to one sentence |
| 3.1 Analyst asks | 2 | No |
| 3.2 Admin asks the identical question | 2 | No |
| 3.3 Version B+ follow-up (empty result, prompt-injection point) | 1 | Yes |
| 4 Audit log evidence | 4 | Reduce to one command (4.5) |
| 5 `/new` reset | 1 | Yes, first to go |
| 6 Failure demo | 2 | **No. Cut anything else first.** |
| Buffer for questions and lag | 2 | — |
| **Total** | **~15** | |

At a relaxed pace with an engaged audience interrupting, assume 20.

### If you only have 5 minutes

Cut to three beats. Nothing else.

1. **(2 min) Both users, same question, different answers.** Use version A, the
   `SESSION_USER()` probe, if the dataset is at all uncertain; version B if you
   are confident. Do not explain the architecture first — show it, then
   explain. Have both Teams windows already open and the question in the paste
   buffer.
2. **(2 min) One audit log command — section 4.5**, the combined one showing
   both subjects in one table. Point at the `principalSubject` column and say
   the one sentence: *"Google Cloud's audit log, naming two Microsoft identities
   that have no Google accounts."*
3. **(1 min) The failure.** Ask for something denied, show the templated refusal,
   and deliver point 3 from section 6.3: a bot that visibly stops working is the
   correct behaviour, because the alternative is a bot that quietly answered
   under somebody else's identity.

Skip framing, skip `/new`, skip the session retrieval, skip the IAM policy
listing. If you have exactly five minutes, the audit log command must already
be in your shell history and the Teams windows must already be open. Practise
the five-minute version separately — it is not the fifteen-minute version
spoken faster.

---

## 9. Crib sheet — fill this in during rehearsal

Print it. Do not trust your memory in front of an audience.

| Item | Value |
| --- | --- |
| Analyst's Teams window | |
| Admin's Teams window | |
| The question, verbatim | |
| Analyst's expected answer | |
| Admin's expected answer | |
| Admin-only deal name (version B+) | |
| Denied resource + exact prompt that triggers it | |
| Observed Agent Runtime `methodName` for session create | |
| `<M365_ADMIN_OBJECT_ID>` | |
| Observed audit log lag in rehearsal | |
| Fallback recording location | |
