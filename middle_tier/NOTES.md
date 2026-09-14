# Bot Middle Tier — build notes

Date: 2026-09-07. Python 3.13.15, Linux x86_64.

This file separates **"this code exists"** from **"this code was executed
successfully"**. Where something was not run, it says so and gives the command
that would run it.

---

## 1. Executive summary

| | |
| --- | --- |
| Files written | 17 under `middle_tier/` |
| Tests written | 94 |
| Tests **actually run** | 94, all passing (real output in §5) |
| Mutation checks run | 6, all caught (real output in §6) |
| Live network verification | Yes — real Bot Framework JWKS, 274 keys (§7) |
| Service started and served traffic | Yes — `/healthz`, `/readyz`, 401 paths (§8) |
| BLOCKED items | 1 (container build — §10) |
| Defects found and fixed during verification | 3 (§9) |
| Decisions needing arbitration | 1 (module name collision — §11) |

The security-critical path — inbound JWT validation — is implemented, tested
against real RSA signatures and a real JWKS, and independently verified by
mutation testing that proves the tests fail when the validator is broken.

---

## 2. SDK vs PyJWT: the decision

**Decision: hand-roll on PyJWT[crypto], in one small auditable module
(`app/auth/inbound.py`, ~470 lines including docs).**

The brief said to prefer the SDK if it is maintained and correct, because
hand-rolled JWT validation is where subtle vulnerabilities live. I agree with
that instinct. I went the other way, and here is the evidence, all of it
obtained by execution rather than by reading blog posts.

### 2.1 The Bot Framework Python SDK is dead

`botbuilder-core` / `botbuilder-integration-aiohttp` — the `CloudAdapter` /
`ConfigurationBotFrameworkAuthentication` path named in the brief — is
end-of-life. From the `microsoft/botbuilder-python` repository README:

> ARCHIVE NOTICE: We are in the process of archiving the Bot Framework Python
> SDK repository on GitHub. This means that this project will no longer be
> updated or maintained. […] Support tickets for the Bot Framework SDK will no
> longer be serviced as of December 31, 2025. We plan to archive this project
> no later than end of December of 2025.

Source: <https://github.com/microsoft/botbuilder-python>

That disqualifies it. Putting an archived, unpatched dependency on the one code
path that stands between the internet and user impersonation is not a
defensible trade, however good the code was on the day it froze. **This is a
real finding and it directly contradicts the framing in the brief**, which
assumed `botbuilder-*` was the maintained path.

### 2.2 The maintained successor is a hosting framework, and has gaps

The successor is the **Microsoft 365 Agents SDK**
(`microsoft-agents-hosting-aiohttp`), GA since 1.0.0 (2026-05-22), currently
1.5.0 (2026-08-26). It installs cleanly:

```
$ python3 -m venv /tmp/probe_ms && /tmp/probe_ms/bin/pip install microsoft-agents-hosting-aiohttp
$ /tmp/probe_ms/bin/pip list
microsoft-agents-activity          1.5.0
microsoft-agents-hosting-aiohttp   1.5.0
microsoft-agents-hosting-core      1.5.0
PyJWT                              2.13.0
...
```

Three things I found by reading and running the installed package:

**(a) It is PyJWT underneath anyway.** `JwtTokenValidator` in
`microsoft_agents/hosting/core/authorization/jwt/jwt_token_validator.py` uses
`PyJWKClient` and `jwt.decode`. So "use the SDK" does not avoid PyJWT; it wraps
it. The choice is not *PyJWT vs something safer*, it is *my ~470 lines vs their
wrapper around the same library*.

**(b) Its dependency closure omits `cryptography`, so RS256 does not work out
of the box.** Executed:

```
$ /tmp/probe_ms/bin/python -c "
import importlib.util as u
print('cryptography present:', u.find_spec('cryptography') is not None)
import jwt; from jwt.algorithms import get_default_algorithms
print('jwt version', jwt.__version__)
print('RS256 available:', 'RS256' in get_default_algorithms())"

cryptography present: False
jwt version 2.13.0
RS256 available: False
```

