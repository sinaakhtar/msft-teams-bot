# Google-side infrastructure for the Teams bot

Terraform for the Google Cloud half of a Microsoft Teams bot that talks to an ADK
agent on Agent Runtime, with the signed-in Microsoft Entra user authenticated end to
end. No service account ever stands in for a person.

The identity chain this exists to support:

```
Teams user (Entra)
  -> Entra ID token (aud = federation app, iss = login.microsoftonline.com/<tenant>/v2.0)
  -> Google STS token exchange
       audience = //iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/providers/entra
       options  = {"userProject": "<GCP_PROJECT_ID>"}
  -> Workforce principal
       principal://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/subject/<entra-oid>
  -> Agent Runtime invocation, and BigQuery/MCP calls underneath it, as that person
```

Target: project `<GCP_PROJECT_ID>` (<GCP_PROJECT_NUMBER>), org `organizations/<GCP_ORG_ID>`
(`<ORG_DOMAIN>`), region `us-central1`.

## What this manages

| File | Manages |
|---|---|
| `versions.tf` | Terraform >= 1.5, `hashicorp/google` >= 6.0 < 8.0, provider defaults |
| `variables.tf` | All inputs, defaulted to the live verified values so `plan` needs no tfvars |
| `apis.tf` | Ten `google_project_service` entries, all `disable_on_destroy = false` |
| `workforce_pool.tf` | The workforce pool and the Entra OIDC provider |
| `iam.tf` | Five additive project role bindings for the workforce principalSet |
| `cloud_run.tf` | Middle-tier service account, two Secret Manager secrets, per-secret accessor IAM, and a count-gated Cloud Run service |
| `outputs.tf` | Pool/provider names, STS audience, principalSet, SA email |
| `IMPORT.md` | **Read before your first apply.** How to adopt the live pool without recreating it |

The five roles on the workforce principalSet are `roles/mcp.toolUser`,
`roles/bigquery.jobUser`, `roles/bigquery.dataViewer`,
`roles/serviceusage.serviceUsageConsumer` and `roles/aiplatform.user`.

The fourth is the one that catches people out. The BigQuery MCP docs list three roles.
A workforce principal has no project of its own to bill, so every call must nominate a
quota project, and nominating one requires
`roles/serviceusage.serviceUsageConsumer` on it. Without it you get a 403 that talks
about service usage while you are busy debugging BigQuery. The fifth,
`roles/aiplatform.user`, is for invoking the agent on Agent Runtime.

## What this deliberately does NOT manage

**Organization policy.** Domain-restricted sharing was checked and needs no change.
The effective `constraints/iam.allowedPolicyMemberDomains` on this org already allows
`<GCP_CUSTOMER_ID>`, `<OTHER_ALLOWED_CUSTOMER_ID>` and `is:principalSet://iam.googleapis.com/organizations/<GCP_ORG_ID>`,
and the org was created in 2022, before the 2024-05-03 cutoff that makes DRS default-on
for newer organizations. There is no `google_org_policy_policy` resource here on
purpose: that resource is authoritative for the constraint, so a demo-scoped config
could silently overwrite an org-wide policy other projects depend on. In a
**newer** org this would not be free and you would need a policy allowing the org
principal set; workforce_pool.tf carries the sketch in a comment.
(`constraints/iam.workforcePoolProviders` does not exist as a constraint ID, so there
is nothing to clear there either.)

**`reasoningEngines/<OTHER_ENGINE_ID_1>`** ("data_science_agent") in us-central1. It
already exists, it belongs to someone else, and it is not ours. No resource or data
source refers to it, so Terraform can never modify or destroy it. Do not point
`var.agent_runtime_resource_name` at it.

**Our own ADK agent on Agent Runtime.** Deployed by the ADK tooling, not Terraform.
Once it exists, pass its resource name in via `var.agent_runtime_resource_name`.

**Everything on the Microsoft side.** The Entra app registrations (federation app
`<FEDERATION_APP_CLIENT_ID>` and the Azure Bot registration), the Teams app manifest, the bot channel
registration and the messaging endpoint are all configured in Entra/Azure. Terraform
stores the two Microsoft client secrets; it does not create them.

**Secret values.** `apis.tf` and `cloud_run.tf` create secret *containers* only. There
is no `google_secret_manager_secret_version` resource, because that would write the
plaintext into the state file. Add versions with `gcloud secrets versions add`.

**The Cloud Run service, by default.** `var.bot_image` defaults to `""` and the service
is `count`-gated on it, so it is not managed until a real image exists. Inventing an
image reference would produce a config that plans cleanly and fails at apply.

## Running it

```bash
cd terraform

# Terraform uses ADC, not gcloud's active account. Point at it explicitly.
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/gcloud/application_default_credentials.json"

terraform init
terraform validate
terraform plan
```

> **Do not apply from an empty state.** The pool and provider are live. A clean
> `terraform plan` here reports `22 to add`, which includes recreating them. Work
> through **IMPORT.md** first. `terraform destroy` would take down a working demo, and
> a deleted workforce pool is unusable for 30 days; the two resources carry
> `prevent_destroy` for that reason.

Once state has been reconciled:

```bash
terraform apply                                              # creates SA + secrets
printf %s "$BOT_APP_PASSWORD"   | gcloud secrets versions add teams-bot-app-password        --project=<GCP_PROJECT_ID> --data-file=-
printf %s "$ENTRA_CLIENT_SECRET"| gcloud secrets versions add teams-bot-entra-client-secret --project=<GCP_PROJECT_ID> --data-file=-

# later, once the middle-tier image exists
terraform apply -var="bot_image=us-central1-docker.pkg.dev/<GCP_PROJECT_ID>/teams-bot/middle-tier:v1"
```

State is local. Move it to a GCS backend before anyone else runs this; the commented
`backend "gcs"` block in `versions.tf` is the place.

## Two things worth knowing before you edit

**`google.subject` = `assertion.oid`** in `workforce_pool.tf` is load-bearing. It maps
the Google subject to the immutable Entra object ID, which makes the IAM identity and
the ADR 003 session key the same string by construction. Changing it re-identifies
every user and orphans their sessions.

**`web_sso_config` has a coupling the API will not explain.**
`assertion_claims_behavior` must be `ONLY_ID_TOKEN_CLAIMS` when `response_type` is
`ID_TOKEN`. `MERGE_USER_INFO_OVER_ID_TOKEN_CLAIMS` is rejected with a bare
`Invalid OIDC WebSsoConfig AssertionClaimsBehavior`, which never mentions that the two
fields are related.

See `docs/adr/` for the decisions this config implements, in particular ADR 002 (two
identity planes) and ADR 004 (fail closed on authorization failure).
