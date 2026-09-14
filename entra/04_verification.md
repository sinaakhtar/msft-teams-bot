# 04 — Verify each hop, before wiring Teams

**This is the most useful page in the guide.** Work through it in order. Each
step is independently runnable and each one fails loudly and locally, so a
problem tells you *which hop* is broken instead of leaving you staring at a
silent bot.

Do this **before** installing into Teams. The Teams client is the worst possible
debugger for this chain: every failure — missing consent, wrong audience, wrong
issuer, expired token, misconfigured bot endpoint — surfaces as the same
non-committal error card.

**Prerequisites:** pages 01 and 02 complete. `jq` and `curl` installed.

## Set up your shell

```bash
export TENANT_ID="<ENTRA_TENANT_ID>"
export APP_A_CLIENT_ID="<APP_A_CLIENT_ID>"
export APP_B_CLIENT_ID="<FEDERATION_APP_CLIENT_ID>"
export WF_POOL="teams-bot-demo"
export WF_PROVIDER="entra"
export EXPECTED_OID="<ANALYST_OBJECT_ID>"   # analyst@

# read the secret at the prompt so it never enters shell history
read -rs APP_A_CLIENT_SECRET && export APP_A_CLIENT_SECRET
```

> Prefer pulling the secret from Secret Manager rather than typing it:
> ```bash
> export APP_A_CLIENT_SECRET=$(gcloud secrets versions access latest \
>   --secret=teams-bot-app-a-secret --project=<GCP_PROJECT_ID>)
> ```

A JWT decoder you will use constantly. Paste this into your shell:

```bash
jwtdecode() {
  local part="${2:-2}"   # 2 = payload (default), 1 = header
  echo "$1" | cut -d. -f"$part" | tr '_-' '/+' \
    | awk '{ n=length($0)%4; if(n) printf "%s", $0 substr("===",1,4-n); else printf "%s", $0 }' \
    | base64 -d 2>/dev/null | jq .
}
```

It handles base64url padding, which plain `base64 -d` does not. Getting
"invalid input" from a decoder is usually the padding, not a malformed token.

> **These tokens are live credentials for `analyst@`.** Do not paste them into
> an online JWT decoder, a ticket, or a chat message. Decode locally.

---

## Step 1 — Obtain a token with `aud` = App A

This is the token Teams SSO would produce. You need one in hand before you can
test anything downstream.

### 1a — Stand-in via device code (do this now)

The real Teams SSO token only exists once the bot is running inside Teams, which
is exactly what you are trying to avoid depending on. A device-code sign-in
against App A produces a token of **the same shape** — same `aud`, same `oid`,
same `scp` — and OBO cannot tell the difference.

**This requires a temporary setting change on App A.** Read the note after the
commands before you make it.

1. Entra admin center → **App registrations** → App A → **Manage** →
   **Authentication**.
2. Bottom of the page, **Advanced settings** → **Allow public client flows** →
   **Yes**. Stable property: `allowPublicClient` in the app manifest.
3. **Save**.

Then:

```bash
# request a device code
DC=$(curl -sS -X POST \
  "https://login.microsoftonline.com/$TENANT_ID/oauth2/v2.0/devicecode" \
  -d "client_id=$APP_A_CLIENT_ID" \
  --data-urlencode "scope=api://botid-$APP_A_CLIENT_ID/access_as_user offline_access")

echo "$DC" | jq -r .message
DEVICE_CODE=$(echo "$DC" | jq -r .device_code)
```

Follow the printed instructions in a browser and **sign in as
`analyst@<TENANT_DOMAIN>`**, not as the admin. Then:

```bash
USER_TOKEN=$(curl -sS -X POST \
  "https://login.microsoftonline.com/$TENANT_ID/oauth2/v2.0/token" \
  -d "grant_type=urn:ietf:params:oauth:grant-type:device_code" \
  -d "client_id=$APP_A_CLIENT_ID" \
  -d "device_code=$DEVICE_CODE" | jq -r .access_token)

test -n "$USER_TOKEN" && test "$USER_TOKEN" != "null" \
  && echo "got a token" || echo "NO TOKEN — re-run, the code expires in ~15 min"
```

> **Turn `Allow public client flows` back to `No` when you are finished with
> this page.** It is a test-only affordance. App A is a confidential client that
> holds a secret; leaving the public-client path open on the same registration
> widens its attack surface for no ongoing benefit. Note it on the checklist so
> it does not get forgotten.

> **This is a stand-in, and you should treat it as one.** It proves the OBO and
> Google hops work for a correctly-shaped App A token. It does **not** prove
> Teams will issue that token — that depends on `webApplicationInfo` and the
> pre-authorized client IDs, and is only settled by step 1b.

