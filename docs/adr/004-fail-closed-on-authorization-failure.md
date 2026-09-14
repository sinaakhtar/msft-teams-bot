# 004. Fail closed on authorization failure, and never explain a denial with the model

## Status
Accepted

## Context & Problem Statement
Two distinct authorization failures will occur in normal operation and both need a
defined behaviour:

1. **Identity acquisition fails.** The On-Behalf-Of exchange is refused, Google's
   Security Token Service rejects the assertion, or the user has been removed from the
   Entra tenant. The system cannot establish who is asking.
2. **A downstream system denies an established identity.** The user is who they claim
   to be, but BigQuery returns a 403 because they lack access to a dataset.

These have different correct responses, and both have a tempting wrong response. For
the first, the tempting wrong answer is to fall back to a service account so the
conversation continues. For the second, it is to hand the raw IAM error to the model and
let it explain in natural language.

## Decision Drivers
- The system exists to demonstrate that authorization is real. A failure path that
  quietly restores service by widening privilege destroys the thing being demonstrated.
- Failures are most likely to be encountered live, in front of an audience, which is
  exactly when the pressure to "just make it work" is highest.
- A language model asked to explain an authorization error has no way to distinguish a
  missing role from a missing dataset from a network fault, and will produce a fluent,
  confident, and possibly fabricated reason.

## Considered Options

### For identity acquisition failure
1. Generic error message. Honest but unactionable; the user cannot tell whether to
   retry, re-consent, or contact an administrator.
2. **Explicit message plus a sign-in card.** Names the failure as an identity problem
   and offers the one action that might fix it.
3. Fall back to a service account. Keeps the conversation alive at the cost of serving
   data under an identity that is not the user's.

### For downstream denial
1. Pass the raw error to the model and let it explain.
2. **Intercept the error and return a templated message**, informing the model only
   that access was denied and to which resource.

## Decision Outcome
Chosen: **option 2 in both cases.** On identity failure the turn is refused with an
explicit message and a sign-in card. On downstream denial the error is intercepted at
the tool boundary and rendered from a template naming the refused resource.

**Option 3 for identity failure is explicitly rejected and must not be reintroduced.**
Falling back to a service account when identity cannot be established is the precise
anti-pattern that ADR 002 exists to prevent. It is recorded here as a rejected option
rather than omitted, because it is the obvious expedient fix and will be proposed again
by someone who has not read ADR 002.

### Positive Consequences
- The system cannot serve data under an identity other than the requesting user's, even
  when degraded.
- Denials are accurate and specific, and the demo can point at a real refusal instead of
  a paraphrase of one.
- The failure surface is small and testable: two paths, two templates.

### Negative Consequences & Risks
- The bot will visibly stop working when federation is broken, including during a live
  demo. This is intended, and the sign-in card is the only mitigation.
- Templated denial messages are less fluent than model-authored prose and will read as
  more abrupt.
- Intercepting tool errors means the model loses the opportunity to suggest a genuine
  workaround, such as querying a table the user can see. Accepted, because the cost of a
  fabricated explanation is higher than the value of a speculative suggestion.
