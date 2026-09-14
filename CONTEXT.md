# Domain Context & Glossary

Ubiquitous language for the Teams-to-Agent-Runtime bot. Glossary only: no
implementation detail, no schemas, no task lists.

## Core Terms

### Agent Runtime
- **Definition**: Google's managed execution surface for deployed agents, addressed as
  a Reasoning Engine resource (`projects/*/locations/*/reasoningEngines/*`) on the
  Gemini Enterprise Agent Platform. It hosts the agent process and exposes query and
  streaming-query entry points.
- **Key Relationships**: Holds the deployed Agent. Associated with a Sessions instance
  and, optionally, a Memory Bank instance.
- **Invariants / Constraints**: Addressed directly by the Bot Middle Tier. It is *not*
  reached through the Gemini Enterprise assistant surface (`streamAssist`); that is a
  different product entry point with its own separate session concept, and mixing the
  two would give us two competing definitions of "session".

### Agent
- **Definition**: The ADK-authored conversational program deployed into the Agent
  Runtime. It reasons over a turn and may call Tools.
- **Key Relationships**: Executes within an Agent Runtime. Acts under a Tool Identity
  when calling any Tool.
- **Invariants / Constraints**: The Agent never holds a long-lived credential of its
  own for user-scoped work; every user-scoped action derives from an identity supplied
  per invocation.

### Bot Middle Tier
- **Definition**: The service that receives Teams activities, resolves identity, and
  invokes the Agent Runtime. It is a relay and an identity broker, never a reasoner.
- **Key Relationships**: Sits between the Teams client and the Agent Runtime. Owns the
  mapping from a Teams Conversation to an Agent Runtime Session.
- **Invariants / Constraints**: Contains no prompt logic and no model calls. If it
  starts making decisions about content, the boundary has been violated. It must
  cryptographically validate every inbound activity before trusting any identity claim
  it carries; an unvalidated activity is an attacker asserting an arbitrary Entra User,
  which bypasses every other control in this system. It depends on platform surfaces
  rather than on any given Agent's exported methods, with the sole exception of the
  event stream it consumes.

### Invocation Identity
- **Definition**: The identity under which the call *into* the Agent Runtime is
  authenticated and authorized. Answers "who is asking the agent?"
- **Key Relationships**: Derived from the Entra User via the Workforce Principal.
  Distinct from Tool Identity.
- **Invariants / Constraints**: Must resolve to the human, not to a shared service
  account. Google Cloud IAM decisions and audit log entries for the invocation must
  name the person. Authorizing the invocation does *not* implicitly authorize anything
  the Agent subsequently does: inside the Agent Runtime the Agent's process runs under
  the runtime's own service identity, so Invocation Identity never silently becomes a
  Tool Identity, not even for Google Cloud resources.

### Tool Identity
- **Definition**: The identity under which the Agent acts when calling a downstream
  system. Answers "who is the agent acting as, right now?"
- **Key Relationships**: Derived per-Tool. In scope today there is exactly one Tool
  Identity: the Workforce Principal, presented as a bearer token to Google Cloud.
  Distinct from Invocation Identity even though both currently name the same person.
- **Invariants / Constraints**: A Tool Identity may never be broader than the Entra
  User's own entitlements. Ambient or default credentials are never a valid Tool
  Identity for user-scoped data. Because one Agent instance serves every user, a Tool
  Identity must be resolved per invocation and never captured at construction time;
  a Tool Identity that outlives the turn that created it is a cross-user data leak.

### Entra User
- **Definition**: The human signed into Teams, as represented in Microsoft Entra ID.
  The single source of truth for who the user is.
- **Key Relationships**: Projects into Google Cloud as a Workforce Principal. Projects
  into Microsoft resources as an On-Behalf-Of Token.
- **Invariants / Constraints**: A work/school account in an Entra tenant. A consumer
  Microsoft account cannot be an Entra User for this system, because consumer Teams
  cannot host a custom app.

### Workforce Principal
- **Definition**: The Google Cloud representation of an Entra User, obtained by
  federating the user's OIDC token through a Workforce Identity Pool and exchanging it
  at Google's Security Token Service.
- **Key Relationships**: The concrete form of Invocation Identity. Referenced in IAM
  allow policies as a workforce pool principal.
