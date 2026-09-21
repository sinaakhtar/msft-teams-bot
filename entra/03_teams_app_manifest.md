# 03 — Package and sideload the Teams app

**Prerequisite:** pages 01 and 02 complete. You need `<APP_A_CLIENT_ID>` and a
public HTTPS `<BOT_DOMAIN>` serving `/api/messages`.

**Where `<BOT_DOMAIN>` comes from:**
- If deploying to **Cloud Run**, deploy the middle tier first (Stage 5 in `README.md`). The domain is your Cloud Run service URL with `https://` stripped (for example `middle-tier-xyz-ew.a.run.app`). Do not package this manifest before Cloud Run is deployed.
- If developing **locally**, start your tunnel first (such as `ngrok http 8000`) and use the tunnel hostname.

**Do page 04 first if you can.** Verification is cheaper than debugging through
the Teams client, where every failure surfaces as the same shrug of an error.

---

## A note on where the manifest docs now live

Microsoft moved this documentation. The Teams-specific *app manifest schema*
page now redirects to a unified **Microsoft 365 app manifest schema reference**
under `learn.microsoft.com/en-us/microsoft-365/extensibility/schema/`, with the
version selected by a `?view=m365-app-1.30` query parameter. Same schema, same
`developer.microsoft.com/json-schemas/teams/...` URLs, new documentation home.
If a bookmark of yours 301s somewhere unfamiliar, that is why.

The manifest is also now called the **Microsoft 365 app manifest**; it was the
*Teams app manifest*, and before that `manifest.json` was informally the "Teams
app package manifest". All the same file.

## Schema version

[`manifest/manifest.json`](manifest/manifest.json) declares:

```json
"$schema": "https://developer.microsoft.com/json-schemas/teams/v1.30/MicrosoftTeams.schema.json",
"manifestVersion": "1.30"
```

**1.30 is the current generally-available version**, dated August 2026, and is
listed first under *All generally available versions* on the schema reference
page. It is GA, not preview.
Source: <https://learn.microsoft.com/en-us/microsoft-365/extensibility/schema/>

The manifest in this repo was validated field-by-field against the published
v1.30 JSON Schema: required properties, property names, enum values, GUID
patterns and string length limits all pass.

`webApplicationInfo` — the property that enables SSO — has been supported since
manifest version 1.5, so nothing here depends on being on the newest schema. If
your tenant rejects 1.30 for any reason, dropping to 1.19 requires only changing
the two version strings; no other field in this manifest is version-sensitive.

## Fill in the placeholders

Three values, in [`manifest/manifest.json`](manifest/manifest.json):

| Placeholder | What to put | Appears in |
| --- | --- | --- |
| `<TEAMS_APP_GUID>` | A **fresh GUID** identifying the Teams app | `id` |
| `<APP_A_CLIENT_ID>` | App A's client ID from page 01 | `bots[0].botId`, `webApplicationInfo.id` |
| `<BOT_DOMAIN>` | Public HTTPS hostname, **no scheme, no path** | `validDomains`, `developer.*Url` |

Generate the app GUID:

```bash
uuidgen | tr 'A-Z' 'a-z'
```

Then:

```bash
cd entra/manifest
sed -i \
  -e "s/<TEAMS_APP_GUID>/$(uuidgen | tr 'A-Z' 'a-z')/g" \
  -e "s/<APP_A_CLIENT_ID>/PASTE_APP_A_CLIENT_ID_HERE/g" \
  -e "s/<BOT_DOMAIN>/bot.example.com/g" \
  manifest.json
```

> Edit a **copy** if you want to keep the placeholder version in the repo. A
> filled-in manifest contains no secrets, so committing it is safe — but it is
> environment-specific, so most teams keep the template and generate the real
> one at package time.

### `<TEAMS_APP_GUID>` is not App A's client ID

They are different identifiers for different things, and both are GUIDs, which
is why this trips people up. `id` identifies *the Teams app* to the Teams app
catalogue. `bots[0].botId` and `webApplicationInfo.id` identify *the Entra app
registration*. Reusing App A's client ID for `id` is technically permitted and
some samples do it, but it makes the two concepts indistinguishable in every log
line thereafter. Use a fresh GUID.