Every real Bot Framework token is RS256. A fresh install of the maintained SDK
cannot verify one until you separately add `pyjwt[crypto]`. That is a packaging
defect in the thing I was being asked to trust as the safer option.

**(c) Its `serviceUrl` binding warns and continues by default.** From
`microsoft_agents/hosting/core/_http_adapter_base.py`, on a claim-vs-activity
host mismatch:

```python
if self._host_validator and self._host_validator.enabled:
    logger.warning("Service URL host mismatch: %s vs %s", ...)
    return False
else:
    logger.warning("Service URL host mismatch (host validator disabled): %s vs %s", ...)
    # falls through — does not reject
```

`serviceUrl` binding is what stops a captured token being replayed with a body
that redirects the conversation to an attacker's host. Unless you separately
enable a host validator, the SDK logs it and proceeds. Our implementation
rejects, unconditionally, and mutation M5 (§6) proves the test suite catches
any attempt to soften that back to a warning.

Separately, issuer validation (`VALIDATE_ISSUER`) is opt-in in that validator
for backward compatibility.

### 2.3 Why not adopt the Agents SDK anyway

Beyond the gaps: `microsoft-agents-hosting-*` is a full hosting framework. It
owns the application shape (`AgentApplication`, `start_agent_process`, state,
storage, its own OAuth flow handlers). Adopting it for token validation means
adopting all of that, into a service whose entire job is to be a thin relay
with no prompt logic and no model calls. That is a large, opinionated
dependency surface bought for one function we can express in a few hundred
well-tested lines.

### 2.4 How I bounded the hand-rolling risk

The honest objection to hand-rolling stands, so:

- **No custom crypto.** Signature verification, claim checks and skew handling
  are all `jwt.decode` with explicit `algorithms=`, `audience=`, `issuer=`,
  `leeway=` and `options={"require": [...]}`. The hand-rolled part is
  *policy* — which issuer, which key, which extra claims — not cryptography.
- **Algorithm allow-list is ours, never the token's.** RSA only. `alg: none`
  is rejected before any I/O; a symmetric `alg` is rejected on the header
  check. Tested (§5) and mutation-checked (§6).
- **No bypass switch exists.** No `allow_anonymous`, no `skip_validation`, no
  "if not app_id: return". Two AST-based tripwire tests fail the build if one
  is added, and a third asserts `verify_signature: False` appears in exactly
  one function (`_select_profile`, the unverified issuer read used only to
  route to a JWKS — the issuer is then re-imposed cryptographically).
- **Mutation-tested.** §6 breaks the validator six ways and confirms the suite
  goes red each time. A green suite that stays green when you delete the
  audience check is worthless; this proves ours does not.

### 2.5 What would change my mind

If the Agents SDK ships `cryptography` in its dependency closure and makes
`serviceUrl` binding and issuer validation enforce-by-default, revisit. The
seam is deliberately narrow — `InboundActivityAuthenticator.authenticate()`
returning an `AuthenticatedCaller` — so swapping the implementation is a
contained change.

**ADR-006 (proposed): the middle tier validates inbound Bot Framework tokens
with PyJWT directly rather than via a Microsoft SDK, because the SDK named in
the brief is archived and its maintained successor has verified defects in the
exact code path we depend on.**

---

## 3. Dependency manifest: `pyproject.toml` + a lock file

Asked to choose; chose `pyproject.toml`, plus `requirements.lock.txt`.

- `app/` is an importable package that `tests/` imports, so it deserves a real
  package manifest rather than sys.path luck.
- Runtime deps, dev deps and pytest config live in one auditable file.
- `pyproject.toml` pins **direct** deps with `==`. `requirements.lock.txt` is
  the **transitive** pin set generated from the venv the tests actually ran in
  (`pip freeze`), and it is what the Dockerfile installs. A security boundary
  should be byte-identical between the image tested and the image deployed.