- **Invariants / Constraints**: No Google account is provisioned for the user. Granting
  a Workforce Principal an IAM role requires domain-restricted sharing to be configured
  against the organization principal set, otherwise the binding is rejected.

### On-Behalf-Of Token
- **Definition**: A Microsoft access token for a specific downstream resource, obtained
  by exchanging the user's Teams SSO token so the caller acts as the Entra User rather
  than as itself.
- **Key Relationships**: The hop that turns a Teams SSO token, which carries the bot
  application's audience, into a token carrying the federation application's audience.
  It is what makes a Workforce Principal obtainable at all.
- **Invariants / Constraints**: Used here purely as an audience-rewriting primitive to
  cross the Microsoft-to-Google trust boundary, not to reach Microsoft Graph. No Graph
  call exists anywhere in this system. The exchange returns an access token and never
  an ID token, but that distinction turns out not to matter to Google's Security Token
  Service: the same token is accepted or refused identically whichever subject token
  type is declared. What does matter is the version. The federation application must
  issue version 2 access tokens;
  at the version 1 default the issuer is `sts.windows.net` rather than the
  `login.microsoftonline.com/{tid}/v2.0` the provider trusts, and the token is refused
  on the issuer check while everything else looks correct.

### Teams Conversation
- **Definition**: A Microsoft-side thread of activities, identified by a conversation
  ID. In scope: one-to-one chats only.
- **Key Relationships**: Maps to exactly one Agent Runtime Session per Entra User.
- **Invariants / Constraints**: Group and channel conversations are out of scope. They
  have many Entra Users behind one conversation ID, so conversation identity alone
  cannot identify a user, and a shared session would place two people's history under
  one `user_id`. Admitting them later is a design change, not a configuration change.

### Agent Runtime Session
- **Definition**: A managed, chronologically ordered sequence of Events for one user's
  interaction with the Agent, held by the Agent Runtime's Sessions service and keyed by
  an opaque user identifier.
- **Key Relationships**: Contains Events. Supplies the conversation history that
  Memory Bank distils into Memories.
- **Invariants / Constraints**: Carries a `user_id`, fixed as `entra:{tid}:{oid}` from
  the Entra User's object ID so that it is the same subject as the Workforce Principal.
  It is never the Teams MRI. Where the object ID is absent from an activity, the turn
  is refused rather than served under a fallback identifier. Events are appended by the
  Agent Runtime itself; the Bot Middle Tier reads history but never writes it, so that
  exactly one component owns the sequence. A Session ends either on an explicit
  Conversation Reset or after 60 minutes without activity, whichever comes first.

  **Naming hazard**: three unrelated things in this system are called a session. The
  Agent Runtime Session is conversational history. The workforce pool
  `sessionDuration` is the lifetime of a federated Google credential, currently also
  3600 seconds. The Teams Conversation is a Microsoft-side thread. They expire
  independently and for different reasons. Never say "the session expired" without
  saying which one.

### Conversation Reset
- **Definition**: A user-initiated act that abandons the current Agent Runtime Session
  and starts a new one for the same Entra User. The only way a Session ends.
- **Key Relationships**: Replaces the Session a Teams Conversation currently maps to.
- **Invariants / Constraints**: The abandoned Session is not deleted and remains
  retrievable by ID, so a reset discards context without destroying history. It is the
  user's only explicit control over context length and cost; the 60-minute idle expiry
  is the implicit one.

### Event
- **Definition**: One recorded interaction within an Agent Runtime Session: a user
  message, an agent reply, or a tool action.
- **Key Relationships**: Appended to an Agent Runtime Session; replayed to reconstruct
  conversational context.

### Memory (deferred, not in scope)
- **Definition**: A durable fact distilled from Session history by Memory Bank, made
  available to future interactions.
- **Key Relationships**: Generated from an Agent Runtime Session. Scoped, by default,
  to the Session's `user_id`.
- **Invariants / Constraints**: Memory scope is a confidentiality boundary. If the
  scope key is ever shared between people, one user's Memories leak into another's
  context. Deferred so that the current scope demonstrates Sessions alone. Adoption
  later requires no change to the session key, which was chosen to be correct for
  memory scoping in advance.
