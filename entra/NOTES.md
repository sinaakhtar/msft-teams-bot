# NOTES: placeholders, provenance, and what was not verified

This page exists so you can tell, for every claim in this runbook, whether it
came from current Microsoft/Google documentation that was read while writing, or
whether it is reasoning that has not been executed. A confidently wrong GUID
costs an hour, so the distinction is drawn explicitly rather than left to tone.

**Author's access, stated plainly:** the author had no Azure or Entra access, no
Teams tenant, and no authenticated `gcloud`/`az` session. **No portal step in
this guide was performed. No command in this guide was executed.** Nothing here
reports a result. Documentation pages were retrieved over HTTPS on **2026-09-07**
and quoted; the Teams manifest was validated offline against the published JSON
Schema. That is the entirety of what was actually done.

---

## 1. Value reference: deployment variables vs true constants

### Deployment variables (values specific to your tenant and GCP project)

| Variable / Placeholder | What it is | Where you get it | Used in |
| --- | --- | --- | --- |
| `<APP_A_CLIENT_ID>` | Teams bot app's Application (client) ID | Created in 01 step 1; read off Overview blade | 01, 02, 03, 04, manifest |
| `<APP_A_CLIENT_SECRET>` | App A client secret **Value** | Created in 01 step 3, shown once | 02, 04 (Google Secret Manager only) |
| `<FEDERATION_APP_CLIENT_ID>` | **App B** Application (client) ID | Created in 02 step 0 (or read from existing federation app) | 02, 04, workforce pool provider, .env |
| `<ENTRA_TENANT_ID>` | Directory (tenant) ID of your Entra tenant | Read off Overview blade in Entra admin center | 01, 02, 04, workforce pool provider, .env |
| `<TENANT_DOMAIN>` | Domain name of your Entra tenant | Entra portal or M365 admin center | 01, 04 |
| `<BOT_DOMAIN>` | Public HTTPS hostname of middle tier, bare (no scheme) | Cloud Run URL minus scheme (Stage 5), or local tunnel hostname (ngrok) | 01 step 7, manifest `validDomains` and `developer.*Url` |
| `<WORKFORCE_POOL_ID>` | Google Cloud workforce pool name | Created in Terraform or pre-existing (e.g. `teams-bot-demo`) | Terraform, 04, 05, .env |
| `<WORKFORCE_PROVIDER_ID>` | Google Cloud workforce pool provider name | Created in Terraform or pre-existing (e.g. `entra`) | Terraform, 04, 05, .env |
| `<GCP_PROJECT_ID>` | Google Cloud project hosting the middle tier and agent | Your GCP project (e.g. `donuts-dev`) | Terraform, 04, .env |
| `<GCP_ORG_ID>` | Google Cloud organization ID | `gcloud organizations list` | Terraform org policies / IAM |
| `<ANALYST_OBJECT_ID>` | Entra User Object ID for test user | Entra ID -> Users -> User -> Object ID | 04 testing, BigQuery RLS |
| `<TEAMS_APP_GUID>` | Identifier for the Teams app package | `uuidgen`; **not** App A's client ID | `manifest.json` `id` |
| `<TEAMS_SSO_TOKEN>` | User assertion fed into OBO | 04 step 1a (device-code stand-in) or 1b (real, from invoke activity) | 02, 04 |
| `<REGION>` | Agent Runtime and Cloud Run region | Your deployment (e.g. `europe-west4`) | 04 step 6, Terraform, .env |
| `<REASONING_ENGINE_ID>` | Reasoning Engine numeric ID | Vertex AI Agent deployment | 04 step 6, .env |
| `color.png` | 192×192 colour icon | You produce it (see `manifest/ICONS.md`) | app package |
| `outline.png` | 32×32 white-on-transparent icon | You produce it (see `manifest/ICONS.md`) | app package |

### True constants (fixed values that must NOT be changed across any deployment)

The eight Microsoft first-party client IDs for Teams SSO pre-authorization (read verbatim from Microsoft documentation):

| Client ID | Application | Status |
| --- | --- | --- |
| `1fec8e78-bce4-4aaf-ab1b-5451cc387264` | Teams Mobile & Desktop client | Required |
| `5e3ce6c0-2b1f-4285-8d4b-75ee78787346` | Teams Web client | Required |
| `4765445b-32c6-49b0-83e6-1d93765276ca` | Microsoft 365 web client | Recommended |
| `0ec893e0-5785-4de6-99da-4ed124e5296c` | Microsoft 365 desktop client | Recommended |
| `d3590ed6-52b3-4102-aeff-aad2292ab01c` | Microsoft 365 mobile / Outlook desktop | Recommended |
| `bc59ab01-8403-45c6-8796-ac3ef710b3e3` | Outlook web client | Optional |
| `27922004-5251-4030-b22d-91ecd9a37ea4` | Outlook mobile client | Optional |
| `c0ab8ce9-e9a0-42e7-b064-33d422df41f1` | Microsoft Edge | Optional |

