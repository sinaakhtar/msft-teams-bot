"""Defensive parsing of the Agent Runtime event stream.

WHY THIS MODULE IS PARANOID
---------------------------
ADR 005 chose ``streaming_agent_run_with_events`` over ``stream_query``
because tool activity is otherwise invisible, and it recorded the cost of
that choice in its own words:

    "Event parsing is more code than forwarding text, and ADK event shapes
    are a moving target across versions. This is the most likely place for
    an upgrade to break the bot."

So this module treats the incoming stream as UNTRUSTED, WEAKLY-TYPED DATA.
Every accessor tolerates a missing key, a null, a wrong type, a dict where
an object was expected and an object where a dict was expected. An
unrecognised event is logged at DEBUG and dropped. Nothing in here is
allowed to raise into the turn: a bot that dies on an unknown event type is
strictly worse than a bot that ignores one.

WHAT THE WIRE ACTUALLY LOOKS LIKE (verified, google-adk 2.8.0)
--------------------------------------------------------------
Verified by reading the installed source, not by guessing:

``vertexai/agent_engines/templates/adk.py`` yields, per event::

    _StreamingRunResponse(events=[event], artifacts=[], session_id=...).dump()

and ``dump()`` calls ``vertexai/agent_engines/_utils.dump_event_for_json``,
which is exactly::

    json.loads(event.model_dump_json(exclude_none=True))

Two consequences fall straight out of that one line, and both matter:

1. **snake_case, not camelCase.** ``model_dump_json`` is called WITHOUT
   ``by_alias=True``, even though ``Event.model_config`` sets
   ``alias_generator=to_camel``. So the wire carries ``invocation_id``,
   ``function_call``, ``error_code``, ``turn_complete``. The camelCase
   aliases are one keyword argument away from becoming reality, so every
   lookup here accepts BOTH spellings.
2. **``exclude_none=True`` means keys are ABSENT, not null.** Never assume
   a field exists. ``event["partial"]`` will ``KeyError`` on most events.

The envelope per streamed chunk is therefore::

    {"events": [ {...event...} ], "session_id": "...", "artifacts": [...]}

...and an individual event dict looks like (real output, captured from a
constructed ``Event`` dumped the way the runtime dumps it)::

    {
      "content": {"parts": [{"text": "Hello "}], "role": "model"},
      "partial": true,
      "invocation_id": "inv-1",
      "author": "agent",
      "actions": {"state_delta": {}, "artifact_delta": {}, ...},
      "node_info": {"path": ""},
      "id": "ab5b0826-...",
      "timestamp": 1788789686.324771
    }

A tool call is a part with ``function_call: {id, name, args}``; a tool
result is a part with ``function_response: {id, name, response}``.

THE PARTIAL/AGGREGATE DOUBLE-COUNT TRAP
---------------------------------------
ADK emits a run of ``partial: true`` events carrying text DELTAS, then a
final non-partial event for the same LLM response carrying the FULL
accumulated text. Naively appending every text you see doubles the answer.
:class:`AdkEventParser` is stateful precisely to absorb that: it tracks the
partial run and, when the aggregate arrives, emits only the genuine
remainder (usually nothing).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# The internal vocabulary
# --------------------------------------------------------------------------
#
# Five words. The renderer knows only these. Everything upstream of this
# module is ADK's problem; everything downstream is Teams' problem. Keeping
# the vocabulary this small is what makes an ADK upgrade a change to ONE
# file.


@dataclass(frozen=True)
class TextChunk:
    """A genuine NEW piece of assistant text.

    Already de-duplicated against the partial/aggregate trap described in
    the module docstring: appending every ``TextChunk`` in order yields the
    complete response exactly once.
    """

    text: str


@dataclass(frozen=True)
class ToolCallStarted:
    """The model asked for a tool. ADR 005's whole reason for existing.

    This is the event that becomes an "Querying BigQuery..." informative
    update in Teams. Without it the slowest part of the turn is a blank
    bubble and the bot looks hung.
    """

    name: str
    call_id: str | None = None
    args: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallFinished:
    """A tool returned. ``ok`` is False for any result we could read as a failure."""

    name: str
    ok: bool = True
    call_id: str | None = None


@dataclass(frozen=True)
class ToolError:
    """A tool failed in a way we must render ourselves (ADR 004).

    ``status`` is the HTTP-ish status we managed to extract ("403", "401",
    "PERMISSION_DENIED", ...). ``resource`` NAMES what was refused, because
    ADR 004 forbids an unattributed denial:

        "a bare 'you don't have access' makes the user open a ticket that
        an admin cannot action"

    ``authorization`` is True for 401/403-class failures, which are the
    ones that MUST be rendered from the fixed template with no model in the
    loop. A 500 from BigQuery is a different animal and gets the transient
    template instead.
    """

    name: str
    status: str
    resource: str
    authorization: bool = True
    detail: str = ""


@dataclass(frozen=True)
class TurnComplete:
    """The runtime says it is done. Advisory only.

    We also treat stream exhaustion as turn completion, because a stream
    that ends without ever setting ``turn_complete`` is a normal thing that
    happens and is not worth failing a turn over.
    """

    reason: str | None = None


ParsedEvent = TextChunk | ToolCallStarted | ToolCallFinished | ToolError | TurnComplete


# --------------------------------------------------------------------------
# Shape-agnostic accessors
# --------------------------------------------------------------------------


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    """Read ``names`` off ``obj`` whether it is a Mapping or an object.

    Tries every name in order, snake_case and camelCase alike, as a mapping
    key first and then as an attribute. Any exception raised by a hostile
    ``__getattr__`` or a pydantic property is swallowed: this function
    cannot fail, it can only return ``default``.
    """
    for name in names:
        try:
            if isinstance(obj, Mapping):
                if name in obj:
                    value = obj[name]
                    if value is not None:
                        return value
                continue
            value = getattr(obj, name, None)
            if value is not None:
                return value
        except Exception:  # pragma: no cover - defensive by design
            logger.debug("shape-drift: could not read %r off %r", name, type(obj))
            continue
    return default


def _as_sequence(value: Any) -> Sequence[Any]:
    """Coerce to a list. A bare item becomes ``[item]``; junk becomes ``[]``."""
    if value is None:
        return []
    if isinstance(value, (str, bytes, Mapping)):
        return [value]
    if isinstance(value, Sequence):
        return list(value)
    try:
        return list(value)
    except TypeError:
        return [value]


def _as_text(value: Any) -> str:
    """Best-effort string. Never raises, never returns None."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return str(value)
    except Exception:  # pragma: no cover - defensive by design
        return ""


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return bool(value)


