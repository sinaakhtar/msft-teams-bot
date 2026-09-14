# Identity Broker

The component that crosses the trust boundary from Microsoft to Google.

Everything downstream of this package runs as the human who typed the message.
Nothing in this package can ever run as anything else.

```
Teams SSO token                 aud = BOT app
      |
      |  OBO exchange (Entra)            <- RE-AUDIENCING, not Graph
      v
token for the FEDERATION app    aud = <FEDERATION_APP_CLIENT_ID>
      |
      |  STS token exchange (Google)
      v
Workforce Principal access token
      |
      |  plain Bearer header
      v
BigQuery / any Google Cloud API, as the human
```

The principal that comes out the far end, confirmed live from inside BigQuery:

```
principal://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/subject/{entra_oid}
```

## Interface

```python
class IdentityBroker(Protocol):
    async def google_access_token(self, *, user_key: str, teams_sso_token: str) -> str: ...
```

`user_key` is `entra:{tid}:{oid}` (ADR 003), built from `from.aadObjectId`.
Any failure raises a typed `IdentityAcquisitionError` naming the stage that
failed. There is no other return.

```python
broker = build_identity_broker(
    session=app["http"],
    tenant_id="<ENTRA_TENANT_ID>",
    bot_client_id=settings.bot_app_id,
    bot_client_secret=settings.bot_app_password,
    federation_app_id="<FEDERATION_APP_CLIENT_ID>",
    workforce_pool_id="teams-bot-demo",
    workforce_provider_id="entra",
    user_project="<GCP_PROJECT_ID>",
)
token = await broker.google_access_token(user_key=user_key, teams_sso_token=sso)
```

| File | What it does |
| --- | --- |
| `obo.py` | Entra On-Behalf-Of exchange + the audience/issuer pre-flight |
| `sts.py` | Google STS token exchange, verified parameter set |
| `cache.py` | Per-user token cache: proactive, single-flight, bounded, in memory |
| `broker.py` | Composes the three behind the Protocol |
| `errors.py` | Typed failures, each naming its stage |

## Why OBO is here

Not for Microsoft Graph. This is the one thing readers get wrong, so it is
also in a comment at the top of `obo.py`.

Google's workforce pool provider validates the incoming assertion's `aud`
against the client ID registered on the provider, which is the **federation
app**. Google's own API reference is explicit:

> `clientId` — Required. The client ID. Must match the audience claim of the
> JWT issued by the identity provider.
> — <https://cloud.google.com/iam/docs/reference/rest/v1/locations.workforcePools.providers>

A Teams SSO token carries the **bot app's** audience, because that is what
Teams issues it for. It therefore cannot go to Google at all. OBO is the
mechanism Entra provides for converting a token audienced at app A into a
token for the same user audienced at app B. That conversion — re-audiencing —
is the entire reason the hop exists.

Note what is *not* available as an escape hatch: a **workforce** pool OIDC
provider has fields `issuerUri`, `clientId`, `clientSecret`, `webSsoConfig`,
`jwksJson` and **no `allowedAudiences`**. Workload identity pool providers do
have `oidc.allowedAudiences`, and people reach for it from memory. On the
workforce side there is exactly one acceptable audience and no way to widen it
from the Google end.

## The audience risk — measured, and it is the issuer

This was written up as an audience risk. Executed against the live endpoints
on 2026-09-07, it is an **issuer** risk. The audience was already correct.

Two tokens for the same user, from the same tenant, minted seconds apart:

| | ID token | Access token |
| --- | --- | --- |
| `aud` | `<FEDERATION_APP_CLIENT_ID>` (bare GUID) | `<FEDERATION_APP_CLIENT_ID>` (bare GUID) |
| `iss` | `https://login.microsoftonline.com/{tid}/v2.0` | `https://sts.windows.net/{tid}/` |
| `ver` | `2.0` | `1.0` |
| Google STS | **accepted** | **refused, HTTP 400** |

The refusal, verbatim from Google:

```
The issuer in ID Token https://sts.windows.net/<ENTRA_TENANT_ID> does not match
the expected one in config: https://login.microsoftonline.com/<ENTRA_TENANT_ID>/v2.0
```

An OBO exchange returns an *access* token, so it inherits the right-hand
column and the chain breaks. The cause is that the federation app registration
is on `api.requestedAccessTokenVersion` 1/null, which makes Entra stamp v1
access tokens with the legacy `sts.windows.net` issuer.

**The fix is one field on the federation app registration:**

```
api.requestedAccessTokenVersion = 2
```

That flips `iss` to the v2.0 endpoint and leaves `aud` as the bare GUID. It has
**not** been applied or verified — see `NOTES.md` for the exact command and the
decisive test.

Two dead ends, so nobody re-walks them:

* **`subject_token_type` is not the lever.** The same access token was rejected
  identically as `…:id_token` and as `…:jwt`, with the same issuer error.