### 1b — The real Teams SSO token (once the bot runs)

A bot does not call `getAuthToken()`. Teams delivers the SSO token to the bot as
an **invoke activity** with name `signin/tokenExchange`, after the bot sends an
`OAuthCard`. The token arrives at:

```
activity.value.token
```

Log it once, at debug level, in a sandbox only, and never in anything that ships:

```
[sso] name=signin/tokenExchange aud=<decoded aud> oid=<decoded oid>
```

Log the **decoded claims**, not the token. A bearer token in a log file is a
credential in a log file.

Then re-run steps 2 through 4 with that real token as `USER_TOKEN`. If 1a passed
and 1b fails, the fault is in Teams-side configuration — `webApplicationInfo`,
the Application ID URI match, or the pre-authorized client IDs — not in the
token chain.

### Verify the token you have

```bash
jwtdecode "$USER_TOKEN" | jq '{aud, iss, oid, scp, appid, ver, exp}'
```

**PASS:**

| Claim | Expected |
| --- | --- |
| `aud` | `<APP_A_CLIENT_ID>` — App A's client ID |
| `iss` | `https://login.microsoftonline.com/<ENTRA_TENANT_ID>/v2.0` |
| `oid` | `<ANALYST_OBJECT_ID>` |
| `scp` | contains `access_as_user` |
| `exp` | in the future |

Checks worth making explicitly:

```bash
# aud must be App A, NOT App B — if it is already App B, you signed in against
# the wrong app and the OBO step below is not testing what you think it is
[ "$(jwtdecode "$USER_TOKEN" | jq -r .aud)" = "$APP_A_CLIENT_ID" ] \
  && echo "OK aud=App A" || echo "WRONG aud"

# oid must be the analyst, not the admin
[ "$(jwtdecode "$USER_TOKEN" | jq -r .oid)" = "$EXPECTED_OID" ] \
  && echo "OK oid=analyst" || echo "WRONG user — you signed in as the admin"
```

**No `scp` claim?** You have an app-only token, not a delegated one. OBO
requires a delegated user assertion and will reject it. Sign in interactively.

> **Sanity check on the design, if you want to see the problem for yourself:**
> take this token straight to Google's STS (step 4). It will be rejected for
> audience. That rejection *is* ADR 002 — it is the reason the OBO hop exists.
> Two minutes here saves a long argument later about whether OBO is really
> necessary.

---

## Step 2 — The OBO exchange

Re-audience the user's token from App A to App B.

```bash
OBO_RESPONSE=$(curl -sS -X POST \
  "https://login.microsoftonline.com/$TENANT_ID/oauth2/v2.0/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer" \
  --data-urlencode "client_id=$APP_A_CLIENT_ID" \
  --data-urlencode "client_secret=$APP_A_CLIENT_SECRET" \
  --data-urlencode "assertion=$USER_TOKEN" \
  --data-urlencode "scope=api://$APP_B_CLIENT_ID/access_as_user" \
  --data-urlencode "requested_token_use=on_behalf_of")

echo "$OBO_RESPONSE" | jq 'if .error then . else {token_type, scope, expires_in} end'

OBO_TOKEN=$(echo "$OBO_RESPONSE" | jq -r .access_token)
```

**PASS** looks like:

```json
{
  "token_type": "Bearer",
  "scope": "api://<FEDERATION_APP_CLIENT_ID>/access_as_user",
  "expires_in": 3599
}
```

**Note what is not there: no `id_token`.** OBO returns an access token. That is
expected and correct; step 3 explains why Google accepts it anyway.

If you got an `error`, go to [05_troubleshooting.md](05_troubleshooting.md) — it
covers `AADSTS65001`, `invalid_grant` and `AADSTS500131` specifically. Do not
proceed; nothing downstream can work.

---

## Step 3 — Decode the OBO token. THE CRITICAL CHECK.

**This is the step that catches the failure this whole design is most likely to
hit.** Do not skip it, and do not skip it *especially* if everything so far went
smoothly — the misconfiguration this catches produces a perfectly successful
step 2.

```bash
jwtdecode "$OBO_TOKEN" | jq '{aud, iss, oid, sub, ver, tid, scp, exp}'
jwtdecode "$OBO_TOKEN" 1 | jq '{alg, kid, typ}'
```

### PASS

```json
{
  "aud": "<FEDERATION_APP_CLIENT_ID>",
  "iss": "https://login.microsoftonline.com/<ENTRA_TENANT_ID>/v2.0",
  "oid": "<ANALYST_OBJECT_ID>",
  "sub": "<opaque>",
  "ver": "2.0",
  "tid": "<ENTRA_TENANT_ID>",
  "scp": "access_as_user"
}
```