# --------------------------------------------------------------------------
# Authorization-failure sniffing
# --------------------------------------------------------------------------
#
# ADR 004 requires that a downstream denial is intercepted and rendered from
# a template naming the refused resource. To do that we first have to
# RECOGNISE one, and the runtime does not hand us a tidy status code. A tool
# failure arrives as prose inside a `function_response`, in whatever shape
# the BigQuery MCP server chose that week.
#
# So: look for a denial in several places, and when in doubt do NOT claim it
# is an authorization problem. A false positive here shows the user a
# security message for a network blip, which is its own kind of wrong.

_AUTH_STATUS_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("403", re.compile(r"\b403\b")),
    ("401", re.compile(r"\b401\b")),
    ("PERMISSION_DENIED", re.compile(r"PERMISSION[_ ]DENIED", re.I)),
    ("PERMISSION_DENIED", re.compile(r"\baccess denied\b", re.I)),
    ("PERMISSION_DENIED", re.compile(r"\bpermission denied\b", re.I)),
    ("PERMISSION_DENIED", re.compile(r"does not have permission", re.I)),
    ("PERMISSION_DENIED", re.compile(r"\bforbidden\b", re.I)),
    ("UNAUTHENTICATED", re.compile(r"UNAUTHENTICATED", re.I)),
    ("UNAUTHENTICATED", re.compile(r"\bunauthorized\b", re.I)),
    ("UNAUTHENTICATED", re.compile(r"invalid[_ ]authentication|invalid credentials", re.I)),
)

_ERROR_HINTS = re.compile(r"\b(error|failed|failure|denied|exception)\b", re.I)

