# Bot Middle Tier

Relay and identity broker between Microsoft Teams (via Azure Bot Service) and a
Google Agent Runtime reasoning engine.

```
Teams user
  -> Azure Bot Service
    -> POST /api/messages          <-- THIS SERVICE (Cloud Run)
      -> identity broker (OBO/STS)
        -> Agent Runtime (Reasoning Engine)
          -> ADK agent
            -> BigQuery MCP, acting AS THE USER
```

This service owns the inbound half: JWT validation, identity extraction,
activity routing, config and secrets, health probes, and the interface seams
the rest of the system plugs into. It contains **no prompt logic and makes no
model calls**. If it starts making decisions about content, the boundary has
been violated.

## The security model

`POST /api/messages` is a public endpoint on the open internet. Downstream of
it, `app/identity.py` reads `from.aadObjectId` out of the request body and
turns it into the Agent Runtime session key `entra:{tid}:{oid}`, which is
exchanged for a Google access token that BigQuery then runs queries under. So
the request body is a claim about *who a person is*, and `app/auth/inbound.py`
is the only thing that makes that claim trustworthy: an activity that reaches
the router without a cryptographically verified Bot Framework token is an
attacker asserting an arbitrary Entra user and reading that user's data. There
is no second gate. Accordingly the validator fails closed on every path, has no
bypass switch of any kind, checks issuer / audience / signature-by-`kid` /
expiry with a 5-minute skew / `serviceUrl`-to-activity binding, rejects
`alg: none` and every symmetric algorithm before it will even fetch a key, and
never reads a claim for a security decision before the signature is verified.
Two further rules follow from the same reasoning: a missing `aadObjectId`
refuses the turn rather than falling back to the Teams MRI (which would mint a
second, unfederated identity for someone who already has one), and no failure
anywhere is ever retried under a service account.

## Layout

| Path | What it is |
| --- | --- |
| `app/auth/inbound.py` | Inbound Bot Framework JWT validation. The crown jewels. |
| `app/caller_identity.py` | `entra:{tid}:{oid}` extraction. ADR 003, fail closed. Named this way because `app/identity/` is the OBO/STS broker package — see NOTES.md. |
| `app/ports.py` | Protocol seams: IdentityBroker, SessionManager, AgentRuntimeClient, StreamingRenderer. |
| `app/routing.py` | Activity dispatch: `message`, `/new`, `conversationUpdate`, `invoke`. |
| `app/errors/` | ADR 004 user-facing templates, taxonomy, classification and the tool boundary. Fixed text, no model in the loop. Includes `sso_prompt`, the OAuthCard that starts the silent Teams exchange. |
| `app/sso.py` | Per-user state spanning the `signin/tokenExchange` invoke and the message that needs it: assertion, parked turn, exchange dedup. In memory, hence the single-instance pin. |
| `app/config.py` | Secret Manager wiring + a loud dev-only env fallback. |
| `app/logging_utils.py` | Cloud Run JSON logging and token redaction. |
| `app/main.py` | aiohttp app: `/api/messages`, `/healthz`, `/readyz`. |
| `tests/mutation_check.py` | Breaks the validator on purpose to prove the suite catches it. |

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock.txt

export MIDDLE_TIER_DEV_MODE=true          # enables the env-var secret fallback
export MICROSOFT_APP_ID=<your bot app id>
export MICROSOFT_APP_PASSWORD=<bot password>
export ENTRA_CLIENT_SECRET=<obo app secret>
export GCP_PROJECT_ID=<GCP_PROJECT_ID>
export GCP_PROJECT_NUMBER=<GCP_PROJECT_NUMBER>
export ENTRA_TENANT_ID=<ENTRA_TENANT_ID>
export PORT=8080

.venv/bin/python -m app.main
```

`MIDDLE_TIER_DEV_MODE=true` logs a `CRITICAL` line naming every secret it reads
from an environment variable. That noise is intentional: if those lines ever
appear in a deployed environment's logs, alert on them. Setting the flag while
`K_SERVICE` is present (i.e. on Cloud Run) raises at startup rather than
degrading quietly.

To trust the Bot Framework Emulator, additionally set `ALLOW_BOT_EMULATOR=true`.
It only takes effect in dev mode and is always pinned to `ENTRA_TENANT_ID` —
there is no wildcard Entra issuer.

### Tests

```bash
.venv/bin/pip install pytest pytest-asyncio
.venv/bin/python -m pytest -v          # 94 tests, no network required
.venv/bin/python tests/mutation_check.py
```

The suite generates a real RSA keypair, mints real tokens and serves a real
JWKS over loopback. Nothing in the crypto or transport path is mocked, because
mocking `jwt.decode` would let a validator that accepts `alg: none` pass a test
that claims to reject it. `mutation_check.py` is the counterpart: it breaks the
validator six different ways and confirms the suite goes red each time, then
restores the files.

## Deploy to Cloud Run

Create the secrets once:

```bash
printf %s "$BOT_PASSWORD"  | gcloud secrets create teams-bot-app-password \
  --project <GCP_PROJECT_ID> --data-file=-