Header must show `"alg": "RS256"` and a `kid`. Google requires `RS256` or
`ES256` plus a `kid`.

### FAIL — the one you are actually looking for

```json
{
  "aud": "api://<FEDERATION_APP_CLIENT_ID>",
  "iss": "https://sts.windows.net/<ENTRA_TENANT_ID>/",
  "ver": "1.0"
}
```

`"ver": "1.0"` is the tell. App B is emitting v1.0 access tokens, so `aud` is
the resource URI rather than the bare client ID, **and** `iss` lacks the `/v2.0`
suffix. Google rejects both. Fix: **page 02 step 4b**, set
`requestedAccessTokenVersion` to `2` on App B, then request a *fresh* token —
Entra caches, and a cached token comes back in the old format.

### Assert it in one line

```bash
AUD=$(jwtdecode "$OBO_TOKEN" | jq -r .aud)
ISS=$(jwtdecode "$OBO_TOKEN" | jq -r .iss)
OID=$(jwtdecode "$OBO_TOKEN" | jq -r .oid)

[ "$AUD" = "$APP_B_CLIENT_ID" ] \
  && echo "PASS aud" \
  || echo "FAIL aud='$AUD' — expected bare GUID $APP_B_CLIENT_ID. See page 02 step 4b."

[ "$ISS" = "https://login.microsoftonline.com/$TENANT_ID/v2.0" ] \
  && echo "PASS iss" \
  || echo "FAIL iss='$ISS' — expected .../v2.0. Same fix: requestedAccessTokenVersion=2."

[ "$OID" = "$EXPECTED_OID" ] \
  && echo "PASS oid" \
  || echo "FAIL oid='$OID' — identity was not preserved across OBO."
```

All three must print PASS. `oid` is what the provider maps with
`google.subject = assertion.oid`; if it changed across the OBO hop, the Google
principal will be wrong or the mapping will fail outright.

### Optional: confirm the signing key is one Google can fetch

Google validates the signature against the issuer's published JWKS. Confirm the
`kid` is present there:

```bash
KID=$(jwtdecode "$OBO_TOKEN" 1 | jq -r .kid)
curl -sS "https://login.microsoftonline.com/$TENANT_ID/discovery/v2.0/keys" \
  | jq --arg k "$KID" '.keys[] | select(.kid == $k) | {kid, kty, alg}'
```

A match means Google can verify the signature. Empty output means the token was
signed with a key not published at the v2.0 discovery endpoint — which is itself
a strong hint you are holding a v1.0 token.

Also confirm the issuer matches the provider's configured issuer exactly:

```bash
curl -sS "https://login.microsoftonline.com/$TENANT_ID/v2.0/.well-known/openid-configuration" \
  | jq -r .issuer
# must print: https://login.microsoftonline.com/<ENTRA_TENANT_ID>/v2.0
```

---

## Step 4 — Exchange at Google STS

Only run this once step 3 prints three PASSes. Running it earlier just produces
a Google-side error for a Microsoft-side cause, which is how people end up
changing the wrong configuration.

```bash
STS_RESPONSE=$(curl -sS -X POST "https://sts.googleapis.com/v1/token" \
  -H "Content-Type: application/json" \
  -d "{
    \"grant_type\": \"urn:ietf:params:oauth:grant-type:token-exchange\",
    \"audience\": \"//iam.googleapis.com/locations/global/workforcePools/$WF_POOL/providers/$WF_PROVIDER\",
    \"scope\": \"https://www.googleapis.com/auth/cloud-platform\",
    \"requested_token_type\": \"urn:ietf:params:oauth:token-type:access_token\",
    \"subject_token_type\": \"urn:ietf:params:oauth:token-type:id_token\",
    \"subject_token\": \"$OBO_TOKEN\",
    \"options\": \"{\\\"userProject\\\":\\\"<GCP_PROJECT_ID>\\\"}\"
  }")

echo "$STS_RESPONSE" | jq 'if .error then . else {token_type, expires_in, issued_token_type} end'
GOOGLE_TOKEN=$(echo "$STS_RESPONSE" | jq -r .access_token)
```

**PASS:**

```json
{
  "token_type": "Bearer",
  "expires_in": 3600,
  "issued_token_type": "urn:ietf:params:oauth:token-type:access_token"
}
```

Notes on the parameters:

- **No `Authorization` header.** Google's STS documentation is explicit that
  sending one causes the request to fail. Do not add it.