Additional protocol constants:
- OAuth grant type for OBO: `urn:ietf:params:oauth:grant-type:jwt-bearer`
- Requested token use for OBO: `on_behalf_of`
- STS token exchange grant type: `urn:ietf:params:oauth:grant-type:token-exchange`
- STS subject token type: `urn:ietf:params:oauth:token-type:id_token`
- STS requested token type: `urn:ietf:params:oauth:token-type:access_token`
- Workforce identity subject attribute mapping: `google.subject = assertion.oid`

### Secret handling

`<APP_A_CLIENT_SECRET>` appears as a placeholder in two pages and must never be
substituted into a file that is saved. It belongs in **Google Secret Manager**
in `<GCP_PROJECT_ID>`. In shells, read it at a prompt or pull it with
`gcloud secrets versions access`; do not pass it as a command-line argument,
where it lands in shell history and the process table.

---

## 2. Verified from current documentation

Each of these was read on the retrieval date from the URL given, and is quoted or
transcribed rather than recalled.

### V1: The eight Microsoft first-party client IDs for Teams SSO pre-authorization
Read verbatim from the table in *Configure app in Microsoft Entra ID*, section
*Configure authorized client application*.
<https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/authentication/bot-sso-register-aad>
Retrieved 2026-09-07.

`1fec8e78-bce4-4aaf-ab1b-5451cc387264` (Teams mobile/desktop),
`5e3ce6c0-2b1f-4285-8d4b-75ee78787346` (Teams web),
`4765445b-32c6-49b0-83e6-1d93765276ca` (M365 web),
`0ec893e0-5785-4de6-99da-4ed124e5296c` (M365 desktop),
`d3590ed6-52b3-4102-aeff-aad2292ab01c` (M365 mobile / Outlook desktop),
`bc59ab01-8403-45c6-8796-ac3ef710b3e3` (Outlook web),
`27922004-5251-4030-b22d-91ecd9a37ea4` (Outlook mobile),
`c0ab8ce9-e9a0-42e7-b064-33d422df41f1` (Microsoft Edge).

These were transcribed off the page, not written from memory. Re-check the page
before blaming a GUID: Microsoft has added rows to this table over time.

### V2: Application ID URI format for a standalone bot
Same page. *"Standalone bot: If you're building a standalone bot, enter the
application ID URI as `api://botid-{YourBotId}`."* Multi-capability apps use
`api://{fully-qualified-domain-name}/botid-{YourClientId}`.

**This contradicts the format given in the original design brief**
(`api://<bot-domain>/<app-a-client-id>`), which omits the `botid-` prefix. The
guide follows the documentation. Flagged in 01 step 4.

### V3: The OBO response contains an access token and no ID token
*Microsoft identity platform and OAuth 2.0 On-Behalf-Of flow*, section
*Middle-tier access token response*. The documented response parameters are
`token_type`, `scope`, `expires_in`, `access_token`, `refresh_token`. There is no
`id_token` field in the schema.
<https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow>
Retrieved 2026-09-07.

### V4: v2.0 access tokens carry the resource's client ID as `aud`
*Access token claims reference*: *"Identifies the intended audience of the token.
In v2.0 tokens, this value is always the client ID of the API. In v1.0 tokens, it
can be the client ID or the resource URI used in the request."*
<https://learn.microsoft.com/en-us/entra/identity-platform/access-token-claims-reference>
Retrieved 2026-09-07.

### V5: `requestedAccessTokenVersion` defaults to 1, and governs access tokens only
*Microsoft Entra application manifest reference*: *"Possible values … are 1, 2, or
null. If the value is null, this parameter defaults to 1."* And: *"The endpoint
used, v1.0 or v2.0, is chosen by the client and only impacts the version of
id_tokens. Resources need to explicitly configure requestedAccessTokenVersion to
indicate the supported access token format."*
<https://learn.microsoft.com/en-us/entra/identity-platform/reference-app-manifest>
Retrieved 2026-09-07.

The second sentence is the basis for the claim in 02 step 4b that changing this
value does **not** disturb the already-proven device-code ID-token path.