### `<BOT_DOMAIN>` formatting

`validDomains` entries are bare hostnames. `bot.example.com`, not
`https://bot.example.com` and not `bot.example.com/api`. The `developer` URLs,
by contrast, are full URLs with the scheme. The same placeholder is used in both
places and the `sed` above handles it correctly, but if you edit by hand, watch
for it.

Do not add `*.microsoftonline.com`, `*.botframework.com` or other Microsoft
domains to `validDomains`. Teams rejects packages listing domains outside your
control, and none of them are needed for a bot with no tab.

## What each block does, and why it is set this way

### `webApplicationInfo` — this is what turns SSO on

```json
"webApplicationInfo": {
  "id": "<APP_A_CLIENT_ID>",
  "resource": "api://botid-<APP_A_CLIENT_ID>"
}
```

Without this block there is no Teams SSO at all, and the bot falls back to an
interactive sign-in for every user. With it, Teams mints a token for App A and
hands it to the bot over the `signin/tokenExchange` invoke activity.

- `id` is App A's client ID as a bare GUID. The schema enforces GUID format
  here, so an Application ID URI in this field fails validation outright.
- `resource` **must exactly match the Application ID URI you set on App A** in
  page 01 step 4, including the `botid-` prefix. Teams compares these strings.
  A mismatch fails SSO with a message that does not mention either value.

If you changed App A to the multi-capability URI form, `resource` becomes
`api://<BOT_DOMAIN>/botid-<APP_A_CLIENT_ID>` and must be changed here in the
same edit.

### `bots` — personal scope only, and why

```json
"scopes": ["personal"],
"isNotificationOnly": false,
"supportsFiles": false
```

The schema permits `team`, `personal`, `groupChat` and `copilot`. This manifest
lists **only `personal`**, deliberately.

In a channel or group chat the bot is addressed through a conversation shared by
many people. This design resolves one Entra `oid` to one Google workforce
principal and keys the Agent Runtime session on it (ADR 003). A shared
conversation has no single `oid`, so there is no correct session key and no
correct identity to invoke the runtime as. The failure mode if you added
`groupChat` would not be an error — it would be the bot quietly answering as
whoever spoke last, or refusing every turn. Both are worse than not being
installable there.

Listing only `personal` means Teams does not offer the app for installation into
teams or group chats. The constraint is enforced by the platform rather than
left to the middle tier to police.

- `isNotificationOnly: false` — this is a two-way conversational bot, not a
  one-way notification sink.
- `supportsFiles: false` — no file upload or download. Nothing in this design
  handles file content, and enabling it would create an identity-scoped data
  path nobody has reviewed.

### `permissions: ["identity"]`

Declares that the app uses the signed-in user's identity. The schema allows only
`identity` and `messageTeamMembers`; the latter is a team-scope capability and
is deliberately absent.

### `commandLists`

Cosmetic. Populates the suggested-commands menu in the compose box. The single
`whoami` entry is a useful demo affordance — it lets you show, from the Teams
UI, which workforce principal the agent resolved you to. Remove it if your bot
does not implement it; an advertised command that does nothing looks broken.

## Build the package

Three files, flat, at the root of the zip:

```
manifest.json
color.png
outline.png
```

```bash
cd entra/manifest
zip -j ../teams-app-package.zip manifest.json color.png outline.png
```

`-j` ("junk paths") is the important flag. It stores the files without directory
entries. **A zip containing a folder is the single most common packaging
failure** — the Teams client reports a generic invalid-package error and says
nothing about the folder.

Verify the layout before uploading:

```bash
unzip -l ../teams-app-package.zip
```

Expect exactly three lines, no path separators:

```
manifest.json
color.png
outline.png
```

If you see `manifest/manifest.json`, rebuild with `-j`.

## Enable custom app upload in the tenant

Sideloading is off by default in many tenants. Do this **before** you try to
upload, as an admin, because the error when it is disabled points at the app
rather than at the policy.

