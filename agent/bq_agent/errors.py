"""Tool-boundary interception of authorization failures. Implements ADR 004.

WHAT THIS FILE IS FOR
================================================================================
ADR 004: when a downstream system denies an established identity, the error is
intercepted AT THE TOOL BOUNDARY and rendered from a template that NAMES the
refused resource. A raw IAM error is never handed to the model to explain,
because a model asked to explain a 403 cannot tell a missing role from a missing
dataset from a network fault, and will produce a fluent, confident, possibly
fabricated reason. And under no circumstances is there a service-account
fallback (ADR 002 / ADR 004 rejected option 3).

THE TWO 403s, AND WHY THE MESSAGE TEXT MUST NEVER BE SWALLOWED
--------------------------------------------------------------------------------
Both arrive as HTTP 403 / PERMISSION_DENIED. They are completely different
problems and only the message text distinguishes them:

  1. A 403 naming a missing ROLE or IAM permission
       "User does not have bigquery.tables.getData permission on ..."
       "Access Denied: Table example-project:teams_bot_demo.orders"
     -> The credential is fine. The user is genuinely who they say they are and
        is genuinely not allowed. This is a PERMISSION FIX (grant a role, or add
        the principal to a row-access policy) and, for the demo, it is often the
        CORRECT and desired outcome: it proves authorization is real.

  2. A 403 (or 401) naming the CREDENTIAL or the PRINCIPAL TYPE
       "Request had invalid authentication credentials"
       "Request had insufficient authentication scopes"
       "The caller does not have permission" with no resource named
       "serviceusage.services.use" (the missing X-Goog-User-Project consumer)
       anything about workforce/external principal type not being supported
     -> The identity plane itself is broken. No amount of BigQuery IAM fixes it.
        This is FATAL for the turn and needs a re-sign-in or a pool/STS fix.

Because the distinction lives ONLY in the text, this module always carries the
verbatim server text through to the operator-facing log and includes a
(bounded) excerpt in the templated message. Classifying is allowed; discarding
the evidence is not.
"""

from __future__ import annotations

import dataclasses
import enum
import logging
import re
from typing import Any, Iterable, Mapping, Optional

from .credentials import AUTHORIZATION_ID, MissingUserCredential

logger = logging.getLogger(__name__)


class DenialKind(enum.Enum):
    """Which of the two 403s (or neither) this is."""

    #: The identity is established; the user lacks a role / row access.
    PERMISSION = "permission"
    #: The credential itself is bad, absent, wrong type, or unscoped.
    CREDENTIAL = "credential"
    #: A denial we could not classify. Fail closed and quote the server.
    UNCLASSIFIED = "unclassified"


# Ordered most-specific first. CREDENTIAL patterns are checked before
# PERMISSION ones because "insufficient authentication scopes" also contains
# words that look permission-ish.
_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b401\b",
        r"UNAUTHENTICATED",
        r"invalid authentication credentials",
        r"insufficient authentication scopes",
        r"request is missing required authentication credential",
        r"invalid[_ ]grant",
        r"token (has been )?(expired|revoked)",
        r"reauthenticat",
        r"credential type",
        r"principal type",
        r"external account",
        r"workforce pool .* (not|cannot)",
        r"serviceusage\.services\.use",
        r"caller does not have permission to use project",
        r"user project",
        r"quota project",
    )
)

_PERMISSION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"access denied",
        r"permission '?[a-z]+\.[a-z.]+'? denied",
        r"does not have [a-z]+\.[a-z.]+ permission",
        r"user does not have permission",
        r"roles/[a-zA-Z.]+",
        r"PERMISSION_DENIED",
        r"\b403\b",
    )
)

_DENIAL_HINTS: tuple[re.Pattern[str], ...] = _CREDENTIAL_PATTERNS + _PERMISSION_PATTERNS

