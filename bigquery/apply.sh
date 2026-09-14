#!/usr/bin/env bash
#
# apply.sh -- build the Teams-bot RLS demo dataset in BigQuery, in order.
#
# Safe to re-run. Every step is idempotent:
#   01  CREATE SCHEMA IF NOT EXISTS
#   02  CREATE OR REPLACE TABLE
#   03  TRUNCATE then INSERT     (re-running does not duplicate rows)
#   04  DROP ALL ROW ACCESS POLICIES then CREATE OR REPLACE
#
# Order matters and is not negotiable. CREATE OR REPLACE TABLE in 02 silently
# drops every row access policy on the table, so 04 must always follow 02. If
# you run 02 on its own against a live demo, the table is left with no RLS at
# all and anyone holding dataViewer sees every row. Run the whole script.
#
# 05_verify.sql is NOT run here. It is meant to be executed interactively as
# each federated user, which this script cannot do -- it runs as whatever
# gcloud account you are holding. It IS rendered, so you have a copy with your
# own identifiers substituted, ready to paste.
#
# ---------------------------------------------------------------------------
# CONFIGURATION
#
# The .sql files in this directory are templates. They contain <ANGLE_BRACKET>
# placeholders and are NOT directly runnable; this script renders them into
# .rendered/ with your values and runs the rendered copies. The templates are
# never modified, so the repo stays clean and re-rendering is free.
#
# Every variable below comes from .env.example at the repository root. Source
# your filled-in .env first, or export them by hand:
#
#     set -a && . ../.env && set +a && ./apply.sh
# ---------------------------------------------------------------------------

set -euo pipefail

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# Directory this script lives in, so it works from any cwd.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RENDERED="$HERE/.rendered"

# --- Required. No defaults: a wrong project is worse than a missing one. ----
GCP_PROJECT_ID="${GCP_PROJECT_ID:-}"
WORKFORCE_POOL_ID="${WORKFORCE_POOL_ID:-}"
ANALYST_OBJECT_ID="${ANALYST_OBJECT_ID:-}"

# --- Optional, with defaults that are names we choose. ----------------------
BQ_DATASET="${BQ_DATASET:-teams_bot_demo}"
BQ_LOCATION="${BQ_LOCATION:-US}"
TENANT_DOMAIN="${TENANT_DOMAIN:-example.onmicrosoft.com}"

# Break-glass grantee on the operator row access policy in 04. This is a DEMO
# convenience so the person running the script can still see the table; 04 says
# to delete it for anything holding real data. Defaults to the active gcloud
# account.
GOOGLE_ADMIN_ACCOUNT="${GOOGLE_ADMIN_ACCOUNT:-$(gcloud config get-value account 2>/dev/null || true)}"

# --- The one that is allowed to be missing, loudly. -------------------------
M365_ADMIN_OBJECT_ID="${M365_ADMIN_OBJECT_ID:-}"

command -v bq >/dev/null 2>&1 || die "bq not found on PATH. Install the Google Cloud SDK."

for required in GCP_PROJECT_ID WORKFORCE_POOL_ID ANALYST_OBJECT_ID GOOGLE_ADMIN_ACCOUNT; do
  [[ -n "${!required}" ]] || die "$required is not set.

Set it in your .env (see .env.example at the repository root) and re-run:

    set -a && . ../.env && set +a && ./apply.sh"
done

# ---------------------------------------------------------------------------
# Guard: refuse to run while the admin object ID is unknown.
#
# Without this you get a demo that applies cleanly, looks fine, and then shows
# the admin zero rows on stage. Failing loudly here is much cheaper.
# Set ALLOW_PLACEHOLDER=1 to proceed anyway (useful for building out the
# analyst half of the demo before the admin oid is known).
# ---------------------------------------------------------------------------
if [[ -z "$M365_ADMIN_OBJECT_ID" ]]; then
  if [[ "${ALLOW_PLACEHOLDER:-0}" != "1" ]]; then
    die "M365_ADMIN_OBJECT_ID is not set.

The M365 admin's Entra object ID has not been filled in, so the admin user
will see ZERO rows and the two-user split will not demo.

