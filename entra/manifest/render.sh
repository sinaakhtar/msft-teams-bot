#!/usr/bin/env bash
#
# render.sh -- render manifest.json with your identifiers and build the
# Teams app package zip.
#
# manifest.json in this directory is a TEMPLATE. It contains <ANGLE_BRACKET>
# placeholders and is not directly installable; Teams rejects it. This script
# renders it into .rendered/ with your values and zips the result. The template
# is never modified, so the repo stays sanitised and re-rendering is free.
#
# This exists because the rendered manifest is the single most leak-prone file
# in the tree: it carries the tenant's Teams app GUID, the bot's Entra app id
# and the public hostname of the middle tier, and it is the one file you are
# tempted to edit in place because Teams needs real values to install it. An
# in-place edit sits in `git status` looking like a legitimate change and gets
# committed by the next `git add -A`. Hence: template tracked, output ignored.
#
# Same pattern as bigquery/apply.sh, and the same reasoning.
#
# ---------------------------------------------------------------------------
# USAGE
#
#     set -a && . ../../.env && set +a && ./render.sh
#
# or export the three required variables by hand. Every one of them comes from
# .env.example at the repository root.
# ---------------------------------------------------------------------------

set -euo pipefail

log()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mWARNING: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RENDERED="$HERE/.rendered"
TEMPLATE="$HERE/manifest.json"
PACKAGE="$RENDERED/teams-app-package.zip"

# --- Required. No defaults: a manifest with a guessed app id installs and
# --- then fails authentication in a way that reads like a bot bug. ----------
TEAMS_APP_GUID="${TEAMS_APP_GUID:-}"
BOT_DOMAIN="${BOT_DOMAIN:-}"
# The manifest's <APP_A_CLIENT_ID>. BOT_APP_CLIENT_ID is the same value under
# the name runbook 11 uses; accept either so this works whichever you filled.
APP_A_CLIENT_ID="${MICROSOFT_APP_ID:-${BOT_APP_CLIENT_ID:-}}"

missing=()
[ -n "$TEAMS_APP_GUID" ]   || missing+=("TEAMS_APP_GUID")
[ -n "$BOT_DOMAIN" ]       || missing+=("BOT_DOMAIN")
[ -n "$APP_A_CLIENT_ID" ]  || missing+=("MICROSOFT_APP_ID (or BOT_APP_CLIENT_ID)")
if [ ${#missing[@]} -gt 0 ]; then
    die "missing required variables: ${missing[*]}
Source the repository .env first:
    set -a && . ../../.env && set +a && ./render.sh"
fi

# BOT_DOMAIN is a host, not a URL. Teams rejects a validDomains entry with a
# scheme, and the error does not say which field is wrong.
case "$BOT_DOMAIN" in
    http://*|https://*) die "BOT_DOMAIN must be a bare host, with no scheme: $BOT_DOMAIN" ;;
    */*)                die "BOT_DOMAIN must be a bare host, with no path: $BOT_DOMAIN" ;;
esac

[ -f "$TEMPLATE" ] || die "template not found: $TEMPLATE"

log "Rendering manifest.json"
mkdir -p "$RENDERED"
sed \
    -e "s|<TEAMS_APP_GUID>|$TEAMS_APP_GUID|g" \
    -e "s|<APP_A_CLIENT_ID>|$APP_A_CLIENT_ID|g" \
    -e "s|<BOT_DOMAIN>|$BOT_DOMAIN|g" \
    "$TEMPLATE" > "$RENDERED/manifest.json"

# Fail loudly rather than shipping a manifest with a literal <PLACEHOLDER> in
# it: Teams accepts some of those at upload and fails later at sign-in.
if grep -q '<[A-Z_]*>' "$RENDERED/manifest.json"; then
    leftover="$(grep -o '<[A-Z_]*>' "$RENDERED/manifest.json" | sort -u | tr '\n' ' ')"
    die "unsubstituted placeholders remain: $leftover"
fi

if command -v python3 >/dev/null 2>&1; then
    python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$RENDERED/manifest.json" \
        || die "rendered manifest is not valid JSON"
fi

# --- Icons. Required, and NOT in the repository: see ICONS.md. -------------
# The two PNGs must sit beside manifest.json at the ROOT of the zip, not in a
# subfolder. A package missing them is rejected with an error that reads like a
# manifest problem and sends you looking in the wrong place.
icons_ok=1
for icon in color.png outline.png; do
    if [ -f "$HERE/$icon" ]; then
        cp "$HERE/$icon" "$RENDERED/$icon"
    else
        warn "$icon not found in $HERE -- see ICONS.md for the spec"
        icons_ok=0
    fi
done

log "Building the app package"
rm -f "$PACKAGE"
if [ "$icons_ok" -eq 1 ]; then
    (cd "$RENDERED" && zip -q "$(basename "$PACKAGE")" manifest.json color.png outline.png)
else
    (cd "$RENDERED" && zip -q "$(basename "$PACKAGE")" manifest.json)
    warn "package built WITHOUT icons; Teams will reject it on upload"
fi

log "Done"
printf '  manifest : %s\n' "$RENDERED/manifest.json"
printf '  package  : %s\n' "$PACKAGE"
printf '\n%s\n' "Both are gitignored. Do not copy either back over the template."
