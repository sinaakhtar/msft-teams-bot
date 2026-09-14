# Bot middle tier: service identity, its secrets, and a gated Cloud Run placeholder.
#
# READ THIS BEFORE ADDING ROLES TO THE SERVICE ACCOUNT BELOW.
#
# ADR 002, two identity planes. This service account authenticates the SERVICE: it
# lets Cloud Run pull an image, read its own secrets, and emit logs. It is never the
# identity under which user data is touched. Everything user-scoped, the Agent Runtime
# invocation and every BigQuery/MCP call underneath it, travels on the workforce
# principal derived from the signed-in Entra user (see iam.tf).
#
# So this SA deliberately does NOT hold roles/aiplatform.user, roles/bigquery.jobUser,
# roles/bigquery.dataViewer or roles/mcp.toolUser. Granting any of them would create a
# second, ambient path to the same data that bypasses per-user authorization, and the
# first time a token-threading bug caused a fallback to ADC the system would keep
# working while silently answering every user as the service account. It must fail
# closed instead (ADR 004). If you find yourself wanting to add one of those roles,
# the bug is in the token threading, not in this file.

resource "google_service_account" "bot_middle_tier" {
  project      = var.project_id
  account_id   = var.middle_tier_sa_id
  display_name = "Teams bot middle tier"
  description  = "Runtime identity for the Cloud Run relay between Teams and Agent Runtime. Service identity only; never used for user-scoped work (ADR 002)."

  depends_on = [google_project_service.required]
}

# --- Secrets ---------------------------------------------------------------
#
# Two secrets, two different trust relationships, both of them Microsoft-side
# credentials that Google Cloud merely stores:
#
#   bot app password     - the Azure Bot registration's client secret. The middle tier
#                          uses it to obtain a Bot Framework token and to validate
#                          inbound activity JWTs. Compromise = someone can impersonate
#                          the bot to Teams.
#   entra client secret  - the client secret of the Entra federation app registration
#                          (var.entra_federation_client_id). Used in the OBO / auth
#                          code exchange that yields the user's ID token, which is
#                          then exchanged at Google STS. Compromise = someone can
#                          obtain user tokens.
#
# CONTAINER ONLY, NO VALUES. Terraform manages the secret containers and their IAM;
# it deliberately does NOT manage google_secret_manager_secret_version. A secret
# version resource puts the plaintext into the Terraform state file, in the clear, on
# whatever laptop or CI runner ran the apply. Add versions out of band:
#
#   printf %s "$BOT_APP_PASSWORD" | gcloud secrets versions add teams-bot-app-password \
#     --project=<GCP_PROJECT_ID> --data-file=-
#
#   printf %s "$ENTRA_CLIENT_SECRET" | gcloud secrets versions add teams-bot-entra-client-secret \
#     --project=<GCP_PROJECT_ID> --data-file=-

locals {
  bot_secrets = {
    "teams-bot-app-password" = {
      description = "Azure Bot registration client secret (Bot Framework app password)."
    }
    "teams-bot-entra-client-secret" = {
      description = "Client secret of the Entra app registration used for user federation into Google STS."
    }
  }
}

resource "google_secret_manager_secret" "bot" {
  for_each = local.bot_secrets

  project   = var.project_id
  secret_id = each.key

  labels = {
    component  = "teams-bot-middle-tier"
    managed-by = "terraform"
  }

  replication {
    auto {}
  }

  depends_on = [google_project_service.required]
}

# Least privilege per secret: secretAccessor on these two secrets only, granted at the
# SECRET level rather than project-wide. A project-level
# roles/secretmanager.secretAccessor would hand this SA every secret in the project,
# including ones belonging to unrelated workloads.
#
# Additive _iam_member again, for the same reason as in iam.tf: _iam_binding on a
# secret is authoritative for that role on that secret.
# for_each iterates local.bot_secrets, NOT google_secret_manager_secret.bot.
#
# This looks like a pointless indirection and is not. Iterating the resource map makes
# the instance KEYS derive from a resource that does not exist yet, and Terraform then
# refuses to enumerate the instances outside a full apply:
#
#   Error: Invalid for_each argument
#   ... google_secret_manager_secret.bot will be known only after apply
#
# That error does not show up in `terraform validate` or in a clean `terraform plan`.
# It surfaces the first time you run `terraform import` or a `-target`ed apply, i.e.
# exactly during the adoption procedure in IMPORT.md, which is the worst possible
# moment to discover it. Keys come from a static local; only values are apply-time.
resource "google_secret_manager_secret_iam_member" "bot_accessor" {
  for_each = local.bot_secrets

  project   = var.project_id
  secret_id = google_secret_manager_secret.bot[each.key].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.bot_middle_tier.email}"
}

# --- Cloud Run service: PLACEHOLDER, NOT YET MANAGED -----------------------
#
# The middle-tier image does not exist yet. var.bot_image defaults to "" and this
# resource is count-gated on it, so by default Terraform manages nothing here and the
# plan is clean. That is a deliberate choice over the two bad alternatives:
#
#   - Inventing an image reference such as "gcr.io/cloudrun/hello" or a path in an
#     Artifact Registry repo that has never been pushed to. It would plan beautifully
#     and then fail at apply with an image-pull error, or worse, succeed and deploy
#     something that is not the bot.
#   - Commenting the block out entirely, which rots and drifts from the variables.
#
# To bring it up: build and push the image, then
#   terraform apply -var="bot_image=us-central1-docker.pkg.dev/<GCP_PROJECT_ID>/teams-bot/middle-tier:v1"
#
# Before that first apply, revisit these two, which are placeholders in the honest
# sense of "decide them when the service is real":
#   - ingress. Teams calls this endpoint from the public internet, so it will need
#     INGRESS_TRAFFIC_ALL. The endpoint is protected by Bot Framework JWT validation
#     in the app, not by network position.
#   - invoker IAM. There is intentionally no google_cloud_run_v2_service_iam_member
#     granting allUsers here. Adding allUsers is a decision with a public blast radius
#     and it should be made explicitly, in a commit that says so, once the JWT
#     validation in the app has actually been tested.
resource "google_cloud_run_v2_service" "bot_middle_tier" {
  count = var.bot_image == "" ? 0 : 1

  project  = var.project_id
  name     = var.bot_service_name
  location = var.region

  # See note above: Teams reaches this from the internet.
  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.bot_middle_tier.email

    containers {
      image = var.bot_image

      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }

      # The quota project every workforce-principal call must nominate. Without it the
      # user's federated token is rejected for having no project to bill.
      env {
        name  = "USER_PROJECT"
        value = var.project_id
      }

      env {
        name  = "STS_AUDIENCE"
        value = local.sts_audience
      }

      env {
        name  = "ENTRA_TENANT_ID"
        value = var.entra_tenant_id
      }

      env {
        name  = "ENTRA_CLIENT_ID"
        value = var.entra_federation_client_id
      }

      env {
        name  = "AGENT_RUNTIME_RESOURCE_NAME"
        value = var.agent_runtime_resource_name
      }

      env {
        name = "ENTRA_CLIENT_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.bot["teams-bot-entra-client-secret"].secret_id
            version = "latest"
          }
        }
      }

      env {
        name = "BOT_APP_PASSWORD"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.bot["teams-bot-app-password"].secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_secret_manager_secret_iam_member.bot_accessor,
    google_project_service.required,
  ]
}