### V6: Google STS requires `aud` to match the provider's client ID
*Security Token Service API: `token`* reference, `subjectToken` parameter: for
OIDC JWTs the token must follow RFC 7523, `subjectTokenType` must be
`urn:ietf:params:oauth:token-type:jwt` or `...:idToken`, headers `kid` and `alg`
are required, `alg` must be `RS256` or `ES256`, and for workforce pools the `aud`
must match the client ID specified in the provider configuration.
<https://cloud.google.com/iam/docs/reference/sts/rest/v1/TopLevel/token>
Retrieved 2026-09-07.

The same page states that the `Authorization` header must **not** be sent and can
cause the request to fail.

### V7: Workforce pool audience resource-name format
`//iam.googleapis.com/locations/global/workforcePools/<pool>/providers/<provider>`:
note the absence of a project segment, unlike workload identity pools.
<https://cloud.google.com/iam/docs/workforce-obtaining-short-lived-credentials>
and the STS reference above. Retrieved 2026-09-07.

### V8: `preAuthorizedApplications` vs `knownClientApplications`
Both from the OBO page (V3). *preAuthorizedApplications*: *"Resources can indicate
that a given application always has permission to receive certain scopes… Any
such application can request these permissions in an OBO flow and receive them
without the user providing consent."* *knownClientApplications*: *"The middle tier
application adds the client to the known client applications list… this is done
using the `.default` scope."* The same page warns that combining `.default` with
other named delegated scopes yields `AADSTS70011`.

### V9: Manifest schema v1.30 is the current GA version
The *Microsoft 365 app manifest schema reference* lists 1.30 (August 2026) at the
top, under a heading reading *All generally available versions*. Not preview.
<https://learn.microsoft.com/en-us/microsoft-365/extensibility/schema/>
Retrieved 2026-09-07.

Note the documentation **move**: the old Teams path
`/microsoftteams/platform/resources/schema/manifest-schema` now 301-redirects to
`/microsoft-365/extensibility/schema/?view=m365-app-1.30`. Flagged in 03.

### V10: `manifest.json` validates against the v1.30 schema
The schema was downloaded from
`https://developer.microsoft.com/json-schemas/teams/v1.30/MicrosoftTeams.schema.json`
and `manifest/manifest.json` was checked against it offline with placeholders
substituted for syntactically valid dummies. Checked and passing:

- all 8 required top-level properties present
- no unknown top-level, `bots[0]`, `webApplicationInfo` or `developer` properties
- `manifestVersion` matches the schema's `const` of `1.30`
- `id`, `webApplicationInfo.id`, `bots[0].botId` match the schema's GUID pattern
- `name.short` 17/30, `name.full` 44/100, `description.short` 40/80,
  `description.full` 267/4000: all within limits
- `bots[0].scopes` value `personal` is in the enum `[team, personal, groupChat, copilot]`
- `permissions` value `identity` is in the enum `[identity, messageTeamMembers]`
- `developer` has all four required sub-properties

This is real structural validation, not an eyeball. It says nothing about whether
Teams will *accept* the package at runtime: see U3.

### V11: `webApplicationInfo` semantics and minimum schema version
*Update app manifest for SSO and preview your app*: *"webApplicationInfo has two
elements, id and resource"*: `id` is the Entra app GUID, `resource` is the
Application ID URI, and *"Use the app manifest version 1.5 or later to
implement the webApplicationInfo property."*
<https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/authentication/bot-sso-manifest>
Retrieved 2026-09-07.

### V12: Custom app upload is governed by Teams admin center setup policies
*Manage custom app policies and settings*: **Teams apps → Setup policies**, with
a separate org-wide custom app setting.
<https://learn.microsoft.com/en-us/microsoftteams/teams-custom-app-policies-and-settings>
Retrieved 2026-09-07.

### V15: The tenant's live OIDC endpoints match what this guide uses
The tenant's discovery document was fetched unauthenticated on 2026-09-07 from
`https://login.microsoftonline.com/<ENTRA_TENANT_ID>/v2.0/.well-known/openid-configuration`
and returned:

| Field | Value |
| --- | --- |
| `issuer` | `https://login.microsoftonline.com/<ENTRA_TENANT_ID>/v2.0` |
| `jwks_uri` | `https://login.microsoftonline.com/<ENTRA_TENANT_ID>/discovery/v2.0/keys` |
| `token_endpoint` | `https://login.microsoftonline.com/<ENTRA_TENANT_ID>/oauth2/v2.0/token` |
| `device_authorization_endpoint` | `https://login.microsoftonline.com/<ENTRA_TENANT_ID>/oauth2/v2.0/devicecode` |

