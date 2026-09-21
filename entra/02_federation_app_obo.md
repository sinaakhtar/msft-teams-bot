# 02: Configure App B for On-Behalf-Of

**App B is `<FEDERATION_APP_CLIENT_ID>`, the Google-facing federation
app registration in Microsoft Entra ID.**

If you are setting up from scratch on a new tenant, you will create App B first (Step 0)
and register its client ID with Google Cloud's Workforce Identity Pool provider.
If App B was already created as part of an existing Workforce Identity Federation setup,
Step 0 is already done and you can proceed directly to opening it.

Everything configured on this page is **additive**. None of it changes App B's client ID,
its issuer, or anything the Google workforce pool provider is configured
against. One step (Step 4) changes the format of access tokens App B receives;
Step 4 explains why that is safe for the ID-token path.

**Who runs this:** `m365-admin@<TENANT_DOMAIN>`. Steps 5 and 6 need
Global Administrator or Privileged Role Administrator to grant tenant-wide
consent.

**Prerequisite:** page 01 complete, `<APP_A_CLIENT_ID>` in hand.

---

## What App B needs, stated up front

Four things, in this order:

1. An **Application ID URI**, because you cannot expose a scope without one.
2. An exposed delegated scope, **`access_as_user`**.
3. **App A listed in `preAuthorizedApplications`**: on App B, naming App A.
4. **`requestedAccessTokenVersion` set to `2`.** This is the one that silently
   breaks everything if you skip it.

Then, on App A: the API permission, plus admin consent.

## Pre-authorized, or known client application? Pre-authorized.

Both properties exist, they sound interchangeable, and only one is correct here.
The distinction is about *which app you put the property on*.

The OBO chain has three roles: **client** (Teams) → **middle tier** (App A) →
**downstream resource** (App B).

- **`preAuthorizedApplications`** goes on the **resource**, and lists callers it
  will accept without a consent prompt. So: on **App B**, listing **App A**.
  Microsoft: *"Resources can indicate that a given application always has
  permission to receive certain scopes… Any such application can request these
  permissions in an OBO flow and receive them without the user providing
  consent."* That is exactly our requirement. **This is the one you want.**

- **`knownClientApplications`** goes on the **middle tier**, and lists clients
  whose consent prompt should be *combined* with the middle tier's own upstream
  permissions, driving a single `.default` consent screen for both hops.

`knownClientApplications` is not wrong so much as unnecessary and, here,
actively counterproductive. Its whole purpose is to make an interactive consent
prompt cover two hops at once. Our client is Teams, which is pre-authorized on
App A precisely so that no prompt is ever shown, and a personal-scope bot has
nowhere good to render one anyway. We eliminate the prompt with pre-authorization
at both hops instead. Adding `knownClientApplications` also drags in the
`.default` scope pattern, and Microsoft warns that combining `.default` with
other delegated scopes in one request throws `AADSTS70011`.

**So: `preAuthorizedApplications` on App B naming App A. Leave
`knownClientApplications` empty on both apps.**

Source for both definitions: *Microsoft identity platform and OAuth 2.0
On-Behalf-Of flow*,
<https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow>
(sections *Preauthorized applications* and *.default and combined consent*).

---

## Step 0: Create App B (if starting from scratch)

If App B does not already exist in your tenant:

1. In the Microsoft Entra admin center (<https://entra.microsoft.com>), navigate to
   **Identity** → **Applications** → **App registrations**.
2. Click **+ New registration**.
3. **Name**: `google-cloud-workforce-federation` (or your preferred name).
4. **Supported account types**: Select **Accounts in this organizational directory only (Single tenant)**.
5. **Redirect URI**: Leave blank (App B is a downstream API resource, not a web client).
6. Click **Register**.
7. From the **Overview** blade, copy the **Application (client) ID**. This GUID is
   your `<FEDERATION_APP_CLIENT_ID>`.
8. Configure this Client ID on Google Cloud: supply it as `entra_federation_client_id`
   in your Terraform variables (or as `--client-id` if creating the Workforce Pool
   OIDC provider via `gcloud`). Google's workforce pool will only accept assertions
   audienced to this client ID.

If App B already exists from an existing federation setup, skip directly to Step 1.

---

## Step 1: Open App B

1. <https://entra.microsoft.com> → **Entra ID** → **App registrations**.
2. Select the **All applications** tab.
3. Search for `<FEDERATION_APP_CLIENT_ID>`.
4. Open it and confirm on **Overview** that the **Application (client) ID**
   reads `<FEDERATION_APP_CLIENT_ID>` exactly.

> If you already configured Google's Workforce Identity Pool with an existing App B,
> make sure you open that exact app registration. Google's provider validates incoming
> assertions against this specific client ID.

## Step 2: Set or confirm the Application ID URI

1. **Manage** → **Expose an API**.
2. If **Application ID URI** is already set, **record its exact value and leave
   it alone** (something may already depend on it). Skip to Step 3.
3. If it is blank, click **Add**, accept the prefilled default, and **Save**:

   ```
   api://<FEDERATION_APP_CLIENT_ID>
   ```

Use the plain `api://{clientId}` form. Do not use the `botid-` form here; that
convention belongs to Teams bot apps, and App B is not one.

Setting an Application ID URI does not alter the client ID and does not affect
Google's provider configuration.

## Step 3: Expose the `access_as_user` scope on App B

1. Still on **Expose an API**, click **+ Add a scope**.
2. Fill in:

   | Field | Value |
   | --- | --- |
   | **Scope name** | `access_as_user` |
   | **Who can consent?** | **Admins only** |
   | **Admin consent display name** | `Federate the signed-in user to Google Cloud` |
   | **Admin consent description** | `Allows the Teams bot middle tier to obtain a token identifying the signed-in user, for exchange at Google Cloud STS.` |
   | **State** | **Enabled** |

3. Click **Add scope**.

Full scope string:
`api://<FEDERATION_APP_CLIENT_ID>/access_as_user`. This is the value
that goes in the OBO request's `scope` parameter.

**Why Admins only here, when page 01 used Admins and users?** Crossing an
organisational trust boundary into a different cloud provider is not a decision
a rank-and-file user should be able to click through in a dialog they will not
read. App A's scope is a within-Microsoft hop and can be user-consentable;
App B's is the boundary crossing. Since App A is pre-authorized in Step 4 and an
admin consents in Step 6, no user is ever prompted regardless.

## Step 4: Pre-authorize App A, and set the access token version

Two changes. The second is the critical one, and doing them together in the
manifest editor is faster and less error-prone than the forms.

### 4a: Pre-authorize App A (portal form)

1. **Expose an API** → **Authorized client applications** →
   **+ Add a client application**.
2. **Client ID**: `<APP_A_CLIENT_ID>`.
3. Tick the scope
   `api://<FEDERATION_APP_CLIENT_ID>/access_as_user`.
4. **Add application**.

### 4b: Set `requestedAccessTokenVersion` to 2: DO NOT SKIP THIS

1. Left nav within App B: **Manage** → **Manifest**.
2. You will see a toggle or two tabs: **Microsoft Graph App Manifest (New)** and
   **AAD Graph App Manifest (Deprecating Soon)**. Microsoft added this split
   relatively recently and the two editors spell things differently:

   | Editor | Where the property lives |
   | --- | --- |
   | **Microsoft Graph App Manifest (New)** | `api.requestedAccessTokenVersion` |
   | **AAD Graph App Manifest (Deprecating Soon)** | top-level `accessTokenAcceptedVersion` |

   Same underlying setting, two names. Use whichever editor your tenant shows;
   if you see both, prefer the Microsoft Graph one.
3. Find the property and set the value to `2`:

   ```jsonc
   // Microsoft Graph App Manifest (New)
   "api": {
       "requestedAccessTokenVersion": 2,
       ...
   }
   ```

   ```jsonc
   // AAD Graph App Manifest (Deprecating Soon)
   "accessTokenAcceptedVersion": 2
   ```

4. Click **Save**.
5. Reload the blade and confirm it reads `2` and not `null`. Saves on this blade
   fail silently more often than they should.

### Why this is the make-or-break setting

Two facts, both from current Microsoft documentation:

1. **Default is 1.** *"Possible values for `requestedAccessTokenVersion` are 1,
   2, or null. If the value is null, this parameter defaults to 1, which
   corresponds to the v1.0 endpoint."*
   Source: <https://learn.microsoft.com/en-us/entra/identity-platform/reference-app-manifest>

2. **Version determines `aud`.** *"Identifies the intended audience of the
   token. In v2.0 tokens, this value is always the client ID of the API. In v1.0
   tokens, it can be the client ID or the resource URI used in the request."*
   Source: <https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference>

And on Google's side, from the STS API reference for the `subjectToken`
parameter: for workforce pools, the JWT's `aud` **must match the client ID
specified in the provider configuration:
<https://cloud.google.com/iam/docs/reference/sts/rest/v1/TopLevel/token>

Put together:

| App B setting | OBO token `aud` | OBO token `iss` | Google STS |
| --- | --- | --- | --- |
| `requestedAccessTokenVersion: 2` | `<FEDERATION_APP_CLIENT_ID>` | `https://login.microsoftonline.com/<ENTRA_TENANT_ID>/v2.0` | **accepted** |
| default (`null` → 1) | `api://<FEDERATION_APP_CLIENT_ID>` | `https://sts.windows.net/<ENTRA_TENANT_ID>/` | **rejected, twice** |

Left at the default you get a wrong audience *and* a wrong issuer, from an
otherwise flawless configuration. Page 04 step 3 exists to catch precisely this.

### Is changing this safe for the flow that already works?

Yes, and this is worth stating because the device-code → ID token → Google chain
is currently your only proven-good path and you should not be nervous about
disturbing it.

Microsoft: *"The endpoint used, v1.0 or v2.0, is chosen by the client and only
impacts the version of `id_tokens`. Resources need to explicitly configure
`requestedAccessTokenVersion` to indicate the supported access token format."*
Source: <https://learn.microsoft.com/en-us/entra/identity-platform/reference-app-manifest>

`requestedAccessTokenVersion` governs **access tokens only**. The ID token your
device-code flow produces is unaffected. Re-run that flow after this change as a
regression check anyway: it takes a minute and it is the control sample.

## Step 5: Grant App A permission to call App B

Now switch to **App A**.

1. **App registrations** → your `teams-bot-agent-runtime` app.
2. **Manage** → **API permissions** → **+ Add a permission**.
3. Choose the **My APIs** tab. Not *Microsoft APIs*.

   > If you find yourself on the *Microsoft Graph* tile, stop. **This design
   > makes no Graph calls.** OBO is being used to re-audience a token across a
   > trust boundary, not to read a mailbox. Adding Graph permissions here does
   > nothing useful and misleads whoever reviews this next.

4. Select the federation app, `<FEDERATION_APP_CLIENT_ID>`.
5. Choose **Delegated permissions**. Not *Application permissions*: this flow
   carries a user identity; application permissions are the app-only flow and
   would defeat the entire point of the design.
6. Tick **`access_as_user`**.
7. Click **Add permissions**.

## Step 6: Grant admin consent

Still on App A's **API permissions** page:

1. Click **✓ Grant admin consent for <TENANT_NAME>**.
2. Confirm.
3. The **Status** column for `access_as_user` must turn to a green
   **Granted for <TENANT_NAME>**. If it stays blank or amber, consent did not apply;
   see [05_troubleshooting.md](05_troubleshooting.md) under `AADSTS65001`.

You now have consent on both belts: pre-authorization on App B (Step 4a) and an
explicit tenant-wide grant (here). Either alone should suffice. Having both
means a failure at this hop is a configuration error you can rule out fast.

---

## The OBO request the middle tier will send

This is the exact call. Page 04 has it wrapped in a verification procedure with
decoding steps; this is the reference form.

```bash
curl -sS -X POST \
  "https://login.microsoftonline.com/<ENTRA_TENANT_ID>/oauth2/v2.0/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer" \
  --data-urlencode "client_id=<APP_A_CLIENT_ID>" \
  --data-urlencode "client_secret=<APP_A_CLIENT_SECRET>" \
  --data-urlencode "assertion=<TEAMS_SSO_TOKEN>" \
  --data-urlencode "scope=api://<FEDERATION_APP_CLIENT_ID>/access_as_user" \
  --data-urlencode "requested_token_use=on_behalf_of"
```

| Parameter | Value | Notes |
| --- | --- | --- |
| endpoint | `.../<ENTRA_TENANT_ID>/oauth2/v2.0/token` | Tenant-specific, **`/v2.0/`**. Not `/common/`, not the v1.0 path. |
| `grant_type` | `urn:ietf:params:oauth:grant-type:jwt-bearer` | Fixed. |
| `client_id` | `<APP_A_CLIENT_ID>` | App A authenticates as itself. |
| `client_secret` | `<APP_A_CLIENT_SECRET>` | Read from Google Secret Manager at runtime. |
| `assertion` | the Teams SSO token | Must have `aud` = `<APP_A_CLIENT_ID>` and an `scp` claim. |
| `scope` | `api://<FEDERATION_APP_CLIENT_ID>/access_as_user` | The **App B** scope. Full URI, not the bare name. |
| `requested_token_use` | `on_behalf_of` | Fixed. Omitting it is a common and confusing mistake. |

**On `scope`:** request only this one scope. Do not add `openid`, `profile`, or
`offline_access` speculatively. Adding `openid` will *not* get you an ID token
(see below), and mixing `.default` with named delegated scopes throws
`AADSTS70011`. If you later want a refresh token to avoid re-running OBO on
every turn, add `offline_access` (and only that).

**Certificate credentials instead of a secret.** Microsoft's preferred form
replaces `client_secret` with `client_assertion_type` +
`client_assertion`, signed with a registered certificate. It is documented on
the same OBO page. A client secret is fine for a sandbox demo; note it as
hardening for anything real.

## The response: an access token, and only an access token

Microsoft's OBO response is documented as containing `token_type`, `scope`,
`expires_in`, `access_token`, and (if `offline_access` was requested)
`refresh_token`.

**There is no `id_token` in the OBO response.** Not by default, and not by
adding `openid` to the scope. The response schema on the OBO documentation page
has no such field.

```json
{
  "token_type": "Bearer",
  "scope": "api://<FEDERATION_APP_CLIENT_ID>/access_as_user",
  "expires_in": 3599,
  "ext_expires_in": 3599,
  "access_token": "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiIs..."
}
```

Take `access_token`. That is what goes to Google's STS.

---

## RISK: read this before you run anything

The Google provider was configured with `responseType = ID_TOKEN` and
`assertionClaimsBehavior = ONLY_ID_TOKEN_CLAIMS`, and the proven-working path
fed it an **ID token**. OBO gives you an **access token**. That mismatch has two
independent parts, and they resolve differently. Do not conflate them.

### Part 1: "access token vs ID token": low risk, understood

Google's STS does not consume Entra's *semantics*, it consumes a *JWT*. Per the
STS API reference, the `subjectToken` must be a JWT in RFC 7523 format with
`RS256` or `ES256`, `kid` and `alg` headers, and `subjectTokenType` set to
`urn:ietf:params:oauth:token-type:jwt` **or**
`urn:ietf:params:oauth:token-type:id_token`. An Entra v2.0 access token issued
for a **custom API you own** is an ordinary RS256 JWT signed with the same
tenant keys, published at the same JWKS endpoint, as the ID token that already
works. It carries `iss`, `aud`, `sub`, `tid` and `oid`. The attribute mapping
`google.subject = assertion.oid` reads `oid`, which is present.

`responseType` and `assertionClaimsBehavior` are properties of the workforce
pool provider's **browser sign-in** behaviour. A direct `POST` to
`sts.googleapis.com/v1/token` bypasses that path and is governed by signature,
`iss`, `aud`, expiry and the attribute mappings.

Keep `subject_token_type=urn:ietf:params:oauth:token-type:id_token`, which is
what the proven exchange used. If it is rejected on token *type* grounds,
`urn:ietf:params:oauth:token-type:jwt` is the documented alternative. Try it
before changing anything else.

> One caveat, and it is why the distinction matters at all: Microsoft warns not
> to parse access tokens for APIs you do not own, because tokens for Microsoft
> services such as Graph can use a special, non-JWT format. That warning does
> **not** apply here: App B is an API you own, so its tokens are plain JWTs.
> This is another reason the "OBO is for Graph" instinct must be resisted: an
> OBO token for Graph genuinely would not work with Google's STS.

### Part 2: the audience format (HIGH risk, and the likeliest failure in the build)

This is the real hazard.

You are requesting the scope by its **Application ID URI**
(`api://<FEDERATION_APP_CLIENT_ID>/access_as_user`). If App B emits **v1.0** access tokens (
which is the **default**, since `requestedAccessTokenVersion` is `null` until
you change it), the resulting `aud` is the **resource URI**,
`api://<FEDERATION_APP_CLIENT_ID>`, not the bare GUID. Google compares
`aud` against the client ID registered on the provider,
`<FEDERATION_APP_CLIENT_ID>`. String mismatch. Rejected.

The same setting also moves `iss` from
`https://login.microsoftonline.com/<ENTRA_TENANT_ID>/v2.0` to
`https://sts.windows.net/<ENTRA_TENANT_ID>/`, which fails issuer validation
independently.

**Mitigation:** Step 4b. That is the whole fix.

**Do not "fix" this on the Google side.** Adding `api://<FEDERATION_APP_CLIENT_ID>` to the
provider's allowed audiences would make the error go away and would leave you
running v1.0 tokens with a `sts.windows.net` issuer that still will not match.
Fix the token format at the source.

### The concrete test

Run this the moment Step 4b is saved and you have any assertion to hand. It
takes ten seconds and it localises the failure completely.

```bash
# 1. Run the OBO curl above, capture the access token
OBO_TOKEN=$(curl -sS -X POST \
  "https://login.microsoftonline.com/<ENTRA_TENANT_ID>/oauth2/v2.0/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer" \
  --data-urlencode "client_id=<APP_A_CLIENT_ID>" \
  --data-urlencode "client_secret=<APP_A_CLIENT_SECRET>" \
  --data-urlencode "assertion=<TEAMS_SSO_TOKEN>" \
  --data-urlencode "scope=api://<FEDERATION_APP_CLIENT_ID>/access_as_user" \
  --data-urlencode "requested_token_use=on_behalf_of" | jq -r .access_token)

# 2. Decode the payload and look at the four claims that decide everything
echo "$OBO_TOKEN" | cut -d. -f2 \
  | tr '_-' '/+' | awk '{ n=length($0)%4; if(n) printf "%s", $0 substr("===",1,4-n); else printf "%s", $0 }' \
  | base64 -d 2>/dev/null | jq '{aud, iss, oid, ver, sub, exp}'
```

**PASS: proceed**

```json
{
  "aud": "<FEDERATION_APP_CLIENT_ID>",
  "iss": "https://login.microsoftonline.com/<ENTRA_TENANT_ID>/v2.0",
  "oid": "<ANALYST_OBJECT_ID>",
  "ver": "2.0",
  "sub": "<opaque pairwise id, value irrelevant, presence required>",
  "exp": 1234567890
}
```

**FAIL: go back to Step 4b**

```json
{
  "aud": "api://<FEDERATION_APP_CLIENT_ID>",   ← URI, not GUID
  "iss": "https://sts.windows.net/<ENTRA_TENANT_ID>/",        ← no /v2.0
  "ver": "1.0"                                            ← the smoking gun
}
```

`"ver": "1.0"` is the single claim to look at. If it is not `"2.0"`, Step 4b did
not take: the manifest did not save, or you edited the property in one manifest
editor while the tenant honoured the other. Reload the Manifest blade and check
the stored value.

Token changes are not always instant. If `ver` is still `1.0` a few minutes after
a confirmed save, request a fresh token rather than reusing a cached one. Entra
will happily return a cached token in the old format.

## Checklist

- [ ] App B Application ID URI set and recorded
- [ ] Scope `access_as_user` exposed on App B, Enabled, admin-consent-only
- [ ] App A pre-authorized on App B for that scope
- [ ] **`requestedAccessTokenVersion` = 2 on App B, verified after reload**
- [ ] App A has the delegated `access_as_user` permission on App B (My APIs)
- [ ] Admin consent granted, Status shows green
- [ ] OBO decode test shows `ver: 2.0`, `aud` = bare GUID, correct `iss`, correct `oid`
- [ ] Device-code → ID token → Google regression check still passes
