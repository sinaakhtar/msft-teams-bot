# Microsoft Teams bot to Google Agent Runtime, authenticated as the user

A Microsoft Teams chatbot that relays a user's question to an ADK agent running
on Google's Agent Runtime, where the agent queries BigQuery **as that person**
rather than as a shared service account. Row-level security in BigQuery decides
what each user sees, and the Google Cloud audit log names the human who asked.

The point of the build is the identity plumbing, not the chatbot. Two users ask
the same question through the same bot, the same agent instance and the same
BigQuery table, and get different rows, because the query genuinely runs under
each user's own federated principal.

---

## How it works

```
Teams client
    |  Bot Framework activity + Teams SSO token
    v
Bot Middle Tier  (Cloud Run, FastAPI)
    |  1. validate the inbound Bot Framework JWT
    |  2. extract the user's Entra object ID (oid)
    |  3. OBO exchange: Teams SSO token -> federation app access token
    |  4. STS exchange: Entra ID token  -> Google workforce principal token
    |  5. create/resolve a session keyed on the oid
    v
Agent Runtime  (reasoning engine, ADK agent)
    |  per-user token passed in authorizations[], read at tool-call time
    v
BigQuery managed MCP server
    |  query executes as principal://.../subject/<oid>
    v
BigQuery table with row access policies
```

### The two identity planes

This is the central idea, and conflating the planes is the mistake the whole
design exists to prevent. See [ADR 002](docs/adr/002-two-identity-planes.md).

| | **Service plane** | **User plane** |
|---|---|---|
| Who it authenticates | the bot, to Microsoft and to Google | the human, to Google |
| Credential | Azure Bot app registration; Cloud Run service account | Entra OBO token exchanged for a Google workforce principal token |
| What it may do | run the process, read its own secrets, call the runtime | read data |
| Never used for | **reading a user's data** | infrastructure calls |

A service account is never a fallback for a failed user identity. When identity
cannot be established the turn is **refused**, with a sign-in card rather than a
degraded answer. That is [ADR 004](docs/adr/004-fail-closed-on-authorization-failure.md),
and the "just fall back to the service account so the demo keeps working"
expedient is recorded there as explicitly rejected because it will be proposed
again.

### Architecture decisions

| ADR | Decision |
|---|---|
| [001](docs/adr/001-address-agent-runtime-directly.md) | Address Agent Runtime directly, not the Gemini Enterprise assistant |
| [002](docs/adr/002-two-identity-planes.md) | Two identity planes, never collapsed |
| [003](docs/adr/003-session-user-key-is-entra-object-id.md) | Session user key is the Entra object ID |
| [004](docs/adr/004-fail-closed-on-authorization-failure.md) | Fail closed on authorization failure; never explain a denial with the model |
| [005](docs/adr/005-runtime-interface-contract.md) | Consume ADK events; own session lifecycle via the REST subresource |

### Repository layout

| Path | What it is |
|---|---|
| `docs/adr/` | The five architecture decisions |
| `terraform/` | Workforce identity pool, provider, IAM, APIs, Cloud Run |
| `bigquery/` | Demo dataset, seed data and row access policies (SQL templates) |
| `entra/` | Click-by-click Microsoft Entra setup, plus the Teams app manifest |
| `agent/` | The ADK agent and its Agent Runtime deploy script |
| `middle_tier/` | The Cloud Run service: JWT validation, identity broker, sessions, streaming, errors |
| `runbook/` | Azure Bot Service wiring and the demo script |
| `spikes/`, `layer3/` | The de-risking experiments, with their raw findings |
| `CONTEXT.md` | Glossary and ubiquitous language |
| `SUMMARY.md` | Build summary: what was executed versus what was merely written |


---

## Prerequisites

**Accounts and privileges**

- A Google Cloud **organization** (workforce identity pools are org-level
  resources; being project Owner is not enough) and a project within it.
- Permission to create workforce identity pools at the org level.
- A **Microsoft Entra tenant** with Global Administrator, or someone who has it,
  since admin consent is required.
- An **Azure subscription** for the Azure Bot resource.
- A **Microsoft Teams** tenant where you are allowed to upload a custom app.

**Tooling**

- `gcloud` (Google Cloud SDK), including `bq`
- `terraform`
- `az` (Azure CLI), optional but assumed by some of the lookup commands
- Python 3.12+
- Docker, to build the middle tier image

**Cost and blast radius.** This creates billable resources (Cloud Run, BigQuery,
Agent Runtime) and an org-level identity pool. Use a sandbox organization. Do not
run it first against anything that matters.

---

## Configuration

Every environment-specific value in this repository comes from one place:
[`.env.example`](.env.example). Copy it, fill it in, and source it.

```bash
cp .env.example .env
$EDITOR .env
set -a && . ./.env && set +a
```

`.env` is gitignored. `.env.example` documents each variable, what it is, and
where to obtain it.

Placeholders in documentation and templates are written in `<ANGLE_BRACKET>`
form, so you can find every slot that still needs a value:

```bash
grep -rn '<[A-Z_]\+>' --exclude-dir=.venv --exclude-dir=.git .
```

Not every angle-bracket token is a slot you fill in: some redact a value
captured from a live run, and some show the shape of something the system
generates at runtime. Those are listed in the appendix of `.env.example`.

---

## Setup

The order matters. Each stage produces an identifier the next stage needs, and
several steps cannot be undone cheaply (renaming the workforce pool invalidates
the STS audience and every IAM binding at once).