- Conflating those two jobs into one `requirements.txt` is why "works on my
  machine" happens.

`PyJWT[crypto]` and an explicit `cryptography==50.0.1` pin are both listed, on
purpose, given §2.2(b).

---

## 4. What exists

All paths relative to `middle_tier/`.

| File | Status |
| --- | --- |
| `pyproject.toml` | Written. Used by the test run (pytest config read from it). |
| `requirements.lock.txt` | Generated from the real venv. 38 pins. |
| `app/auth/inbound.py` | Written **and executed** — 42 tests + live JWKS + live 401s. |
| `app/caller_identity.py` | Written **and executed** — 21 tests. (Name: see §11.) |
| `app/ports.py` | Written. Imported and type-referenced by executed code; the Protocols themselves have no implementations yet by design. |
| `app/routing.py` | Written **and executed** — routing/reset/invoke/unknown-type tests. |
| `app/errors.py` | Written **and executed** — template tests. |
| `app/config.py` | Written **and partly executed** — dev-mode guardrails and the env fallback are tested; the **real Secret Manager call is NOT executed** (§10). |
| `app/logging_utils.py` | Written **and executed** — redaction tests + live structured output. |
| `app/main.py` | Written **and executed** — served real HTTP (§8). |
| `Dockerfile` | Written. **NOT built** (§10). |
| `.dockerignore` | Written. Not exercised. |
| `README.md` | Written. |
| `tests/conftest.py`, `tests/test_inbound_auth.py`, `tests/test_identity.py`, `tests/test_app_smoke.py` | Written and run. |
| `tests/mutation_check.py` | Written and run (§6). |
| `tests/live_jwks_probe.py` | Written and run (§7). Needs egress; not part of the offline suite. |

`tests/test_app_smoke.py` was not requested. I added it so `main.py`,
`routing.py`, `errors.py` and `config.py` would be *executed* rather than
merely written — otherwise this file could not honestly claim anything about
them.

---

## 5. Tests: REAL output

Command:

```
cd middle_tier && .venv/bin/python -m pytest -v
```

Genuine output (trimmed in the middle for length; the head, the tail and the
count are verbatim):

```
============================= test session starts ==============================
platform linux -- Python 3.13.15, pytest-9.1.1, pluggy-1.6.0 -- <REPO_ROOT>/middle_tier/.venv/bin/python
cachedir: .pytest_cache
rootdir: <REPO_ROOT>/middle_tier
configfile: pyproject.toml
testpaths: tests
plugins: asyncio-1.4.0
asyncio: mode=Mode.AUTO
collecting ... collected 94 items

tests/test_app_smoke.py::test_healthz_is_dependency_free PASSED          [  1%]
tests/test_app_smoke.py::test_messages_rejects_missing_authorization_with_401_and_no_detail PASSED [  2%]
tests/test_app_smoke.py::test_messages_rejects_forged_token_with_401 PASSED [  3%]
tests/test_app_smoke.py::test_messages_rejects_non_json_body PASSED      [  4%]
tests/test_app_smoke.py::test_valid_token_reaches_the_router PASSED      [  5%]
tests/test_app_smoke.py::test_guest_user_without_aad_object_id_gets_the_fail_closed_message PASSED [  6%]
...
tests/test_identity.py::test_missing_aad_object_id_fails_closed[absent] PASSED [ 39%]
tests/test_identity.py::test_missing_aad_object_id_fails_closed[null] PASSED [ 40%]
tests/test_identity.py::test_missing_aad_object_id_fails_closed[empty] PASSED [ 41%]
tests/test_identity.py::test_missing_aad_object_id_fails_closed[whitespace] PASSED [ 42%]
tests/test_identity.py::test_missing_aad_object_id_never_falls_back_to_teams_mri PASSED [ 43%]
tests/test_identity.py::test_teams_mri_is_never_the_user_key_even_on_the_happy_path PASSED [ 44%]
tests/test_identity.py::test_identity_module_never_reads_from_id_for_identity PASSED [ 45%]
...
tests/test_inbound_auth.py::test_jwks_is_fetched_once_and_cached PASSED  [ 93%]
tests/test_inbound_auth.py::test_unknown_kid_triggers_exactly_one_refresh PASSED [ 94%]
tests/test_inbound_auth.py::test_key_rollover_is_picked_up_on_refresh PASSED [ 95%]
tests/test_inbound_auth.py::test_unreachable_jwks_fails_closed PASSED    [ 96%]
tests/test_inbound_auth.py::test_metadata_advertising_only_bad_algs_fails_closed PASSED [ 97%]
tests/test_inbound_auth.py::test_there_is_no_validation_bypass_switch PASSED [ 98%]
tests/test_inbound_auth.py::test_signature_verification_is_never_disabled_in_the_verify_path PASSED [100%]

============================== 94 passed in 0.98s ==============================
```

