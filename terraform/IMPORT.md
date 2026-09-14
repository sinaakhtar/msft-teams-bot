# Adopting the existing infrastructure into Terraform state

## Read this first

The workforce pool `teams-bot-demo` and its `entra` provider **already exist and are
working**. A federated Entra user has already queried BigQuery as themselves through
them. This config describes that reality; it must **adopt** it, not rebuild it.

With an empty state, Terraform does not know any of it exists. This was run for real,
against this config:

```
$ terraform plan
Plan: 22 to add, 0 to change, 0 to destroy.
```

Twenty-two creates, including `google_iam_workforce_pool.teams_bot_demo` and
`google_iam_workforce_pool_provider.entra`. **Applying that plan without importing
first is the failure mode this document exists to prevent.** The pool create would
collide with the live pool and fail, and you would be left half-applied, mid-demo,
debugging IAM.

> ### `terraform destroy` on this config would take down a working demo
>
> It would delete the workforce pool and the OIDC provider. Every federated
> credential dies instantly, all five IAM bindings vanish with the principalSet, and
> **a deleted workforce pool is soft-deleted for 30 days, during which the ID
> `teams-bot-demo` cannot be reused.** The STS audience string is baked into the pool
> ID, so you cannot simply recreate it under the same name and carry on.
>
> `google_iam_workforce_pool` and `google_iam_workforce_pool_provider` therefore both
> carry `lifecycle { prevent_destroy = true }`. That makes `terraform destroy` fail
> loudly instead of proceeding. Leave it there. If you genuinely intend to tear the
> demo down, delete the `prevent_destroy` block in the same commit, so that the
> intent is reviewable, rather than passing a flag that leaves no trace.

## Verification status of the commands below

Every import ID format below was verified empirically against the real provider
(`hashicorp/google v7.46.1`) by issuing the import with a deliberately malformed ID
and reading the format the provider itself reported. They are not copied from memory.

What that does and does not prove:

- **Proven:** the ID syntax is correct and the provider accepts it.
- **Not proven:** that the import completes here. The credential available in this
  environment lacks `iam.workforcePools.get` on `organizations/<GCP_ORG_ID>`, so the
  pool and provider imports return `IAM_PERMISSION_DENIED`. See BLOCKED below.

One existence fact *was* established live: the service account
`teams-bot-middle-tier@<GCP_PROJECT_ID>.iam.gserviceaccount.com` **does not exist yet**
(`Cannot import non-existent remote object`). It is created by `apply`, not imported.

## Step 0: preconditions

```bash
cd terraform

# Terraform ignores gcloud's active account and uses ADC. Point at it explicitly.
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/gcloud/application_default_credentials.json"

terraform init
```

You need an identity with **`roles/iam.workforcePoolAdmin` on
`organizations/<GCP_ORG_ID>`** (or equivalent). Project Owner on `<GCP_PROJECT_ID>` is *not*
sufficient: workforce pools are organization-level resources. This is the same blocker
recorded in the spike findings.

Do **not** run `gcloud auth application-default login` on this workstation; it breaks
other agents sharing the ADC file. Use an already-authorized credential.

> **Shell note (zsh):** resource addresses contain `[` and `"`, which zsh globs.
> Every address below is single-quoted. Keep the quoting exactly as written or you
> will get `no matches found`.

## Step 1: check what actually exists before importing anything

Never import blind. Import only what these report as present.

```bash
gcloud iam workforce-pools describe teams-bot-demo --location=global
gcloud iam workforce-pools providers describe entra \
  --workforce-pool=teams-bot-demo --location=global

gcloud projects get-iam-policy <GCP_PROJECT_ID> --format=json \
  | jq '.bindings[] | select(.members[]? | contains("workforcePools/teams-bot-demo"))'

gcloud services list --enabled --project=<GCP_PROJECT_ID>
gcloud secrets list --project=<GCP_PROJECT_ID> --filter="name~teams-bot"
gcloud iam service-accounts describe \
  teams-bot-middle-tier@<GCP_PROJECT_ID>.iam.gserviceaccount.com --project=<GCP_PROJECT_ID>
```