Find the oid with one of:
  az ad user show --id m365-admin@$TENANT_DOMAIN --query id -o tsv
  (or have the admin ask the bot: SELECT SESSION_USER()  and read subject/)

To build the analyst half only, re-run with:  ALLOW_PLACEHOLDER=1 $0"
  fi
  printf '\033[33mWARNING: M365_ADMIN_OBJECT_ID is unset. The admin user will see ZERO rows. Continuing because ALLOW_PLACEHOLDER=1.\033[0m\n'
  # A syntactically valid GUID that matches nobody, so the SQL still parses.
  M365_ADMIN_OBJECT_ID="00000000-0000-0000-0000-000000000000"
fi

# ---------------------------------------------------------------------------
# Render templates -> .rendered/
# ---------------------------------------------------------------------------
render() {
  local src="$1" dst="$2"
  sed -e "s|<GCP_PROJECT_ID>|$GCP_PROJECT_ID|g" \
      -e "s|<BQ_DATASET>|$BQ_DATASET|g" \
      -e "s|<WORKFORCE_POOL_ID>|$WORKFORCE_POOL_ID|g" \
      -e "s|<ANALYST_OBJECT_ID>|$ANALYST_OBJECT_ID|g" \
      -e "s|<M365_ADMIN_OBJECT_ID>|$M365_ADMIN_OBJECT_ID|g" \
      -e "s|<TENANT_DOMAIN>|$TENANT_DOMAIN|g" \
      -e "s|<GOOGLE_ADMIN_ACCOUNT>|$GOOGLE_ADMIN_ACCOUNT|g" \
      "$src" > "$dst"
  # Any placeholder left in an EXECUTABLE line is a template this script does
  # not know about. Full-line SQL comments are skipped on purpose: they use
  # angle brackets to describe the SHAPE of a value (for example
  # ".../subject/<ENTRA_OID>") rather than to mark a substitution slot.
  local leftover
  # `|| true`: grep exits 1 when it finds nothing, which is the SUCCESS case
  # here, and `set -e` would otherwise abort the script on a clean render.
  leftover="$(grep -v '^[[:space:]]*--' "$dst" | grep -ohE '<[A-Z0-9_]+>' | sort -u | tr '\n' ' ' || true)"
  if [[ -n "$leftover" ]]; then
    die "unsubstituted placeholder in $(basename "$src"): $leftover
Add it to render() in apply.sh and to .env.example."
  fi
}

STEPS=(
  "01_dataset.sql"
  "02_tables.sql"
  "03_seed_data.sql"
  "04_row_access_policies.sql"
)

mkdir -p "$RENDERED"
log "Rendering templates into $RENDERED"
for step in "${STEPS[@]}" "05_verify.sql"; do
  [[ -f "$HERE/$step" ]] || die "missing $HERE/$step"
  render "$HERE/$step" "$RENDERED/$step"
  printf '    %s\n' "$step"
done

log "Project: $GCP_PROJECT_ID   Location: $BQ_LOCATION   Dataset: $BQ_DATASET"
log "Active account: $(gcloud config get-value account 2>/dev/null || echo '(unknown)')"

for step in "${STEPS[@]}"; do
  log "Running $step"
  bq --project_id="$GCP_PROJECT_ID" --location="$BQ_LOCATION" \
     query --use_legacy_sql=false --format=none < "$RENDERED/$step"
done

# ---------------------------------------------------------------------------
# Post-apply summary. Read-only.
# ---------------------------------------------------------------------------
log "Row access policies now on sales_opportunities"
bq --project_id="$GCP_PROJECT_ID" ls --row_access_policies \
   "$GCP_PROJECT_ID:$BQ_DATASET.sales_opportunities"

log "Done."
cat <<EOF

Next steps -- neither can be done from this script:

  1. Set M365_ADMIN_OBJECT_ID if you have not already (see the guard above)
     and re-run.

  2. Run .rendered/05_verify.sql as EACH federated Entra user, through the
     Teams bot. That is the only thing that actually proves the demo works.
     Applying this script proves the objects exist; it does NOT prove that
     the two users see different rows.

Note: if you query the table as a first-party Google account that is not on
any grantee list, you will correctly see ZERO rows. That is row-level
security working, not a failed seed.
EOF