Quiet run, verbatim and complete:

```
$ .venv/bin/python -m pytest -q
........................................................................ [ 76%]
......................                                                   [100%]
94 passed in 0.65s
```

### The cases the brief asked for

All in `tests/test_inbound_auth.py`, all passing:

| Required case | Test |
| --- | --- |
| valid token passes | `test_valid_token_is_accepted` |
| wrong audience rejected | `test_wrong_audience_is_rejected` |
| expired rejected | `test_expired_token_is_rejected` |
| `alg: none` rejected | `test_alg_none_is_rejected` (+ `..._before_any_network_call`) |
| unknown signing key rejected | `test_token_signed_by_unknown_key_is_rejected` |
| tampered payload rejected | `test_tampered_payload_is_rejected` |

Plus, beyond the brief: symmetric-algorithm confusion, rogue key reusing a
*published* `kid` (the case where key lookup succeeds and only the signature
catches it), tampered `exp`, tampered `serviceUrl`, untrusted/missing issuer,
`serviceUrl` mismatch, missing `kid`, missing `exp`, skew boundary both ways,
JWKS caching, key rollover, unreachable JWKS, and startup-time refusals.

`tests/test_identity.py` covers ADR 003: the key format, and the fail-closed
behaviour on missing `aadObjectId` across absent/null/empty/whitespace. The
central test is
`test_missing_aad_object_id_never_falls_back_to_teams_mri`, which asserts not
merely that an exception is raised but that **no identity object was produced
and the MRI does not appear in the error**. There is also an AST tripwire,
`test_identity_module_never_reads_from_id_for_identity`, that fails if anyone
writes `sender.get("aadObjectId") or sender.get("id")`.

### These tests are not mocked where it matters

The suite generates a real RSA-2048 keypair, mints real signed tokens, and
serves a real OpenID metadata document and JWKS from a real aiohttp server on
an ephemeral loopback port. The code under test does its normal HTTP fetch and
its normal signature verification. Nothing in the crypto or transport path is
stubbed — mocking `jwt.decode` would let a validator that accepts `alg: none`
pass a test that claims to reject it. No external network is required.

One deliberate exception: the HS256 confusion token is assembled by hand,
because PyJWT's *encoder* refuses to use a PEM public key as an HMAC secret.
That refusal is PyJWT's protection on the signing side and tells us nothing
about our validator, so the test bypasses it.

---

## 6. Mutation testing: proof the tests are not vacuous

A passing suite is weak evidence unless it also fails when the thing it guards
is broken. `tests/mutation_check.py` applies six mutations to real files, runs
the real suite, and restores unconditionally.

```
$ .venv/bin/python tests/mutation_check.py
```

Real output:

```
=== BASELINE (unmutated) ===
exit=0  94 passed in 0.94s

--- M1: accept alg:none (remove the unsecured-JWS guard)
    CAUGHT: exit=1  1 failed, 93 passed in 1.14s

--- M2: stop verifying the audience
    CAUGHT: exit=1  1 failed, 93 passed in 1.17s

--- M3: stop verifying the signature
    CAUGHT: exit=1  6 failed, 88 passed in 0.96s

--- M4: stop verifying expiry
    CAUGHT: exit=1  1 failed, 93 passed in 0.95s

--- M5: downgrade serviceUrl mismatch to a warning (the SDK's behaviour)
    CAUGHT: exit=1  1 failed, 93 passed in 1.03s

--- M6: ADR 003 violation - fall back to the Teams MRI
    CAUGHT: exit=1  6 failed, 88 passed in 0.92s

=== RESTORED; re-running baseline ===
exit=0  94 passed in 0.55s
```

6/6 caught. Baseline restored green, verified by re-run. Note M5: the mutation
is precisely the maintained Microsoft SDK's default behaviour from §2.2(c), and
our suite rejects it.

---

## 7. Live verification against the real Bot Framework JWKS

Outbound HTTPS turned out to be available in this sandbox, so the production
key-resolution path was exercised for real, not just against the fake server.

```
$ .venv/bin/python tests/live_jwks_probe.py
bogus kid -> unknown_kid (expected: unknown_kid)
live JWKS keys fetched: 274
advertised+accepted algorithms: ['RS256']
first 3 real kids: ['-23FvVZ2sCsmGPgUF-nNEI60M9c', '-4XjYFrr90ngUgYhKhC47VrKeYA', '-nDBkmvbEGDoH1Ax-nzj-nBJWkg']
resolved real kid '-23FvVZ2sCsmGPgUF-nNEI60M9c' -> RSAPublicKey, algs=['RS256']
```

Confirms against **production Microsoft infrastructure**: the metadata document
at `https://login.botframework.com/v1/.well-known/openidconfiguration` parses,
its `jwks_uri` resolves, 274 real signing keys load into `PyJWK` objects, the
document advertises RS256 (so our allow-list intersection is non-empty against
the real thing, not just the fixture), an unknown `kid` produces `unknown_kid`
rather than an exception, and a real `kid` resolves to an `RSAPublicKey`.

What this does **not** prove: no genuine Azure-signed activity was validated
end to end, because that requires a registered bot and a Teams tenant. See §10.

---

## 8. The service was actually started and served traffic

```
$ MIDDLE_TIER_DEV_MODE=true MICROSOFT_APP_ID=... PORT=8098 .venv/bin/python -m app.main
```

Real responses:

```
=== GET /healthz ===
{"status": "ok"}

=== GET /readyz (hits LIVE botframework JWKS) ===
{"status": "ready", "checks": {"config": "ok", "app_id_configured": true, "jwks": "ok"}, "uptime_seconds": 6.8}

=== POST /api/messages  no Authorization ===
HTTP 401

=== POST /api/messages  alg:none forged token ===
HTTP 401
```

Real log output (note: structured JSON, `severity` set, secret *lengths* logged
but never values, and the 401 reason recorded server-side while the HTTP
response body stays empty so it is not an oracle):

```json
{"severity": "CRITICAL", "message": "DEV-ONLY SECRET FALLBACK IN USE - reading a secret from an environment variable instead of Secret Manager. This MUST NOT happen outside local development.", "logger": "app.config", "secret_id": "teams-bot-app-password", "env_var": "MICROSOFT_APP_PASSWORD", "value_length": 6}
{"severity": "INFO", "message": "middle tier initialised", "logger": "__main__", "project": "<GCP_PROJECT_ID>", "location": "us-central1", "tenant": "<ENTRA_TENANT_ID>", "app_type": "SingleTenant", "dev_mode": true, "emulator_trusted": false}
{"severity": "WARNING", "message": "inbound activity rejected", "logger": "__main__", "reason": "missing_authorization_header", "detail": "", "activity_type": "message", "channel_id": null, "remote": "127.0.0.1"}
{"severity": "WARNING", "message": "inbound activity rejected", "logger": "__main__", "reason": "alg_none_rejected", "detail": "", "activity_type": "message", "channel_id": null, "remote": "127.0.0.1"}
```

