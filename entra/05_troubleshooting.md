# 05 — Troubleshooting

Four errors account for nearly everything that goes wrong in this topology. For
each: what the message literally means, what it means *here*, and what to
change.

**Before anything else, localise the failure with
[04_verification.md](04_verification.md).** These entries assume you know which
hop broke. Guessing from the Teams client's behaviour wastes more time than the
verification run costs.

Every Entra error carries a **Trace ID**, **Correlation ID** and **Timestamp**.
Keep them. If you end up in a support conversation they are the only thing that
identifies your specific failure.

---

## `AADSTS500131` — audience validation failed

```
AADSTS500131: Audience validation failed. Audience did not match.
```

**Literally:** the `assertion` you presented to the token endpoint has an `aud`
claim that is not the app authenticating the OBO request.

**Here:** you passed a token to OBO whose `aud` is not `<APP_A_CLIENT_ID>`.

The OBO grant requires the assertion to have been *issued to App A*. App A is
proving "someone gave this user token to me, and I am asking for a downstream
token with it". If the token was minted for a different audience, App A has no
standing to exchange it and Entra refuses.

**Most likely causes, in order:**

1. **You passed a token whose `aud` is App B.** Very easy to do if you reused
   the device-code script from the already-proven federation test, which
   deliberately targets App B. That token is the *output* shape of OBO, not the
   input. Check:

   ```bash
   jwtdecode "$USER_TOKEN" | jq -r .aud   # must equal $APP_A_CLIENT_ID
   ```

2. **You requested the wrong scope when getting the user token.** It must be
   `api://botid-<APP_A_CLIENT_ID>/access_as_user`. Requesting
   `<APP_A_CLIENT_ID>/.default` or a Graph scope produces a token audienced
   elsewhere.

3. **`client_id` in the OBO body is not the app the assertion was issued for.**
   Typo, or App B's ID pasted into App A's slot.

4. **App A's Application ID URI does not match what was requested.** If you set
   `api://<APP_A_CLIENT_ID>` (no `botid-` prefix) but requested
   `api://botid-<APP_A_CLIENT_ID>`, the token is issued for something else, or
   not at all. Page 01 step 4.

5. **A Graph token was passed as the assertion.** Graph tokens have
   `aud: https://graph.microsoft.com` and cannot be exchanged by App A. If you
   are holding one, the "OBO is for Graph" instinct has taken over — reread the
   callout in the [README](README.md).

**Fix:** get the audience right on the *input* token. Verification step 1.

> **Do not confuse this with the Google-side audience failure.** `AADSTS500131`
> is Microsoft rejecting your *input* to OBO. `Invalid value for "audience"` is
> Google rejecting OBO's *output*. Opposite ends of the chain, opposite fixes.
> The audience mismatch that this design is most at risk from is the second one.

---

## `AADSTS65001` — consent missing

```
AADSTS65001: The user or administrator has not consented to use the application
with ID '<APP_A_CLIENT_ID>' named '<name>'. Send an interactive authorization
request for this user and resource.
```

**Literally:** the permission is *listed* on the app registration but nobody has
*granted* it. Listing and granting are two separate acts and the portal makes
them look like one.

**Here:** App A is asking for `access_as_user` on App B, and there is no consent
grant behind that request.

**Diagnose by which app ID is named in the error:**

| App ID in message | Meaning | Fix |
| --- | --- | --- |
| `<APP_A_CLIENT_ID>` | The Teams → App A hop has no consent | Page 01 step 6 — pre-authorize the Teams client IDs |
| `<FEDERATION_APP_CLIENT_ID>` | The App A → App B hop has no consent | Page 02 steps 4a, 5 and 6 |

**Fixes, in order of likelihood:**

1. **Admin consent was never granted.** App A → **API permissions** → the
   **Status** column must read a green *Granted for <TENANT_NAME>*. Blank or amber means
   not granted. Click **✓ Grant admin consent for <TENANT_NAME>**. Page 02 step 6.

2. **The permission was added but not saved.** The portal can show a permission
   in the list that was never committed. Reload the blade and check it is still
   there.

3. **Wrong permission type.** You added an **Application** permission where a
   **Delegated** one is required. This flow carries a user identity;
   application permissions are the app-only flow. Check the **Type** column
   reads *Delegated*. Remove and re-add if wrong — page 02 step 5.

4. **App A is not pre-authorized on App B.** Page 02 step 4a. Pre-authorization
   and admin consent are independent belts; with both in place you can rule this
   hop out quickly.

5. **You added the permission to the wrong app.** The permission goes on **App A**
   (the caller), pointing at App B. Not on App B. Easy to invert when you have
   both blades open.

**Nuclear option**, a tenant-wide admin consent URL — sign in as
`m365-admin@<TENANT_DOMAIN>`:

```
https://login.microsoftonline.com/<ENTRA_TENANT_ID>/adminconsent?client_id=<APP_A_CLIENT_ID>
```