1. Sign in to the **Teams admin center** (<https://admin.teams.microsoft.com>)
   as `m365-admin@<TENANT_DOMAIN>`.
2. **Teams apps** → **Setup policies** → **Global (Org-wide default)**.
3. Turn **Upload custom apps** to **On**.
4. **Save**.

There is a second, org-wide control: **Teams apps → Manage apps →
Org-wide app settings**, which has its own custom-app toggle. In an E5 dev
sandbox both are usually already permissive, but if upload is blocked, check
both — the setup policy governs *who may upload*, the org-wide setting governs
*whether custom apps are allowed at all*.

> **Policy changes take time to propagate.** Microsoft documents up to 24 hours,
> though in a small sandbox it is usually minutes. If you flip the toggle and
> the Upload option is still missing, wait and fully restart the Teams client
> before concluding something is wrong.

## Sideload the app

### Option A — upload directly in the Teams client

1. Open Teams as `analyst@<TENANT_DOMAIN>`, the test user. Not as the
   admin: you want to exercise the ordinary-user consent and SSO path, and an
   admin account can mask a missing consent grant.
2. Left rail: **Apps**.
3. **Manage your apps** → **Upload an app**.
4. **Upload a custom app** → select `teams-app-package.zip`.
5. **Add**.

The menu wording here has changed more than once — older docs say *Upload a
custom app*, some builds say *Upload an app*, and the entry point has moved
between the Apps page and the "..." overflow. If you cannot find it, it is
almost always the policy from the previous section rather than the UI.

### Option B — Teams Developer Portal

<https://dev.teams.microsoft.com> (formerly App Studio; the older
`preview.teams.microsoft.com` host redirects here).

1. **Apps** → **Import app**, and select your zip.
2. Fix anything its validator flags. It checks more than the JSON Schema does,
   including icon dimensions and cross-field consistency, which makes it worth a
   pass even if you intend to install via Option A.
3. **Preview in Teams** to install into your own client.

Option B is the better choice while iterating: it re-validates on every import
and shows the specific field at fault, where the Teams client shows a generic
failure.

### Option C — publish to the org catalogue

**Teams admin center → Teams apps → Manage apps → Upload new app** (in some
builds, **Actions → Upload new app**). This publishes to the org-wide catalogue
for all users. Overkill for a demo, and it makes iteration slower — every change
needs a version bump and a re-upload. Use A or B.

## After installing

Open a personal chat with the bot and send a message. The **first** message is
the interesting one: it triggers the SSO token exchange. What should happen is
nothing visible — no consent prompt, no sign-in card — because Teams is
pre-authorized on App A (page 01 step 6) and App A is pre-authorized on App B
(page 02 step 4a).

If you get a consent prompt, a sign-in card, or silence, **stop and go to
[04_verification.md](04_verification.md)** rather than guessing. The Teams client
does not tell you which hop failed. The verification page tests each hop
separately and will tell you within a couple of minutes.

## Iterating on the manifest

Teams caches app packages aggressively. When you change the manifest:

1. Bump `version` (`1.0.0` → `1.0.1`). Teams keys its cache on this; leaving it
   unchanged means your edit may simply not take effect.
2. Keep `id` **the same** — changing it registers a second, separate app rather
   than updating the first.
3. Remove the old install (**Manage your apps** → the app → **Remove**) before
   re-uploading, if behaviour looks stale.
4. Fully quit and reopen the Teams client. Not just the window — the client
   holds the manifest in memory.

## Checklist

- [ ] `<TEAMS_APP_GUID>` is a fresh GUID, not App A's client ID
- [ ] `bots[0].botId` and `webApplicationInfo.id` both = `<APP_A_CLIENT_ID>`
- [ ] `webApplicationInfo.resource` character-for-character equal to App A's Application ID URI
- [ ] `scopes` is `["personal"]` and nothing else
- [ ] `validDomains` contains the bare hostname, no scheme
- [ ] `color.png` is 192×192, `outline.png` is 32×32 white-on-transparent
- [ ] `unzip -l` shows three files and no folder
- [ ] Upload custom apps enabled in the Teams admin center
- [ ] Installed as `analyst@`, not as the admin