# Resource shapes, most specific first.
_RESOURCE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p)
    for p in (
        r"(?:Table|Dataset|View|Model|Routine)\s+([\w\-]+[:.][\w$]+(?:\.[\w$]+)?)",
        r"\b(projects/[\w\-]+/datasets/[\w$]+(?:/tables/[\w$]+)?)",
        r"\bon (?:table|dataset|resource) ([\w\-]+[:.][\w$]+(?:\.[\w$]+)?)",
        r"`([\w\-]+\.[\w$]+\.[\w$]+)`",
        r"\b([\w\-]+\.[\w$]+\.[\w$]+)\b",
    )
)

_PERMISSION_NAME = re.compile(r"\b((?:bigquery|serviceusage|iam)\.[a-zA-Z.]+)\b")

_MAX_QUOTED_CHARS = 700


@dataclasses.dataclass(frozen=True)
class Denial:
    """A classified authorization failure, with the evidence attached."""

    kind: DenialKind
    resource: Optional[str]
    permission: Optional[str]
    raw_text: str
    tool_name: Optional[str] = None

    @property
    def quoted(self) -> str:
        text = " ".join(self.raw_text.split())
        if len(text) > _MAX_QUOTED_CHARS:
            text = text[:_MAX_QUOTED_CHARS] + " ...[truncated]"
        return text


def looks_like_denial(text: str) -> bool:
    """True if ``text`` looks like a 401/403 from Google, not a generic error."""
    if not text:
        return False
    return any(p.search(text) for p in _DENIAL_HINTS)


def classify(text: str) -> DenialKind:
    """Which of the two 403s this is. Credential patterns win ties."""
    for pattern in _CREDENTIAL_PATTERNS:
        if pattern.search(text):
            return DenialKind.CREDENTIAL
    for pattern in _PERMISSION_PATTERNS:
        if pattern.search(text):
            return DenialKind.PERMISSION
    return DenialKind.UNCLASSIFIED


def extract_resource(text: str, *, fallback_args: Mapping[str, Any] | None = None) -> Optional[str]:
    """Name the refused resource. ADR 004 requires the template to name it.

    Tries the error text first, then the tool arguments (a denied
    ``execute_sql_readonly`` usually still tells us which table was asked for).
    """
    for pattern in _RESOURCE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1)

    if fallback_args:
        query = fallback_args.get("query") or fallback_args.get("statement") or ""
        if isinstance(query, str):
            from_match = re.search(
                r"\bFROM\s+`?([\w\-]+\.[\w$]+\.[\w$]+)`?", query, re.IGNORECASE
            )
            if from_match:
                return from_match.group(1)
        for key in ("tableId", "datasetId", "projectId"):
            value = fallback_args.get(key)
            if isinstance(value, str) and value:
                return value
    return None


def extract_permission(text: str) -> Optional[str]:
    match = _PERMISSION_NAME.search(text)
    return match.group(1) if match else None


def build_denial(
    text: str,
    *,
    tool_name: Optional[str] = None,
    tool_args: Mapping[str, Any] | None = None,
) -> Denial:
    return Denial(
        kind=classify(text),
        resource=extract_resource(text, fallback_args=tool_args),
        permission=extract_permission(text),
        raw_text=text,
        tool_name=tool_name,
    )


# --------------------------------------------------------------------------
# Templates. These strings are what the MODEL sees. They are written to be
# relayed, not interpreted: every one of them tells the model not to explain,
# guess, retry differently, or substitute data.
# --------------------------------------------------------------------------

_RELAY_RULE = (
    "Relay this to the user as-is. Do NOT speculate about the cause, do NOT "
    "invent or estimate the data that was refused, and do NOT retry with a "
    "different table or a different identity."
)