This grants everything App A currently requests. Review the permission list
first — it is exactly why you should not have added speculative Graph
permissions.

> If the error names a **Teams** client GUID such as
> `1fec8e78-bce4-4aaf-ab1b-5451cc387264`, that is the pre-authorization step on
> App A, not admin consent. Page 01 step 6.

---

## `invalid_grant` on the OBO call

The most generic error of the four. `invalid_grant` is OAuth for "your grant is
no good", and Entra uses it for several unrelated conditions. **Read the
`error_description` and the `AADSTS` code inside it** — that is where the actual
information is.

```bash
echo "$OBO_RESPONSE" | jq -r '.error, .error_description'
```

| Sub-error / code | Meaning here | Fix |
| --- | --- | --- |
| `AADSTS50013` — assertion invalid | Malformed, truncated or tampered assertion | Shell variable expansion. Use `--data-urlencode`, and check `$USER_TOKEN` has exactly two dots. |
| `AADSTS700082` / expired | The assertion has expired | Entra access tokens live ~60–90 min. Get a fresh one. Do not cache the assertion. |
| `AADSTS50027` — invalid JWT | Not a JWT, or wrong format | You captured `id_token` where you wanted `access_token`, or grabbed the whole JSON response. |
| `AADSTS7000215` — invalid client secret | Secret wrong, expired, or the **Secret ID** was copied instead of the **Value** | Page 01 step 3. Create a new secret; check its expiry date. |
| `AADSTS500131` | Audience mismatch | See above. |
| `AADSTS65001` | Consent missing | See above. |
| `interaction_required` / `AADSTS50079` | Conditional Access requires MFA for App B | See below. |

**The most common cause by a wide margin: a mangled assertion.** Check the shape
before you check anything else:

```bash
echo "$USER_TOKEN" | awk -F. '{print "segments:", NF}'   # must print 3
echo -n "$USER_TOKEN" | wc -c                             # must be > 1000
```

Fewer than three segments means the variable is empty or truncated. Watch for
shells that wrap long values, and for copy-paste from a terminal that inserted
line breaks.

**Second most common: the assertion has no `scp` claim.** OBO requires a
delegated token. An app-only (client credentials) token has `roles` instead of
`scp` and will be rejected:

```bash
jwtdecode "$USER_TOKEN" | jq '{scp, roles}'
```

`scp` present and `roles` absent is what you want.

### `interaction_required` / `AADSTS50079` — Conditional Access

```json
{"error": "interaction_required",
 "error_description": "AADSTS50079: ... you must enroll in multifactor authentication ..."}
```

A Conditional Access policy on App B requires an interaction — typically MFA —
that the user's original token does not satisfy. OBO cannot satisfy it, because
there is no user present at the middle tier.

Correct handling, and Microsoft documents this precisely: the middle tier
returns **HTTP 401** with a `WWW-Authenticate` header carrying the error and the
`claims` challenge from the response body. The client re-authenticates,
presenting that challenge. **Do not retry with the cached token** — it will fail
identically, and a retry loop against the token endpoint gets you throttled.

For a sandbox demo, the simpler answer is to check whether a Conditional Access
policy is scoped to App B at all and exclude it if this is just a stray sandbox
default. Entra admin center → **Protection** → **Conditional Access** →
**Policies**.

---

## Google STS: `Invalid value for "audience"`

```json
{"error": "invalid_request",
 "error_description": "Invalid value for \"audience\". This value should be the full resource name of the Identity Provider."}
```

**Two completely different things are called "audience" in this request, and the
error does not reliably tell you which one is wrong.** Check both.

### Case A — the `audience` request parameter is malformed

