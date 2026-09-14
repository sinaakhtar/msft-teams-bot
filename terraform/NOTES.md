# NOTES: what exists, what was actually verified, what is blocked

Written 2026-09-07. This file separates three different claims that are easy to
conflate: **a file exists**, **Terraform parsed and type-checked it**, and **it was
proven correct against live infrastructure**. Only the first two are fully true here.

## 1. Files

All nine live under `terraform/`.

| File | Exists | Covered by `terraform validate` | Proven against live GCP |
|---|---|---|---|
| `versions.tf` | yes | yes | n/a (provider constraints only) |
| `variables.tf` | yes | yes | no |
| `apis.tf` | yes | yes | no (403 on serviceusage read) |
| `workforce_pool.tf` | yes | yes | **no** (403 on `iam.workforcePools.get`) |
| `iam.tf` | yes | yes | no |
| `cloud_run.tf` | yes | yes | partly (SA confirmed absent) |
| `outputs.tf` | yes | yes | values are string-composed, checked in plan output |
| `IMPORT.md` | yes | n/a | ID **formats** verified; imports themselves blocked |
| `README.md` | yes | n/a | n/a |

"Covered by validate" means the whole config, including the `count = 0` Cloud Run
block, passed schema and type checking. It does **not** mean the values are right.

## 2. What actually ran, verbatim

Binary: `~/.local/bin/terraform`, `Terraform v1.15.7 on linux_amd64`.

### `terraform init` — RAN, SUCCEEDED

Network was **not** blocked in this sandbox, contrary to expectation.

```
Initializing provider plugins found in the configuration...
- Finding hashicorp/google versions matching ">= 6.0.0, < 8.0.0"...
- Installing hashicorp/google v7.46.1...
- Installed hashicorp/google v7.46.1 (signed by HashiCorp)

Initializing the backend...

Terraform has been successfully initialized!
```

Left behind: `.terraform/` and `.terraform.lock.hcl` (google v7.46.1). Keep the lock
file.

### `terraform validate` — RAN, PASSED

```
Success! The configuration is valid.
```

Exit 0. Re-run after every edit below; the quoted result is from the final state of
the files.

### `terraform fmt -check` — RAN, PASSED

Exit 0, no diffs.

### `terraform plan` — RAN, SUCCEEDED

```
Plan: 22 to add, 0 to change, 0 to destroy.
```

**Do not read this as reassurance.** It is the central warning of IMPORT.md: with an
empty state Terraform intends to *create* the live workforce pool and provider.

Two outputs resolved at plan time and match the required literals exactly:

```
sts_audience            = "//iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/providers/entra"
workforce_principal_set = "principalSet://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/*"
```

All 22 planned addresses were enumerated from `terraform show -json`: the pool, the
provider, 5 `google_project_iam_member`, 10 `google_project_service`, 2 secrets,
2 secret IAM members, 1 service account. No Cloud Run service, because `var.bot_image`
is empty and the resource is `count`-gated — that gate works as intended.

> **Important caveat about the plan.** The first successful plan ran with **no working
> credentials at all**. With an empty state there is nothing to refresh, so the
> provider never called the API. A green `terraform plan` here proves the config is
> internally coherent; it proves **nothing** about GCP, about permissions, or about
> what already exists. Treat it accordingly.

### `terraform apply` — NOT RUN

Prohibited by the task, and it would be wrong anyway before the imports in IMPORT.md.

### `terraform destroy` — NOT RUN

Would destroy a working demo. Both workforce resources carry `prevent_destroy`.

## 3. A real bug that only running things caught

`terraform validate` and `terraform plan` both passed while the config contained a
defect that would have detonated during the IMPORT.md procedure:

```
Error: Invalid for_each argument

  on cloud_run.tf line 91, in resource "google_secret_manager_secret_iam_member" "bot_accessor":
  91:   for_each = google_secret_manager_secret.bot
    │ google_secret_manager_secret.bot will be known only after apply

The "for_each" map includes keys derived from resource attributes that cannot
be determined until apply...
```

It surfaced only when `terraform import` was invoked. Fixed by iterating the static
`local.bot_secrets` and indexing the resource for values. Re-validated after the fix:
`Success! The configuration is valid.` and `Plan: 22 to add, 0 to change, 0 to destroy.`
unchanged.

## 4. How the import ID formats were verified

Not from memory or documentation. Each import was issued with a deliberately malformed
ID, which fails at parse time before any API call, and the provider printed the formats
it accepts:

| Resource | Provider-reported accepted format |
|---|---|
| `google_iam_workforce_pool` | `^locations/(?P<location>[^/]+)/workforcePools/(?P<workforce_pool_id>[^/]+)$` and `^(?P<location>[^/]+)/(?P<workforce_pool_id>[^/]+)$` |
| `google_iam_workforce_pool_provider` | `^locations/(?P<location>[^/]+)/workforcePools/(?P<workforce_pool_id>[^/]+)/providers/(?P<provider_id>[^/]+)$` and `^(?P<location>[^/]+)/(?P<workforce_pool_id>[^/]+)/(?P<provider_id>[^/]+)$` |
| `google_project_iam_member` | `expected 'resource_name role member [condition_title]'` |
| `google_secret_manager_secret_iam_member` | `expected 'resource_name role member [condition_title]'` |
| `google_project_service` | ``expecting `{project}/{service}` `` |

