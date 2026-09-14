# Workforce identity pool and the Microsoft Entra OIDC provider.
#
# This is the whole identity plane. An Entra user signs in against their tenant, the
# resulting ID token is exchanged at Google STS for the audience
#   //iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/providers/entra
# and the caller becomes a workforce principal that Google Cloud IAM can name.
#
# BOTH RESOURCES ALREADY EXIST AND ARE ACTIVE. They must be imported, not created.
# See IMPORT.md. `prevent_destroy` below is the seatbelt; do not remove it casually.

resource "google_iam_workforce_pool" "teams_bot_demo" {
  # Workforce pools are ORGANIZATION-level, which is why parent is an org and not a
  # project, and why being project Owner on var.project_id is not enough to administer
  # them (that needs roles/iam.workforcePoolAdmin at the org).
  parent   = "organizations/${var.org_id}"
  location = "global" # Workforce pools are global-only. There is no regional variant.

  workforce_pool_id = var.pool_id
  display_name      = "teams-bot-demo"
  description       = "Federates Microsoft Entra users into Google Cloud for the Teams bot. Invocation identity and tool identity both resolve to the human (ADR 002)."

  # Credentials minted from this pool live one hour. Matches the live pool.
  session_duration = var.pool_session_duration
  disabled         = false

  lifecycle {
    # This pool is the demo. Recreating it invalidates every issued credential and
    # every IAM binding that names its principalSet, and deleted workforce pools sit
    # in a soft-deleted state that blocks reusing the same ID for 30 days. Any plan
    # that wants to replace this resource is a bug in the config, not an intention.
    prevent_destroy = true
  }
}

resource "google_iam_workforce_pool_provider" "entra" {
  workforce_pool_id = google_iam_workforce_pool.teams_bot_demo.workforce_pool_id
  location          = google_iam_workforce_pool.teams_bot_demo.location
  provider_id       = var.provider_id

  display_name = "entra"
  description  = "Microsoft Entra ID (OIDC) for tenant ${var.entra_tenant_id}."
  disabled     = false

  # THE LOAD-BEARING LINE.
  #
  # google.subject is mapped to the Entra object ID (`oid`), not to `sub`, `email` or
  # `upn`. Consequences, all of them deliberate:
  #   - `oid` is immutable and tenant-unique; email and UPN are neither.
  #   - The subject BigQuery reports from SESSION_USER() is therefore
  #       principal://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/subject/<oid>
  #     which is the same value ADR 003 fixes as the session key. Session ownership and
  #     IAM identity are one value by construction rather than by coincidence.
  # Changing this mapping re-identifies every existing user and orphans their sessions.
  attribute_mapping = {
    "google.subject" = "assertion.oid"

    # Present on the LIVE provider and therefore required here, or `terraform
    # import` reports drift on the very first plan. Discovered by reading the
    # deployed provider back rather than from the original build notes.
    # Cosmetic in effect: it is what shows up as the principal's display name.
    # Unlike google.subject above, changing this re-identifies nobody.
    "google.display_name" = "assertion.preferred_username"
  }

  oidc {
    issuer_uri = "https://login.microsoftonline.com/${var.entra_tenant_id}/v2.0"

    # The Entra app registration used for FEDERATION. This is the `aud` the pool will
    # accept in an inbound ID token. Not the Azure Bot registration.
    client_id = var.entra_federation_client_id

    web_sso_config {
      # CRITICAL COUPLING, DISCOVERED THE HARD WAY:
      # assertion_claims_behavior MUST be ONLY_ID_TOKEN_CLAIMS when response_type is
      # ID_TOKEN. Setting MERGE_USER_INFO_OVER_ID_TOKEN_CLAIMS with response_type =
      # ID_TOKEN is rejected with a bare
      #     "Invalid OIDC WebSsoConfig AssertionClaimsBehavior"
      # which never tells you that the two fields are coupled. The merge behaviour
      # requires a userinfo endpoint call, which is only available on the CODE flow.
      # If you ever switch response_type to CODE, this field is what you revisit.
      response_type             = "ID_TOKEN"
      assertion_claims_behavior = "ONLY_ID_TOKEN_CLAIMS"

      # No extra scopes. Everything the mapping needs (`oid`) is already in the ID
      # token; asking for more scopes would mean more consent for no gain.
      additional_scopes = []
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

# ---------------------------------------------------------------------------
# DOMAIN-RESTRICTED SHARING: CHECKED, NO RESOURCE REQUIRED. DO NOT ADD ONE.
# ---------------------------------------------------------------------------
#
# Granting IAM roles to a workforce principalSet is normally the step that trips over
# constraints/iam.allowedPolicyMemberDomains. It does not here, and this was verified
# rather than hoped:
#
#   - The effective allowedPolicyMemberDomains policy on the organization used for
#     this build already allowed its own customer IDs and, crucially, the value
#     is:principalSet://iam.googleapis.com/organizations/<GCP_ORG_ID>
#     That last entry is the organization principal set, which is exactly what lets
#     workforce identity pool principals receive role grants.
#   - That org predates 2024-05-03, so the default-on domain-restricted-sharing rule
#     that applies to newer organizations did not apply to it.
#
#   CHECK THIS FOR YOUR OWN ORG BEFORE ASSUMING IT IS FREE. Read the effective policy
#   with:
#     gcloud org-policies describe iam.allowedPolicyMemberDomains \
#       --organization=<GCP_ORG_ID> --effective
#
# So there is deliberately NO google_org_policy_policy resource in this config.
# Writing one would be actively harmful: an org policy resource is authoritative for
# that constraint, so Terraform would happily overwrite a working org-wide policy that
# other projects depend on, from a config whose scope is one demo.
#
# IN A NEWER ORGANIZATION (created on or after 2024-05-03) this would NOT be free.
# There you would need something like the following, applied by whoever owns org
# policy, and ideally in a separate org-scoped state:
#
#   resource "google_org_policy_policy" "allowed_policy_member_domains" {
#     name   = "organizations/${var.org_id}/policies/iam.allowedPolicyMemberDomains"
#     parent = "organizations/${var.org_id}"
#     spec {
#       rules {
#         values {
#           allowed_values = [
#             "<GCP_CUSTOMER_ID>",
#             "is:principalSet://iam.googleapis.com/organizations/${var.org_id}",
#           ]
#         }
#       }
#     }
#   }
#
# Also checked: constraints/iam.workforcePoolProviders does not exist as a constraint
# ID, so there is no provider-level allowlist to clear. (The similarly named
# constraints/iam.workloadIdentityPoolProviders is a different, workload-identity
# constraint, and is allValues: ALLOW here anyway.)