This confirms three things without needing any credential: the tenant exists and
is reachable; the `issuer` **exactly matches** the value the Google workforce
pool provider is configured with, so the `iss` expectations in 02, 04 and 05 are
correct; and the token, devicecode and JWKS URLs used in the commands throughout
this guide are the real ones for this tenant.

It confirms nothing about App A, App B, consent, or any token contents.

### V13: Implicit-flow ID tokens with wildcard reply URLs cannot be used for OBO
From the OBO page (V3). This is the basis for the advice in 01 step 7 to leave
both implicit grant checkboxes unticked.

### V14: Conditional Access `interaction_required` handling on OBO
The OBO page (V3) documents the 401 + `WWW-Authenticate` + claims-challenge
pattern, and the `AADSTS50079` example. Reproduced in 05.

---

## 3. Inference: reasoned, not verified

Clearly distinguished because these are where the guide could be wrong.

**I1: `requestedAccessTokenVersion: 2` will make the OBO token acceptable to
Google.** This is the guide's central recommendation and it is a **deduction**
from V4 + V5 + V6, not an observed result. The chain is: Google needs `aud` =
bare client ID (V6); v2.0 access tokens have `aud` = bare client ID (V4); the
setting that produces v2.0 access tokens defaults to off (V5). The logic is
sound and each premise is sourced, but **nobody has run this exchange**. Page 04
step 3 exists precisely so you confirm it empirically before trusting it.

