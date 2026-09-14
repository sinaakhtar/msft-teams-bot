# 11 — Azure Bot Service to Cloud Run

**Who runs this:** `m365-admin@<TENANT_DOMAIN>`, or any account holding
**Contributor** on the target Azure subscription *and* the Entra
**Application Administrator** role. Creating the bot resource is an Azure RBAC
action; pointing it at an existing app registration is an Entra action. In the
E5 sandbox one account usually has both, but if the "Create" button greys out
on the app-registration picker, that split is why.

**What you end up with:** an Azure Bot resource that accepts activities from
the Microsoft Teams channel, signs them with a Bot Connector JWT, and POSTs
them to the middle tier on Cloud Run at `/api/messages` — and a real message
sent from a real Teams client that reaches that endpoint and comes back with a
reply.

**What this page does NOT do:** it does not create App A (that is
[`entra/01_bot_app_registration.md`](../entra/01_bot_app_registration.md)), it
does not configure the OBO exchange to Google (that is
[`entra/02_federation_app_obo.md`](../entra/02_federation_app_obo.md)), and it
does not package or upload the Teams app (that is
[`entra/03_teams_app_manifest.md`](../entra/03_teams_app_manifest.md)). It
covers exactly the Azure-Bot-to-Cloud-Run wire and proving a round trip over
it.

**Nothing on this page has been executed.** No portal step here was performed
and no command here was run by the author. See [NOTES.md](NOTES.md) for the
line between documented fact and reasoning.

---

## Placeholders you must supply

| Placeholder | What it is | Where it comes from |
| --- | --- | --- |
| `<APP_A_CLIENT_ID>` | Teams bot app registration's Application (client) ID | `entra/01`, step 2 |
| `<APP_A_CLIENT_SECRET>` | App A client secret **Value** | `entra/01`, step 3 — Google Secret Manager only |
| `<AZURE_SUBSCRIPTION>` | Azure subscription the bot resource is billed to | Your tenant |
| `<AZURE_RESOURCE_GROUP>` | Resource group for the bot resource | Create or reuse |
| `<BOT_HANDLE>` | Globally unique bot handle, 4–42 chars, `a-z A-Z 0-9 - _`, starts with letter or digit | You choose |
| `<CLOUD_RUN_SERVICE>` | Cloud Run service name for the middle tier | Your deploy |
| `<CLOUD_RUN_URL>` | Full HTTPS base URL Cloud Run assigns, no trailing slash | `gcloud run services describe` |
| `<BOT_DOMAIN>` | `<CLOUD_RUN_URL>` with the scheme stripped | Derived |
| `<REASONING_ENGINE_ID>` | Numeric ID of **our** reasoning engine | The Agent Runtime deploy |
| `<OAUTH_CONNECTION_NAME>` | Name of the OAuth connection setting, if you create one | Step 6 |

Known values, do not retype from memory and do not change:

| Value | What |
| --- | --- |
| `<ENTRA_TENANT_ID>` | Entra tenant ID (`<TENANT_DOMAIN>`) |
| `<FEDERATION_APP_CLIENT_ID>` | App B, the federation app Google trusts. Not the bot. |
| `<GCP_PROJECT_ID>` / `<GCP_PROJECT_NUMBER>` | GCP project ID / number |
| `us-central1` | Region |

> **`reasoningEngines/<OTHER_ENGINE_ID_1>` is not ours.** It is a pre-existing
> `data_science_agent` belonging to someone else in `<GCP_PROJECT_ID>`. Never set
> `REASONING_ENGINE_ID` to it, never modify it, never delete it.

---

## A note on portal naming before you start

Microsoft renames these surfaces frequently, and two of the renames are recent
enough that most search results are stale. Where a label has moved, this page
gives the current path **and** the underlying stable name.

- The service is **Azure AI Bot Service**. It was "Azure Bot Service", and
  before that "Bot Framework". The resource type you create is still called
  **Azure Bot** in the Marketplace, and the ARM type is still
  `Microsoft.BotService/botServices`.
- **Web App Bot** and **Bot Channels Registration** are gone. Microsoft's own
  quickstart states new resources of those types "can't be created; however,
  any such existing resources that are configured and deployed will continue to
  work."
  (<https://learn.microsoft.com/en-us/azure/bot-service/abs-quickstart>,
  page last updated 2026-09-01.) If a guide tells you to create a Web App Bot,
  it is out of date; you want **Azure Bot**.
- The settings pages moved under a **Settings** group in the left nav. The
  messaging endpoint lives on **Settings → Configuration** (older docs say just
  "Configuration"). The stable ARM property is `properties.endpoint`.
- Channels live on **Settings → Channels** (older docs say "Channels" at the
  top level).
- **Microsoft Entra ID** is what used to be Azure AD. Breadcrumbs inside the
  Azure portal still say Azure AD in places.

---

## Step 0 — What must already be true

Do not start until all four hold. Each has a check.

| Precondition | Check | If it fails |
| --- | --- | --- |
| App A exists, single-tenant | Entra admin center → App registrations → your app → Overview shows **Supported account types: My organization only** | Go back to `entra/01`. Account type cannot be changed to single-tenant after the Azure Bot resource is bound to it without recreating the bot resource. |
| App A has a client secret, stored in Google Secret Manager | `gcloud secrets versions access latest --secret=teams-bot-app-password --project=<GCP_PROJECT_ID> \| head -c 8` prints something | `entra/01` step 3. The secret **Value** is shown once and only once. |
| The middle tier is deployed to Cloud Run and answers `/healthz` | `curl -sS -o /dev/null -w '%{http_code}\n' <CLOUD_RUN_URL>/healthz` prints `200` | Deploy first. There is no point registering an endpoint that 404s; you will spend an hour debugging Azure for a Google-side problem. |
| The middle tier is **ready**, not merely alive | `curl -sS <CLOUD_RUN_URL>/readyz` prints `{"status": "ready", ...}` | `/readyz` returns 503 with a `checks` object naming the failure. `jwks: unavailable:*` means the container cannot reach `login.botframework.com`; check egress. `config` failures mean a missing environment variable — the container also logs `startup configuration failed` at CRITICAL and exits 2. |

