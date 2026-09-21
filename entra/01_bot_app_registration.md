# 01 — Create App A, the Teams bot app registration

**Who runs this:** `m365-admin@<TENANT_DOMAIN>`, or any account with the
Application Administrator role.

**What you end up with:** a new single-tenant app registration that Azure Bot
Service points at, that Teams SSO mints tokens for, and that will perform the
On-Behalf-Of call in page 02.

**Placeholders you will fill in:** `<APP_A_CLIENT_ID>`, `<APP_A_CLIENT_SECRET>`.
(`<BOT_DOMAIN>` is needed in Steps 7 and 8 only, and can be backfilled once the
middle tier is deployed or tunneled). Record them on the checklist in [NOTES.md](NOTES.md).

---

## A note on portal naming before you start

Microsoft renames these surfaces often. Where a label has moved, this guide
gives the current path *and* the underlying stable property name, so you can
find the field by searching the app manifest even if the UI label has changed
again since writing.

- The service is **Microsoft Entra ID**. It was Azure Active Directory. Older
  docs, and some breadcrumbs inside the portal itself, still say Azure AD.
- Two portals reach the same objects: the **Microsoft Entra admin center**
  (<https://entra.microsoft.com>) and the **Azure portal**
  (<https://portal.azure.com>). This guide uses the Entra admin center. If your
  muscle memory is the Azure portal, the blades are identically named under
  *Microsoft Entra ID → App registrations*.
- The **Manifest** blade now shows two editors: *Microsoft Graph App Manifest
  (New)* and *AAD Graph App Manifest (Deprecating Soon)*. They spell some
  properties differently. This matters on page 02, and is called out there.

---

## Step 1 — Create the registration

1. Sign in to <https://entra.microsoft.com> as `m365-admin@<TENANT_DOMAIN>`.
2. Confirm you are in the right tenant. Top right, click your avatar and check
   the directory reads `<TENANT_DOMAIN>`, tenant ID
   `<ENTRA_TENANT_ID>`. In a sandbox it is easy to be signed
   in to two directories at once and configure the wrong one.
3. Left nav: **Entra ID** → **App registrations**.
4. Click **+ New registration**.
5. **Name**: `teams-bot-agent-runtime` (any name; it appears on consent prompts).
6. **Supported account types**: select
   **Accounts in this organizational directory only (<TENANT_NAME> only - Single tenant)**.
   Stable property: `signInAudience` = `AzureADMyOrg`.

   Single-tenant is required, not merely preferred. The Google provider trusts
   exactly one issuer, `https://login.microsoftonline.com/<ENTRA_TENANT_ID>/v2.0`.
   A multi-tenant app can mint tokens with a different `tid` and a different
   issuer, which Google will reject — and, worse, would be a trust hole if it
   did not.
7. **Redirect URI**: leave blank for now. Step 5 adds it.
8. Click **Register**.

## Step 2 — Record the client ID

On the **Overview** page you now see:

- **Application (client) ID** — this is `<APP_A_CLIENT_ID>`. Copy it. You will
  paste it into at least five places: the Application ID URI, the Azure Bot
  registration, `webApplicationInfo.id` and `bots[0].botId` in the Teams
  manifest, and the OBO request body.
- **Directory (tenant) ID** — should read `<ENTRA_TENANT_ID>`.
  If it does not, you are in the wrong tenant. Go back to step 2 of Step 1.
- **Object ID** — a different GUID. You do not need it. Do not confuse it with
  the client ID; this is a classic hour-loser.

## Step 3 — Create a client secret

1. Left nav within the app: **Manage** → **Certificates & secrets**.
2. **Client secrets** tab → **+ New client secret**.
3. **Description**: `obo-exchange`. **Expires**: 180 days is fine for a sandbox.
4. Click **Add**.
5. Copy the **Value** column immediately.

> **The secret is shown exactly once.** Navigate away and it is gone forever and
> you create a new one. Copy it now.

> **Where this goes.** Straight into **Google Secret Manager**, in project
> `<GCP_PROJECT_ID>`. It does not go into this repository, a `.env` file, a terraform
> variable file, a Slack message, or your notes app. The middle tier reads it at
> runtime from Secret Manager. Suggested secret name: `teams-bot-app-a-secret`.
>
> ```bash
> # run this from a shell that IS authenticated to GCP; paste at the prompt,
> # so the secret never lands in your shell history
> read -rs APP_A_SECRET && \
> printf '%s' "$APP_A_SECRET" | gcloud secrets create teams-bot-app-a-secret \
>   --project=<GCP_PROJECT_ID> --replication-policy=automatic --data-file=- && \
> unset APP_A_SECRET
> ```
>
> The `--data-file=-` form matters: passing a secret as a command-line argument
> puts it in your shell history and in the process table.

Note the **Secret ID** column too. That is not the secret; it is a harmless
identifier. Only the **Value** is sensitive.

## Step 4 — Set the Application ID URI

1. Left nav: **Manage** → **Expose an API**.
2. At the top, next to **Application ID URI**, click **Add** (in some tenants
   the link reads **Set**).
3. The field is prefilled with `api://<APP_A_CLIENT_ID>`. **Change it.**

**Use this value:**

```
api://botid-<APP_A_CLIENT_ID>
```

for example `api://botid-00000000-1111-2222-3333-444444444444`.

4. Click **Save**.

> **This deviates from the format given in the original design brief**, which
> said `api://<bot-domain>/<app-a-client-id>`. Microsoft's current Teams SSO
> documentation specifies two forms, and neither is that one:
>
> - **Standalone bot** (what we are building — a bot, no tab, no message
>   extension): `api://botid-{YourBotId}`
> - **App with multiple capabilities** (bot + tab + message extension):
>   `api://{fully-qualified-domain-name}/botid-{YourClientId}`
>
> Source: *Configure app in Microsoft Entra ID* (Teams SSO for bots),
> <https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/authentication/bot-sso-register-aad>
>
> The `botid-` prefix is the part that is easy to drop and is not optional.
> Teams matches this string against `webApplicationInfo.resource` in the app
> manifest; a mismatch fails SSO with an unhelpful error.
>
> If you later add a tab to this app, switch to the FQDN form
> `api://<BOT_DOMAIN>/botid-<APP_A_CLIENT_ID>` and update
> `webApplicationInfo.resource` in the manifest to match, in the same change.

## Step 5 — Expose the `access_as_user` scope

Still on **Expose an API**:

1. Click **+ Add a scope**.
2. Fill in:

   | Field | Value |
   | --- | --- |
   | **Scope name** | `access_as_user` |
   | **Who can consent?** | **Admins and users** |
   | **Admin consent display name** | `Teams can access the user's profile` |
   | **Admin consent description** | `Allows Teams to call the app's web APIs as the current user.` |
   | **User consent display name** | `Teams can access your profile and make requests on your behalf` |
   | **User consent description** | `Enable Teams to call this app's APIs with the same rights that you have.` |
   | **State** | **Enabled** |

3. Click **Add scope**.

The full scope string is now
`api://botid-<APP_A_CLIENT_ID>/access_as_user`. This is the scope *Teams*
requests. It is not the scope the OBO call requests — that one belongs to App B
and is configured on page 02. Two scopes with the same short name on two
different apps is confusing by design; keep the full URIs straight.

## Step 6 — Pre-authorize the Microsoft client applications

This is what allows Teams to obtain a token for your bot silently, with no
consent dialog. A bot in personal scope has no reliable surface on which to show
a consent prompt, so without this, SSO simply fails.

Still on **Expose an API**, scroll to **Authorized client applications** and,
for **each** GUID below:

1. Click **+ Add a client application**.
2. Paste the **Client ID**.
3. Under **Authorized scopes**, tick `api://botid-<APP_A_CLIENT_ID>/access_as_user`.
4. Click **Add application**.

**The client IDs.** These are Microsoft's own fixed, well-known first-party
client IDs — the same in every tenant. They are not generated for you and you
must not alter them.

| Client ID | Authorizes | Add it? |
| --- | --- | --- |
| `1fec8e78-bce4-4aaf-ab1b-5451cc387264` | Teams mobile or desktop application | **Required** |
| `5e3ce6c0-2b1f-4285-8d4b-75ee78787346` | Teams web application | **Required** |
| `4765445b-32c6-49b0-83e6-1d93765276ca` | Microsoft 365 web application | Recommended |
| `0ec893e0-5785-4de6-99da-4ed124e5296c` | Microsoft 365 desktop application | Recommended |
| `d3590ed6-52b3-4102-aeff-aad2292ab01c` | Microsoft 365 mobile application / Outlook desktop application | Recommended |
| `bc59ab01-8403-45c6-8796-ac3ef710b3e3` | Outlook web application | Optional |
| `27922004-5251-4030-b22d-91ecd9a37ea4` | Outlook mobile application | Optional |
| `c0ab8ce9-e9a0-42e7-b064-33d422df41f1` | Microsoft Edge | Optional |

**Source, verbatim from the table in Microsoft's Teams SSO documentation:**
<https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/authentication/bot-sso-register-aad>
(section *Configure authorized client application*; page retrieved 2026-09-07).
These GUIDs were read directly off that page, not recalled.

**Which ones do you actually need?** The two marked *Required* cover the Teams
desktop, mobile and web clients, which is the whole of the demo surface. The
Microsoft 365 rows matter because a Teams personal bot is also reachable from
the Microsoft 365 Copilot app (formerly the "Microsoft 365 (Office)" app), and
users increasingly arrive that way. Adding all eight costs nothing and removes a
class of "works on my machine, fails on the demo laptop" failures. The Outlook
and Edge rows are only relevant if you extend the app to those hosts later.

> Microsoft's own warning, which is worth repeating: pre-authorising a client
> means your users never get the chance to decline consent to it. That is
> acceptable here because every entry above is a Microsoft first-party client.
> Never pre-authorize a third-party client ID you have not verified.

## Step 7 — Configure the redirect URI for the auth popup fallback

Teams SSO fails in some legitimate situations — the user has not consented, the
tenant requires step-up MFA, or the mobile WebView blocks the silent iframe
token acquisition. In those cases the bot falls back to an interactive sign-in
popup, and that popup needs a registered redirect URI.

> **Deployment sequencing note:** If deploying to Cloud Run, you can complete
> Steps 1 through 6 first to obtain `<APP_A_CLIENT_ID>` and `<APP_A_CLIENT_SECRET>`,
> complete page 02, deploy the backend and Cloud Run middle tier, and then return here
> to register the Redirect URI (Step 7) and Azure Bot endpoint (Step 8). For local dev,
> start your tunnel (such as `ngrok http 8000`) and use that hostname immediately.

1. Left nav: **Manage** → **Authentication**.
2. Click **+ Add a platform** → **Web**.
3. **Redirect URIs**, add:

   ```
   https://<BOT_DOMAIN>/auth-end
   ```

   `<BOT_DOMAIN>` is the public HTTPS hostname of your bot middle tier. It must
   be HTTPS, must not be `localhost` (see below), and must match a domain listed
   in `validDomains` in the Teams app manifest.
4. If your bot uses the Bot Framework OAuth card for the fallback rather than a
   self-hosted page, **also** add:

   ```
   https://token.botframework.com/.auth/web/redirect
   ```

   Add this only if you are using `OAuthCard` / the Bot Framework token service.
   It is inert otherwise. *(Inference from the Bot Framework OAuth connection
   pattern, not verified against a current doc page — see NOTES.md.)*
5. Under **Implicit grant and hybrid flows**, leave **both** checkboxes
   (*Access tokens*, *ID tokens*) **unticked**. The implicit flow is not used
   here and Microsoft documents a specific hazard: an ID token obtained via
   implicit flow by a client with a wildcard reply URL **cannot be used in an
   OBO flow at all**. Leaving these off avoids the trap entirely.
6. Click **Configure**, then **Save**.

**On localhost:** for local development, add
`http://localhost:3978/auth-end` as a **second** redirect URI rather than
replacing the production one. Teams will not load content from a plain-HTTP
origin, so local testing of the *popup* path needs a tunnel (dev tunnels, ngrok)
with an HTTPS URL registered here as well.

## Step 8 — Point Azure Bot Service at App A

The Azure Bot resource is a separate object from the app registration. This
guide covers only the Entra side, but the join between them is here, so get it
right:

1. In the **Azure portal**, create or open your **Azure Bot** resource.
2. Under **Configuration**, the **Microsoft App ID** must be `<APP_A_CLIENT_ID>`.
3. **App type** must be **Single Tenant**, and **App Tenant ID** must be
   `<ENTRA_TENANT_ID>`.
4. **Messaging endpoint**: `https://<BOT_DOMAIN>/api/messages`.
5. Under **Channels**, add the **Microsoft Teams** channel.

> **Do not let the Azure Bot creation wizard create a new app registration for
> you.** If you pick *Create new Microsoft App ID*, you get a second app that
> is not the one you just configured, and Teams SSO will mint tokens for an app
> with no exposed scope and no pre-authorized clients. Choose *Use existing app
> registration* and paste `<APP_A_CLIENT_ID>`.

> **Do not use a User-Assigned Managed Identity bot for this design.** Managed
> identity bots do not get an app registration, and this design needs a
> confidential client with a secret in order to perform OBO.

## What you should have now

- [ ] `<APP_A_CLIENT_ID>` recorded
- [ ] `<APP_A_CLIENT_SECRET>` in Google Secret Manager, and nowhere else
- [ ] Application ID URI = `api://botid-<APP_A_CLIENT_ID>`
- [ ] Scope `access_as_user` exposed and Enabled
- [ ] At least the two required Teams client IDs pre-authorized
- [ ] Redirect URI `https://<BOT_DOMAIN>/auth-end` registered, implicit flow off
- [ ] Azure Bot resource pointing at `<APP_A_CLIENT_ID>`, single-tenant

App A cannot yet reach App B. That is page 02.
