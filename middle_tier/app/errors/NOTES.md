# NOTES — `app.errors` (ADR 004 error layer)

Build notes for the error-handling layer and the no-service-account-fallback
test. Written to be checkable: every claim below is either "this file exists"
or "this command was run and here is its real output".

Environment: Python 3.13.15, pytest 9.1.1, `middle_tier/.venv`. Everything
here runs offline. Nothing in this document is a simulated result.

---

## 1. "Exists" vs "executed successfully"

| item | exists | executed successfully |
|---|---|---|
| `app/errors/taxonomy.py` | yes | yes — imported and exercised by 54 tests |
| `app/errors/classify.py` | yes | yes — 13 classification tests, real error strings |
| `app/errors/templates.py` | yes | yes — every template rendered and asserted |
| `app/errors/boundary.py` | yes | yes — interceptor, leak guard and refusal path all run |
| `app/errors/__init__.py` (compat surface) | yes | yes — the pre-existing suite still passes through it |
| `tests/test_error_templates.py` | yes | yes — **30 passed** |
| `tests/test_no_service_account_fallback.py` | yes | yes — **24 passed** |
| `app/errors/README.md` | yes | n/a |
| Sign-in card renders correctly in a real Teams client | **no** | **BLOCKED** — see §6 |
| Live re-observation of the two 403s | **no** | **BLOCKED** — see §6 |
| Live OBO/STS failure against tenant `<ENTRA_TENANT_ID>` | **no** | **BLOCKED** — see §6 |

The classifier is validated against error *strings*, one of which is verified
(quoted in ADR 004 from the earlier spike) and three of which are clearly
labelled SYNTHETIC in `classify.py`. No live 403 was produced in this session.

---

## 2. What exists

```
middle_tier/app/errors/
├── __init__.py     package surface; re-exports the old app/errors.py API unchanged
├── taxonomy.py     IdentityAcquisitionError(stage=teams_sso|obo|sts), MissingEntraObjectId,
│                   DownstreamAuthorizationDenied(resource=…), UpstreamUnavailable
│                   + template_fields() (narrow) vs log_fields() (everything)
├── classify.py     (status, message) -> taxonomy; the two-403s distinction; verified fixture
├── templates.py    identity failure + sign-in card; denial naming the resource; OAuthCard
├── boundary.py     tool-boundary interceptor; model gets one sentence; leak guard
├── README.md       the two paths, two templates, two 403s, rejected option
└── NOTES.md        this file

middle_tier/tests/
├── test_error_templates.py            30 tests
└── test_no_service_account_fallback.py 24 tests
```

Design points worth knowing before editing:

- **The asymmetry is in the type, not just the comments.** Every error exposes
  `template_fields()` (what the user template may interpolate) and
  `log_fields()` (everything, including the full raw upstream text). The
  model's view is narrower than both and is produced only by
  `boundary.model_facing()`, which re-checks its own output against the raw
  text and raises `ModelContextLeak` if any fragment, URL or `roles/…` token
  from the upstream error slipped through.
- **A credential-type 403 is routed to path 1, not path 2**, with
  `design_fatal=True`, and `_log_level_for()` puts it at CRITICAL. Reason: no
  role grant fixes it, so rendering "ask for a role" would send an admin
  hunting for something that was never the problem.
- **A 404 is not rendered as a denial.** We cannot tell "absent" from "hidden";
  ADR 004's own argument forbids guessing.
- **An unrecognised 403** is still refused and still rendered as a denial, but
  carries `confidently_classified=False` and is logged at ERROR.
- **`taxonomy` and `boundary` are duck-typed** against `app.ports` and
  `app.identity.errors` rather than importing them. Those modules are owned by
  other people and were being refactored while this was written; an
  error-handling layer that fails to import is worse than useless.

---

## 3. Overlap with other workers' code

**`middle_tier/app/errors.py` (single module) already existed** and contained a
good first pass at the ADR 004 templates. Python resolves a *package* before a
module of the same name, so `app/errors/` now shadows it and `import
app.errors` lands in this package.