def render(denial: Denial) -> str:
    """The templated, model-facing denial message."""
    resource = denial.resource or "the requested BigQuery resource"

    if denial.kind is DenialKind.PERMISSION:
        head = (
            f"ACCESS DENIED. Your signed-in identity was recognised, but it is "
            f"not authorised to read {resource}."
        )
        if denial.permission:
            head += f" Missing permission: {denial.permission}."
        action = (
            "This is a permissions matter, not a sign-in problem. Signing in "
            "again will not change the answer; an administrator has to grant "
            "access to that resource or add you to its row-access policy."
        )
    elif denial.kind is DenialKind.CREDENTIAL:
        head = (
            "SIGN-IN REQUIRED. Your request could not be authenticated to "
            f"BigQuery, so {resource} was not read."
        )
        action = (
            "This is an identity problem, not a permissions problem. The user "
            "needs to sign in again; no BigQuery grant will fix it."
        )
    else:
        head = (
            f"ACCESS REFUSED. BigQuery refused the request for {resource} and "
            "the refusal could not be classified as either a permissions "
            "problem or a sign-in problem."
        )
        action = (
            "Report it unclassified. Do not guess which of the two it is."
        )

    return (
        f"{head}\n{action}\n"
        f"Verbatim BigQuery response: {denial.quoted}\n"
        f"{_RELAY_RULE}"
    )


def render_missing_credential(detail: str = "") -> str:
    """Template for 'no per-user credential was bound to this invocation'."""
    return (
        "SIGN-IN REQUIRED. No per-user credential was attached to this request, "
        "so no BigQuery query was run. The bot will not fall back to a service "
        "account: every query must run as the signed-in user.\n"
        f"Detail: {detail.strip() or 'credential absent'}\n"
        f"{_RELAY_RULE}"
    )


# --------------------------------------------------------------------------
# The plugin that enforces all of the above at the tool boundary
# --------------------------------------------------------------------------

from google.adk.plugins.base_plugin import BasePlugin  # noqa: E402

#: Tool arguments this agent will not let the model get wrong silently.
#: `execute_sql_readonly` takes CAMELCASE `projectId` and `query`. Verified:
#: passing `project_id`/`statement` returns a bare "Request contains an invalid
#: argument" that names NEITHER field, so the model cannot self-correct from it.
_REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "execute_sql_readonly": ("projectId", "query"),
    "execute_sql": ("projectId", "query"),
    "get_dataset_info": ("projectId", "datasetId"),
    "list_table_ids": ("projectId", "datasetId"),
    "get_table_info": ("projectId", "datasetId", "tableId"),
    "list_dataset_ids": ("projectId",),
}

#: snake_case / plausible-but-wrong aliases the model actually emits, mapped to
#: the camelCase names the MCP server accepts.
_ARG_ALIASES: dict[str, str] = {
    "project_id": "projectId",
    "project": "projectId",
    "dataset_id": "datasetId",
    "dataset": "datasetId",
    "table_id": "tableId",
    "table": "tableId",
    "statement": "query",
    "sql": "query",
    "sql_query": "query",
}