# Resource names, most specific first. BigQuery is the only tool in scope
# (ADR: single tool, the managed BigQuery MCP server), so these are tuned
# for BigQuery's several ways of spelling the same table.
_RESOURCE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "Table example-project:sales.orders" / "Dataset example-project:sales"
    re.compile(r"\b(?:Table|Dataset|View|Model|Routine)\s+([A-Za-z0-9\-_]+[:.][A-Za-z0-9\-_.$]+)"),
    # fully-qualified projects/.../datasets/... resource paths
    re.compile(r"\b(projects/[A-Za-z0-9\-_./]+)"),
    # backticked or plain `project.dataset.table`
    re.compile(r"`([A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+(?:\.[A-Za-z0-9\-_]+)?)`"),
    re.compile(r"\b([a-z0-9][a-z0-9\-_]*\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+)\b"),
)

# Extracted last, and only as a hint for the operator: never used as the
# resource name, because "bigquery.tables.getData" is a PERMISSION, and
# ADR 004 wants the RESOURCE.
_ACTION_PATTERN = re.compile(r"\b(bigquery\.[a-z]+\.[a-zA-Z]+)\b")


def classify_authorization_failure(text: str) -> str | None:
    """Return a status string if ``text`` reads as a 401/403, else ``None``."""
    if not text:
        return None
    for status, pattern in _AUTH_STATUS_PATTERNS:
        if pattern.search(text):
            return status
    return None


def extract_resource(text: str, *, fallback: str) -> str:
    """Pull the refused resource out of an error string.

    ADR 004 requires the denial to name what was refused. If we genuinely
    cannot find a resource in the error we return ``fallback`` (the tool
    name) rather than an empty string, because ``errors.downstream_denial``
    refuses to render an unattributed denial and would raise - turning a
    handled denial into an unhandled crash.
    """
    for pattern in _RESOURCE_PATTERNS:
        match = pattern.search(text or "")
        if match:
            return match.group(1)
    return fallback


def extract_action(text: str) -> str | None:
    """Pull an IAM permission out of an error string, if one is present."""
    match = _ACTION_PATTERN.search(text or "")
    return match.group(1) if match else None


def _response_looks_failed(response: Any) -> tuple[bool, str]:
    """Does a ``function_response.response`` payload read as a failure?

    Returns ``(failed, flattened_text)``. The flattened text is what we then
    run the authorization patterns over.
    """
    if response is None:
        return False, ""

    # Common structured shapes first: {"status": "ERROR", "error_details": ...},
    # {"error": {...}}, {"success": false}, {"isError": true} (MCP).
    if isinstance(response, Mapping):
        flat = _as_text(response)
        status = _as_text(_get(response, "status", "state"))
        explicit_error = _get(
            response, "error", "error_details", "errorDetails", "error_message", "errorMessage"
        )
        is_error_flag = _get(response, "isError", "is_error")
        success_flag = _get(response, "success", default=None)

        failed = bool(
            explicit_error
            or _truthy(is_error_flag)
            or (success_flag is not None and not _truthy(success_flag))
            or (status and status.strip().upper() in {"ERROR", "FAILED", "FAILURE", "DENIED"})
        )
        # MCP content blocks sometimes carry the error as prose only.
        if not failed and _ERROR_HINTS.search(flat) and classify_authorization_failure(flat):
            failed = True
        return failed, flat

    flat = _as_text(response)
    return bool(_ERROR_HINTS.search(flat)), flat


# --------------------------------------------------------------------------
# The parser
# --------------------------------------------------------------------------