Handled as follows:

- `app/errors/__init__.py` re-exports **every name the old module exported**
  with the same signatures and the same observable behaviour
  (`identity_failure`, `missing_entra_object_id`, `downstream_denial`,
  `transient_failure`, `signin_card`, `welcome`, `conversation_reset`,
  `unsupported_activity`, `SIGNIN_CARD_CONTENT_TYPE`,
  `ADAPTIVE_CARD_CONTENT_TYPE`, …). `app/routing.py` and
  `tests/test_app_smoke.py` were not touched and still pass — proof in §4.
- One rename: the classifier function is exported as **`errors.classify_failure`**,
  not `errors.classify`, because `errors.classify` is the submodule and having
  a function shadow it is a landmine (importing the submodule anywhere would
  silently rebind the name). `errors.classify_exception` is also available.
- `app/errors.py` is now unreachable code. This package does not import it, so
  **deleting it is safe and changes nothing**. I did not delete it — it is
  another worker's file. Recommend deleting it in a cleanup pass.

Other overlaps, left alone deliberately:

- `app/identity/errors.py` has its own richer identity-failure hierarchy
  (`OboConsentRequired`, `StsSubjectTokenRejected`, …). Not duplicated:
  `taxonomy.from_identity_error()` adapts any of them onto the three ADR 004
  stages via a documented alias table.
- `app/ports.py` defines `AuthorizationDenied` / `IdentityUnavailable` /
  `TransientBackendError`. Not duplicated: `taxonomy.from_port_error()` adapts
  them.
- `tests/test_session_manager.py` (another worker) already has a
  `test_missing_access_token_fails_closed_no_service_account_fallback`. Mine
  does not replace it; mine scans the whole tree rather than one call path.

---

## 4. Tests that were RUN — real output

### 4.1 `tests/test_error_templates.py` — 30 passed

```
$ cd middle_tier && .venv/bin/python -m pytest tests/test_error_templates.py -v
platform linux -- Python 3.13.15, pytest-9.1.1, pluggy-1.6.0
rootdir: <REPO_ROOT>/middle_tier
configfile: pyproject.toml
plugins: asyncio-1.4.0
collected 30 items

tests/test_error_templates.py::test_identity_failure_names_the_problem_and_carries_a_signin_card PASSED [  3%]
tests/test_error_templates.py::test_identity_failure_without_a_url_refuses_without_a_broken_button PASSED [  6%]
tests/test_error_templates.py::test_identity_failure_never_leaks_upstream_detail_into_the_message PASSED [ 10%]
tests/test_error_templates.py::test_oauth_card_shape_matches_the_documented_teams_shape PASSED [ 13%]
tests/test_error_templates.py::test_missing_aad_object_id_refuses_and_offers_no_signin_loop PASSED [ 16%]
tests/test_error_templates.py::test_missing_aad_object_id_is_routed_to_its_own_template_by_render PASSED [ 20%]
tests/test_error_templates.py::test_denial_names_the_refused_resource PASSED [ 23%]
tests/test_error_templates.py::test_denial_without_a_resource_is_a_programming_error PASSED [ 26%]
tests/test_error_templates.py::test_denial_quotes_a_named_role_only_when_the_upstream_text_named_one PASSED [ 30%]
tests/test_error_templates.py::test_transient_failure_is_not_worded_as_a_permission_problem PASSED [ 33%]
tests/test_error_templates.py::test_verified_service_usage_consumer_403_is_a_missing_role_permission_error PASSED [ 36%]
tests/test_error_templates.py::test_bigquery_table_and_dataset_403s_also_classify_as_denials PASSED [ 40%]
tests/test_error_templates.py::test_credential_type_403_is_an_identity_failure_not_a_missing_role PASSED [ 43%]
tests/test_error_templates.py::test_missing_role_wins_over_an_incidental_mention_of_a_service_account PASSED [ 46%]
tests/test_error_templates.py::test_unrecognised_403_is_still_refused_but_flagged_for_a_human PASSED [ 50%]
tests/test_error_templates.py::test_403_message_text_is_never_swallowed PASSED [ 53%]
tests/test_error_templates.py::test_404_is_not_rendered_as_a_denial PASSED [ 56%]
tests/test_error_templates.py::test_5xx_and_429_are_retryable_upstream_failures[500] PASSED [ 60%]
tests/test_error_templates.py::test_5xx_and_429_are_retryable_upstream_failures[502] PASSED [ 63%]
tests/test_error_templates.py::test_5xx_and_429_are_retryable_upstream_failures[503] PASSED [ 66%]
tests/test_error_templates.py::test_5xx_and_429_are_retryable_upstream_failures[429] PASSED [ 70%]
tests/test_error_templates.py::test_401_is_an_identity_failure_with_a_stage PASSED [ 73%]
tests/test_error_templates.py::test_resource_extraction_never_guesses PASSED [ 76%]
tests/test_error_templates.py::test_model_is_told_only_that_access_was_denied_and_to_what PASSED [ 80%]
tests/test_error_templates.py::test_raw_iam_error_never_reaches_the_model PASSED [ 83%]
tests/test_error_templates.py::test_identity_failure_gives_the_model_nothing_at_all PASSED [ 86%]
tests/test_error_templates.py::test_leak_guard_fires_if_someone_widens_the_model_view PASSED [ 90%]
tests/test_error_templates.py::test_intercept_splits_the_three_audiences PASSED [ 93%]
tests/test_error_templates.py::test_guard_tool_call_returns_no_data_on_denial PASSED [ 96%]
tests/test_error_templates.py::test_guard_tool_call_passes_success_straight_through PASSED [100%]

============================== 30 passed in 0.09s ==============================
```