**I2: Google's STS will accept an Entra *access* token where an ID token was
used before.** Reasoned from V6: the STS validates a JWT by signature, `iss`,
`aud` and expiry, and accepts `subjectTokenType` of `jwt` or `idToken`; an Entra
v2.0 access token for a custom API is an ordinary RS256 JWT signed with the same
tenant keys. The provider's `responseType = ID_TOKEN` and
`assertionClaimsBehavior = ONLY_ID_TOKEN_CLAIMS` are understood to govern the
browser sign-in path rather than direct STS exchange (**this specific reading of
those two provider fields was not confirmed against Google documentation and is
the weakest inference in the guide.** If step 4 of verification fails with the
`aud`/`iss`/`ver` checks all passing, this inference is the thing to doubt, and
the first thing to try is `subject_token_type=urn:ietf:params:oauth:token-type:jwt`.

**I3: `preAuthorizedApplications` on App B is the right mechanism, and
`knownClientApplications` is not needed.** Follows from the role definitions in
V8 mapped onto this topology. Confident, but the mapping is the author's.

**I4: `d3590ed6-52b3-4102-aeff-aad2292ab01c` covers both M365 mobile and Outlook
desktop.** The source table's row rendering merged two labels into one cell. The
GUID is transcribed correctly; the label pairing may be a rendering artefact.
Harmless either way: pre-authorizing it is correct in both readings.

**I5: `https://token.botframework.com/.auth/web/redirect` as a redirect URI.**
Standard Bot Framework OAuth-card practice, included conditionally in 01 step 7.
**Not confirmed against a current doc page in this pass.** Inert if you are not
using `OAuthCard`; omit it if you are not.

**I6: The device-code stand-in in 04 step 1a will work.** Reasoned: with
`allowPublicClient = true`, a device-code sign-in against App A requesting App A's
own scope should yield a delegated token with `aud` = App A and an `scp` claim,
which is the shape OBO requires. The client-equals-resource case was **not
verified**. If it misbehaves, get the assertion from the real Teams invoke
activity (1b) instead; the rest of page 04 is unaffected.

**I7: Single-tenant is required rather than merely advisable.** Reasoned from
the provider trusting exactly one issuer. A multi-tenant App A would still mint
home-tenant tokens with the right issuer for home-tenant users, so "required" is
a security judgement about closing off the other case, not a hard technical
constraint for the happy path.

**I8: Teams client caching behaviour and the version-bump advice in 03.**
Widely-reported practitioner behaviour, not read off a doc page in this pass.
Harmless if unnecessary.

**I9: The `accentColor` value `#2C5F9E`, the app name, and all description
strings.** Invented placeholders. Schema-valid, but change them to whatever you
want.

**I10: The Agent Runtime request body in 04 step 6.** Illustrative. The real
shape comes from ADR 005 and your deployment. Do not read a failure there as an
identity problem without checking the contract first.

---

## 4. Could NOT be verified: no Azure access

Everything in this section is a gap, not a claim.

**U1: No portal step was executed.** Every click path, blade name, button label
and field position is from documentation and may have moved. Where a rename is
known, the guide gives the stable underlying property name too
(`signInAudience`, `allowPublicClient`, `requestedAccessTokenVersion` /
`accessTokenAcceptedVersion`, `preAuthorizedApplications`) so you can find the
field by searching the Manifest blade if a label has changed again.

**U2: No command in this guide was run.** The `curl`, `gcloud`, `zip`,
`uuidgen` and `magick` commands were written to be correct and are not test
output. Shell quoting around JSON in the STS call in 04 step 4 is the most
likely place for a transcription slip; if it misbehaves, put the body in a file
and use `-d @body.json`.

**U3: The manifest was never uploaded to Teams.** V10 is structural validation
against the JSON Schema only. The Teams Developer Portal validates more than the
schema does (icon dimensions, cross-field consistency, domain rules) so import
there first (03, Option B).

**U4: App B's current state is unknown.** Whether it already has an Application
ID URI, existing scopes, existing pre-authorized apps, or a non-null
`requestedAccessTokenVersion` could not be inspected. Page 02 is written to be
additive and tells you to record and preserve anything already set. **Read
before you write.**

**U5: Conditional Access policies in the tenant are unknown.** An E5 sandbox may
have security defaults enabled, which can force MFA and surface as
`interaction_required` on OBO. Covered in 05, unverified for this tenant.

**U6: The exact workforce pool provider configuration was not read.** The
issuer, `google.subject = assertion.oid` mapping, `responseType` and
`assertionClaimsBehavior` are taken from the brief, not from
`gcloud iam workforce-pools providers describe`. Worth dumping it yourself
before starting:
```bash
gcloud iam workforce-pools providers describe entra \
  --workforce-pool=teams-bot-demo --location=global
```
In particular, confirm whether any attribute condition is set; the brief did not
mention one, and a condition rejecting the token would look like an audience
failure.

**U7: No IAM bindings were checked.** Whether the workforce principal for
`analyst@` has roles on `<GCP_PROJECT_ID>` or the Reasoning Engine is unknown. A `403`
in verification step 5 or 6 with a valid token is this, not an auth failure.

**U8: Azure Bot Service configuration was not verified.** 01 step 8 gives the
join between the bot resource and App A but the Azure portal Bot blades were not
walked.

**U9: Nothing about the middle tier's own code.** Inbound Bot Framework activity
signature validation (which CONTEXT.md correctly identifies as the control that
everything else rests on, since an unvalidated activity is an attacker asserting
an arbitrary `oid`) is out of scope here and is not addressed by any Entra
configuration in this guide. It is a code requirement in the middle tier.

---

## 5. Where this guide departs from the brief

Two places, both deliberate, both sourced.

1. **Application ID URI for App A.** Brief: `api://<bot-domain>/<app-a-client-id>`.
   Guide: `api://botid-<APP_A_CLIENT_ID>`, per V2. The `botid-` prefix is
   required and the brief's form omits it. The FQDN variant is documented for
   multi-capability apps and is given as the alternative if a tab is added later.

2. **The audience risk is narrower and more actionable than the brief framed it.**
   The brief asked whether there is a real risk the OBO output is an access token
   whose `aud` is the Application ID URI rather than the bare client ID. There is,
   and it is not a coin toss: it is **determined by a single setting with an
   unhelpful default** (V4 + V5). Left alone it also breaks `iss`. So it is
   reframed throughout as one specific, checkable, fixable condition (`requestedAccessTokenVersion`) rather than as an open uncertainty.

---

## 6. Suggested order of work

1. Read [README.md](README.md). Understand why OBO is here and why it is not Graph.
2. Dump the current state of App B and the Google provider before changing
   anything (U4, U6).
3. Page 01: create App A.
4. Page 02: configure App B. **Do not skip step 4b.**
5. Page 04 steps 1a-5: prove the chain with the device-code stand-in. **Stop and
   fix anything that fails here.** This is much cheaper than debugging in Teams.
6. Set `Allow public client flows` back to `No` on App A.
7. Page 03: package and sideload.
8. Page 04 step 1b: repeat with a real Teams SSO token.
9. Page 04 step 6: invoke the Agent Runtime, then read the audit log and confirm
   it names the human.

If something breaks, [05_troubleshooting.md](05_troubleshooting.md), and check
`requestedAccessTokenVersion` first.
