# Input variables.
#
# The five variables at the top have NO default. They identify your Google Cloud
# organization, your project and your Entra tenant, and there is no sane value to
# guess. Terraform will prompt for anything you do not supply, so a missing value
# fails before it can create a resource in the wrong place.
#
# Supply them with a `terraform.tfvars` file (gitignored) or `-var` flags. The
# names match the variables in `.env.example` at the repository root, which is
# the single documented source for every environment-specific value in this repo.
#
# Everything below the "Defaulted" divider has a default that is a NAME we choose
# rather than an identifier your environment imposes. Those are safe to leave
# alone; changing `pool_id` or `provider_id` after the fact breaks federation and
# every IAM binding at once, so decide early.

# --- Required: no default --------------------------------------------------

variable "org_id" {
  description = "Google Cloud organization ID (numeric, no 'organizations/' prefix). Workforce pools are org-level resources, not project-level. Find it with: gcloud organizations list"
  type        = string
}

variable "project_id" {
  description = "Project that holds the IAM bindings, APIs, secrets and the bot middle tier."
  type        = string
}

variable "project_number" {
  description = "Numeric project number for var.project_id. Needed where an API wants the number rather than the ID (and for reading audit log entries). Find it with: gcloud projects describe <project-id> --format='value(projectNumber)'"
  type        = string
}

variable "entra_tenant_id" {
  description = "Microsoft Entra tenant (directory) ID. Forms the OIDC issuer URI. Entra admin center > Overview > Tenant ID."
  type        = string
}

variable "entra_federation_client_id" {
  description = <<-EOT
    Application (client) ID of the Entra app registration used for FEDERATION into
    Google STS. This is the value that appears as `aud` in the ID token the workforce
    pool provider accepts.

    Note this is a distinct identity from the Azure Bot registration used by the Teams
    channel. Do not collapse the two: the bot app authenticates the bot to the Bot
    Framework, this app authenticates the human to Google.

    Created in entra/02_federation_app_obo.md.
  EOT
  type        = string
}

# --- Defaulted: names we choose, not identifiers the environment imposes ----

variable "pool_id" {
  description = "Workforce pool ID. Part of both the STS audience and the principalSet used in IAM bindings, so renaming it breaks federation and every role grant at once."
  type        = string
  default     = "teams-bot-demo"
}

variable "provider_id" {
  description = "Workforce pool provider ID. Part of the STS audience string."
  type        = string
  default     = "entra"
}

variable "region" {
  description = "Region for regional resources. Agent Runtime (reasoning engines) and the bot middle tier both live here."
  type        = string
  default     = "us-central1"
}

variable "pool_session_duration" {
  description = "Lifetime of credentials minted from this pool."
  type        = string
  default     = "3600s"
}

# --- Bot middle tier -------------------------------------------------------

variable "middle_tier_sa_id" {
  description = "Account ID (local part) of the service account the Cloud Run middle tier runs as. This SA authenticates the SERVICE, never a user: see ADR 002."
  type        = string
  default     = "teams-bot-middle-tier"
}

variable "bot_service_name" {
  description = "Cloud Run service name for the bot middle tier."
  type        = string
  default     = "teams-bot-middle-tier"
}

variable "bot_image" {
  description = <<-EOT
    Container image for the bot middle tier, for example
    "us-central1-docker.pkg.dev/<GCP_PROJECT_ID>/teams-bot/middle-tier:v1".

    Default is deliberately EMPTY. Putting a made-up reference here would produce a
    config that plans cleanly and then fails at apply with an image-pull error. While
    this is empty the Cloud Run service resource has count = 0, so it is simply not
    managed. Set it (or pass -var) once a real image has been pushed, and the service
    appears in the plan.
  EOT
  type        = string
  default     = ""
}

variable "agent_runtime_resource_name" {
  description = <<-EOT
    Full resource name of YOUR ADK agent on Agent Runtime, passed to the middle tier as
    an env var, for example
    "projects/<GCP_PROJECT_NUMBER>/locations/us-central1/reasoningEngines/<REASONING_ENGINE_ID>".

    Empty by default, because the agent does not exist until you run agent/deploy.py.

    Point this at the engine YOU deployed and nothing else. A project that already
    hosts other reasoning engines will happily accept the resource name of one of
    them, and the failure is silent: the bot will drive somebody else's agent. See
    PROTECTED_ENGINE_IDS in agent/deploy.py for the matching guard on the delete path.
  EOT
  type        = string
  default     = ""
}