`/healthz` is a constant and `/readyz` does real work, deliberately. A liveness
probe that touches a dependency turns a downstream blip into a restart loop.
Use `/readyz` for "is this thing actually going to work", not `/healthz`.

---

## Step 1 — Choose the app type. Get this right; it is not reversible in place.

This is the single highest-consequence decision on the page, so read the whole
step before clicking anything.

When you create an Azure Bot resource, the portal asks for **Type of App**
under the Microsoft App ID section. Three options, and Microsoft's guidance for
each (<https://learn.microsoft.com/en-us/azure/bot-service/abs-quickstart>):

| Type of App | Microsoft's stated fit | Fit for **this** bot |
| --- | --- | --- |
| **User-assigned managed identity** | Bot doesn't need resources outside its home tenant, and is hosted on an Azure resource that supports managed identities | **Wrong twice over.** See below. |
| **Single tenant** | Bot doesn't need resources outside its home tenant, but isn't hosted on an Azure resource that supports managed identities | **Correct. Use this.** |
| **Multi-tenant** | Bot needs resources outside its home tenant or serves multiple tenants | **Wrong, and being retired.** |

### Why single-tenant is the only correct answer here

**1. A managed-identity bot has no client secret, and this bot must have one.**

The whole point of this system is the On-Behalf-Of exchange: the middle tier
takes the Teams SSO token for the signed-in user and exchanges it, at
`login.microsoftonline.com/<ENTRA_TENANT_ID>/oauth2/v2.0/token`, for a token
audienced to App B (`<FEDERATION_APP_CLIENT_ID>`), which Google's
workforce pool provider trusts. OAuth 2.0 On-Behalf-Of (RFC 8693 token
exchange, as implemented by the Microsoft identity platform) requires the
calling application to authenticate as a **confidential client** — a client
secret or a certificate. A user-assigned managed identity gives the bot an
Azure-issued identity that Azure resources can use to get tokens for Azure
resources; it does not hand you a credential you can present in an OBO request
from a process running on Cloud Run.

Two independent reasons that path is closed:

- The middle tier does not run on Azure. Managed identity token acquisition
  depends on the Azure Instance Metadata Service endpoint available to Azure
  compute. Cloud Run is not Azure compute. There is no IMDS to call.
- Even if it ran on Azure, the identity assigned by a managed-identity bot is
  not a confidential client registration whose secret you can use for OBO
  against App B.

The consequence of choosing managed identity anyway: everything up to and
including a Teams round trip may appear to work. Teams sends the activity, Bot
Service signs a JWT, your endpoint validates it, you extract `aadObjectId`, and
then the OBO call fails — and per ADR 004 the bot returns an identity-failure
message and a sign-in card and stops. You will have a bot that chats about
nothing and cannot reach BigQuery, and the error will surface three layers away
from the checkbox that caused it.

**2. Multi-tenant breaks the single-issuer trust the Google provider depends
on, and Microsoft is retiring it.**

The workforce pool provider `entra` trusts exactly one issuer,
`https://login.microsoftonline.com/<ENTRA_TENANT_ID>/v2.0`.
A multi-tenant app can mint tokens carrying a different `tid` and therefore a
different issuer, which Google rejects — and if Google did *not* reject them,
that would be the trust hole, not the fix.

Separately, Microsoft's quickstart carries this notice verbatim:
"Multi-tenant bot creation will be deprecated after July 31, 2025. Existing
multi-tenant bots will continue to function, but new multi-tenant bot creation
will no longer be supported after that date. To ensure continued support, use
single-tenant or user-assigned managed identity going forward."
(<https://learn.microsoft.com/en-us/azure/bot-service/abs-quickstart>.) Whether
the portal still lets you pick it in your subscription is not something this
page can tell you; if it does, do not.

**3. The trade-off you are accepting, stated plainly.**

Single-tenant and user-assigned managed identity are, per the same page,
supported in "Azure AI Bot Service; C#, JavaScript, and Python SDKs" and are
explicitly **not** supported in "other SDK languages, Bot Framework Composer,
**Bot Framework Emulator**, or Dev Tunnels." Multi-tenant is the only type the
Emulator supports.

So: choosing single-tenant costs you the Bot Framework Emulator as a debugging
tool against the real bot registration. That is a real loss and you should know
about it before you hit a wall in the troubleshooting section. The middle tier
has an `ALLOW_BOT_EMULATOR` flag for local development, gated behind
`MIDDLE_TIER_DEV_MODE`, and the process refuses to start if you set the former
without the latter. Never set either on the Cloud Run revision that Teams
talks to.

### 1a. Create the resource

1. Sign in to <https://portal.azure.com> as an account with Contributor on
   `<AZURE_SUBSCRIPTION>`.
2. **Confirm the directory.** Top right, avatar → **Switch directory**, and
   confirm `<TENANT_DOMAIN>`, tenant
   `<ENTRA_TENANT_ID>`. In a sandbox it is very easy to be
   signed in to two directories and configure the wrong one. You will not get a
   useful error; you will get a bot that Teams cannot see.
3. **Create a resource** → search `bot` → press Enter → select the **Azure Bot**
   card → **Create**.
4. **Bot handle**: `<BOT_HANDLE>`. Must be globally unique, 4–42 characters,
   `a-z A-Z 0-9 - _`, starting with a letter or digit.
5. **Subscription**: `<AZURE_SUBSCRIPTION>`. **Resource group**:
   `<AZURE_RESOURCE_GROUP>` (create new if needed).
6. **Data residency**: choose **Global** unless you have a specific requirement.
   If you pick a regional option, note it — it changes the Bot Connector
   endpoints and the OpenID metadata document your endpoint must trust, and the
   middle tier's default profile is the public-cloud one
   (`https://login.botframework.com/v1/.well-known/openidconfiguration`).
7. **Pricing tier**: **F0 (Free)** is fine for a demo. The Teams channel is not
   metered on standard channels.
8. **Type of App**: **Single Tenant**. Stable ARM property:
   `properties.msaAppType` = `SingleTenant`.
9. **Creation type**: **Use existing app registration**. Do **not** let the
   portal create a new one — App A already exists with the exposed
   `access_as_user` scope, the `api://botid-<APP_A_CLIENT_ID>` Application ID
   URI, and the pre-authorized Teams client IDs that Teams SSO needs. A
   portal-created app has none of that.
10. **App ID**: `<APP_A_CLIENT_ID>`. **App tenant ID**:
    `<ENTRA_TENANT_ID>`.
11. **Review + create** → **Create**. Deployment takes a minute or two.

### 1b. Verify the app type actually landed

The portal has been known to show the field and not persist it if you switched
creation type mid-form. Check it, from a shell, before moving on:

```bash
az bot show \
  --name "<BOT_HANDLE>" \
  --resource-group "<AZURE_RESOURCE_GROUP>" \
  --query "{appType:properties.msaAppType, appId:properties.msaAppId, tenant:properties.msaAppTenantId, endpoint:properties.endpoint}" \
  -o json
```

You want `appType: "SingleTenant"`, `appId` equal to `<APP_A_CLIENT_ID>`, and
`msaAppTenantId` equal to the tenant GUID. If `appType` says
`UserAssignedMSI` or `MultiTenant`, **delete the bot resource and recreate it.**
Do not try to patch it. The app type determines how Bot Service acquires tokens
to call your endpoint and how it expects your endpoint to authenticate back;
changing it under a live registration is not a supported edit and the failure
mode is intermittent 401s that look like a code bug.

---

## Step 2 — Point the messaging endpoint at Cloud Run

### 2a. Get the Cloud Run URL, exactly

```bash
gcloud run services describe <CLOUD_RUN_SERVICE> \
  --project <GCP_PROJECT_ID> --region us-central1 \
  --format='value(status.url)'
```

That prints something of the form `https://<CLOUD_RUN_SERVICE>-<GCP_PROJECT_NUMBER>.us-central1.run.app`
(the current default format) or the older
`https://<CLOUD_RUN_SERVICE>-<hash>-uc.a.run.app`. Either is fine. Copy it
verbatim rather than reconstructing it by hand — a mistyped hostname produces a
DNS failure that Azure reports as a generic endpoint error.

**Cloud Run's URL is HTTPS with a Google-managed, publicly trusted
certificate.** This matters because Bot Framework requires the messaging
endpoint to be HTTPS and will not accept a self-signed certificate. You do not
need to buy a certificate, configure a load balancer, or run certbot for this
demo. If you later put a custom domain in front, you must ensure the
certificate chain is publicly valid before you change the endpoint.

### 2b. Set the endpoint

Portal path: **Azure Bot resource → Settings → Configuration → Messaging
endpoint**. Stable ARM property: `properties.endpoint`.

Set it to:

```
<CLOUD_RUN_URL>/api/messages
```

The path is not optional and not conventional-only — it is the only POST route
the middle tier registers. `/healthz` and `/readyz` are GET-only. Pointing the
endpoint at the bare base URL gives you a 404 on every activity, which Bot
Service surfaces to Teams as a failed send with no useful text.

Click **Apply**.

Or from the CLI:

```bash
az bot update \
  --name "<BOT_HANDLE>" \
  --resource-group "<AZURE_RESOURCE_GROUP>" \
  --endpoint "<CLOUD_RUN_URL>/api/messages"
```

### 2c. Confirm the endpoint is reachable from the internet

Before Teams is in the picture at all:

```bash
# Expect 401 — unauthenticated POST is exactly what a forged activity looks like.
curl -sS -o /dev/null -w '%{http_code}\n' \
  -X POST -H 'Content-Type: application/json' \
  -d '{"type":"message","text":"hi"}' \
  <CLOUD_RUN_URL>/api/messages
```

**`401` is the pass condition.** A `200` here means the endpoint is accepting
unauthenticated activities and you must stop and fix that before going further —
see the next step for why. A `403` usually means Cloud Run's invoker check is
still on. A `404` means the path is wrong. A connection error means the URL or
the ingress setting is wrong.

Note the response has no body detail. That is deliberate: "wrong audience" vs
"bad signature" vs "expired" would be a free oracle for anyone tuning a forged
token.

---

## Step 3 — Cloud Run must allow unauthenticated invocations. Read this part twice.

### 3a. What you have to do

Bot Framework authenticates itself to your bot with **its own JWT** in the
`Authorization` header — a token issued by the Bot Connector service, with
issuer `https://api.botframework.com` and audience equal to the bot's Microsoft
App ID. It is not a Google identity token and Bot Service has no mechanism for
minting one. Therefore Cloud Run's own IAM invoker check must not stand in
front of the endpoint, because it would reject every activity before your
process ever sees it.

Two supported ways, per
<https://cloud.google.com/run/docs/authenticating/public>:

- **Disable the Cloud Run Invoker IAM check** on the service (Google's
  recommended option), or
- grant `roles/run.invoker` to the special member `allUsers`.

```bash
# Option B, the one most orgs' policies are written around:
gcloud run services add-iam-policy-binding <CLOUD_RUN_SERVICE> \
  --project <GCP_PROJECT_ID> --region us-central1 \
  --member="allUsers" --role="roles/run.invoker"
```

If your organization enforces
`constraints/iam.allowedPolicyMemberDomains` in a way that blocks `allUsers`,
use the invoker-check-disable option instead. Also confirm ingress is not
restricted to internal traffic:

```bash
gcloud run services describe <CLOUD_RUN_SERVICE> \
  --project <GCP_PROJECT_ID> --region us-central1 \
  --format='value(metadata.annotations["run.googleapis.com/ingress"])'
```

Anything other than `all` (or empty, which defaults to `all`) and Bot Service
cannot reach you.

### 3b. What you have just done, stated without euphemism

> **This is the moment the endpoint becomes internet-facing with no network
> authentication in front of it.** Any host on the internet can now POST a JSON
> body to `<CLOUD_RUN_URL>/api/messages`. An attacker who wants to impersonate
> `analyst@<TENANT_DOMAIN>` does not need to compromise Entra, Teams, or
> Google. They need to send one HTTP request containing
> `"from": {"aadObjectId": "<ANALYST_OBJECT_ID>"}`.
>
> **The inbound JWT validation in the middle tier is the only thing standing
> between the public internet and a forged activity claiming to be any user in
> the tenant.** Not a firewall. Not Cloud Run IAM. Not Teams. One code path,
> `app/auth/inbound.py`, executed before any identity field is read.

This is why `app/main.py` puts a hard comment above the call —
*"AUTHENTICATION. Nothing below this line may read identity fields until this
returns an AuthenticatedCaller"* — and why the router receives an
`AuthenticatedCaller` rather than the raw body. The `aadObjectId` on an
unvalidated activity is an attacker-controlled string. Once validated, it is
the claim of the Bot Connector service, cryptographically signed, and only then
is it allowed to become the `entra:{tid}:{oid}` session key from ADR 003 and,
downstream, the subject of the OBO exchange.

Cross-references you should not skip:

- **ADR 002 (two identity planes)** — the service identity of the Cloud Run
  service is never a substitute for the user identity. A forged activity that
  slipped past validation would be laundered into a real federated Google
  identity by the OBO exchange, which is precisely the attack this validation
  prevents.
- **ADR 003 (`user_id` = `entra:{tid}:{oid}`)** — an activity with no
  `aadObjectId` is refused rather than served under a fallback key.
- **ADR 004 (fail closed)** — a validation failure is a 401 with no body, not a
  degraded-but-working path.

### 3c. Verify the validation is really on before you demo

The middle tier must reject each of these. Run all four; a `200` on any of them
is a stop-the-line finding.

```bash
BASE=<CLOUD_RUN_URL>

# 1. No Authorization header at all.
curl -sS -o /dev/null -w 'no-auth: %{http_code}\n' -X POST \
  -H 'Content-Type: application/json' \
  -d '{"type":"message","text":"x","from":{"aadObjectId":"<ANALYST_OBJECT_ID>"}}' \
  $BASE/api/messages

# 2. Garbage bearer token.
curl -sS -o /dev/null -w 'garbage: %{http_code}\n' -X POST \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer not.a.jwt' \
  -d '{"type":"message","text":"x"}' $BASE/api/messages

# 3. Wrong content type.
curl -sS -o /dev/null -w 'wrong-ct: %{http_code}\n' -X POST \
  -H 'Content-Type: text/plain' --data 'hello' $BASE/api/messages

# 4. Malformed JSON with a plausible-looking header.
curl -sS -o /dev/null -w 'bad-json: %{http_code}\n' -X POST \
  -H 'Content-Type: application/json' -H 'Authorization: Bearer eyJ.eyJ.sig' \
  --data '{' $BASE/api/messages
```

Expected: `401`, `401`, `415`, `400` or `401`. The ordering of the 400/401 on
case 4 depends on whether the body or the header is examined first and either
is acceptable; what is not acceptable is `200`.

The rejections are logged. Confirm they arrived (see Step 8 for the full log
recipe):

```bash
gcloud logging read \
  'resource.type="cloud_run_revision"
   resource.labels.service_name="<CLOUD_RUN_SERVICE>"
   jsonPayload.message="inbound activity rejected"' \
  --project <GCP_PROJECT_ID> --freshness=10m --limit=10 \
  --format='table(timestamp, jsonPayload.reason, jsonPayload.channel_id, jsonPayload.remote)'
```

### 3d. What the validation must check

For reference when reviewing `app/auth/inbound.py`, Microsoft's normative list
for Connector-to-bot tokens
(<https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-authentication>):

1. Token sent in the HTTP `Authorization` header with the `Bearer` scheme.
2. Valid JSON conforming to the JWT standard.
3. `iss` claim equals `https://api.botframework.com`.
4. `aud` claim equals the bot's Microsoft App ID — `<APP_A_CLIENT_ID>`.
5. Within its validity period; industry-standard clock skew is 5 minutes.
6. Valid signature against a key from the OpenID keys document at
   `https://login.botframework.com/v1/.well-known/keys`, discovered via the
   static metadata document
   `https://login.botframework.com/v1/.well-known/openidconfiguration`, using an
   algorithm listed in `id_token_signing_alg_values_supported` (`RS256`).
7. `serviceUrl` claim matches the `serviceUrl` property at the root of the
   incoming Activity.
8. Where a channel requires endorsements, the token must carry an endorsement
   for the `channelId` in the activity; if absent, reject with **403**.

Microsoft's own emphasis: "All of these requirements are important,
particularly requirements 4 and 6. Failure to implement ALL of these
verification requirements will leave the bot open to attacks which could cause
the bot to divulge its JWT token." And: "Implementers shouldn't expose a way to
disable validation of the JWT token that is sent to the bot."

The same page notes the key list is stable and cacheable but new keys can be
added at any time, so every bot instance should refresh its cached copy **at
least once every 24 hours**. Check that the JWKS cache TTL in the middle tier is
not longer than that.

---

## Step 4 — Enable the Microsoft Teams channel

Path: **Azure Bot resource → Settings → Channels**. Older docs and some
breadcrumbs say just "Channels"; Microsoft's channel article says "In the left
pane, select **Channels** under **Settings**"
(<https://learn.microsoft.com/en-us/azure/bot-service/bot-service-manage-channels>).

1. In the channel gallery, select the **Microsoft Teams** icon. You may have to
   scroll to see all **Available Channels**.
2. Read and accept the terms of service.
3. On the **Messaging** tab, select the **cloud environment** for your bot.
   For this tenant that is the commercial/public cloud — the default. Do not
   pick a government cloud option; it changes the Connector endpoints and the
   OpenID metadata document, and your endpoint validates against the public one.
4. Leave the **Calling** tab off. It is for Teams voice/video bots and this bot
   does not handle calls.
5. Ignore the **Publish** tab. That is for listing in the Teams Store, which
   this demo does not do — the Teams app is sideloaded from
   `entra/03_teams_app_manifest.md`.
6. Click **Apply**.

(Steps per
<https://learn.microsoft.com/en-us/azure/bot-service/channel-connect-teams>.)

### 4a. Grab the embed code for the fast smoke test

Still on the Teams channel page, select **Get bot embed code** and copy the
`https://teams.microsoft.com/l/chat/0/0?users=28:...` part. Opening that URL in
a browser drops you into a 1:1 Teams chat with the bot **without** installing a
Teams app package. It is the quickest possible proof that the Azure-to-Cloud-Run
wire works.

Two caveats, both from Microsoft's page:

- "Adding a bot by GUID, for anything other than testing purposes, isn't
  recommended. Doing so severely limits the functionality of a bot." In
  particular the embed-code chat does **not** exercise the Teams app manifest,
  so it does **not** exercise Teams SSO. Expect the bot to reach the
  ADR 004 identity-failure path in this mode unless a sign-in card flow is
  configured. That is a correct result, not a bug — see Step 7.
- "Deleting the Teams channel registration will cause a new pair of keys to be
  generated when it's re-enabled. This invalidates all `29:xxx` and `a:xxx` IDs
  that the bot may have stored for proactive messaging." Do not delete and
  re-add the channel to "reset" something the day before a demo.

Also worth heeding: "Use one bot channel registration per environment, since
your endpoint changes when you switch between local development, staging, and
production environments." If you develop locally against a tunnel, use a second
bot resource, not this one.

---

## Step 5 — Where Teams SSO fits, and when you need an OAuth connection

These are two different mechanisms and people conflate them constantly. Decide
which one you are using before touching the Configuration blade.

### The two paths

**Path A — Teams SSO (this project's primary design).** The Teams client
obtains a token for App A silently, using the `access_as_user` scope you exposed
on the `api://botid-<APP_A_CLIENT_ID>` Application ID URI and the pre-authorized
Teams client IDs, both configured in `entra/01`. The bot sends an OAuth card,
and the Teams client responds with a `signin/tokenExchange` **invoke** activity
carrying the token. Your middle tier reads that token off the invoke activity
and uses it as the user assertion for the OBO exchange to App B, and from there
to Google's STS.

Microsoft's description of the runtime flow: "After the Teams client receives
the OAuth card for the app user, if SSO is enabled, it sends a token exchange
request for the app user back to the bot. The bot calls the Bot Framework Token
Service, attempting to exchange the received token from Microsoft Entra ID."
(<https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/authentication/bot-sso-overview>.)

Two constraints from that page that shape the demo:

- **SSO for a bot is supported in one-on-one and group chat scope, and is not
  supported in channel scope.** Demo in a 1:1 chat with the bot. If you demo in
  a Teams channel, SSO will not fire and every turn will hit the identity
  failure path.
- First use requires consent. "For the app user who's using the bot service for
  the first time, the token exchange can occur only after app user gives their
  consent." Both demo users must consent **before** the audience is watching.
  See runbook 12's pre-flight.

**Path B — OAuth connection setting with a sign-in card.** Bot Service holds
the client credentials, runs the authorization-code flow, and stores the
resulting token in the Bot Framework Token Store. Your bot asks the Token
Service for the user's token rather than exchanging one itself.

### Which do you need?

You need **Path A** for the demo to be honest — the identity has to originate
from the signed-in Teams user without a separate login. You need **Path B**
configured as well if either of these is true:

- You want the ADR 004 identity-failure **sign-in card** to actually be
  clickable rather than decorative. ADR 004 says on identity failure the turn is
  refused "with an explicit message and a sign-in card", and that card needs a
  connection to point at.
- You intend to demo via the Step 4a embed-code chat, which bypasses the Teams
  app manifest and therefore bypasses SSO.

Note that the Microsoft SSO overview describes the fallback explicitly: if
consent fails, "the authentication falls back to the sign-in prompt and the app
user must sign in to use the bot app." So Path B is the fallback rail under
Path A, not an alternative to it.

### 5a. Create the OAuth connection setting (only if you decided you need it)

Path: **Azure Bot resource → Settings → Configuration**, then scroll to
**OAuth Connection Settings near the bottom of the page** and select
**Add Setting** (wording per
<https://learn.microsoft.com/en-us/azure/bot-service/bot-builder-authentication>).

| Field | Value |
| --- | --- |
| **Name** | `<OAUTH_CONNECTION_NAME>` — you will put this in the middle tier config; keep it short and free of spaces |
| **Service Provider** | **Azure Active Directory v2** (the v2/Entra endpoint variant, not v1) |
| **Client id** | `<APP_A_CLIENT_ID>` |
| **Client secret** | `<APP_A_CLIENT_SECRET>` |
| **Token Exchange URL** | `api://botid-<APP_A_CLIENT_ID>` — **this field is what makes SSO work.** Leaving it blank gives you a plain sign-in card and no silent token exchange. |
| **Tenant ID** | `<ENTRA_TENANT_ID>` — not `common`, not `organizations`; this is a single-tenant app |
| **Scopes** | The delegated scopes you need on the token, space-separated. At minimum `openid profile offline_access`. Do not add Graph scopes you do not use. |

Click **Save**, reopen the setting, and click **Test Connection**. It opens a
consent window and should end on a page showing a token was acquired. Test it
as *each* demo user, not just as the admin — consent is per user.

> The client secret is now in two places: Azure Bot Service's configuration and
> Google Secret Manager. That is unavoidable if you use Path B, and it is worth
> saying out loud rather than discovering at rotation time. When you rotate the
> secret you must update both. It must not be in the repo, in a `.env` file, or
> in a terminal history.

### 5b. If you skip the OAuth connection

Set `SIGNIN_URL` on the Cloud Run service to a URL that tells the user what to
do (a wiki page, a support contact), so the ADR 004 identity-failure message has
somewhere to point. A message with a dead card is worse than a message with a
link to a human. `SUPPORT_CONTACT` serves the same purpose in the templated
errors.

---

## Step 6 — Environment on the Cloud Run revision

Bot Service will not work against a service that does not know its own app ID.
The middle tier reads these at startup and refuses to boot without the required
ones (logging `startup configuration failed` at CRITICAL and exiting 2).

| Variable | Value | Notes |
| --- | --- | --- |
| `MICROSOFT_APP_ID` | `<APP_A_CLIENT_ID>` | **Required.** This is the expected `aud` on every inbound Connector JWT. |
| `MICROSOFT_APP_TYPE` | `SingleTenant` | Must match the Azure Bot resource's `msaAppType`. |
| `MICROSOFT_APP_PASSWORD_SECRET` | `teams-bot-app-password` | Secret Manager secret name; default already this |
| `ENTRA_CLIENT_SECRET_SECRET` | `entra-obo-client-secret` | Secret Manager secret name for the OBO credential |
| `ENTRA_TENANT_ID` | `<ENTRA_TENANT_ID>` | Defaulted, but set it explicitly |
| `GCP_PROJECT_ID` | `<GCP_PROJECT_ID>` | |
| `GCP_PROJECT_NUMBER` | `<GCP_PROJECT_NUMBER>` | Used to build the reasoning engine resource name |
| `GCP_LOCATION` | `us-central1` | |
| `REASONING_ENGINE_ID` | `<REASONING_ENGINE_ID>` | **Ours.** Never `<OTHER_ENGINE_ID_1>`. |
| `SIGNIN_URL` | optional | Backs the ADR 004 sign-in card |
| `SUPPORT_CONTACT` | optional | Appears in templated failure messages |
| `LOG_LEVEL` | `INFO` | `DEBUG` is noisy and the formatter redacts, but do not run a demo on DEBUG |
| `MIDDLE_TIER_DEV_MODE` | **unset** | |
| `ALLOW_BOT_EMULATOR` | **unset** | Only meaningful with dev mode on; the process refuses to start if you set it without dev mode |

Confirm what is actually deployed, rather than what you think you deployed:

```bash
gcloud run services describe <CLOUD_RUN_SERVICE> \
  --project <GCP_PROJECT_ID> --region us-central1 \
  --format='yaml(spec.template.spec.containers[0].env)'
```

Then confirm the process agrees, by finding its startup line:

```bash
gcloud logging read \
  'resource.type="cloud_run_revision"
   resource.labels.service_name="<CLOUD_RUN_SERVICE>"
   jsonPayload.message="middle tier initialised"' \
  --project <GCP_PROJECT_ID> --freshness=1h --limit=3 \
  --format='table(timestamp, jsonPayload.project, jsonPayload.tenant, jsonPayload.app_type, jsonPayload.dev_mode, jsonPayload.emulator_trusted)'
```

`dev_mode` and `emulator_trusted` must both be `false`. If they are not, you
are one environment variable away from an endpoint that trusts a token minted by
anyone with the Emulator.

---

## Step 7 — Round-trip validation in a real Teams client

Do this in the order given. Each step isolates one layer, so when it breaks you
know where.

### 7a. Fast wire test (embed code, no app package)

Open the URL you copied in Step 4a. Teams opens a 1:1 chat with the bot.

Send: `hello`

**What "the wire works" looks like:** the middle tier logs an accepted activity
within a second or two, and Teams shows *some* reply. Given SSO is not available
in this mode, the honest expected reply is the ADR 004 identity-failure message
plus a sign-in card (if you did Step 5) or the identity-failure message alone.

**Do not read that as failure.** At this point you have proved: Teams reached
Bot Service, Bot Service reached Cloud Run over the public internet with a valid
certificate, the JWT validated, the router ran, and a reply travelled back. Only
the identity plane is missing, and it is missing for a documented reason.

If you see *nothing at all* in Teams, jump to troubleshooting.

### 7b. Real test (installed Teams app, SSO live)

Install the Teams app package from `entra/03_teams_app_manifest.md` and open a
**one-to-one chat** with the bot. Not a channel — SSO does not work in channel
scope.

Send: `hello`

**What a healthy exchange looks like, in order:**

1. Teams shows the message as sent.
2. On the very first turn only, a consent prompt may appear. Approve it. This is
   the one-time consent the SSO overview describes.
3. The bot replies. What it says depends on which components are wired; the
   welcome template is the expected first response.
4. Ask a real question, e.g. `who am I to BigQuery?`. Expect an informative
   update while the tool runs, then an answer. (The streaming/informative-update
   renderer is a separate work item — see [NOTES.md](NOTES.md). If it is not
   wired, you get the answer with no intermediate update, which is less
   compelling on stage but not a fault in this runbook's scope.)

**What unhealthy looks like, and what each means:**

| Symptom in Teams | Almost certainly |
| --- | --- |
| Red "Sorry, something went wrong" / retry chevron | Bot Service could not get a usable response: 5xx, timeout, or unreachable endpoint |
| Nothing at all, no error | Activity never left Teams, or the Teams channel is not enabled, or you are in channel scope with an app that only declares personal scope |
| A reply saying identity could not be established, with a sign-in card | The wire is fine; the OBO/STS chain is not. This is ADR 004 behaving correctly. Go to `entra/05_troubleshooting.md`. |
| A reply naming a refused resource | Identity worked and a downstream system said no. This is also correct behaviour, and it is the failure demo in runbook 12. |

### 7c. Confirm in Cloud Run logs that the activity arrived and validated

Run this **while** you send the message, in a second window:

```bash
gcloud beta run services logs tail <CLOUD_RUN_SERVICE> \
  --project <GCP_PROJECT_ID> --region us-central1
```

Or after the fact, structured:

```bash
gcloud logging read \
  'resource.type="cloud_run_revision"
   resource.labels.service_name="<CLOUD_RUN_SERVICE>"' \
  --project <GCP_PROJECT_ID> --freshness=10m --limit=25 \
  --format='table(timestamp, severity, jsonPayload.message, jsonPayload.reason, jsonPayload.activity_type, jsonPayload.channel_id)'
```

The middle tier logs structured JSON with a `severity` field and a `json_fields`
payload, so `jsonPayload.*` selectors work. What you are looking for:

- **No `inbound activity rejected` entry** for your message. That message with a
  `reason` field is the 401 path. Common `reason` values map directly to the
  numbered requirements in Step 3d.
- The **absence** of `unhandled error routing activity`.
- The request itself in the Cloud Run **request log**: a `POST /api/messages`
  with `httpRequest.status` of `200`.

To see the HTTP layer specifically:

```bash
gcloud logging read \
  'resource.type="cloud_run_revision"
   resource.labels.service_name="<CLOUD_RUN_SERVICE>"
   httpRequest.requestUrl:"/api/messages"' \
  --project <GCP_PROJECT_ID> --freshness=10m --limit=20 \
  --format='table(timestamp, httpRequest.status, httpRequest.latency, httpRequest.userAgent)'
```

Watch `httpRequest.latency`. This is the number that decides whether you are
about to have a timeout problem in front of an audience. See the next section.

> Logging note: the middle tier's formatter redacts secret-shaped values, and
> auth failures log a truncated `detail`. Do not expect to see raw tokens, and
> do not add logging that would. `fingerprint()` exists so you can correlate a
> token across log lines without recording it.

---

## Step 8 — Troubleshooting

### 401 from the bot endpoint

The endpoint returns 401 with an empty body by design, so the diagnosis is in
the logs, not the response. Find the reason:

```bash
gcloud logging read \
  'resource.type="cloud_run_revision"
   resource.labels.service_name="<CLOUD_RUN_SERVICE>"
   jsonPayload.message="inbound activity rejected"' \
  --project <GCP_PROJECT_ID> --freshness=30m --limit=20 \
  --format='table(timestamp, jsonPayload.reason, jsonPayload.detail, jsonPayload.channel_id, jsonPayload.remote)'
```

| Likely `reason` | Cause | Fix |
| --- | --- | --- |
| audience mismatch | `MICROSOFT_APP_ID` on the revision is not `<APP_A_CLIENT_ID>` | Requirement 4 in Step 3d. Fix the env var, redeploy, retest. This is the most common single cause. |
| issuer mismatch | You chose a regional/government data residency, or the token is from the Emulator | Requirement 3. Check Step 1a.6. |
| `jwks_unavailable` / `unknown_kid` | Container cannot reach `login.botframework.com`, or its key cache is stale | Check egress from the Cloud Run service. `/readyz` will also be failing. Confirm cache refresh is ≤ 24h. |
| expired / not yet valid | Clock skew | 5 minutes is the industry-standard allowance. If the container clock is wrong you have a bigger problem. |
| serviceUrl mismatch | Requirement 7: `serviceUrl` claim vs the activity's `serviceUrl` | Usually means something is replaying or rewriting activities. |
| no `aadObjectId` | Not an auth failure — ADR 003 refusing to key a session on nothing | Almost always means the activity did not come from a Teams user context. |

If there is **no log entry at all** and Azure still reports 401, the request is
not reaching your process. Check the messaging endpoint path, then Cloud Run
ingress and the invoker binding (Step 3a). A Cloud Run IAM rejection produces a
403 in the Cloud Run request log with no application log line.

### 502 / Gateway timeout, and the reply window

**The limit, cited.** Microsoft's long-operations guidance states: "When the
Azure AI Bot Service sends an activity to your bot from a channel, the bot is
expected to process the activity quickly. **If the bot doesn't complete the
operation within 10 to 15 seconds, depending on the channel, the Azure AI Bot
Service will time out and report back to the client a 504:GatewayTimeout**"
(<https://learn.microsoft.com/en-us/azure/bot-service/bot-builder-howto-long-operations-guidance>).
Note the range: 10 to 15 seconds, channel-dependent. Design to the lower bound,
not the upper one. Community reports of a flat 15s on Direct Line are consistent
with this but are not the normative source; the Learn page is.

**The collision.** Cloud Run's own request timeout defaults to 5 minutes
(<https://cloud.google.com/run/docs/configuring/request-timeout>), which is
vastly longer than the channel's patience. That mismatch is the trap: Cloud Run
will happily let your handler run for four minutes while Bot Service gave up at
twelve seconds and told Teams the bot failed. Your logs will show a successful
200 and the user will have seen an error. **Cloud Run's timeout will not save
you and its success logs will lie to you about the user experience.**

**Cold start is the usual culprit.** A scale-from-zero Cloud Run instance must
pull the image, start the interpreter, load config, fetch secrets from Secret
Manager, and warm the JWKS cache — and only then handle the first activity. Add
an OBO exchange, a Google STS exchange, a session create and a BigQuery job on
the same turn and the first message after an idle period is exactly the one that
blows the window.

Mitigations, in the order to apply them:

1. **Set minimum instances to 1 for the demo.** This is the single highest-value
   change and it is one command.
   ```bash
   gcloud run services update <CLOUD_RUN_SERVICE> \
     --project <GCP_PROJECT_ID> --region us-central1 --min-instances=1
   ```
   Turn it back to 0 afterwards if cost matters.
2. **Bring the Cloud Run timeout down** so failures are fast and visible rather
   than silent, e.g. `--timeout=30s`. A handler still running at 30s has already
   lost the turn.
3. **Warm it before the demo.** `curl <CLOUD_RUN_URL>/readyz` a few times, then
   send one throwaway Teams message, five minutes before you present.
4. **Acknowledge fast and update later.** The architectural fix is to return
   from the HTTP POST quickly and send the real answer as a subsequent
   proactive/streamed activity. That is what the streaming renderer and the
   informative-update pattern are for. Whether it is wired is a separate work
   item — see [NOTES.md](NOTES.md).

Measure it rather than guessing:

```bash
gcloud logging read \
  'resource.type="cloud_run_revision"
   resource.labels.service_name="<CLOUD_RUN_SERVICE>"
   httpRequest.requestUrl:"/api/messages"' \
  --project <GCP_PROJECT_ID> --freshness=1h --limit=50 \
  --format='table(timestamp, httpRequest.status, httpRequest.latency)'
```

Anything above about 8 seconds is a demo risk. Anything above 10 is a demo
failure waiting for its moment.

A genuine **502** from Cloud Run (as opposed to a 504 from Bot Service) usually
means the container is not listening on `$PORT`, crashed on startup, or died
mid-request. Check for `startup configuration failed`:

```bash
gcloud logging read \
  'resource.type="cloud_run_revision"
   resource.labels.service_name="<CLOUD_RUN_SERVICE>"
   severity>=ERROR' \
  --project <GCP_PROJECT_ID> --freshness=1h --limit=20 \
  --format='table(timestamp, severity, jsonPayload.message, jsonPayload.detail, textPayload)'
```

### The endpoint is not reachable

Work outward from the container:

```bash
# 1. Does the service exist and what URL does it actually have?
gcloud run services describe <CLOUD_RUN_SERVICE> --project <GCP_PROJECT_ID> \
  --region us-central1 --format='value(status.url, status.conditions)'

# 2. Is it publicly invocable?
gcloud run services get-iam-policy <CLOUD_RUN_SERVICE> \
  --project <GCP_PROJECT_ID> --region us-central1

# 3. Is ingress open?
gcloud run services describe <CLOUD_RUN_SERVICE> --project <GCP_PROJECT_ID> \
  --region us-central1 \
  --format='value(metadata.annotations["run.googleapis.com/ingress"])'

# 4. From outside Google, does TLS terminate cleanly?
curl -sSv <CLOUD_RUN_URL>/healthz 2>&1 | grep -Ei 'subject|issuer|SSL|HTTP/'

# 5. Does Azure have the URL you think it has?
az bot show --name "<BOT_HANDLE>" --resource-group "<AZURE_RESOURCE_GROUP>" \
  --query "properties.endpoint" -o tsv
```

Step 5 catches the embarrassing one: a redeploy under a different service name
changes the hostname, and Azure keeps pointing at the old one.

### The Teams channel does not appear, or does not work

- **No Teams icon in the channel gallery.** Scroll — the list is long and the
  gallery paginates. If it is genuinely absent, you are probably in the wrong
  directory (Step 1a.2) or looking at a resource type that is not **Azure Bot**.
- **Channel shows as configured but no messages arrive.** The Teams *channel*
  being enabled is necessary, not sufficient. The Teams *app* must also be
  installed for the user, and custom app upload must be permitted in the
  tenant: **Teams admin center → Teams apps → Setup policies** has an
  **Upload custom apps** toggle, and there is an org-wide custom-app setting
  as well
  (<https://learn.microsoft.com/en-us/microsoftteams/teams-custom-app-policies-and-settings>).
  Both must allow it for your demo users. Policy changes in Teams admin center
  can take time to propagate — do not make this change on demo morning.
- **Works for you, not for the analyst.** App setup policy is per user. Check
  the policy assigned to `analyst@<TENANT_DOMAIN>` specifically.
- **Works in 1:1, silent in a channel.** Expected if the manifest only declares
  the `personal` scope — and in any case SSO is unsupported in channel scope.
- **You deleted and re-added the Teams channel.** Per Microsoft, this
  regenerates keys and invalidates stored `29:xxx`/`a:xxx` conversation IDs.
  Anything doing proactive messaging with cached references is now broken.

### The bot works in the Bot Framework Emulator but not in Teams

Start by re-reading Step 1: **the Emulator does not support single-tenant or
user-assigned managed identity bots.** Per Microsoft's quickstart, those app
types are "not supported in other SDK languages, Bot Framework Composer, Bot
Framework Emulator, or Dev Tunnels."

So there are only two ways you can be in this situation, and they have opposite
fixes:

1. **You are running the Emulator against a local process with
   `MIDDLE_TIER_DEV_MODE` and `ALLOW_BOT_EMULATOR` set.** Then "works in the
   Emulator" tells you the router and templates are fine and tells you nothing
   about production auth, because the Emulator's tokens come from a different
   issuer (`https://login.microsoftonline.com/common/v2.0`-family metadata,
   distinct from the Connector's `https://api.botframework.com`). Do not draw
   conclusions about Teams from it. And confirm neither flag is set on the Cloud
   Run revision — Step 6's `middle tier initialised` log line reports
   `dev_mode` and `emulator_trusted` for exactly this reason.
2. **You created the bot resource as multi-tenant so the Emulator would work.**
   Then the Emulator working is a symptom of the misconfiguration, and the OBO
   path to Google will fail. Recreate the resource as single-tenant (Step 1b).

Other differences that produce "fine in Emulator, broken in Teams":

- The Emulator sends no `channelData` and no Teams-specific `from.aadObjectId`.
  Code that reads `aadObjectId` cannot be exercised there at all, which means
  the entire identity plane is untested by an Emulator pass.
- The Emulator never sends `signin/tokenExchange` invoke activities, so SSO is
  untestable there by construction.
- Teams prefixes channel messages with an `<at>Bot</at>` mention entity. The
  router strips mention entities before matching commands — which is why
  `@Bot /new` still resets — but any code you add that compares raw text will
  behave differently in the two clients.

---

## Sign-off checklist

Tick every line before you consider this runbook complete. Each is checkable.

- [ ] Azure Bot resource exists; `az bot show` reports `msaAppType: SingleTenant`
- [ ] `msaAppId` equals `<APP_A_CLIENT_ID>`; `msaAppTenantId` equals `<ENTRA_TENANT_ID>`
- [ ] Messaging endpoint is exactly `<CLOUD_RUN_URL>/api/messages`
- [ ] `curl <CLOUD_RUN_URL>/readyz` returns `{"status": "ready"}`
- [ ] Unauthenticated `POST /api/messages` returns **401**, and the rejection appears in the logs
- [ ] Cloud Run allows unauthenticated invocation and ingress is `all`
- [ ] `MICROSOFT_APP_ID` on the live revision matches `<APP_A_CLIENT_ID>`
- [ ] `dev_mode: false` and `emulator_trusted: false` in the `middle tier initialised` log line
- [ ] `REASONING_ENGINE_ID` is ours, and is **not** `<OTHER_ENGINE_ID_1>`
- [ ] Microsoft Teams channel enabled, commercial cloud, Calling off
- [ ] OAuth connection setting created and **Test Connection** passed for each demo user — or consciously skipped with `SIGNIN_URL` set instead
- [ ] A message sent from a real Teams 1:1 chat produced a reply
- [ ] `POST /api/messages` latency observed under 8 seconds on a warm instance
- [ ] `--min-instances=1` set for the demo window

Next: [12_demo.md](12_demo.md).
