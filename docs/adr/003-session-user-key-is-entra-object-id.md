# 003. Key Agent Runtime Sessions on the Entra object ID

## Status
Accepted

## Context & Problem Statement
Every Agent Runtime Session carries an opaque `user_id`. The Bot Middle Tier must
choose what string to put there, and three identifiers for the same human are available
at the moment a Teams activity arrives:

- `from.id`, the Teams MRI (`29:...`), which the Bot Framework supplies as the sender.
- `from.aadObjectId`, the Entra object ID (`oid`) of the signed-in user.
- The user principal name or email address.

Separately, the Workforce Principal that authorizes the invocation into the Agent
Runtime derives its subject from a claim in the user's federated OIDC token.

## Decision Drivers
- The session's owner and the IAM principal performing the work should be the same
  string, so that "who owns this session" is answerable in IAM terms rather than only
  through a lookup table the middle tier happens to maintain.
- The key is effectively permanent. Changing it later orphans every existing session,
  and would orphan every Memory scope if Memory Bank is adopted later.
- The key should not be personally identifying, since it is written into a managed
  Google service and appears in logs.

## Considered Options
1. **`from.id`, the Teams MRI.** Pro: handed to us directly on every activity, always
   present, no extra claim needed. Con: it is a Bot Framework channel-scoped
   identifier, not an Entra identity, so it will never match the Workforce Principal
   subject; reconciling the two requires a mapping table that exists for no reason
   other than this choice.
2. **UPN or email.** Pro: human-readable, which is pleasant when debugging. Con:
   mutable, since people change names, and it writes personally identifying data into
   session keys and logs.
3. **`from.aadObjectId`, the Entra object ID.** Pro: immutable per user per tenant,
   not personally identifying, and it is the same `oid` claim the federated token
   carries, so the session key and the Workforce Principal subject agree by
   construction. Con: can be absent for identities that are not fully resolved members
   of the tenant, so the middle tier must fail closed when it is missing.

## Decision Outcome
Chosen option: **the Entra object ID, tenant-qualified as `entra:{tid}:{oid}`**, taken
from `from.aadObjectId`. The tenant prefix costs nothing now and prevents an identifier
collision if the bot is ever exposed to a second tenant.

Option 1 was initially selected and then reversed once it became clear that the Teams
MRI cannot match the federated subject. The reversal is recorded here because the
attraction of option 1 is real and a future reader will otherwise re-propose it.

### Positive Consequences
- Session ownership and IAM identity are the same string, so no reconciliation table.
- No personally identifying data in session keys.
- If Memory Bank is adopted later, its default scoping on `user_id` is already correct.

### Negative Consequences & Risks
- `from.aadObjectId` is not guaranteed present on every activity. The middle tier must
  refuse to serve a turn rather than fall back to `from.id`, because a silent fallback
  would create a second, unfederated session identity for the same person.
- Session keys are opaque during debugging; correlating a session to a human requires a
  deliberate directory lookup.
