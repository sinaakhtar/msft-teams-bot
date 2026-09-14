"""The loop: ADK events in, Teams streamed message out, final text returned.

This module is deliberately boring. It buffers, it counts, it calls the
sink. It contains NO prompt logic and makes NO model calls, because the Bot
Middle Tier is not allowed to have any: the renderer formats, it does not
reason about content.

Three things it MUST get right, in order of how badly they break:

1. **Cumulative text.** Every content update carries the entire response so
   far. See the shouting at the top of ``teams_sink.py``. The buffer here is
   the reason that is possible, and ADR 005 accepted holding it in memory
   for the duration of a turn.

2. **Informative updates on tool start.** ADR 005 chose
   ``streaming_agent_run_with_events`` over ``stream_query`` specifically so
   that tool activity is visible:

       "Tool activity is visible, so Teams can show progress during the slow
       part of a turn, which is also the part worth demonstrating."

   A BigQuery round trip is the slowest thing in a turn. Without an
   informative update the bubble sits empty and the bot reads as hung. This
   is the payoff for the entire ADR, not decoration - if you are tempted to
   strip it to simplify the loop, you have just reverted to ``stream_query``
   with extra steps.

3. **ADR 004 denials.** If a tool event carries a 401/403, the user gets the
   FIXED TEMPLATE naming the refused resource, and the model's own words are
   discarded. Never a paraphrase, never a service-account retry.

On the last point, the reasoning is worth repeating because the shortcut is
so tempting. ADR 004:

    "A language model asked to explain an authorization error has no way to
    distinguish a missing role from a missing dataset from a network fault,
    and will produce a fluent, confident, and possibly fabricated reason."

So when a denial is seen, whatever prose the model had already streamed is
REPLACED by the template. Appending the template after the model's
explanation would still leave the model's explanation on screen, which is
the thing ADR 004 forbids. Set ``replace_text_on_denial=False`` only if you
have a specific reason and have re-read the ADR.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Callable, Mapping

from .. import errors as error_templates
from .events import (
    AdkEventParser,
    TextChunk,
    ToolCallFinished,
    ToolCallStarted,
    ToolError,
    TurnComplete,
)
from .teams_sink import CumulativeContractViolation, TeamsSink

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Informative-update copy
# --------------------------------------------------------------------------
#
# Fixed strings, not model output. The middle tier has no prompt logic, so
# these are a lookup table and a fallback, nothing cleverer.
#
# The single tool in scope is BigQuery via the managed MCP server, so
# `execute_sql_readonly` and its siblings are the names that will actually
# show up. Unknown tools get a readable fallback derived from the name
# rather than a blank bubble.

DEFAULT_TOOL_LABELS: Mapping[str, str] = {
    "execute_sql_readonly": "Querying BigQuery...",
    "execute_sql": "Querying BigQuery...",
    "list_dataset_ids": "Looking up BigQuery datasets...",
    "list_table_ids": "Looking up BigQuery tables...",
    "get_dataset_info": "Reading BigQuery dataset metadata...",
    "get_table_info": "Reading BigQuery table schema...",
    "search_catalog": "Searching the BigQuery catalog...",
    "forecast": "Running a BigQuery forecast...",
    "analyze_contribution": "Running a BigQuery contribution analysis...",
    "ask_data_insights": "Asking BigQuery for insights...",
}

WORKING_LABEL = "Working on it..."
COMPOSING_LABEL = "Got the data. Composing the answer..."


def default_label_for(name: str) -> str:
    """Human-readable progress line for a tool we have no copy for."""
    if not name:
        return WORKING_LABEL
    pretty = name.replace("_", " ").strip()
    if not pretty:
        return WORKING_LABEL
    return f"Running {pretty}..."


class TeamsStreamingRenderer:
    """Consumes an ADK event stream and drives a :class:`TeamsSink`.

    Satisfies the interface contract other components build against::

        async def render(self, events: AsyncIterator[Any],
                         sink: TeamsSink) -> str

    One instance per turn is the intended usage (the parser it owns is
    stateful), though :meth:`render` constructs a fresh parser each call so
    reuse is merely wasteful rather than wrong.

    NOTE ON ``app.ports.StreamingRenderer``: ``ports.py`` declares an older
    push-style protocol (``begin`` / ``push`` / ``finish``). The pull-style
    ``render`` signature above is the one this component was specified
    against and the one other components consume. The two are reconcilable
    with a thin adapter if the push style is ever needed; that adapter is
    deliberately not written yet, and the divergence is recorded in NOTES.md
    rather than papered over.
    """

    def __init__(
        self,
        *,
        tool_labels: Mapping[str, str] | None = None,
        label_for: Callable[[str], str] | None = None,
        replace_text_on_denial: bool = True,
        announce_tool_completion: bool = True,
        request_id: str | None = None,
        user_display: str | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self._labels = dict(DEFAULT_TOOL_LABELS)
        if tool_labels:
            self._labels.update(tool_labels)
        self._label_for = label_for or default_label_for
        self._replace_text_on_denial = replace_text_on_denial
        self._announce_tool_completion = announce_tool_completion
        self._request_id = request_id
        self._user_display = user_display
        self._log = log or logger

    # -- public ---------------------------------------------------------

    def label(self, tool_name: str) -> str:
        """The informative-update text for a tool. Fixed copy, never model output."""
        return self._labels.get(tool_name) or self._label_for(tool_name)

    async def render(self, events: AsyncIterator[Any], sink: TeamsSink) -> str:
        """Drive the stream to completion and return the final text.

        Returns the exact text sent in the terminating ``message`` activity.
        For a normal turn that is the concatenation of every text chunk. For
        a denied turn it is the ADR 004 template.

        Never raises for anything the STREAM did. A stream that dies
        mid-turn still produces a terminating Teams message, because leaving
        a stream open leaves the user staring at a half-written bubble
        forever.
        """
        parser = AdkEventParser(log=self._log)
        buffer: list[str] = []
        denial: ToolError | None = None
        failure: ToolError | None = None
        stream_error: BaseException | None = None
        saw_turn_complete = False
        tools_started = 0

        try:
            async for raw in events:
                for item in parser.feed(raw):
                    if isinstance(item, TextChunk):
                        if not item.text:
                            continue
                        buffer.append(item.text)
                        # CUMULATIVE: the whole buffer, every time.
                        await self._safe_content(sink, "".join(buffer))

                    elif isinstance(item, ToolCallStarted):
                        tools_started += 1
                        # The ADR 005 payoff. Do not remove.
                        await self._safe_informative(sink, self.label(item.name))

                    elif isinstance(item, ToolCallFinished):
                        if (
                            self._announce_tool_completion
                            and item.ok
                            and not buffer
                        ):
                            # Only useful before content starts; afterwards
                            # informative updates are invisible anyway.
                            await self._safe_informative(sink, COMPOSING_LABEL)

                    elif isinstance(item, ToolError):
                        if item.authorization:
                            if denial is None:
                                denial = item
                                self._log.info(
                                    "ADR 004 denial intercepted: tool=%s status=%s resource=%s",
                                    item.name,
                                    item.status,
                                    item.resource,
                                )
                        elif failure is None:
                            failure = item
                            self._log.warning(
                                "tool failure: tool=%s status=%s detail=%s",
                                item.name,
                                item.status,
                                item.detail[:200],
                            )

                    elif isinstance(item, TurnComplete):
                        saw_turn_complete = True
                        self._log.debug("turn complete (reason=%s)", item.reason)

        except CumulativeContractViolation:
            # A bug in THIS file, not a failure of the stream. Let it out
            # loudly instead of dressing it up as a transient backend error;
            # the tests that guard the cumulative contract depend on this.
            raise
        except BaseException as exc:  # noqa: BLE001 - see docstring
            # Includes asyncio.CancelledError: even a cancelled turn should
            # close its Teams stream rather than abandon an open bubble.
            stream_error = exc
            self._log.warning("event stream failed mid-turn: %r", exc)

        final_text = self._compose_final(
            buffer=buffer,
            denial=denial,
            failure=failure,
            stream_error=stream_error,
        )

        try:
            await sink.final(final_text)
        except Exception:
            # The answer is lost to the user if this fails, so it is worth a
            # loud log, but the caller still gets the text back and can
            # decide what to do with it.
            self._log.exception("failed to send the terminating Teams message")

        self._log.debug(
            "turn rendered: chunks=%d tools=%d unknown_events=%d turn_complete=%s denial=%s",
            len(buffer),
            tools_started,
            parser.unknown_event_count,
            saw_turn_complete,
            bool(denial),
        )

        if isinstance(stream_error, BaseException) and not isinstance(
            stream_error, Exception
        ):
            # A cancellation was swallowed to close the stream cleanly; do
            # not silently convert it into a normal return.
            raise stream_error

        return final_text

    # -- internals ------------------------------------------------------

    def _compose_final(
        self,
        *,
        buffer: list[str],
        denial: ToolError | None,
        failure: ToolError | None,
        stream_error: BaseException | None,
    ) -> str:
        model_text = "".join(buffer)

        if denial is not None:
            template = self._denial_text(denial)
            if self._replace_text_on_denial:
                # ADR 004: the model does not get to explain a refusal, and
                # that includes explaining it in text it already streamed.
                return template
            return f"{model_text}\n\n{template}" if model_text else template

        # An authorization failure surfaced as a raised port error rather
        # than as an event: same ADR 004 treatment.
        raised_denial = self._denial_from_exception(stream_error)
        if raised_denial is not None:
            return raised_denial

        if model_text:
            # A non-authorization tool failure is not fatal if the model
            # still produced an answer around it; trust the answer.
            return model_text

        if failure is not None:
            return self._transient_text()

        if stream_error is not None:
            return self._transient_text()

        # Nothing at all. Better an honest empty-handed message than an
        # empty bubble that never resolves.
        return self._transient_text()

    def _denial_text(self, denial: ToolError) -> str:
        """Render the ADR 004 template. Single source of truth: ``app.errors``."""
        from .events import extract_action

        try:
            activity = error_templates.downstream_denial(
                resource=denial.resource or denial.name or "the requested resource",
                action=extract_action(denial.detail),
                user_display=self._user_display,
                request_id=self._request_id,
            )
            return str(activity.get("text", ""))
        except Exception:
            self._log.exception("denial template failed to render; using minimal fallback")
            resource = denial.resource or denial.name or "the requested resource"
            return (
                "**Access denied - I stopped here rather than working around it.**\n\n"
                f"Your account is not permitted to use `{resource}`. I did not "
                "retry under a service account."
            )

    def _denial_from_exception(self, exc: BaseException | None) -> str | None:
        if exc is None:
            return None
        try:
            from ..ports import AuthorizationDenied
        except Exception:  # pragma: no cover - import guard only
            return None
        if not isinstance(exc, AuthorizationDenied):
            return None
        resource = getattr(exc, "resource", "") or "the requested resource"
        detail = getattr(exc, "detail", "") or ""
        return self._denial_text(
            ToolError(
                name="runtime",
                status="403",
                resource=str(resource),
                authorization=True,
                detail=str(detail),
            )
        )

    def _transient_text(self) -> str:
        try:
            activity = error_templates.transient_failure(request_id=self._request_id)
            return str(activity.get("text", ""))
        except Exception:  # pragma: no cover - template is static
            self._log.exception("transient template failed to render")
            return (
                "**Something on my side failed - this is not a permissions "
                "problem.** Please try again."
            )

    async def _safe_informative(self, sink: TeamsSink, text: str) -> None:
        """A failed progress indicator must never cost the user their answer."""
        try:
            await sink.informative(text)
        except CumulativeContractViolation:
            raise
        except Exception:
            self._log.warning("informative update failed; continuing", exc_info=True)

    async def _safe_content(self, sink: TeamsSink, cumulative_text: str) -> None:
        try:
            await sink.content(cumulative_text)
        except CumulativeContractViolation:
            # NOT a transport failure. This is the middle tier violating the
            # Teams contract, i.e. a bug in this file, and swallowing it
            # would hide the exact regression the tests exist to catch.
            raise
        except Exception:
            # The protocol is cumulative, so a dropped intermediate update
            # costs nothing: the next one, and the final message, still
            # carry the whole answer.
            self._log.warning("content update failed; continuing", exc_info=True)


__all__ = [
    "TeamsStreamingRenderer",
    "DEFAULT_TOOL_LABELS",
    "default_label_for",
    "WORKING_LABEL",
    "COMPOSING_LABEL",
]