`/readyz` returning `"jwks": "ok"` is a live fetch of Microsoft's real key set
from inside the running service.

---

## 9. Defects found during verification, and fixed

Reported because they were found by running the code, which is the point.

**D1 — the most safety-critical log lines were not structured.** In the first
live run the two `DEV-ONLY SECRET FALLBACK` lines came out as plain text, not
JSON. Cause: `configure_logging()` ran inside `create_app()`, but
`get_settings()` — which reads the secrets and emits those CRITICALs — ran
first. Cloud Logging would have filed them as unparsed blobs with no
`severity`, so a log-based alert on `severity=CRITICAL` would **silently never
fire** — the exact alert this design depends on. Fixed by configuring logging
as the first statement in `main()`. Verified by re-running: the lines are now
`{"severity": "CRITICAL", ...}` (§8).

**D2 — `test_symmetric_alg_is_rejected` initially failed for the wrong
reason.** PyJWT's encoder raised `InvalidKeyError: The specified key is an
asymmetric key … should not be used as an HMAC secret`, so the test never
reached our validator. It was testing PyJWT's signing-side guard, not ours.
Fixed by hand-building the HS256 token. The test now also asserts zero JWKS
fetches occurred, proving rejection happens on the header check.

**D3 — the bypass tripwire matched its own documentation.** The first version
grepped the module text for `allow_anonymous` etc. and failed, because the
module docstring says those flags deliberately do not exist. Rewritten to walk
the AST and inspect actual identifiers. A second test was added asserting
`verify_signature: False` appears in exactly one function.

Also fixed: a UTF-8 BOM in `pyproject.toml` that made pytest fail to parse it
(`Invalid statement (at line 1, column 1)`), and unclosed `aiohttp`
`ClientSession`s in tests — the fixture now exercises the real `close()` path,
because the same leak in the Cloud Run process is slow FD exhaustion.

One test assertion of mine was simply wrong (`"as **you**"` vs the template's
`"**as you**"`); I corrected the test, not the template.

---

## 10. BLOCKED items

**B1 — the container image was never built.** No Docker daemon in the sandbox,
and rootless podman also failed.

```
$ cd middle_tier && docker build -t teams-middle-tier:local .
ERROR: mkdir ~/.docker/buildx: read-only file system

$ DOCKER_CONFIG=/tmp/dockercfg docker build -t teams-middle-tier:local .
ERROR: Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?

$ XDG_RUNTIME_DIR=/tmp/podrun XDG_CONFIG_HOME=/tmp/podcfg XDG_DATA_HOME=/tmp/poddata \
    podman build -t teams-middle-tier:local .
