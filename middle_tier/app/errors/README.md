# `app.errors` — the ADR 004 failure surface

Two paths. Two templates. No third option.

This package is the whole authorization-failure surface of the middle tier. It
is deliberately small, because ADR 004's claim is that the failure surface is
"small and testable", and a small surface is only true if nobody adds a third
path when the first two are inconvenient.

```
taxonomy.py    typed errors; separates what the template may see from what the log keeps
classify.py    (status code, message text) -> taxonomy. The two-403s distinction lives here
templates.py   the two user-facing templates + the sign-in card / OAuthCard
boundary.py    the tool-boundary interceptor; narrows the model's view
```

---

## THE REJECTED OPTION, STATED FIRST

> **Falling back to a service account is explicitly rejected and must not be
> reintroduced.**

ADR 004 records it as a rejected alternative and predicts precisely how it
comes back: someone who has not read ADR 002 hits a failed OBO exchange during
a demo, sees that a service account would make the query work, and proposes it
as the obvious expedient fix. It is not a fix. It answers the user with data
they may have no right to see, under an identity that is not theirs, which is
the exact failure the entire design exists to prevent.

There is no code path in this package that produces a credential.
`acquire_credential_or_refuse()` returns `None` for the token on every failure,
and `tests/test_no_service_account_fallback.py` fails the build if ambient
credentials, a key file, an impersonated principal or the metadata server
appear anywhere in `middle_tier/` or `agent/` source.

---

## Path 1 — identity acquisition failure

**Trigger:** the OBO exchange is refused, Google's STS rejects the assertion,
the user has been removed from the tenant, or the activity carried no
`from.aadObjectId` (ADR 003).

**Behaviour:** refuse the turn. Send an explicit message naming this as an
*identity* problem, plus a **sign-in card**.

```python
from app import errors

token, outcome = await errors.acquire_credential_or_refuse(
    broker_call, signin_url=settings.signin_url
)
if outcome:                        # refused
    return outcome.user_activity   # message + sign-in card
```

Rejected alternatives, recorded so nobody reintroduces them:

| alternative | why not |
|---|---|
| a bare generic error | honest but unactionable — the user cannot tell whether to sign in again, wait, or open a ticket |
| **fall back to a service account** | see above. Explicitly rejected. |

The message says *identity problem*, not *permission problem*, because those
send the user to two different people. `reason_code` is a correlation token for
a support ticket, never an explanation. Upstream text never appears in it.

**A sign-in card is not always offered.** For a guest or anonymous participant
with no Entra object id, there is no directory to sign in to, and a button
would loop forever. That variant refuses and says why.

## Path 2 — downstream authorization denial

**Trigger:** the user's own token is valid, but IAM or BigQuery refuses this
specific resource.

**Behaviour:** intercept at the **tool boundary** and return a templated
message that **names the refused resource**.

```python
result, outcome = await errors.guard_tool_call(
    lambda: bq.query(sql), resource="<GCP_PROJECT_ID>.sales.orders", action="bigquery.jobs.create"
)
if outcome:
    tell_model(outcome.model_message)      # "Access denied to <GCP_PROJECT_ID>.sales.orders."
    reply(outcome.user_activity)           # the template
```

### The asymmetry, which is the point

| party | gets | why |
|---|---|---|
| log | **everything**: full raw upstream text, status, resource, stage, request id, classifier confidence | ADR 004: "Log the full text" |
| user | the template, naming the resource | actionable and fixed |
| model | **one sentence**: `Access denied to <resource>.` | it cannot be trusted with more |

The model is never handed the raw IAM error to explain. A language model asked
to explain an authorization error cannot distinguish a missing role from a
non-existent table from a typo, so it invents a cause, and a fabricated
explanation costs more than a missing suggestion.

**Accepted cost:** the model loses the chance to suggest a genuine workaround,
such as querying a table the user *can* see. That is deliberate.

`boundary.model_facing()` is the only sanctioned way to build the model's
string, and it re-checks the result against the raw text before returning it.
Widening it raises `ModelContextLeak` — there is a test that plants exactly
that regression.

---

## The two 403s

Both failures observed in the earlier spike were HTTP 403 and they meant
completely different things. **The message text is the only signal.**

**A 403 naming a missing role** is an ordinary permission fix. Verified,
verbatim:

```
Caller does not have required permission to use project <GCP_PROJECT_ID>.
Grant the caller the roles/serviceusage.serviceUsageConsumer role...
```

→ `DownstreamAuthorizationDenied(resource="project <GCP_PROJECT_ID>",
named_role="roles/serviceusage.serviceUsageConsumer")` → path 2.

**A 403 naming the credential or principal type** would have been fatal to the
design: Google would be rejecting the workforce-pool principal as a *kind of
caller*, not this caller's access to a *thing*. No role grant fixes it and no
sign-in loop fixes it.

→ `IdentityAcquisitionError(reason_code="credential_type_rejected",
design_fatal=True)` → path 1, logged at **CRITICAL**.

**Precedence.** Real missing-role messages sometimes mention a service account
in passing, so the strong role-grant signatures are tested *first* and the
credential-type patterns only on text that did not match one. Both orders are
covered by tests.

**Nothing is swallowed.** Every classification carries the full upstream text
into a log-only field. A classifier that discarded the text would make this
distinction unauditable after the fact.

**An unrecognised 403** is still refused, still rendered as a denial, but
flagged `confidently_classified=False` and logged at ERROR so a human reads the
text the classifier could not place.

**A 404 is not a denial.** BigQuery answers "Not found: Table x" both for a
table that does not exist and for one the caller cannot see. ADR 004's whole
argument is that this distinction cannot be guessed — so the classifier does
not guess it either.

---

## Templates read as abrupt. That is the trade.

Plain, specific, non-apologetic, no model in the loop. ADR 004 accepts that
they are blunter than model prose. A refusal a user can act on beats a fluent
one they cannot.

## Sign-in card shape

- Bot Framework card spec (sign-in card: `application/vnd.microsoft.card.signin`,
  `content.text`, `content.buttons[]` with `type: "signin"` and `value` = URL):
  <https://github.com/microsoft/botframework-sdk/blob/main/specs/botframework-activity/botframework-cards.md>
- Teams card reference:
  <https://learn.microsoft.com/en-us/microsoftteams/platform/task-modules-and-cards/cards/cards-reference>
- Teams bot authentication / OAuthCard (`application/vnd.microsoft.card.oauth`,
  `connectionName`, `tokenExchangeResource`):
  <https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/authentication/add-authentication>
- Teams SSO overview:
  <https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/authentication/bot-sso-overview>
- `401` + `application/vnd.microsoft.activity.loginRequest` invoke response:
  <https://learn.microsoft.com/en-us/microsoftteams/platform/task-modules-and-cards/cards/universal-actions-for-adaptive-cards/authentication-flow-in-universal-action-for-adaptive-cards>

`oauth_card()` is preferred where an Azure Bot OAuth connection exists: Teams
can satisfy it silently through SSO token exchange. Google credentials live
about 3600s, so expiry mid-conversation is a **normal** event, and a visible
sign-in button every hour trains users to click through consent without
reading it.

## Tests

```
cd middle_tier
.venv/bin/python -m pytest tests/test_error_templates.py tests/test_no_service_account_fallback.py -v
```

See `NOTES.md` for the recorded runs and their real output.
