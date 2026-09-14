# Terraform and provider version constraints.
#
# PROVIDER CHOICE: google (GA), not google-beta.
#
# Verified, not assumed: the workforce identity pool resources this config needs
# are present in the GA provider. Checked against the actual provider binary
# hashicorp/google v7.37.0 on this machine:
#
#   google_iam_workforce_pool           -> present
#   google_iam_workforce_pool_provider  -> present
#   oidc.web_sso_config.assertion_claims_behavior / ONLY_ID_TOKEN_CLAIMS -> present
#
# These resources were beta-only when workforce identity federation launched, which
# is why a lot of older sample code pins google-beta. That is stale advice on any
# current provider: both resources have been GA since the 4.x series, and every
# argument used in workforce_pool.tf exists in the GA schema.
#
# google-beta is therefore deliberately NOT declared here. Declaring a provider you
# do not use still forces `terraform init` to download it, which costs a provider
# fetch and a lockfile entry for nothing. If a future resource genuinely needs beta
# (for example an Agent Runtime / reasoning engine resource, which as of this
# writing has no GA Terraform resource at all), add the block below and set
# `provider = google-beta` on that resource only:
#
#   google-beta = {
#     source  = "hashicorp/google-beta"
#     version = "~> 7.0"
#   }

terraform {
  # 1.5 is the floor because IMPORT.md offers config-driven `import` blocks as the
  # safer alternative to `terraform import`, and those landed in 1.5.
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 6.0.0, < 8.0.0"
    }
  }

  # No backend block. State is local by default. Before this is shared with anyone
  # else, move it to a GCS backend: the state will contain the workforce pool and
  # provider configuration, and a lost/forked state is how a working pool gets
  # recreated or destroyed by accident.
  #
  # backend "gcs" {
  #   bucket = "<state-bucket>"
  #   prefix = "msft-teams-bot/workforce"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