Error: no such file or directory     (exit 125)
```

**BLOCKED: no container runtime available in this sandbox.** The `Dockerfile`
is therefore *written but unverified*. Unblock with either:

```
docker build -t teams-middle-tier:local middle_tier/
```

or, skipping the local daemon entirely:

```
gcloud builds submit middle_tier/ --tag us-central1-docker.pkg.dev/<GCP_PROJECT_ID>/bot/middle-tier:v0 --project <GCP_PROJECT_ID>
```

Mitigating evidence: every dependency in `requirements.lock.txt` is installed
and working on Python 3.13 in the venv the tests ran in, and `python -m
app.main` — the image's exact `ENTRYPOINT` — was executed successfully (§8).
The plausible remaining failure is base-image/apt-level, not application-level.

**B2 — Secret Manager was never actually read.** `SecretResolver.get()`'s
Secret Manager branch is written but not executed; only the dev env-var
fallback and the Cloud-Run-refusal guardrail are tested. No secrets exist in
`<GCP_PROJECT_ID>` yet, and creating them was not in scope. Unblock:

```
printf %s "test-value" | gcloud secrets create teams-bot-app-password --project <GCP_PROJECT_ID> --data-file=-
gcloud secrets versions access latest --secret teams-bot-app-password --project <GCP_PROJECT_ID>
```

**B3 — no genuine Azure-signed activity was validated.** Requires a registered
Azure Bot and a Teams tenant, neither of which exists yet. §7 verifies the key
side against production Microsoft infrastructure; the token side is verified
only against tokens we minted. Unblock: register the bot, point its messaging
endpoint at the deployed `/api/messages`, and send a Teams message. The
`{"severity": "INFO", "message": "inbound activity authenticated", ...}` line
is the success signal.

**B4 — Cloud Run deploy not performed.** Out of scope for this task; the
command is in `README.md`.

Not blocked, for the record: PyPI was reachable and every dependency installed
cleanly, and outbound HTTPS to `login.botframework.com` worked (§7).

---

## 11. Needs arbitration: a module-name collision

**`app/identity.py` was renamed to `app/caller_identity.py`.** This deviates
from the brief, which named `middle_tier/app/identity.py`, so flagging it
rather than doing it quietly.

While I was working, concurrently-built components appeared in the same tree:
`app/identity/` (a package for the **OBO/STS identity broker** — the thing that
implements my `app.ports.IdentityBroker`), plus `app/sessions/` and
`app/streaming/`. Python prefers a package over a same-named module, so
`app/identity.py` became unimportable:

```
ImportError: cannot import name 'caller_from_activity' from 'app.identity'
(/…/middle_tier/app/identity/__init__.py)
```

This broke my module and the suite. The collision is purely lexical — two
different things called "identity":

- `app.caller_identity` (mine) — **who** the activity says the human is. Reads
  the request body. No network, no credentials.
- `app.identity` (theirs) — **how** that human gets a Google credential.
  Crosses the Microsoft → Google trust boundary.

I moved mine because it is one file against a multi-module package, because the
type it returns is already `CallerIdentity`, and because deleting or editing
another worker's in-progress files would be destructive. **I did not touch
their files.** All 94 tests and all 6 mutation checks were re-run green after
the rename.

If the intended layout was the reverse, the fix is to rename their package to
`app/identity_broker/` and move mine back; either way it needs one decision
from whoever owns the overall layout. `app/ports.py` is unaffected — the
`IdentityBroker` Protocol is the contract, and it does not care which module
implements it.

---

## 11b. State of the shared `tests/` directory at handoff

Other workers are landing their tests into the same `tests/` directory. At the
time of writing, a whole-directory run looks like this:

```
$ .venv/bin/python -m pytest -q
FAILED tests/test_no_service_account_fallback.py::test_no_bare_return_none_in_the_acquisition_path
1 failed, 210 passed, 2 skipped in 1.53s
```

**That failure is not mine and I did not touch it.** It belongs to the identity
broker component: the test parses `app/identity/broker.py` and asserts its
`google_access_token` accessor is annotated `-> str`. It scans only files in
`app/identity/`, so nothing I wrote or renamed can affect it, and it is almost
certainly just that component being mid-write. I deliberately left it alone
rather than "fixing" another worker's in-progress file.

My three test files, run in isolation, are green:

```
$ .venv/bin/python -m pytest tests/test_inbound_auth.py tests/test_identity.py tests/test_app_smoke.py -q
........................................................................ [ 76%]
......................                                                   [100%]
94 passed in 0.57s
```

Every "94 passed" claim elsewhere in this file refers to those three files plus
`conftest.py`. When the whole directory is green, the combined number will be
larger and that is expected.

---

## 12. What is stubbed, pending another component

The four Protocols in `app/ports.py` are the seams. None is implemented here.
Each documents its failure modes, because ADR 004 only works if callers can
tell "the user is not allowed" (`AuthorizationDenied`, names the resource) from
"the backend is down" (`TransientBackendError`, retryable) from "we have no
credential" (`IdentityUnavailable`, sign-in card).

| Protocol | Owner | Middle-tier behaviour today |
| --- | --- | --- |
| `IdentityBroker` | OBO/STS component (now `app/identity/`) | Turn refused with the identity-failure message + sign-in card. Never a service account. |
| `SessionManager` | Session component (now `app/sessions/`) | `/new` returns the transient-failure template, explicitly *not* a denial. |
| `AgentRuntimeClient` | Runtime component | Message turns return the transient-failure template. |
| `StreamingRenderer` | Streaming component (now `app/streaming/`) | Skipped when absent. |

**Teams SSO token exchange** (`invoke` / `signin/tokenExchange`) is stubbed in
`routing.py::_handle_invoke` with a `TODO` naming the owning component and the
required implementation in order, including the `value.id` de-duplication step
(Teams sends the same exchange to every active instance; without dedup two
instances race to redeem one assertion and one gets a replay error).

It returns **501, deliberately not 200**. A 200 tells Teams the exchange
succeeded, and the user then waits for a reply that never comes. Tested:
`test_sso_invoke_returns_501_not_200`.

Degradation is honest throughout: where a component is missing the user gets a
"something on my side failed — this is not a permissions problem" message. The
middle tier never fabricates anything resembling an agent answer, and never
words an outage as a permission problem (that sends users to an admin who finds
nothing wrong and disbelieves the next report).

---

## 13. ADR observations

I did not deviate from any of ADR 001–005. Three notes.

**ADR 004 is stronger than it looks, and I'd keep it.** "Never hand a raw
authorization error to the model to explain" is the clause doing the most work.
Beyond inconsistent explanations, a raw IAM error carries principals, resource
paths and occasionally tokens, and handing it to an inference endpoint exports
all of that. `app/errors.py` is therefore fixed templates with no model in the
loop. One thing worth making explicit in the ADR text: `AuthorizationDenied`
requires a resource name — `downstream_denial(resource="")` raises — because an
unattributed denial makes the user open a ticket an admin cannot action.

**ADR 003's fail-closed rule has a real user-visible cost, and the wording
should acknowledge it.** Guest and anonymous Teams participants genuinely do
not carry `aadObjectId`. Those users will *always* be refused. That is correct —
they cannot be federated, so there is nothing to serve them as — but it is a
support burden, not an edge case. I gave it a distinct message
(`errors.missing_entra_object_id`) rather than the generic identity failure,
because telling a guest to "sign in again" sends them round a loop that cannot
terminate. Worth stating in the ADR so it is not rediscovered as a bug.

**ADR 005, small addition.** The middle tier reading history but never writing
events is right. I'd add: `SessionManager.reset()` should create the new
session *before* discarding the old handle, so a failure leaves the user with a
working conversation rather than none. Documented on the Protocol.

One thing outside the ADRs that deserves a decision: **`/api/messages` must be
deployed `--allow-unauthenticated`**, because Azure Bot Service cannot present
a Google IAM credential. That is correct and unavoidable, and it means the Bot
Framework JWT is the *entire* authentication boundary for this service. Anyone
reviewing the Cloud Run config will flag the flag; the answer is §2 and §5, not
a change to the flag.

---

## 14. Honest summary

**Executed successfully:** inbound JWT validation (94 tests, 6/6 mutations
caught), identity extraction and its fail-closed behaviour, activity routing,
error templates, redaction, dev-mode config guardrails, the aiohttp app serving
real HTTP including live 401s, and live key resolution against Microsoft's
production JWKS (274 keys).

**Exists but not executed:** the `Dockerfile` (no container runtime — B1), the
Secret Manager read path (B2), and validation of a genuine Azure-signed
activity (B3).

**Exists by design without implementation:** the four `ports.py` Protocols and
the Teams SSO `invoke` handler, all owned by other components.

**Deviation to ratify:** `app/identity.py` → `app/caller_identity.py` (§11).