## Step 2: import the workforce pool and provider

Verified accepted formats, straight from the provider:

- pool: `^locations/(?P<location>[^/]+)/workforcePools/(?P<workforce_pool_id>[^/]+)$`
- provider: `^locations/(?P<location>[^/]+)/workforcePools/(?P<workforce_pool_id>[^/]+)/providers/(?P<provider_id>[^/]+)$`

```bash
terraform import \
  'google_iam_workforce_pool.teams_bot_demo' \
  'locations/global/workforcePools/teams-bot-demo'

terraform import \
  'google_iam_workforce_pool_provider.entra' \
  'locations/global/workforcePools/teams-bot-demo/providers/entra'
```

## Step 3: import the five IAM bindings

Verified format: `resource_name role member [condition_title]`, space-separated, as
a single quoted argument.

The member ends in `/*`, which is a glob character. It is inside double quotes in
every command below. Do not remove them.

```bash
terraform import \
  'google_project_iam_member.workforce_pool["roles/mcp.toolUser"]' \
  "<GCP_PROJECT_ID> roles/mcp.toolUser principalSet://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/*"

terraform import \
  'google_project_iam_member.workforce_pool["roles/bigquery.jobUser"]' \
  "<GCP_PROJECT_ID> roles/bigquery.jobUser principalSet://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/*"

terraform import \
  'google_project_iam_member.workforce_pool["roles/bigquery.dataViewer"]' \
  "<GCP_PROJECT_ID> roles/bigquery.dataViewer principalSet://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/*"

terraform import \
  'google_project_iam_member.workforce_pool["roles/serviceusage.serviceUsageConsumer"]' \
  "<GCP_PROJECT_ID> roles/serviceusage.serviceUsageConsumer principalSet://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/*"

terraform import \
  'google_project_iam_member.workforce_pool["roles/aiplatform.user"]' \
  "<GCP_PROJECT_ID> roles/aiplatform.user principalSet://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/*"
```

Because these are additive `google_project_iam_member` resources, importing one
`(role, member)` pair captures only that pair. Nobody else's access to
`roles/bigquery.jobUser` enters this state, so nothing else can later be removed by
it. That safety property is the reason iam.tf does not use `google_project_iam_binding`.

## Step 4: import already-enabled APIs

Verified format: `{project}/{service}`. Import only the ones Step 1 showed as enabled;
skip the rest and let `apply` enable them.

```bash
for svc in iam sts iamcredentials bigquery aiplatform run secretmanager \
           cloudresourcemanager serviceusage logging; do
  terraform import \
    "google_project_service.required[\"${svc}.googleapis.com\"]" \
    "<GCP_PROJECT_ID>/${svc}.googleapis.com"
done
```

## Step 5: service account and secrets, only if they exist

The service account was confirmed **absent**, so normally you skip this and let
`apply` create it. Included for the case where a partial apply has already happened.

```bash
terraform import \
  'google_service_account.bot_middle_tier' \
  'projects/<GCP_PROJECT_ID>/serviceAccounts/teams-bot-middle-tier@<GCP_PROJECT_ID>.iam.gserviceaccount.com'

terraform import \
  'google_secret_manager_secret.bot["teams-bot-app-password"]' \
  'projects/<GCP_PROJECT_ID>/secrets/teams-bot-app-password'

terraform import \
  'google_secret_manager_secret.bot["teams-bot-entra-client-secret"]' \
  'projects/<GCP_PROJECT_ID>/secrets/teams-bot-entra-client-secret'

terraform import \
  'google_secret_manager_secret_iam_member.bot_accessor["teams-bot-app-password"]' \
  "projects/<GCP_PROJECT_ID>/secrets/teams-bot-app-password roles/secretmanager.secretAccessor serviceAccount:teams-bot-middle-tier@<GCP_PROJECT_ID>.iam.gserviceaccount.com"

terraform import \
  'google_secret_manager_secret_iam_member.bot_accessor["teams-bot-entra-client-secret"]' \
  "projects/<GCP_PROJECT_ID>/secrets/teams-bot-entra-client-secret roles/secretmanager.secretAccessor serviceAccount:teams-bot-middle-tier@<GCP_PROJECT_ID>.iam.gserviceaccount.com"
```

