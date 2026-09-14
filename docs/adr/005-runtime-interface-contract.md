# 005. Consume ADK events, and own session lifecycle through the REST subresource

## Status
Accepted

## Context & Problem Statement
The deployed agent exposes several overlapping ways to do the same two jobs, and the
Bot Middle Tier must commit to one of each. Inspection of the live reasoning engine in
`<GCP_PROJECT_ID>` showed these exported class methods:

- Streaming: `stream_query`, `streaming_agent_run_with_events`, `async_stream_query`.
- Sessions: `create_session`, `get_session`, `list_sessions`, `delete_session`, plus
  async variants.

Separately, sessions are reachable through the REST `sessions` subresource on the
reasoning engine, which was confirmed working as a federated user.

Picking one of each defines the contract between the middle tier and the runtime.

## Decision Drivers
- Teams streamed messages need *informative updates* ("Querying BigQuery...") during
  long tool calls, otherwise a turn involving a slow query looks like a hung bot. The
  Teams streaming API also requires each update to carry all previously streamed text,
  so the middle tier must buffer regardless.
- ADR 001 placed session lifecycle ownership in the Bot Middle Tier.
- The agent will be rewritten during development. The contract should not break when it
  is.

## Considered Options

### Streaming
1. `stream_query`. Simpler: a stream of text to forward. But tool activity is invisible,
   so no informative updates are possible and the slowest part of every turn is silent.
2. **`streaming_agent_run_with_events`.** Yields structured ADK events including tool
   call start and completion. More parsing, and the event shape is a surface that can
   change, but it is the only source of the signal Teams needs.

### Session lifecycle
1. The agent's exported `create_session` / `get_session` / `delete_session` class
   methods. Convenient, and already present on this agent. But it couples the middle
   tier to one agent's exported surface, and an agent that stops exporting them breaks
   the bot.
2. **The REST `sessions` subresource on the reasoning engine.** The managed Sessions
   service directly. Uniform across agents, independent of what any agent chooses to
   export.

## Decision Outcome
Chosen: **`streaming_agent_run_with_events` for streaming, and the REST `sessions`
subresource for lifecycle.**

The through-line is that the middle tier depends on platform surfaces, not on agent
surfaces. The one exception is the event stream itself, which is unavoidably
agent-shaped, and is accepted because there is no platform-level alternative that
carries tool activity.

### Positive Consequences
- Tool activity is visible, so Teams can show progress during the slow part of a turn,
  which is also the part worth demonstrating.
- Session handling survives the agent being rewritten, redeployed or replaced.
- Session lifecycle stays in one component, as ADR 001 requires.

### Negative Consequences & Risks
- Event parsing is more code than forwarding text, and ADK event shapes are a moving
  target across versions. This is the most likely place for an upgrade to break the bot.
- Buffering cumulative text for Teams means holding partial responses in middle tier
  memory for the duration of a turn.
- Two components now understand sessions: the middle tier creates and resolves them,
  the runtime appends events to them. The boundary is that the middle tier never writes
  events, and violating that silently corrupts history.
