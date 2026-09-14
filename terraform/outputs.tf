# Outputs. These are the strings the bot middle tier and the spike scripts need, so
# they are emitted rather than copied by hand into config files where they can drift.

locals {
  # The STS audience. Composed from variables rather than hardcoded so it cannot drift
  # from the pool and provider resources, but the value it must produce is exactly:
  #
  #   //iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/providers/entra
  #
  # Note the leading double slash and the absence of a scheme. It is not a URL, it is
  # a Google resource audience, and STS rejects it if you "fix" it into https://.
  sts_audience = "//iam.googleapis.com/locations/global/workforcePools/${var.pool_id}/providers/${var.provider_id}"
}

output "workforce_pool_name" {
  description = "Full resource name of the workforce pool, as returned by the API (locations/global/workforcePools/teams-bot-demo)."
  value       = google_iam_workforce_pool.teams_bot_demo.name
}

output "workforce_pool_id" {
  description = "Short pool ID."
  value       = google_iam_workforce_pool.teams_bot_demo.workforce_pool_id
}

output "workforce_pool_provider_name" {
  description = "Full resource name of the OIDC provider (locations/global/workforcePools/teams-bot-demo/providers/entra)."
  value       = google_iam_workforce_pool_provider.entra.name
}

output "sts_audience" {
  description = <<-EOT
    The `audience` parameter for the token exchange at https://sts.googleapis.com/v1/token.

    Used together with:
      subject_token_type = urn:ietf:params:oauth:token-type:id_token
      grant_type         = urn:ietf:params:oauth:grant-type:token-exchange
      options            = {"userProject": "<project_id>"}

    The userProject option is not optional: a workforce principal has no project of its
    own to bill, which is also why roles/serviceusage.serviceUsageConsumer appears in
    iam.tf.
  EOT
  value       = local.sts_audience
}

output "workforce_principal_set" {
  description = "IAM member string for every identity federated through this pool. This is what the five role bindings in iam.tf are granted to."
  value       = local.workforce_principal_set
}

output "workforce_subject_principal_prefix" {
  description = <<-EOT
    Prefix of the per-user principal. A single federated user appears as
    <prefix>/<entra-oid>, and that exact string is what BigQuery's SESSION_USER()
    returns, which is the on-screen proof that the query ran as the human.
  EOT
  value       = "principal://iam.googleapis.com/locations/global/workforcePools/${var.pool_id}/subject"
}

output "middle_tier_service_account_email" {
  description = "Service account the Cloud Run middle tier runs as. Service identity only, never a user identity (ADR 002)."
  value       = google_service_account.bot_middle_tier.email
}

output "bot_secret_ids" {
  description = "Secret Manager secret IDs the middle tier reads. Containers are managed here; values are added out of band, never through Terraform."
  value       = sort([for s in google_secret_manager_secret.bot : s.secret_id])
}

output "bot_service_url" {
  description = "Cloud Run URL of the middle tier, or null while var.bot_image is unset and the service is therefore not managed. The Teams messaging endpoint is this URL + /api/messages."
  value       = length(google_cloud_run_v2_service.bot_middle_tier) > 0 ? google_cloud_run_v2_service.bot_middle_tier[0].uri : null
}