class FailClosedToolPlugin(BasePlugin):
    """Normalises tool arguments and enforces ADR 004 on every tool result.

    Three jobs, in the order they fire:

    1. ``before_tool_callback`` -- repair the model's tool call. The Layer 3
       spike measured Gemini 2.5 Flash omitting the required ``query`` argument
       in ~17% of calls, and the MCP server's reply to a malformed call names no
       field, so the model cannot recover on its own. Here we rename known
       aliases to camelCase, default ``projectId``, and, if a required argument
       is still missing, short-circuit with an explicit corrective message that
       names the missing field. Returning a value from this callback skips the
       tool call, and the model gets another turn -- that IS the retry.

    2. ``on_tool_error_callback`` -- the tool raised. If it is a denial or a
       missing credential, return the template instead of letting the raw
       exception reach the model.

    3. ``after_tool_callback`` -- the tool returned normally but the payload
       carries an error string (which is what MCP does with a BigQuery 403).
       Same interception.
    """

    def __init__(
        self,
        name: str = "fail_closed_tool_plugin",
        *,
        default_project: str = "",
    ) -> None:
        super().__init__(name=name)
        self._default_project = default_project

    # -- 1. argument repair ------------------------------------------------
    async def before_tool_callback(
        self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any
    ) -> Optional[dict]:
        name = getattr(tool, "name", "")
        required = _REQUIRED_ARGS.get(name)
        if required is None:
            return None

        for wrong, right in _ARG_ALIASES.items():
            if wrong in tool_args and right not in tool_args:
                tool_args[right] = tool_args.pop(wrong)
                logger.info("Repaired tool arg %s -> %s for %s", wrong, right, name)

        if "projectId" in required and not tool_args.get("projectId"):
            tool_args["projectId"] = self._default_project

        missing = [key for key in required if not tool_args.get(key)]
        if missing:
            logger.warning("Tool %s called without %s; asking model to retry", name, missing)
            return {
                "error": (
                    f"MALFORMED TOOL CALL: {name} was called without required "
                    f"argument(s) {', '.join(missing)}. No query was run. "
                    f"Call {name} again and include every one of: "
                    f"{', '.join(required)}. Argument names are camelCase "
                    "exactly as listed. Do not answer the user until the tool "
                    "has actually returned data."
                ),
                "retryable": True,
            }
        return None

    # -- 2. tool raised ----------------------------------------------------
    async def on_tool_error_callback(
        self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any, error: Exception
    ) -> Optional[dict]:
        name = getattr(tool, "name", "")

        if isinstance(error, MissingUserCredential):
            logger.error("Fail-closed: %s", error)
            return {"error": render_missing_credential(str(error))}

        text = f"{type(error).__name__}: {error}"
        if looks_like_denial(text):
            denial = build_denial(text, tool_name=name, tool_args=tool_args)
            _log_denial(denial)
            return {"error": render(denial)}
        return None

    # -- 3. tool returned an error payload ---------------------------------
    async def after_tool_callback(
        self, *, tool: Any, tool_args: dict[str, Any], tool_context: Any, result: Any
    ) -> Optional[dict]:
        text = _error_text(result)
        if not text or not looks_like_denial(text):
            return None
        denial = build_denial(
            text, tool_name=getattr(tool, "name", ""), tool_args=tool_args
        )
        _log_denial(denial)
        return {"error": render(denial)}


def _log_denial(denial: Denial) -> None:
    """Operator-facing log. ALWAYS carries the verbatim text."""
    logger.error(
        "TOOL DENIAL kind=%s tool=%s resource=%s permission=%s verbatim=%r",
        denial.kind.value,
        denial.tool_name,
        denial.resource,
        denial.permission,
        denial.quoted,
    )
    if denial.kind is DenialKind.CREDENTIAL:
        logger.error(
            "  -> identity plane. Check the STS exchange, the workforce pool "
            "provider, roles/serviceusage.serviceUsageConsumer on the pool "
            "principal, and that authorizations[%r] was sent on the request.",
            AUTHORIZATION_ID,
        )
    elif denial.kind is DenialKind.PERMISSION:
        logger.error(
            "  -> IAM/row-access. Grant on the resource, or accept it: for the "
            "demo this is often the correct, intended refusal."
        )


def _error_text(result: Any) -> str:
    """Pull any error-ish text out of an MCP tool result, whatever its shape."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    if isinstance(result, Mapping):
        chunks: list[str] = []
        for key in ("error", "message", "detail", "details", "status", "content"):
            value = result.get(key)
            if value:
                chunks.append(_error_text(value) if not isinstance(value, str) else value)
        if not chunks and result.get("isError"):
            chunks.append(str(result))
        return "\n".join(chunks)
    if isinstance(result, Iterable) and not isinstance(result, (bytes, bytearray)):
        return "\n".join(_error_text(item) for item in result)
    text = getattr(result, "text", None)
    if isinstance(text, str):
        return text
    content = getattr(result, "content", None)
    if content is not None:
        collected = _error_text(content)
        if getattr(result, "isError", False) and not collected:
            collected = str(result)
        return collected
    return ""