The four claims the brief asked for, and the tests that carry them:

| claim | test |
|---|---|
| identity failure yields the message AND a sign-in card | `test_identity_failure_names_the_problem_and_carries_a_signin_card` |
| downstream denial names the resource | `test_denial_names_the_refused_resource`, `test_intercept_splits_the_three_audiences` |
| raw IAM text NEVER reaches the model | `test_raw_iam_error_never_reaches_the_model`, `test_leak_guard_fires_if_someone_widens_the_model_view` |
| verified serviceUsageConsumer 403 = missing-role permission error | `test_verified_service_usage_consumer_403_is_a_missing_role_permission_error` |

### 4.2 `tests/test_no_service_account_fallback.py` — 24 passed

```
$ cd middle_tier && .venv/bin/python -m pytest tests/test_no_service_account_fallback.py -v
collected 24 items

tests/test_no_service_account_fallback.py::test_required_trees_exist PASSED [  4%]
tests/test_no_service_account_fallback.py::test_no_unallowlisted_service_identity_in_code PASSED [  8%]
tests/test_no_service_account_fallback.py::test_error_layer_itself_imports_no_credential_library PASSED [ 12%]
tests/test_no_service_account_fallback.py::test_scan_coverage_is_reported PASSED [ 16%]
tests/test_no_service_account_fallback.py::test_broker_failure_refuses_the_turn_and_returns_no_credential PASSED [ 20%]
tests/test_no_service_account_fallback.py::test_broker_returning_nothing_is_a_failure_not_a_downgrade PASSED [ 25%]
tests/test_no_service_account_fallback.py::test_no_outcome_field_can_carry_a_credential PASSED [ 29%]
tests/test_no_service_account_fallback.py::test_a_denied_tool_is_never_retried_under_another_identity PASSED [ 33%]
tests/test_no_service_account_fallback.py::test_taxonomy_has_no_type_that_can_express_a_fallback PASSED [ 37%]
tests/test_no_service_account_fallback.py::test_missing_aad_object_id_is_refused_end_to_end PASSED [ 41%]
tests/test_no_service_account_fallback.py::test_refusal_for_a_missing_oid_never_shows_or_uses_the_mri PASSED [ 45%]
tests/test_no_service_account_fallback.py::test_user_key_format_is_the_adr_003_shape PASSED [ 50%]
tests/test_no_service_account_fallback.py::test_scanner_catches_a_planted_adc_fallback PASSED [ 54%]
tests/test_no_service_account_fallback.py::test_scanner_catches_every_documented_danger_pattern[from google.oauth2 import service_account-service_account_identifier] PASSED [ 58%]
tests/test_no_service_account_fallback.py::test_scanner_catches_every_documented_danger_pattern[creds = service_account.Credentials.from_service_account_file(p)-from_service_account] PASSED [ 62%]
tests/test_no_service_account_fallback.py::test_scanner_catches_every_documented_danger_pattern[os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/k.json"-app_default_credentials_env] PASSED [ 66%]
tests/test_no_service_account_fallback.py::test_scanner_catches_every_documented_danger_pattern[from google.auth import impersonated_credentials-impersonated_credentials] PASSED [ 70%]
tests/test_no_service_account_fallback.py::test_scanner_catches_every_documented_danger_pattern[from google.auth import compute_engine-compute_engine_credentials] PASSED [ 75%]
tests/test_no_service_account_fallback.py::test_scanner_catches_every_documented_danger_pattern[r = get("http://metadata.google.internal/computeMetadata/v1/token")-metadata_server_host] PASSED [ 79%]
tests/test_no_service_account_fallback.py::test_scanner_catches_every_documented_danger_pattern[r = get("http://169.254.169.254/computeMetadata/v1/token")-metadata_server_ip] PASSED [ 83%]
tests/test_no_service_account_fallback.py::test_scanner_ignores_prose_but_not_code_on_the_same_topic PASSED [ 87%]
tests/test_no_service_account_fallback.py::test_declaration_carveout_does_not_hide_the_body PASSED [ 91%]
tests/test_no_service_account_fallback.py::test_allowlisted_hit_still_fails_without_an_inline_justification PASSED [ 95%]
tests/test_no_service_account_fallback.py::test_unlisted_path_is_never_allowlisted PASSED [100%]
============================== 24 passed in 9.14s ==============================
```

