# Service (API) enablement on var.project_id.
#
# disable_on_destroy = false on every entry. Turning an API off is a project-wide
# blast radius: it breaks anything else in the project that happens to use it, and
# for shared APIs like logging or serviceusage it is close to unrecoverable in the
# moment. Destroying this Terraform config should at worst leave APIs enabled that
# nobody needs, never take down a neighbouring workload.
#
# disable_dependent_services = false for the same reason: never let one removal
# cascade into others.

locals {
  required_services = [
    # Workforce identity federation itself: pools, providers, IAM policy reads.
    "iam.googleapis.com",

    # Security Token Service. This is the endpoint that exchanges the Entra ID token
    # for a Google access token (sts.googleapis.com/v1/token). Without it there is
    # no federation, only a pool that nobody can use.
    "sts.googleapis.com",

    # IAM Service Account Credentials. Not used for user-scoped work (ADR 002 forbids
    # that), but required for any service-identity token minting the middle tier does,
    # e.g. signing its own ID token when calling another Cloud Run service.
    "iamcredentials.googleapis.com",

    # BigQuery, which also hosts the managed MCP endpoint at
    # https://bigquery.googleapis.com/mcp that the agent's tool calls.
    "bigquery.googleapis.com",

    # Vertex AI / Agent Runtime (reasoning engines).
    "aiplatform.googleapis.com",

    # Cloud Run: the bot middle tier.
    "run.googleapis.com",

    # Secret Manager: bot app password and Entra client secret.
    "secretmanager.googleapis.com",

    # Resource Manager: project and org policy reads, IAM policy get/set.
    "cloudresourcemanager.googleapis.com",

    # Service Usage. Load-bearing here, not boilerplate: a workforce principal has no
    # project of its own to bill, so every call carries a quota project
    # (X-Goog-User-Project / the STS `userProject` option), and that path goes through
    # the Service Usage API. See the matching role grant in iam.tf.
    "serviceusage.googleapis.com",

    # Cloud Logging: the audit trail that proves the invocation was attributed to the
    # human rather than to a service account. That evidence is the point of the demo.
    "logging.googleapis.com",
  ]
}

resource "google_project_service" "required" {
  for_each = toset(local.required_services)

  project = var.project_id
  service = each.value

  disable_on_destroy         = false
  disable_dependent_services = false
}