The `audience` field in the POST body is the **full resource name of the
workforce pool provider**. The wording of the error above ("full resource name
of the Identity Provider") points at this case.

Correct value, exactly:

```
//iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/providers/entra
```

Things that break it:

- Missing the leading `//`. It is two slashes, no scheme. Not `https://`.
- Using the **workload** path `/projects/<n>/locations/...`. Workforce pools are
  **not** project-scoped: the path is `/locations/global/workforcePools/...`
  with no project segment. This is a very common slip when adapting a workload
  identity example.
- Wrong pool or provider ID. Here: pool `teams-bot-demo`, provider `entra`.
- A trailing slash.

Confirm the provider exists and the name matches character for character:

```bash
gcloud iam workforce-pools providers describe entra \
  --workforce-pool=teams-bot-demo --location=global --format='value(name)'
```

### Case B — the JWT's `aud` claim does not match the provider's client ID

**This is the one this design is genuinely at risk of, and page 02 is largely
about preventing it.**

Google validates the incoming JWT's `aud` claim against the client ID configured
on the workforce pool provider — `<FEDERATION_APP_CLIENT_ID>`. If your
OBO token carries `aud: api://<FEDERATION_APP_CLIENT_ID>` instead of the bare GUID, this is a
string mismatch and Google rejects it.

Diagnose in one command:

```bash
jwtdecode "$OBO_TOKEN" | jq '{aud, iss, ver}'
```

| What you see | Diagnosis | Fix |
| --- | --- | --- |
| `aud: "<FEDERATION_APP_CLIENT_ID>"`, `ver: "2.0"` | Correct. The problem is Case A. | Fix the `audience` parameter. |
| `aud: "api://<FEDERATION_APP_CLIENT_ID>"`, `ver: "1.0"` | App B is emitting v1.0 access tokens | **Page 02 step 4b** — `requestedAccessTokenVersion: 2`, then get a **fresh** token |
| `aud: "<APP_A_CLIENT_ID>"` | You sent the pre-OBO token | You skipped or silently failed the OBO step |
| `aud: "https://graph.microsoft.com"` | Graph token | Wrong scope on the OBO call entirely |

**Do not fix Case B on the Google side.** Adding `api://<FEDERATION_APP_CLIENT_ID>` to the
provider's allowed audiences makes this specific error disappear and leaves you
with a v1.0 token whose `iss` is `https://sts.windows.net/<ENTRA_TENANT_ID>/` — which
still fails issuer validation, now with a less obvious error. Fix the token
format at the source.

### Related Google-side errors

**`Invalid value for "subject_token"` / signature verification failed**

Google could not verify the JWT. Check, in order:

```bash
jwtdecode "$OBO_TOKEN" 1 | jq '{alg, kid}'   # alg must be RS256 or ES256; kid required
jwtdecode "$OBO_TOKEN" | jq '.exp'           # must be in the future
```

Then confirm the signing key is published where Google will look:

```bash
KID=$(jwtdecode "$OBO_TOKEN" 1 | jq -r .kid)
curl -sS "https://login.microsoftonline.com/$TENANT_ID/discovery/v2.0/keys" \
  | jq --arg k "$KID" '.keys[] | select(.kid == $k) | .kid'
```

No match usually means a v1.0 token again — signed with a key published at the
v1.0 endpoint, not the v2.0 one the provider's issuer points at.

**Issuer mismatch**

The provider's issuer is
`https://login.microsoftonline.com/<ENTRA_TENANT_ID>/v2.0`.
A v1.0 token's `iss` is `https://sts.windows.net/<ENTRA_TENANT_ID>/` and will not
match. Same root cause, same fix: page 02 step 4b.

**`Unable to map the subject` / attribute mapping failure**

The mapping is `google.subject = assertion.oid`. If `oid` is absent from the
assertion, the mapping produces nothing and the exchange fails.

```bash
jwtdecode "$OBO_TOKEN" | jq '.oid'
```

`null` means the identity was lost. Almost always because the assertion into OBO
was an app-only token — no user, so no `oid`. Back to verification step 1.

**HTTP 401 from STS with an otherwise correct request**

You sent an `Authorization` header. Google's STS documentation states explicitly
that this method does not require it and that including it can cause the request
to fail. Remove it.

**`403` on the downstream Google API call, with a valid STS token**

Not an authentication failure. The token is fine and the principal resolved; the
workforce principal simply has no IAM role granted for that resource. Google-side
IAM binding, nothing on the Microsoft side will change it. Check that a binding
exists for
`principal://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/subject/<oid>`
or a `principalSet://` covering it.

---

## Teams-side symptoms with no error text

Once you are inside the Teams client, failures often present as nothing at all.

| Symptom | Likely cause |
| --- | --- |
| Bot never responds to the first message | Messaging endpoint unreachable, or the bot is not validating/handling the invoke activity. Check the middle tier logs first. |
| A consent prompt appears | Teams client IDs not pre-authorized on App A — page 01 step 6 |
| Sign-in card appears instead of silent SSO | `webApplicationInfo` missing, or `resource` does not match App A's Application ID URI exactly |
| "App not found" on install | Custom app upload policy, or a zip containing a folder — page 03 |
| Works on desktop, fails on mobile | Mobile WebView blocks the silent iframe token acquisition. Known platform limitation; the interactive popup fallback (page 01 step 7) is the answer. |
| Works for the admin, fails for `analyst@` | Admin consent was never granted tenant-wide; the admin's own consent covered only the admin |
| Bot offered for a team or group chat | Manifest `scopes` is not `["personal"]` only |

## When you are properly stuck

Collect, in this order:

1. Which verification step in page 04 last passed.
2. The decoded claims — **not the tokens** — at each hop: `aud`, `iss`, `oid`,
   `ver`, `scp`.
3. The full Entra error JSON including Trace ID, Correlation ID, Timestamp.
4. The exact `audience` string sent to Google STS.
5. `requestedAccessTokenVersion` as currently stored on App B, read back from the
   Manifest blade after a reload.

Nine times in ten, item 5 is the answer.