* **Asking OBO for an `id_token` does not help.** Adding `openid` to the scope
  does make Entra return one, but by OIDC Core §2 an ID token's `aud` is the
  `client_id` of the client it was issued *to* — and in an OBO exchange that
  client is the **bot** app, which is the audience we invoked OBO to escape.
  Measured: the probe requested tokens with `client_id` = the federation app
  and got an ID token audienced at the federation app. The audience tracked the
  client, not the resource.

`obo.py` performs this check locally before calling Google, so the failure
reads *"iss (got 'https://sts.windows.net/…', provider issuerUri is
'https://login.microsoftonline.com/…/v2.0'). Fix: set
api.requestedAccessTokenVersion=2 …"* instead of a 400 from another cloud that
names neither app registration nor field.

## Cache design

Keyed on `entra:{tid}:{oid}`. **Not** the conversation id, **not** the Teams
MRI. A conversation-keyed cache is a cross-user leak waiting to happen — a
group chat is one conversation containing many people — and the MRI is not the
identifier Google ends up authorizing.

* **Proactive refresh at 80 % of lifetime.** ~48 minutes into a ~1 hour
  Workforce credential. Refresh-on-401 would make every user's first request
  after expiry a failed downstream call.
* **Single-flight per user.** A per-key `asyncio.Lock` plus a re-check inside
  the lock. 25 concurrent cold-cache turns for one user produce exactly one
  OBO+STS chain; the other 24 reuse it. The re-check is what collapses the
  stampede rather than merely serializing it. Locks are refcounted and reaped,
  so the lock table does not grow with the user base.
* **Bounded, LRU.** Default 5000 entries. Eviction costs the evicted user one
  STS round trip and nothing else.
* **Hard floor.** A token with under 60 seconds left is treated as unusable
  regardless of the refresh ratio, so nothing is handed out that could die
  mid-request.
* **Refresh failure while the current token is still valid** serves the current
  token and logs a warning. That is the same human's own unexpired credential,
  not a fallback identity. Once it is genuinely expired, the error propagates
  and the turn is refused.
* **In memory only.** Never written to disk, never logged, and specifically
  **never put in Agent Runtime Session state** — that state is *persisted*, so
  a token placed there becomes a live bearer credential in durable
  conversation history. The cache dies with the process, which is correct.

Only two read paths exist: `get_or_mint`, and `snapshot()` which returns
fingerprints and timings. There is deliberately no accessor that returns a
token to a diagnostics endpoint.

## Fail-closed guarantee (ADR 004)

Every path out of `google_access_token` is either **a token belonging to the
calling human** or **a raised `IdentityAcquisitionError`**. There is no third
outcome. No `return None`, no ADC, no service-account impersonation, no
degraded mode, no flag.

This is not fussiness. A fallback does not degrade gracefully — it *succeeds*,
as the wrong principal. BigQuery answers as the service account, every
row-level policy written against the human evaluates against something else,
and the user gets a plausible answer. The failure mode is not an outage, it is
undetected over-disclosure. ADR 002 splits the Bot Identity plane from the Tool
Identity plane precisely so that cannot happen; a fallback here silently
converts one into the other.

Two independent layers enforce it:

* `tests/test_no_service_account_fallback.py` — a repo-wide source scan (owned
  by another workstream; it walks `app/identity/` too).
* `tests/test_identity_broker_fail_closed.py` — this component's behavioural
  tests, that a failing OBO or STS **raises**, plus AST checks that nothing
  here imports a credential library and that the accessor returns `str`.

Mutation-tested: injecting an ADC fallback into `broker.py` turns 8 tests red
across both layers. See `NOTES.md` §2.3 and §6.

### Failure stages

| `reason_code` | Meaning | Who fixes it |
| --- | --- | --- |
| `identity.precondition.invalid_request` | No/malformed `user_key`, no assertion | Caller (bug) |
| `identity.obo.consent_required` | AADSTS65001, MFA, conditional access | The **user** — sign-in card |
| `identity.obo.transient` | Entra 5xx / throttle / timeout | Nobody; retry |
| `identity.obo_audience.mismatch` | Token unusable against Google | An **admin** — app registration |
| `identity.sts.subject_token_rejected` | STS 400: audience, issuer, expiry | An **admin** |
| `identity.sts.permission_denied` | STS/Google 403: IAM or quota project | An **admin** |
| `identity.sts.transient` | STS 5xx / timeout | Nobody; retry |

Only one of those is fixed by showing the user a sign-in card. Collapsing them
all into "sorry, sign in again" asks six out of seven users to fix a problem
they do not have.

## Token hygiene

No token is ever logged, at any level. Everything logged is a fingerprint —
SHA-256, first 12 hex, via `app.logging_utils.fingerprint` — plus claim values
that are not credentials (`aud`, `iss`, `ver`, `oid`, `exp`). `OboConfig`,
`MintedToken`, cache entries and the cache itself all override `__repr__`, so
an accidental `print()` or an exception repr cannot leak one.