### 4.3 The whole middle-tier suite still passes with this package shadowing `app/errors.py`

```
$ cd middle_tier && .venv/bin/python -m pytest -q
270 passed, 2 skipped in 8.77s
```

(Final run, 2026-09-07 16:24 CEST. 54 of those are mine; the rest are other
workers' and were growing throughout the session — an earlier run in the same
session read `252 passed, 2 skipped`. The 2 skips are theirs, not mine, and
predate this work. No test in the pre-existing suite was modified.)

### 4.4 The scanner is not passing vacuously

`test_no_unallowlisted_service_identity_in_code` passing over a clean tree is
indistinguishable from a broken scanner passing. So seven planted violations —
one per documented danger pattern — are written into a throwaway tree and the
scanner is required to catch every one
(`test_scanner_catches_every_documented_danger_pattern`, all 7 PASSED above),
plus a planted ADC fallback inside a realistic `except:` branch. Two further
tests prove the allowlist cannot be used to smuggle something through: an
allowlisted hit **without** an inline `sa-allow:` justification still fails,
and a path outside the allowlist glob is never matched.

While the scanner was being written it fired for real on two lines (test
function *names* containing `service_account`). That is what produced the
declaration carve-out described in §5.

---

## 5. Exactly what the fallback scan covered

Real output of `test_scan_coverage_is_reported` (printed by the test itself, so
the coverage claim is measured rather than asserted in prose):