class AdkEventParser:
    """Turns raw runtime output into :data:`ParsedEvent` values.

    Stateful on purpose: it remembers the run of ``partial`` text events so
    the aggregate event that follows them is not appended a second time. One
    instance per turn.

    Not thread-safe, and does not need to be: a turn is a single coroutine.
    """

    def __init__(self, *, log: logging.Logger | None = None) -> None:
        self._log = log or logger
        self._partial_run: str = ""
        self._seen_calls: dict[str, str] = {}  # call_id -> tool name
        self._unknown_count = 0

    # -- public ---------------------------------------------------------

    @property
    def unknown_event_count(self) -> int:
        """How many raw items we could make no sense of. Useful in logs."""
        return self._unknown_count

    def feed(self, raw: Any) -> list[ParsedEvent]:
        """Parse one item off the stream. NEVER raises.

        One raw item can yield several parsed events (an envelope holds a
        list; a single event's ``content.parts`` can hold text and a
        function call at once), or none at all.
        """
        try:
            return self._feed(raw)
        except Exception:  # pragma: no cover - the whole point of the method
            self._unknown_count += 1
            self._log.debug("unparseable stream item %r; ignoring", type(raw), exc_info=True)
            return []

    # -- internals ------------------------------------------------------

    def _feed(self, raw: Any) -> list[ParsedEvent]:
        out: list[ParsedEvent] = []
        for event in self._unwrap(raw):
            out.extend(self._parse_event(event))
        return out

    def _unwrap(self, raw: Any) -> list[Any]:
        """Peel the ``{"events": [...]}`` envelope, if there is one.

        The runtime wraps each yielded chunk in a ``_StreamingRunResponse``
        dump. Some callers (and every test that hand-rolls a stream) pass
        bare events instead. Accept both, and accept a plain list.
        """
        if raw is None:
            return []
        if isinstance(raw, (str, bytes)):
            # A raw SSE line or a JSON string. Try to decode; if it is not
            # JSON, it is not something we know how to render.
            import json

            try:
                return self._unwrap(json.loads(raw))
            except Exception:
                self._unknown_count += 1
                self._log.debug("stream item was a non-JSON string; ignoring")
                return []
        if isinstance(raw, Mapping):
            events = _get(raw, "events")
            if events is not None:
                return list(_as_sequence(events))
            return [raw]
        if isinstance(raw, Sequence):
            flat: list[Any] = []
            for item in raw:
                flat.extend(self._unwrap(item))
            return flat
        # A pydantic Event object, or something pretending to be one.
        events = _get(raw, "events")
        if events is not None:
            return list(_as_sequence(events))
        return [raw]

    def _parse_event(self, event: Any) -> list[ParsedEvent]:
        out: list[ParsedEvent] = []
        recognised = False

        # 1. Event-level error. `error_code` / `errorCode` is set by ADK when
        #    the model or the flow itself failed. A 403 here is still an ADR
        #    004 denial and must not reach the model.
        error_code = _as_text(_get(event, "error_code", "errorCode"))
        error_message = _as_text(_get(event, "error_message", "errorMessage"))
        if error_code or error_message:
            recognised = True
            blob = f"{error_code} {error_message}".strip()
            status = classify_authorization_failure(blob)
            author = _as_text(_get(event, "author")) or "agent"
            if status:
                out.append(
                    ToolError(
                        name=author,
                        status=status,
                        resource=extract_resource(blob, fallback=author),
                        authorization=True,
                        detail=error_message or error_code,
                    )
                )
            else:
                out.append(
                    ToolError(
                        name=author,
                        status=error_code or "ERROR",
                        resource=extract_resource(blob, fallback=author),
                        authorization=False,
                        detail=error_message or error_code,
                    )
                )

        # 2. content.parts - text, function calls, function responses.
        content = _get(event, "content")
        parts = _as_sequence(_get(content, "parts")) if content is not None else []
        is_partial = _truthy(_get(event, "partial", default=False))

        text_pieces: list[str] = []
        for part in parts:
            handled = False

            call = _get(part, "function_call", "functionCall")
            if call is not None:
                name = _as_text(_get(call, "name")) or "tool"
                call_id = _get(call, "id")
                args = _get(call, "args", "arguments", default={}) or {}
                if not isinstance(args, Mapping):
                    args = {}
                if call_id:
                    self._seen_calls[_as_text(call_id)] = name
                out.append(
                    ToolCallStarted(
                        name=name,
                        call_id=_as_text(call_id) if call_id else None,
                        args=args,
                    )
                )
                handled = True
                recognised = True

            response = _get(part, "function_response", "functionResponse")
            if response is not None:
                out.extend(self._parse_function_response(response))
                handled = True
                recognised = True

            if not handled:
                text = _get(part, "text")
                if text is not None:
                    text_pieces.append(_as_text(text))
                    recognised = True
                else:
                    # inline_data, executable_code, thought signatures,
                    # whatever ADK adds next quarter. Not renderable as
                    # Teams text; drop it and say so at DEBUG.
                    self._log.debug(
                        "ignoring unrenderable content part with keys %r",
                        list(part.keys()) if isinstance(part, Mapping) else type(part),
                    )

        if text_pieces:
            out.extend(self._absorb_text("".join(text_pieces), is_partial=is_partial))

        # 3. turn completion.
        if _truthy(_get(event, "turn_complete", "turnComplete", default=False)):
            recognised = True
            reason = _as_text(
                _get(event, "turn_complete_reason", "turnCompleteReason", "finish_reason", "finishReason")
            )
            out.append(TurnComplete(reason=reason or None))

        if not recognised:
            self._unknown_count += 1
            self._log.debug(
                "unrecognised event (keys=%r); ignoring - this is expected after an "
                "ADK upgrade, see app/streaming/README.md",
                sorted(event.keys()) if isinstance(event, Mapping) else type(event).__name__,
            )

        return out

    def _parse_function_response(self, response: Any) -> list[ParsedEvent]:
        name = _as_text(_get(response, "name")) or "tool"
        call_id = _get(response, "id")
        if not name or name == "tool":
            resolved = self._seen_calls.get(_as_text(call_id)) if call_id else None
            if resolved:
                name = resolved

        payload = _get(response, "response", "result", "output")
        failed, flat = _response_looks_failed(payload)
        if not failed:
            return [
                ToolCallFinished(
                    name=name, ok=True, call_id=_as_text(call_id) if call_id else None
                )
            ]

        status = classify_authorization_failure(flat)
        if status:
            return [
                ToolError(
                    name=name,
                    status=status,
                    resource=extract_resource(flat, fallback=name),
                    authorization=True,
                    detail=flat[:500],
                ),
                ToolCallFinished(
                    name=name, ok=False, call_id=_as_text(call_id) if call_id else None
                ),
            ]
        return [
            ToolError(
                name=name,
                status="ERROR",
                resource=extract_resource(flat, fallback=name),
                authorization=False,
                detail=flat[:500],
            ),
            ToolCallFinished(
                name=name, ok=False, call_id=_as_text(call_id) if call_id else None
            ),
        ]

    def _absorb_text(self, text: str, *, is_partial: bool) -> list[ParsedEvent]:
        """Handle the partial/aggregate double-count trap.

        Partial events carry deltas. The non-partial event that closes a
        streaming run carries the WHOLE text again. Emitting both doubles
        the answer, which is the most visible possible bug and also the
        easiest one to write.
        """
        if not text:
            return []

        if is_partial:
            self._partial_run += text
            return [TextChunk(text)]

        run = self._partial_run
        self._partial_run = ""

        if not run:
            return [TextChunk(text)]
        if text == run:
            self._log.debug("aggregate event repeated the partial run verbatim; dropping")
            return []
        if text.startswith(run):
            remainder = text[len(run) :]
            self._log.debug("aggregate event extended the partial run by %d chars", len(remainder))
            return [TextChunk(remainder)] if remainder else []
        if run.startswith(text):
            # Aggregate is SHORTER than what we already streamed. Teams
            # cannot un-send text, and the cumulative contract forbids
            # shrinking, so keep what we have.
            self._log.debug("aggregate event was a prefix of the partial run; dropping")
            return []
        # No relationship we recognise. Append it: showing the user too much
        # beats silently losing the answer.
        self._log.debug("aggregate event did not extend the partial run; appending verbatim")
        return [TextChunk(text)]


def parse_stream_item(raw: Any) -> list[ParsedEvent]:
    """One-shot parse, for callers that do not need the partial de-duplication.

    Prefer :class:`AdkEventParser` inside a turn; this exists for tests and
    for log-replay tooling.
    """
    return AdkEventParser().feed(raw)


__all__ = [
    "AdkEventParser",
    "ParsedEvent",
    "TextChunk",
    "ToolCallStarted",
    "ToolCallFinished",
    "ToolError",
    "TurnComplete",
    "classify_authorization_failure",
    "extract_resource",
    "extract_action",
    "parse_stream_item",
]