## Step 6: the step that actually matters

```bash
terraform plan
```

**Expected: no changes to the workforce pool or the provider.**

If the plan wants to **destroy or replace** the pool or the provider, `prevent_destroy`
will stop it. Do not work around it. A replacement means an import did not take, or an
identifier in variables.tf disagrees with reality. Fix that, do not force it through.

If the plan wants to **update in place**, read carefully which attributes:

- `display_name`, `description` on either resource: these two were **not** part of the
  verified environment record and are written from intent, not observation. If live
  values differ, **edit workforce_pool.tf to match what is live** rather than applying
  the change. There is no reason to mutate a working pool to match a guess.
- `attribute_mapping`, `oidc.issuer_uri`, `oidc.client_id`,
  `web_sso_config.assertion_claims_behavior`: these are load-bearing and were verified.
  A diff here means something changed out of band. **Stop and investigate.** Do not
  apply. Changing `google.subject` re-identifies every user and orphans their sessions.

Only when `terraform plan` is clean on the pool, the provider and the five bindings
should you apply to create the genuinely new resources (service account, secrets).

## Alternative: config-driven import blocks (Terraform >= 1.5)

Safer for review, because the intent lands in a file a colleague can read rather than
in someone's shell history. Put these in a temporary `imports.tf`, run
`terraform plan` to preview the adoption, apply, then delete the file.

```hcl
import {
  to = google_iam_workforce_pool.teams_bot_demo
  id = "locations/global/workforcePools/teams-bot-demo"
}

import {
  to = google_iam_workforce_pool_provider.entra
  id = "locations/global/workforcePools/teams-bot-demo/providers/entra"
}

import {
  for_each = toset([
    "roles/mcp.toolUser",
    "roles/bigquery.jobUser",
    "roles/bigquery.dataViewer",
    "roles/serviceusage.serviceUsageConsumer",
    "roles/aiplatform.user",
  ])
  to = google_project_iam_member.workforce_pool[each.key]
  id = "<GCP_PROJECT_ID> ${each.key} principalSet://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/*"
}
```

`terraform plan -generate-config-out=` is **not** recommended here: it would overwrite
hand-written configuration whose comments carry the reasoning (the
`ONLY_ID_TOKEN_CLAIMS` coupling, the `serviceusage` rationale) that the generated code
would silently drop.

## BLOCKED in the environment where this was written

The import commands were **not executed to completion**. The pool import was run and
reached the API, then failed on authorization, which confirms the ID format parses but
leaves the adoption itself unverified.

```
BLOCKED: terraform import of the workforce pool and provider - the available
credential lacks iam.workforcePools.get on organizations/<GCP_ORG_ID>.
Live error: IAM_PERMISSION_DENIED, permission "iam.workforcePools.get",
resource "locations/global/workforcePools/teams-bot-demo".
```

Unblock by granting an org-level admin role, then re-running Step 2:

```bash
gcloud organizations add-iam-policy-binding <GCP_ORG_ID> \
  --member="user:admin@<ORG_DOMAIN>" \
  --role="roles/iam.workforcePoolAdmin"

export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/gcloud/application_default_credentials.json"
cd terraform && terraform init && \
terraform import 'google_iam_workforce_pool.teams_bot_demo' \
  'locations/global/workforcePools/teams-bot-demo'
```

```
BLOCKED: existence check and import of the two Secret Manager secrets and the ten
google_project_service entries - the available credential lacks
secretmanager.secrets.get and serviceusage read on <GCP_PROJECT_ID>.
Live errors: 403 "Permission 'secretmanager.secrets.get' denied on resource
(or it may not exist)" and 403 "The caller does not have permission".
```

Unblock by running Step 1 and Steps 4-5 as an identity with at least
`roles/secretmanager.viewer` and `roles/serviceusage.serviceUsageViewer` on
`<GCP_PROJECT_ID>`:

```bash
gcloud secrets list --project=<GCP_PROJECT_ID> --filter="name~teams-bot"
gcloud services list --enabled --project=<GCP_PROJECT_ID>
```