```
=== service-account fallback scan coverage ===
SCANNED (fatal)      middle_tier/  files=43  fatal-surface hits=0  declaration-only=2
SCANNED (fatal)      agent/  files=9  fatal-surface hits=0  declaration-only=0
SCANNED (report-only) spikes/  files=2 danger-surface hits=8 declaration-only=2
    spikes/mcp_identity_spike.py:51: [app_default_credentials_env] os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path
    spikes/mcp_identity_spike.py:55: [google_auth_default] creds, _ = google.auth.default(
    spikes/mcp_identity_spike.py:55: [bare_default_call] creds, _ = google.auth.default(
    spikes/mcp_identity_spike.py:64: [service_account_identifier] from google.oauth2 import service_account
    spikes/mcp_identity_spike.py:66: [service_account_identifier] creds = service_account.Credentials.from_service_account_file(
    spikes/mcp_identity_spike.py:66: [from_service_account] creds = service_account.Credentials.from_service_account_file(
    spikes/mcp_identity_spike.py:201: [service_account_identifier] token = token_from_service_account(args.sa_key)
    spikes/mcp_identity_spike.py:201: [from_service_account] token = token_from_service_account(args.sa_key)
SCANNED (report-only) layer3/  files=3 danger-surface hits=1 declaration-only=0
    layer3/tokens.py:35: [gcloud_adc_file] ADC_FILE = pathlib.Path.home() / ".config/gcloud/application_default_credentials.json"
SCANNED (report-only) terraform/  files=7 danger-surface hits=5 declaration-only=0
    terraform/cloud_run.tf:19: [service_account_identifier] resource "google_service_account" "bot_middle_tier" {
    terraform/cloud_run.tf:109: [service_account_identifier] member    = "serviceAccount:${google_service_account.bot_middle_tier.email}"
    terraform/cloud_run.tf:147: [service_account_identifier] service_account = google_service_account.bot_middle_tier.email
    terraform/outputs.tf:60: [service_account_identifier] output "middle_tier_service_account_email" {
    terraform/outputs.tf:62: [service_account_identifier] value       = google_service_account.bot_middle_tier.email
SCANNED (report-only) bigquery/  files=1 danger-surface hits=0 declaration-only=0
SCANNED (report-only) entra/  files=1 danger-surface hits=0 declaration-only=0
total files scanned in fatal mode: 52
declaration-only hits in required trees (names of tests asserting the absence; not fatal): 2
    middle_tier/tests/test_error_templates.py:181: [service_account_identifier] def test_missing_role_wins_over_an_incidental_mention_of_a_service_account():
    middle_tier/tests/test_session_manager.py:542: [service_account_identifier] async def test_missing_access_token_fails_closed_no_service_account_fallback():
prose mentions of 'service account' in required trees (not fatal): 40
```

**Covered in fatal mode:** `middle_tier/` and `agent/`, 52 files total,
**0 fatal hits**.

> The capture above was taken mid-session, when `agent/` held only its two
> manifests, and I originally recorded the `agent/` result as thin evidence
> about an empty tree. That is no longer true: the agent source landed while
> this document was being written. The scan was **re-run** afterwards and still
> reports `agent/ files=9 fatal-surface hits=0`, now covering
> `agent/bq_agent/{__init__,agent,credentials,errors}.py`, `agent/deploy.py`
> and three agent test files. I checked that clean result by hand rather than
> trusting it: `agent/bq_agent/credentials.py` (488 lines) contains no
> occurrence of `google.auth`, `Credentials`, `service_account`,
> `impersonat*`, `metadata` or `GOOGLE_APPLICATION_CREDENTIALS` at all — it
> imports nothing from any credential library and carries the user's
> `access_token` string only. The clean scan is real, not a blind spot.
> `middle_tier/` also grew to 44 files in that time, still with 0 fatal hits.

**Not covered, and why:**
- `spikes/`, `layer3/`, `terraform/`, `bigquery/`, `entra/` are scanned in
  **report-only** mode and their hits are printed above rather than failing the
  build: a spike exploring service-account auth is what a spike is for, and
  Terraform provisioning a runtime service identity is infrastructure, not a
  credential fallback in the user path. Numbers are reported so nobody has to
  take that on trust.