### 1. Google Cloud side

```bash
cd terraform
terraform init
terraform plan    # five variables have no default; supply them from your .env
```

The workforce identity pool, its Entra provider, the IAM bindings and the API
enablement all live here. For a fresh deployment from scratch, `terraform apply`
creates the workforce pool and provider directly. If adopting an existing workforce
pool in your organization, follow `terraform/IMPORT.md` before applying to bring
existing resources into state.

Note what is deliberately **absent**: there is no `google_org_policy_policy`
resource. An org policy resource is authoritative for its constraint, so
Terraform would happily overwrite a working org-wide policy from a config whose
scope is one demo. If your organization was created on or after 2024-05-03,
domain-restricted sharing is on by default and you will need that policy changed
by whoever owns it. `terraform/workforce_pool.tf` explains the check to run.

### 2. Microsoft Entra side

Follow `entra/` in order for registrations, with one sequencing note for `<BOT_DOMAIN>`:

- Complete `entra/01_bot_app_registration.md` (steps 1 to 6) and `entra/02_federation_app_obo.md` first to produce `<APP_A_CLIENT_ID>` and `<APP_A_CLIENT_SECRET>`, which the middle tier requires.
- If deploying to Cloud Run, pause before step 7 of 01 and page 03. Deploy BigQuery, the agent, and the Cloud Run middle tier (Stages 3 to 5) to obtain your live Cloud Run URL. Strip `https://` to get `<BOT_DOMAIN>`, then return to finish the Redirect URI (01 step 7), messaging endpoint (01 step 8), and Teams app manifest packaging (03).
- If developing locally with a tunnel (such as `ngrok http 8000`), use the tunnel hostname as `<BOT_DOMAIN>` from the start and follow all pages sequentially.

| Page | What it does |
|---|---|
| `entra/01_bot_app_registration.md` | App A: the Azure Bot app registration |
| `entra/02_federation_app_obo.md` | App B: the federation app, SSO scope, OBO grant |
| `entra/03_teams_app_manifest.md` | The Teams app package |
| `entra/04_verification.md` | Isolates each hop so a failure localises |
| `entra/05_troubleshooting.md` | The failure modes, by symptom |

**App A and App B are not interchangeable.** App A authenticates the bot to the
Bot Framework; App B authenticates the human to Google. Collapsing them is the
most common setup mistake.

### 3. BigQuery demo data

The `.sql` files are templates containing `<ANGLE_BRACKET>` placeholders and are
not directly runnable. `apply.sh` renders them with your values into
`bigquery/.rendered/` and runs the rendered copies.

```bash
cd bigquery
set -a && . ../.env && set +a
./apply.sh
```

It refuses to run without `M365_ADMIN_OBJECT_ID` unless you pass
`ALLOW_PLACEHOLDER=1`, because the failure mode is a demo that applies cleanly
and then shows the admin zero rows on stage.

### 4. Deploy the agent

```bash
cd agent
python -m venv .venv && ./.venv/bin/pip install -r requirements.txt
set -a && . ../.env && set +a
./.venv/bin/python deploy.py
```

Record the engine ID it prints into `REASONING_ENGINE_ID` in your `.env`.

If your project hosts any reasoning engine you did not deploy from this repo,
put its ID in `PROTECTED_ENGINE_IDS` first. `--rollback` takes a resource name
that is one fat-finger away from somebody else's agent, and that mistake is
destructive. The guard is empty by default because this repo cannot know your
engine IDs; that is honest, not safe.

### 5. Deploy the middle tier

```bash
cd middle_tier
python -m venv .venv && ./.venv/bin/pip install -r requirements.lock.txt
./.venv/bin/python -m pytest tests/ -q
```

Create the two secrets in Secret Manager, grant the runtime service account read
access, then deploy. The full command is in `middle_tier/README.md`.

Two things there look like mistakes and are not:

- `--allow-unauthenticated` is correct. Azure Bot Service cannot present a
  Google IAM credential, so the endpoint must be reachable without one. **The
  Bot Framework JWT is the entire authentication boundary**, which is why
  `app/auth/inbound.py` is written and tested the way it is.
- Secrets are **not** passed with `--set-secrets`. A Cloud Run secret mount puts
  the value on a filesystem, and therefore in container layer diffs and core
  dumps. The service reads them over the API at startup so they exist only in
  process memory.

### 6. Wire up Azure Bot Service and install in Teams

Follow `runbook/11_azure_bot_service.md`, then point the Azure Bot registration's
messaging endpoint at `https://<CLOUD_RUN_URL>/api/messages` and upload the Teams
app package.

**This is the stage nobody has completed.** Expect to debug the OBO exchange
here; `entra/05_troubleshooting.md` is organised by symptom for exactly that
reason, and `entra/04_verification.md` isolates each hop so a failure tells you
which one broke.

`runbook/12_demo.md` is the demo script. Its expected outputs are deliberately
left as blanks for you to observe rather than as assertions, because they have
never been observed.

---

## Contributing

If you get the Teams path working, the most valuable contribution update the
verified-versus-unverified records in `SUMMARY.md`, `spikes/FINDINGS.md`,
`agent/DEPLOYMENT.md`, `middle_tier/NOTES.md` and `middle_tier/INTEGRATION.md`,
saying what you actually saw return.

Please keep those distinctions intact. They are the most valuable content in
this repository, and they are much easier to destroy than to rebuild.