- `subject_token_type` is `...:id_token`, matching the exchange already proven
  working. Google accepts `urn:ietf:params:oauth:token-type:jwt` as well; if and
  only if you get an error specifically about the *token type*, try that variant
  before touching anything else.
- `options.userProject` sets the billing/quota project for workforce pool
  callers. `<GCP_PROJECT_ID>` here. Without it you may get a quota-project error that
  reads like an auth failure.

If this returns `Invalid value for "audience"`, note **which** audience: the
`audience` request parameter (the pool provider resource name) and the JWT's
`aud` claim are different things and the error does not always distinguish them.
[05_troubleshooting.md](05_troubleshooting.md) separates the two cases.

---

## Step 5 — Prove the Google token is really the user

A token that parses is not the same as a token that carries the right identity.

> **Do not use `sts.googleapis.com/v1/introspect` here.** An earlier version of
> this page did. It cannot work: introspection is an RFC 7662 endpoint that
> requires the *caller* to authenticate as an OAuth client, and a workforce
> federation flow has no client ID and secret to present. It fails with
> `invalid_client` / *"Request is missing basic authentication credentials"*,
> which reads like a token problem and is not one. Verified 2026-09-07.

Ask BigQuery who it thinks you are. This is better than introspection anyway:
introspection reports what Google believes the token says, whereas this reports
the identity a real query actually executed under.

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $GOOGLE_TOKEN" \
  -H "x-goog-user-project: <GCP_PROJECT_ID>" \
  -H "Content-Type: application/json" \
  "https://bigquery.googleapis.com/bigquery/v2/projects/<GCP_PROJECT_ID>/queries" \
  -d '{"query":"SELECT SESSION_USER() AS whoami","useLegacySql":false}' \
  | jq -r '.rows[0].f[0].v'
```

The single value returned must be:

```
principal://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/subject/<ANALYST_OBJECT_ID>
```

The trailing GUID must be the analyst's `oid`. That is
`google.subject = assertion.oid` having worked end to end.

Then make a real authorized call — this is the one that proves IAM actually
honours the principal, not just that a token was minted:

```bash
curl -sS -H "Authorization: Bearer $GOOGLE_TOKEN" \
  -H "x-goog-user-project: <GCP_PROJECT_ID>" \
  "https://cloudresourcemanager.googleapis.com/v1/projects/<GCP_PROJECT_ID>" | jq '{projectId, lifecycleState}'
```

A `403` here with a valid token is an **IAM binding** problem, not an
authentication problem — the workforce principal has no role granted. That is a
Google-side fix and nothing on the Microsoft side will change it.

Finally, repeat whatever call the proven BigQuery test used, as your regression
control.

---

## Step 6 — Invoke the Agent Runtime

The point of the whole exercise.

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $GOOGLE_TOKEN" \
  -H "Content-Type: application/json" \
  -H "x-goog-user-project: <GCP_PROJECT_ID>" \
  "https://<REGION>-aiplatform.googleapis.com/v1/projects/<GCP_PROJECT_ID>/locations/<REGION>/reasoningEngines/<REASONING_ENGINE_ID>:query" \
  -d '{"input": {"message": "who am I?"}}' | jq .
```

`<REGION>` and `<REASONING_ENGINE_ID>` come from your Agent Runtime deployment;
the exact request body follows the runtime interface contract in ADR 005 rather
than anything in this guide. Confirm the shape against your deployment before
concluding a failure here is an identity problem.

Afterwards, check Cloud Audit Logs and confirm the invocation is attributed to
the workforce principal and not to a service account. That is the property
ADR 002 exists to deliver, and it is worth seeing with your own eyes.

---

## Verification checklist

| # | Hop | Passes when |
| --- | --- | --- |
| 1 | User token exists | `aud` = App A, `oid` = analyst, `scp` present |
| 2 | OBO succeeds | HTTP 200 with an `access_token`, no `error` |
| 3 | **OBO token re-audienced** | **`ver` = 2.0, `aud` = bare App B GUID, `iss` ends `/v2.0`, `oid` unchanged** |
| 4 | Google STS accepts it | Returns a Google access token |
| 5 | Identity is right | Introspect shows `.../subject/<analyst oid>`; an authorized call succeeds |
| 6 | Agent Runtime responds | Query returns; audit log names the human |

Housekeeping when you are done:

- [ ] **`Allow public client flows` set back to `No` on App A**
- [ ] No tokens left in shell history, scratch files, or logs
- [ ] `unset APP_A_CLIENT_SECRET USER_TOKEN OBO_TOKEN GOOGLE_TOKEN`

Only after all six pass should you go install the Teams app.
