"""Streaming: ADK event stream in, Microsoft Teams streamed message out.

Three modules, one job each:

* :mod:`app.streaming.events` - defensive parsing of whatever the Agent
  Runtime yields into a five-word internal vocabulary.
* :mod:`app.streaming.teams_sink` - the Teams wire protocol, including the
  CUMULATIVE content contract that everyone gets wrong exactly once.
* :mod:`app.streaming.renderer` - the loop that joins them.

Read ``README.md`` in this directory before changing any of it. In
particular, read it before "fixing" the renderer to send only the newest
chunk, which is the single most tempting and most wrong change available.
"""

from __future__ import annotations

from .events import (
    AdkEventParser,
    ParsedEvent,
    TextChunk,
    ToolCallFinished,
    ToolCallStarted,
    ToolError,
    TurnComplete,
)
from .renderer import TeamsStreamingRenderer
from .teams_sink import (
    ConnectorTeamsSink,
    CumulativeContractViolation,
    RecordingTeamsSink,
    StreamState,
    TeamsSink,
    TEAMS_MIN_UPDATE_INTERVAL_SECONDS,
)

__all__ = [
    "AdkEventParser",
    "ParsedEvent",
    "TextChunk",
    "ToolCallStarted",
    "ToolCallFinished",
    "ToolError",
    "TurnComplete",
    "TeamsSink",
    "ConnectorTeamsSink",
    "RecordingTeamsSink",
    "CumulativeContractViolation",
    "StreamState",
    "TEAMS_MIN_UPDATE_INTERVAL_SECONDS",
    "TeamsStreamingRenderer",
]