The provider-choice question was settled the same way, by checking the actual binary
rather than trusting docs: `google_iam_workforce_pool`,
`google_iam_workforce_pool_provider`, `web_sso_config`, `assertion_claims_behavior` and
`ONLY_ID_TOKEN_CLAIMS` are all present in the **GA** `hashicorp/google` provider.
`google-beta` is not required and is deliberately not declared.

## 5. The one live fact established

```
$ terraform import google_service_account.bot_middle_tier \
    projects/<GCP_PROJECT_ID>/serviceAccounts/teams-bot-middle-tier@<GCP_PROJECT_ID>.iam.gserviceaccount.com
Error: Cannot import non-existent remote object
```

The middle-tier service account **does not exist yet**. It is created by `apply`, not
imported. No state file was written by any probe; `terraform.tfstate` does not exist.

## 6. BLOCKED items

### BLOCKED: cannot read or import the workforce pool and provider

```
BLOCKED: IAM_PERMISSION_DENIED on iam.workforcePools.get for
locations/global/workforcePools/teams-bot-demo. The credential available here cannot
administer or even read org-level workforce pools on organizations/<GCP_ORG_ID>.
```

Confirmed twice, through both `gcloud` and `terraform import`. This is the same blocker
already recorded in `spikes/FINDINGS.md`: project Owner on `<GCP_PROJECT_ID>` is not enough,
because workforce pools are organization-level.

Consequence: **the pool and provider configuration in `workforce_pool.tf` has never
been diffed against the live objects.** `session_duration`, `attribute_mapping`,
`issuer_uri`, `client_id` and the `web_sso_config` pair are transcribed from the
verified environment record and are trustworthy. `display_name` and `description` on
both resources are **written from intent, not observed**, and are the most likely
source of a post-import in-place diff. IMPORT.md Step 6 says to fix the config to match
reality rather than mutate the pool.

Exact unblocking commands:

```bash
gcloud organizations add-iam-policy-binding <GCP_ORG_ID> \
  --member="user:admin@<ORG_DOMAIN>" \
  --role="roles/iam.workforcePoolAdmin"

gcloud iam workforce-pools describe teams-bot-demo --location=global
gcloud iam workforce-pools providers describe entra \
  --workforce-pool=teams-bot-demo --location=global

cd terraform
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/gcloud/application_default_credentials.json"
terraform import 'google_iam_workforce_pool.teams_bot_demo' \
  'locations/global/workforcePools/teams-bot-demo'
terraform import 'google_iam_workforce_pool_provider.entra' \
  'locations/global/workforcePools/teams-bot-demo/providers/entra'
terraform plan   # expect: no changes to pool or provider
```

### BLOCKED: cannot confirm the five IAM bindings exist

Never read live. They are asserted by the verified environment record, not observed
here.

```bash
gcloud projects get-iam-policy <GCP_PROJECT_ID> --format=json \
  | jq '.bindings[] | select(.members[]? | contains("workforcePools/teams-bot-demo"))'
```

### BLOCKED: cannot read Secret Manager on <GCP_PROJECT_ID>

```
BLOCKED: 403 "Permission 'secretmanager.secrets.get' denied on resource (or it may not
exist)" for projects/<GCP_PROJECT_ID>/secrets/teams-bot-app-password.
```

So whether the two secrets already exist is **unknown**. Unblock:

```bash
gcloud secrets list --project=<GCP_PROJECT_ID> --filter="name~teams-bot"
```

### BLOCKED: cannot read enabled services on <GCP_PROJECT_ID>

```
BLOCKED: 403 "The caller does not have permission" reading Project Service
<GCP_PROJECT_ID>/sts.googleapis.com.
```

So which of the ten APIs are already enabled is **unknown**. Unblock:

```bash
gcloud services list --enabled --project=<GCP_PROJECT_ID>
```

### Environment gotcha, not a blocker

`$HOME` is unset in this shell, so Terraform's ADC discovery failed with a confusing
fallback to the GCE metadata server:

```
Original error: google: error getting credentials using GOOGLE_APPLICATION_CREDENTIALS
environment variable: open /.config/gcloud/application_default_credentials.json:
no such file or directory
```

Note the path starts `/.config`, not `/home/.../.config`. Setting
`GOOGLE_APPLICATION_CREDENTIALS` to the absolute path fixed it immediately. Also worth
knowing: Terraform ignores `gcloud config`'s active account, so the identity `gcloud`
shows and the identity Terraform uses can differ, and here they did.

`gcloud auth application-default login` was **not** run, per the standing instruction
that it breaks other agents on this workstation.

## 7. Bottom line

The configuration is written, formatted, initialized, validated and planned with real
tooling against the real provider. It is **not** reconciled with live infrastructure,
and the workforce pool — the single most important resource, and the one that is
already serving a working demo — has never been read. Nothing here should be applied
until IMPORT.md has been worked through by an identity holding
`roles/iam.workforcePoolAdmin` on the organization.
