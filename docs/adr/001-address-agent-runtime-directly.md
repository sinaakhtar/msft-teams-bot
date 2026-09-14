# 001. Address the Agent Runtime directly rather than the Gemini Enterprise assistant

## Status
Accepted

## Context & Problem Statement
Google exposes two distinct entry points that both look like "chat with an agent":

1. The Agent Runtime on the Gemini Enterprise Agent Platform, addressed as a Reasoning
   Engine resource and invoked with a streaming query. Sessions, Memory Bank and the
   agent's own tool calls are first-class, separately addressable services.
2. The Gemini Enterprise assistant, invoked through the Discovery Engine assistant
   surface. It orchestrates registered agents and data stores behind a single call and
   maintains its own, separate notion of a session.

The Teams bot must pick one as the thing it talks to. The two have different session
models, different identity plumbing and different failure surfaces, and the choice
determines the shape of the middle tier.

## Decision Drivers
- The stated purpose is to showcase how Agent Runtime sessions and its surrounding
  components actually work. Machinery that is hidden cannot be showcased.
- Exactly one definition of "session" must exist in the system. Two would be a standing
  source of confusion in both the code and the demo narrative.
- The end-user identity must reach Google Cloud IAM in a form we control and can point
  at in an audit log.

## Considered Options
1. **Agent Runtime directly.** The bot calls the Reasoning Engine's streaming query
   entry point and interacts with the Sessions and Memory Bank services explicitly.
   Pro: every component is visible and demonstrable; full control over session
   lifecycle and identity. Con: more moving parts to build; no built-in grounding,
   retrieval or answer-formatting.
2. **Gemini Enterprise assistant.** The bot calls the assistant surface and lets it
   orchestrate. Pro: much less to build; grounding and citations come for free.
   Con: the session machinery we want to demonstrate is internal and largely opaque;
   the demo becomes a product tour rather than an architecture walkthrough.
3. **Both, behind an abstraction.** Pro: preserves optionality. Con: the two session
   models do not share a shape, so the abstraction would leak immediately and we would
   pay for a seam we have no concrete plan to use.

## Decision Outcome
Chosen option: **Agent Runtime directly**, because the explicit goal is to make
sessions and the surrounding components legible. Option 2 would satisfy a user asking
for a working Teams chatbot but not one asking to see how the runtime works, and option
3 buys optionality at the cost of an abstraction over two genuinely dissimilar models.

### Positive Consequences
- Session creation, event append, history and memory generation are all explicit calls
  we can narrate, log and show.
- The invocation is a plain authenticated API call, so the end-user identity model is
  ours to define rather than the assistant's to impose.
- Agent behaviour is authored in ADK and versioned with this repository.

### Negative Consequences & Risks
- No grounding, retrieval or citation behaviour is inherited. Anything of that kind
  must be built as an explicit tool.
- More surface to get wrong: session lifecycle, event ordering and history truncation
  all become our responsibility.
- If the demo audience turns out to care about Gemini Enterprise as a product rather
  than the platform beneath it, this is the wrong entry point and the middle tier would
  need reworking.