printf %s "$ENTRA_SECRET"  | gcloud secrets create entra-obo-client-secret \
  --project <GCP_PROJECT_ID> --data-file=-
```

Grant the runtime service account read access, then deploy:

```bash
gcloud run deploy teams-middle-tier \
  --project <GCP_PROJECT_ID> \
  --region us-central1 \
  --source middle_tier \
  --service-account teams-middle-tier@<GCP_PROJECT_ID>.iam.gserviceaccount.com \
  --allow-unauthenticated \
  --min-instances=1 --max-instances=1 \
  --set-env-vars GCP_PROJECT_ID=<GCP_PROJECT_ID>,\
GCP_PROJECT_NUMBER=<GCP_PROJECT_NUMBER>,\
GCP_LOCATION=us-central1,\
ENTRA_TENANT_ID=<ENTRA_TENANT_ID>,\
MICROSOFT_APP_ID=<bot app id>,\
MICROSOFT_APP_TYPE=SingleTenant,\
OAUTH_CONNECTION_NAME=<OAUTH_CONNECTION_NAME>,\
REASONING_ENGINE_ID=<REASONING_ENGINE_ID>
```

`OAUTH_CONNECTION_NAME` is the name of the OAuth Connection Setting on the
Azure Bot resource (runbook 11, step 5a). Without it the service starts, logs a
WARNING, and refuses every turn: Teams only begins the silent token exchange
when the bot sends an OAuthCard naming a connection, so no assertion ever
arrives. It is not optional for a working bot.

`--min-instances=1 --max-instances=1` is a **correctness** constraint, not a
latency one. The Teams SSO assertion (`app/sso.py`) and the session mapping
(`app/sessions/`) are both held in process memory. On more than one instance
the `signin/tokenExchange` invoke and the `message` that needs it can land on
different processes, and the user gets an endless sign-in loop; on scale to
zero the assertion dies with the instance. Runbook 11, step 6a.

`REASONING_ENGINE_ID=<REASONING_ENGINE_ID>` is **ours**: display name
`teams-bot-bq-analyst`, deployed 2026-09-07, full resource name
`projects/<GCP_PROJECT_NUMBER>/locations/us-central1/reasoningEngines/<REASONING_ENGINE_ID>`.
See `agent/DEPLOYMENT.md`. It is **never** `<OTHER_ENGINE_ID_1>`,
`<OTHER_ENGINE_ID_2>` or `<OTHER_ENGINE_ID_3>` — those three are pre-existing
`data_science_agent` engines belonging to someone else. The value is read from
the environment by `app/config.py` only; nothing hardcodes it in application
logic.

`--allow-unauthenticated` is correct and is not a mistake: Azure Bot Service
cannot present a Google IAM credential, so the endpoint must be reachable
without one. **The Bot Framework JWT is the entire authentication boundary.**
That is precisely why `app/auth/inbound.py` is written and tested the way it is.

Secrets are deliberately *not* passed with `--set-secrets`. A Cloud Run secret
mount puts the value on a filesystem, which means it lands in core dumps and in
anything that walks the container. The service reads them over the API at
startup instead, so they exist only in process memory.

Then point the Azure Bot registration's messaging endpoint at
`https://<cloud-run-url>/api/messages`.

### Probes

- `GET /healthz` — liveness. Dependency-free, so a downstream blip cannot cause
  a restart loop.
- `GET /readyz` — readiness. Warms and checks the channel JWKS, so an instance
  with no egress to `login.botframework.com` never joins the load balancer.
  That instance would 401 every request, so keeping it out is the point.

## Status

Working end to end against a real Teams client as of 2026-09-21: a 1:1 message
is authenticated, the Teams SSO assertion is exchanged silently, the turn is
answered as the signed-in human, and `select session_user()` returns the
workforce-pool principal rather than a service account.

Built and tested here: JWT validation, identity extraction, routing, error
templates, config/secrets, logging, health, outbound Bot Connector delivery,
and the Teams SSO token exchange. The `app/ports.py` Protocols are all wired in
`app/composition.py`.

Two constraints worth knowing before you deploy this anywhere real:

- Per-user state is in process memory, so the service must be pinned to a
  single always-on instance. See the deploy section above.
- The SSO replay runs inside the `invoke` request, so it is subject to the same
  reply-window limit as any other turn.

See `NOTES.md` for what was executed versus merely written, the SDK-versus-PyJWT
decision, and open items.
