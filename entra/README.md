# Microsoft Entra ID / Teams setup for the Teams → Agent Runtime bot

This directory is a runbook. It is written to be followed click-by-click by a
human with tenant-admin rights in the `<TENANT_DOMAIN>` sandbox. Nothing
in here was executed by the author: every value you must produce is a marked
placeholder, and every factual claim about Microsoft or Google behaviour is
sourced in [NOTES.md](NOTES.md).

Read this page first. It explains *why* there are two app registrations, which
is the one thing that, if misunderstood, makes the rest of the guide look
arbitrary.

## Pages

| Page | What it does |
| --- | --- |
| [01_bot_app_registration.md](01_bot_app_registration.md) | Create **App A**, the Teams bot app. New work. |
| [02_federation_app_obo.md](02_federation_app_obo.md) | Configure **App B**, the existing federation app, to accept On-Behalf-Of. |
| [03_teams_app_manifest.md](03_teams_app_manifest.md) | Package and sideload the Teams app. |
| [manifest/manifest.json](manifest/manifest.json) | The Teams app manifest itself. |
| [manifest/ICONS.md](manifest/ICONS.md) | Icon dimensions and constraints. |
| [04_verification.md](04_verification.md) | Prove each hop independently, before wiring Teams. **Start here when something breaks.** |
| [05_troubleshooting.md](05_troubleshooting.md) | The four errors you will actually hit. |
| [NOTES.md](NOTES.md) | Placeholder checklist, what is verified vs inferred, what could not be checked. |

## The environment you are working in

| Thing | Value |
| --- | --- |
| Entra tenant ID | `<ENTRA_TENANT_ID>` |
| Tenant domain | `<TENANT_DOMAIN>` (M365 E5 dev sandbox) |
| Tenant admin | `m365-admin@<TENANT_DOMAIN>` |
| Test user | `analyst@<TENANT_DOMAIN>`, `oid` `<ANALYST_OBJECT_ID>` |
| **App B** (federation app, **already exists**) | `<FEDERATION_APP_CLIENT_ID>` |
| Google workforce pool | `locations/global/workforcePools/teams-bot-demo` |
| Google provider | `entra` |
| GCP org / project | `organizations/<GCP_ORG_ID>` / `<GCP_PROJECT_ID>` |
| Provider issuer | `https://login.microsoftonline.com/<ENTRA_TENANT_ID>/v2.0` |

> **Do not recreate App B and do not change its client ID.** Google's workforce
> pool provider is configured to accept tokens whose `aud` equals
> `<FEDERATION_APP_CLIENT_ID>`. Changing that ID means reconfiguring
> the Google side, which is out of scope here. Everything page 02 asks you to do
> to App B is additive and leaves the client ID untouched.

## What is already proven, and what is not

Proven working before this runbook was written: a device-code sign-in as
`analyst@` produced an **ID token** with `aud` = App B's client ID; that token
was exchanged at `https://sts.googleapis.com/v1/token` for a Google access
token, which queried BigQuery as a workforce principal.

So the right-hand half of the chain — *a JWT with the right `aud` and `iss` gets
you into Google* — is not in question.

Not yet built, and the entire subject of this runbook:

1. App A, the Teams bot app registration.
2. The On-Behalf-Of hop that turns a Teams SSO token into a token App B owns.

## The critical design point (ADR 002)

**A Teams SSO token cannot be sent to Google's STS. Not "should not" — cannot.**

When Teams performs SSO for a bot, it mints a token whose audience is *the bot's
own app* (App A). Google's workforce pool provider validates the incoming
assertion's `aud` against the client ID registered on the provider, which is
**App B**. Two different apps, two different audience values, so Google rejects
the token. There is no setting on either side that makes App A's audience
acceptable to a provider configured for App B's.

The fix is an **On-Behalf-Of (OBO) exchange**. The bot middle tier takes the
user's App A token and asks Entra for a token *for App B*, still carrying the
user's identity. That re-audienced token is what goes to Google.

> **If you already know OBO, read this bit.** In every tutorial you have seen,
> OBO exists to let a middle tier call **Microsoft Graph** on the user's behalf.
> **That is not what it is doing here.** There is no Graph call anywhere in this
> design. OBO is being used purely as an *audience-rewriting* primitive, to move
> the user's identity across the Microsoft → Google trust boundary. If you
> configure Graph permissions expecting them to matter, you will have configured
> the wrong thing and the chain will still be broken. The only API permission
> App A needs is a scope on **App B**.

## The two apps

