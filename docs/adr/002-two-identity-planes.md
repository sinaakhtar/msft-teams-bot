# 002. Separate Invocation Identity from Tool Identity

## Status
Accepted

## Context & Problem Statement
"The bot should authenticate as the user" is ambiguous. It can mean at least two
different things, and they are satisfied by entirely different mechanisms:

- The call *into* the Agent Runtime is authorized as the human, so Google Cloud IAM and
  audit logs name the person rather than a shared robot account.
- The agent's calls *out* to downstream systems act as the human, so each system
  applies its own authorization rules to that person.

A design that conflates them produces the common anti-pattern: a bot that authenticates
users at the front door for show, then does all real work under one service account
that can see everything.

## Decision Drivers
- Both properties were explicitly requested.
- A demo whose security story does not survive a customer security review is worse than
  no demo, because it teaches the wrong pattern.
- The user signs in on the Microsoft side, but the agent runs on the Google side, so
  an identity must cross a trust boundary in a form each side accepts natively.

## Considered Options
1. **Service account for everything.** The bot authenticates the user only to decide
   whether to answer at all. Pro: trivial. Con: the agent's blast radius is the union
   of everything any user may see; fails the stated goal outright.
2. **Invocation Identity only.** Federate the user into Google Cloud, then let tools
   use ambient credentials. Pro: audit logs look right. Con: authorization is theatre,
   since the tools still ignore who asked.
3. **Tool Identity only.** Invoke the runtime as a service account but pass the user's
   Microsoft token through into session state for tools to use. Pro: simplest path to a
   convincing demo; no Workforce Identity Federation setup. Con: nothing on the Google
   side knows who the user is, so Google-side authorization and audit remain blind.
4. **Both planes, named and separated.** Invocation Identity comes from federating the
   Entra User into a Workforce Principal; Tool Identity is acquired per tool, via
   On-Behalf-Of exchange for Microsoft resources.

## Decision Outcome
Chosen option: **both planes, named and separated**, and named explicitly in the
ubiquitous language so that "as the user" is never again used without qualification.
Option 3 is the pragmatic path most integrations take and is a legitimate fallback if
organization-level Workforce Identity Federation setup proves unavailable, but it
leaves the Google side unable to distinguish one user from another, which is precisely
the property being showcased.

### Positive Consequences
- Google Cloud IAM and Cloud Audit Logs attribute agent invocations to the human.
- Every downstream system enforces its own rules against the real user, so the agent
  cannot become a confused deputy.
- The two planes fail independently and can be explained, tested and demoed separately.

## Scope Note (added after the decision, same design session)
The initial scope carried one Microsoft-side Tool over Graph and one Google-side Tool
over BigQuery, so that each identity plane had a visible instance. The Microsoft-side
Tool was subsequently dropped. The decision to separate the two planes stands, but its
consequences change:

- ~~On-Behalf-Of exchange no longer appears anywhere.~~ **Corrected 2026-09-07.** This
  was wrong. On-Behalf-Of returns to the design, for a different reason than before.
  Google's Security Token Service validates the `aud` claim of the incoming token
  against the client ID registered on the Workforce Pool provider. A Teams SSO token is
  minted with the *bot's* own audience, so it cannot be presented to Google directly
  unless Google is configured to trust the bot's audience. The Bot Middle Tier
  therefore performs an On-Behalf-Of exchange to re-audience the user's token for a
  dedicated federation application before exchanging it at Google's STS. On-Behalf-Of
  is no longer used to reach Microsoft Graph; it is used to cross the trust boundary.

- **Correction, 2026-09-07, from the Entra runbook research.** The chain proven in
  the spike is *not* byte-identical to the production chain, and the difference is
  load-bearing. The spike used a device-code sign-in, which yields an **ID token**,
  exchanged at Google's STS with `subjectTokenType` of `...:id_token`. An On-Behalf-Of
  exchange returns an **access token** and no ID token at all.

  **Second correction, same day, from the identity broker research.** I concluded from
  that difference that production must send `urn:ietf:params:oauth:token-type:jwt`
  instead. That was wrong. The same access token is rejected identically under either
  `subjectTokenType`, so it is not the lever and there is no reason to change it.
  `...:id_token` stays, because it is the value verified end to end.

  The trap sits behind that. A v2.0 access token carries the resource's client ID as
  `aud`, which is what the provider wants, but `requestedAccessTokenVersion` on the
  federation application **defaults to 1**, and a v1.0 token is issued with
  `https://sts.windows.net/{tid}/` as its issuer rather than the
  `https://login.microsoftonline.com/{tid}/v2.0` the provider is configured to trust.
  Left at the default, the OBO token is rejected on the issuer check even though every
  other part of the setup is correct. The federation application must therefore set
  `requestedAccessTokenVersion` to 2. That setting governs access tokens only, so it
  does not disturb the already-proven device-code path.

  Note also that Google's STS rejects requests carrying an `Authorization` header.
- Both planes are now Google-side and name the same person, which makes the separation
  harder to see in a demo and easier for a future reader to mistake for redundancy. It
  is not redundant: the Agent's process runs under the runtime's service identity, so
  the Workforce Principal still has to be threaded explicitly to the Tool.
- The single remaining Tool is reached through the managed BigQuery MCP server, which
  authorizes per bearer token. The design therefore rests entirely on that endpoint
  accepting a workforce-federated token, an assumption that is untested and is the
  subject of the first spike. If it is rejected, this ADR must be revisited rather than
  patched.

### Negative Consequences & Risks
- Two token acquisition paths, two expiry regimes and two failure modes to handle in a
  single turn.
- Requires organization-level Workforce Identity Pool configuration, plus a
  domain-restricted sharing policy scoped to the organization principal set. Without
  organization-level access this decision cannot be implemented as written.
- The Workforce Principal has no Google account, which constrains which Google services
  can be called and how roles are granted.
- Token exchange latency is added to the critical path of every turn unless cached.