- `docs/` and `.venv/`, `__pycache__/`, `.terraform/`, `site-packages/` are not
  scanned at all.
- Only these suffixes/filenames are scanned: `.py`, `.pyi`, `.sh`, `.bash`,
  `.toml`, `.cfg`, `.ini`, `.yaml`, `.yml`, `.json`, `.tf`, `.env`, plus
  `Dockerfile`, `Procfile`, `entrypoint.sh`. A fallback written in a file type
  outside that list would not be seen.

**Two deliberate carve-outs, both visible in the output above:**

1. **Comments and docstrings are stripped from Python before matching.** A
   docstring saying "there is no service account fallback here" must not fail
   the build; `from google.oauth2 import service_account` must. String literals
   are *kept*, because `os.environ["GOOGLE_APPLICATION_CREDENTIALS"]` is code.
   The prose form is counted separately and reported (40 mentions, all of them
   documentation of the rejected option).
2. **A `def`/`class` NAME containing a danger token is declaration-only.** A
   name cannot acquire a credential; the body under it is scanned normally, and
   `test_declaration_carveout_does_not_hide_the_body` proves it. The two
   declaration-only hits are both test names asserting the *absence* of the
   fallback, one of them another worker's.

**The allowlist is currently EMPTY.** Nothing in `middle_tier/` or `agent/`
needs a service identity today. When something does (Secret Manager at
startup, the agent's own model calls), it needs both an `ALLOWLIST` entry with
a written reason *and* an inline `sa-allow:` comment on the line — the tests in
§4.4 prove one without the other still fails.

### Behavioural layers (b) and (c)

- **(b)** `test_broker_failure_refuses_the_turn_and_returns_no_credential`: the
  broker raises, the turn is refused, the token is `None`, `outcome.credential`
  is `None`, and `test_no_outcome_field_can_carry_a_credential` walks every
  field of the refusal object asserting nothing token-shaped is populated.
  `test_a_denied_tool_is_never_retried_under_another_identity` counts attempts
  and asserts exactly one.
- **(c)** `test_missing_aad_object_id_is_refused_end_to_end` ran against the
  real extractor — it printed `[c] caller-identity extractor used:
  app.caller_identity`, i.e. it did **not** skip. Missing `aadObjectId` raises;
  with it present the key is `entra:{tid}:{oid}`; the Teams MRI
  (`29:…`) appears nowhere in the key, and the refusal template neither shows
  nor stores it. If that module is ever moved, the test looks in
  `app.identity` too and otherwise skips loudly rather than passing silently.

---

## 6. BLOCKED items

Nothing was faked to avoid these. Each one needs credentials or a deployed
endpoint that this sandbox does not have (no network, no Entra secret, no
Google session).

1. **BLOCKED: the sign-in card was never rendered in a real Teams client.**
   The shape is built from the published card schema (§7) and asserted
   structurally, which is not the same as seeing Teams draw it. Needs a
   deployed bot endpoint and a Teams tenant install.
   ```
   # after deploying the middle tier and side-loading entra/manifest/manifest.json:
   # DM the bot from a Teams account in tenant <ENTRA_TENANT_ID>
   # with the identity broker deliberately unconfigured, and confirm the
   # sign-in button renders and is clickable.
   ```
2. **BLOCKED: the two 403s were not re-observed live in this session.** The
   `serviceUsageConsumer` message is quoted from the earlier spike via ADR 004,
   not re-captured. To re-verify:
   ```
   gcloud auth print-access-token > /dev/null   # as a principal WITHOUT roles/serviceusage.serviceUsageConsumer
   curl -sS -H "Authorization: Bearer $(gcloud auth print-access-token)" \
        -H "Content-Type: application/json" \
        -X POST "https://bigquery.googleapis.com/bigquery/v2/projects/<GCP_PROJECT_ID>/jobs" \
        -d '{"configuration":{"query":{"query":"SELECT 1","useLegacySql":false}}}'
   ```
   Expect HTTP 403 with `Caller does not have required permission to use
   project <GCP_PROJECT_ID>...`. Feed the captured string back into
   `classify.SERVICE_USAGE_CONSUMER_403` if it has drifted.
3. **BLOCKED: no live OBO or STS failure was produced.** The identity path is
   tested with a raising fake broker, not with Entra. To exercise for real:
   ```
   # needs the federation app secret for <FEDERATION_APP_CLIENT_ID>
   curl -sS -X POST "https://login.microsoftonline.com/<ENTRA_TENANT_ID>/oauth2/v2.0/token" \
     -d grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer \
     -d client_id=<FEDERATION_APP_CLIENT_ID> \
     -d client_secret="$FEDERATION_APP_SECRET" \
     -d assertion="$TEAMS_SSO_TOKEN" -d scope="openid" -d requested_token_use=on_behalf_of
   # then exchange at https://sts.googleapis.com/v1/token for the workforce pool
   # principal://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/subject/{entra_oid}
   ```
4. **BLOCKED: the credential-type 403 has never been observed.** Its fixture is
   labelled `CREDENTIAL_TYPE_403_SYNTHETIC` in `classify.py` and the label is
   load-bearing — the fixture proves the classifier's handling, not that the
   failure occurs. If it is ever seen in production, the CRITICAL log line is
   the trigger to revisit the design, not just the regex.
5. **Not blocked, but noted:** the workspace was being edited by other workers
   throughout this session (the agent source and several middle-tier modules
   landed while this was being written). Every number in §4 and §5 comes from a
   run made after those changes, but the tree will keep moving — re-run both
   test files rather than trusting the counts.

---

## 7. Doc URLs for the sign-in card shape

Checked 2026-09-07.

- Bot Framework card schema — sign-in card is `contentType`
  `application/vnd.microsoft.card.signin`, with `content.text` and
  `content.buttons[]`, each button a `cardAction` of type `signin` whose
  `value` is the sign-in URL:
  <https://github.com/microsoft/botframework-sdk/blob/main/specs/botframework-activity/botframework-cards.md>
- Teams cards reference (which Bot Framework cards Teams renders, sign-in card
  included):
  <https://learn.microsoft.com/en-us/microsoftteams/platform/task-modules-and-cards/cards/cards-reference>
- Add authentication to a Teams bot — OAuthCard
  (`application/vnd.microsoft.card.oauth`) with `connectionName` and
  `tokenExchangeResource`:
  <https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/authentication/add-authentication>
- Teams SSO overview (silent `signin/tokenExchange`):
  <https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/authentication/bot-sso-overview>
- Adaptive Card universal action auth flow — the `401` +
  `application/vnd.microsoft.activity.loginRequest` invoke response:
  <https://learn.microsoft.com/en-us/microsoftteams/platform/task-modules-and-cards/cards/universal-actions-for-adaptive-cards/authentication-flow-in-universal-action-for-adaptive-cards>

Both shapes are implemented: `signin_card()` (works without an Azure Bot OAuth
connection) and `oauth_card()` (preferred where one exists, because Teams can
satisfy it silently — relevant given ~3600s Google credential lifetime makes
mid-conversation expiry normal rather than exceptional).

---

## 8. Suggested next steps for whoever picks this up

1. Delete the now-unreachable `middle_tier/app/errors.py` (§3).
2. Wire `boundary.guard_tool_call()` into the agent's BigQuery tool as its only
   error path, so the model literally cannot receive an IAM error.
3. Re-capture the live 403 (§6.2) and replace the fixture if the wording drifted.
4. `agent/bq_agent/errors.py` (450 lines, landed late in this session) looks
   like it overlaps this layer. Nobody has reconciled the two yet: decide
   whether the agent-side module defers to `app.errors.boundary` for tool
   denials, or whether it is doing something genuinely agent-local. Two
   implementations of the ADR 004 denial path is exactly the drift this
   package was meant to prevent.