```
App A — Teams bot app                      App B — federation app
NEW: you create this                       EXISTS: <FEDERATION_APP_CLIENT_ID>
<APP_A_CLIENT_ID>                          DO NOT recreate

- has a client secret                      - Google's workforce pool provider
- Azure Bot Service points at it             trusts this client ID as `aud`
- Teams SSO mints tokens for it            - exposes scope `access_as_user`
- performs the OBO call                    - pre-authorizes App A
- granted App B's access_as_user           - MUST have requestedAccessTokenVersion = 2
```

Relationship, stated once, precisely:

- **App B exposes** an API: an Application ID URI plus a delegated scope
  (`access_as_user`).
- **App A is granted** that scope, under *API permissions → My APIs*.
- **App B pre-authorizes App A** (`preAuthorizedApplications`), so no user is
  ever shown a consent prompt for a bot that has no UI to show one in.
- An admin grants tenant-wide consent, as a belt-and-braces backstop.

Note the direction. `preAuthorizedApplications` goes on the **resource** (App B)
and names the **caller** (App A). The other manifest property people reach for,
`knownClientApplications`, is the wrong knob for this topology — page 02
explains why in one paragraph.

## The trust chain, end to end

```
1. analyst@<TENANT_DOMAIN> signs in to Teams
        │
2. Teams SSO issues a token
        │   aud = <APP_A_CLIENT_ID>        ← App A. Google will NOT accept this.
        │   oid = <ANALYST_OBJECT_ID>
        ▼
3. Bot middle tier receives it on the signin/tokenExchange invoke activity
        │   (middle tier validates the inbound activity signature FIRST —
        │    an unvalidated activity is an attacker asserting any oid it likes)
        ▼
4. Middle tier calls Entra token endpoint, OBO
        │   grant_type      = urn:ietf:params:oauth:grant-type:jwt-bearer
        │   requested_token_use = on_behalf_of
        │   client_id       = <APP_A_CLIENT_ID>
        │   client_secret   = <APP_A_CLIENT_SECRET>   (from Google Secret Manager)
        │   assertion       = the token from step 2
        │   scope           = api://<FEDERATION_APP_CLIENT_ID>/access_as_user
        ▼
5. Entra returns an ACCESS token — note: an access token, not an ID token
        │   aud = <FEDERATION_APP_CLIENT_ID>   ← ONLY if App B has
        │   iss = https://login.microsoftonline.com/<ENTRA_TENANT_ID>/v2.0
        │   oid = <ANALYST_OBJECT_ID>     requestedAccessTokenVersion = 2
        ▼
6. Middle tier exchanges it at Google STS
        │   POST https://sts.googleapis.com/v1/token
        │   audience         = //iam.googleapis.com/locations/global/workforcePools/
        │                      teams-bot-demo/providers/entra
        │   subject_token_type = urn:ietf:params:oauth:token-type:id_token
        │   subject_token     = the token from step 5
        ▼
7. Google returns an access token for the workforce principal
        │   principal://iam.googleapis.com/locations/global/workforcePools/
        │   teams-bot-demo/subject/<ANALYST_OBJECT_ID>
        │   (google.subject = assertion.oid)
        ▼
8. Middle tier invokes the Agent Runtime with that token.
   Cloud IAM and audit logs name the human.
```

## The one thing most likely to break this

Step 5. Read it again.

Microsoft's rule is that a **v2.0** access token's `aud` is the resource's bare
client ID, while a **v1.0** access token's `aud` is the resource URI that was
requested. A v1.0 token also has a different `iss`
(`https://sts.windows.net/{tid}/`, no `/v2.0` suffix).

Entra's default for `requestedAccessTokenVersion` is **null, which means 1**.

So an App B left at its default will hand you an OBO token with
`aud = api://<FEDERATION_APP_CLIENT_ID>` and `iss = https://sts.windows.net/<ENTRA_TENANT_ID>/`,
and Google will reject it twice over — wrong audience *and* wrong issuer —
while every other part of your setup looks perfectly correct.

Setting `requestedAccessTokenVersion: 2` on App B fixes both. Page 02 walks
through it; page 04 step 3 makes you actually decode the token and look, before
you go anywhere near Teams. Do not skip that step.

Changing this value is safe for the already-proven device-code path: per
Microsoft's app manifest reference, `requestedAccessTokenVersion` governs
**access token** format only, while ID token version is determined by which
endpoint the client calls. Your working ID-token flow is unaffected.

## Deliberately out of scope

**Group chats and channels.** The manifest restricts the bot to `personal`
scope only. This is a design constraint, not an oversight: in a shared
conversation the bot receives one conversation ID for many participants, and
this whole design turns on resolving a single `oid` to a single Google workforce
principal. There is no correct answer to "which user is this Agent Runtime
session for?" in a channel, so the bot declines to be installed there. See
ADR 003, which fixes the session user key to the Entra object ID.

**Microsoft Graph.** Not called. See the callout above.
