# Project-level IAM for the workforce principalSet.
#
# The member is every identity that federates through the pool:
#   principalSet://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/*
#
# For a demo pool with a handful of Entra test users, granting at the pool level is
# the right granularity. In production you would narrow this to an attribute-scoped
# principalSet (e.g. .../attribute.groups/<group-id>) so that membership of an Entra
# group, not mere existence in the tenant, is what confers access. The resource type
# and the import syntax are identical either way; only the member string changes.

locals {
  # Built from variables so the pool ID cannot drift between here and the pool
  # resource. The literal value today is:
  #   principalSet://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/*
  workforce_principal_set = "principalSet://iam.googleapis.com/locations/global/workforcePools/${var.pool_id}/*"

  workforce_roles = [
    # The BigQuery MCP endpoint (https://bigquery.googleapis.com/mcp). This is what
    # authorizes calling the managed MCP tools at all.
    "roles/mcp.toolUser",

    # Run BigQuery jobs. Without it the MCP tool can list metadata but every
    # execute_sql call fails.
    "roles/bigquery.jobUser",

    # Read table data. jobUser without dataViewer gets you a job that runs and then
    # cannot see anything.
    "roles/bigquery.dataViewer",

    # NOT OPTIONAL, AND NOT IN THE DOCS. The reason it is here:
    #
    # A workforce principal has no project of its own to bill against. Every API call
    # it makes must therefore nominate a quota/billing project, which is what the STS
    # `options={"userProject": "<GCP_PROJECT_ID>"}` and the X-Goog-User-Project header do.
    # The moment a request carries a quota project, Google Cloud checks that the
    # CALLER holds roles/serviceusage.serviceUsageConsumer on that project. Miss it
    # and the failure is a 403 that talks about service usage, not about BigQuery,
    # which sends you debugging the wrong subsystem entirely.
    #
    # The BigQuery MCP documentation lists three roles (mcp.toolUser, bigquery.jobUser,
    # bigquery.dataViewer). For the workforce-federated path four are required. The
    # fifth below is for Agent Runtime.
    "roles/serviceusage.serviceUsageConsumer",

    # Invoke the ADK agent on Agent Runtime (reasoning engines) AS THE HUMAN. This is
    # the invocation-identity half of ADR 002: the query into the runtime is
    # authorized against the person, and the audit log names the person, rather than
    # everything arriving as one shared service account.
    "roles/aiplatform.user",
  ]
}

# google_project_iam_member, NOT google_project_iam_binding, and NOT
# google_project_iam_policy.
#
#   _member  -> additive. Manages exactly one (role, member) pair and leaves every
#               other member of that role untouched.
#   _binding -> AUTHORITATIVE FOR THE WHOLE ROLE. Applying it would strip every other
#               member already holding that role on the project. roles/bigquery.jobUser
#               and roles/aiplatform.user in particular are near-certain to be held by
#               other humans and service accounts in a shared dev project, and they
#               would be silently removed on the first apply.
#   _policy  -> authoritative for the ENTIRE project policy. Worse again.
#
# The additive form is also what makes the import in IMPORT.md safe: importing one
# (role, member) pair cannot capture, and therefore cannot later delete, anyone else's
# access.
resource "google_project_iam_member" "workforce_pool" {
  for_each = toset(local.workforce_roles)

  project = var.project_id
  role    = each.value
  member  = local.workforce_principal_set

  # The IAM grant is meaningless until the API behind the role is on, and
  # serviceusage in particular must be enabled for the quota-project path above.
  depends_on = [google_project_service.required]
}
