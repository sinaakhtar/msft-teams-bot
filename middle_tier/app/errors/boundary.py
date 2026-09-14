"""The tool boundary. Where the model's view is deliberately narrowed.

THE ASYMMETRY, STATED ONCE
--------------------------
When a tool call is refused, three parties learn about it and they learn
different amounts. That is not an accident of implementation; it is the ADR.

    party   | what it gets                                   | why
    --------+------------------------------------------------+------------------
    log     | EVERYTHING: full raw upstream text, status,    | ADR 004: "Log the
            | stage, resource, request id, classifier        | full text."
            | confidence                                     |
    user    | the ADR 004 template, naming the resource      | actionable, fixed
    model   | ONE sentence: "Access denied to <resource>."   | it cannot be
            |                                                | trusted to explain

The model's line is the narrowest of the three on purpose. A language model
asked to explain an authorization error cannot distinguish a missing role from
a non-existent table from a typo, so it invents a plausible cause, and a
confident wrong explanation costs more than a missing suggestion. It also must
never see the raw text, which routinely carries principals, project ids,
console URLs and occasionally tokens.

:func:`model_facing` is the ONLY sanctioned way to produce the model's string,
and :func:`_assert_no_leak` re-checks the result against the raw text before it
is handed over. The guard is cheap and catches the realistic regression: a
future edit that "helpfully" appends the upstream detail.

WHAT THIS MODULE WILL NOT DO
----------------------------
It will not retry a refused call under any other identity. There is no code
path here that produces a credential, which is why
:func:`acquire_credential_or_refuse` returns ``None`` for the token on every
failure. ADR 004 records the service-account fallback as explicitly rejected;
the tool boundary is precisely where someone who has not read ADR 002 would
add it as "the obvious fix".
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Final, Mapping, TypeVar

from . import templates
from .classify import classify_exception
from .taxonomy import (
    DownstreamAuthorizationDenied,
    IdentityAcquisitionError,
    MiddleTierError,
    MissingEntraObjectId,
    UpstreamUnavailable,
)

__all__ = [
    "BoundaryOutcome",
    "ModelContextLeak",
    "MODEL_DENIAL_TEMPLATE",
    "MODEL_UPSTREAM_TEMPLATE",
    "model_facing",
    "intercept",
    "guard_tool_call",
    "acquire_credential_or_refuse",
]

logger = logging.getLogger("app.errors.boundary")

T = TypeVar("T")

#: The complete set of words the model is given about a denial. One fact:
#: access was denied, and to what. Interpolation is the resource name only.
MODEL_DENIAL_TEMPLATE: Final[str] = "Access denied to {resource}."

#: Backend failure. Also one fact, and pointedly not worded as a permission
#: problem so the model does not narrate one.
MODEL_UPSTREAM_TEMPLATE: Final[str] = "The tool call failed for a non-permission reason and returned no data."


class ModelContextLeak(RuntimeError):
    """Raised when text bound for the model contains raw upstream error text.

    This is a programming error, not a runtime condition: it means someone
    widened the model's view past what ADR 004 allows. It fails loudly rather
    than silently shipping an IAM error to an inference endpoint.
    """


@dataclass(frozen=True)
class BoundaryOutcome:
    """What the boundary decided, split by audience.

    :param error: the classified error. Full detail lives here and in the log.
    :param user_activity: the rendered ADR 004 template. Bot Framework shape.
    :param model_message: the narrow sentence for the model, or ``None`` when
        the model is not to be invoked at all (identity failures end the turn
        before any model call).
    :param refused: True when the whole turn is refused rather than the single
        tool call. Identity failures refuse; downstream denials do not
        necessarily have to, since the model may still respond around a single
        refused tool.
    :param log_fields: everything, including the raw upstream text.
    """

    error: MiddleTierError
    user_activity: dict[str, Any]
    model_message: str | None
    refused: bool
    log_level: int
    log_fields: Mapping[str, Any] = field(default_factory=dict)

    # Deliberately absent: any attribute that could hold a credential, a token,
    # or a "retry as" principal. See the module docstring.

    @property
    def credential(self) -> None:
        """Always ``None``. Present so the absence is explicit and testable."""
        return None


# ==========================================================================
# The model's view
# ==========================================================================

_URL_RE: Final[re.Pattern[str]] = re.compile(r"https?://\S+")
_ROLE_RE: Final[re.Pattern[str]] = re.compile(r"roles/[A-Za-z0-9_.\-]+")


def _shingles(text: str, size: int = 6) -> set[str]:
    words = re.findall(r"\S+", text)
    return {" ".join(words[i : i + size]) for i in range(0, max(0, len(words) - size + 1))}


def _assert_no_leak(model_text: str, raw: str, *, allowed: tuple[str, ...] = ()) -> None:
    """Fail if raw upstream text made it into the model's string.

    Checks three things, cheapest first: the whole raw string, any URL or role
    name from the raw text, and any six-word run of it. Values in ``allowed``
    (the resource name, which the model is explicitly permitted to know) are
    removed from the candidate text before checking.
    """
    if not raw:
        return
    probe = model_text
    for permitted in allowed:
        if permitted:
            probe = probe.replace(permitted, " ")

    if raw.strip() and raw.strip() in probe:
        raise ModelContextLeak("raw upstream error text reached the model context")
    for pattern in (_URL_RE, _ROLE_RE):
        for token in pattern.findall(raw):
            if token in probe:
                raise ModelContextLeak(f"upstream detail {token!r} reached the model context")
    overlap = _shingles(raw) & _shingles(probe)
    if overlap:
        raise ModelContextLeak(
            f"upstream error text fragment reached the model context: {sorted(overlap)[0]!r}"
        )


def model_facing(error: MiddleTierError) -> str | None:
    """The ONLY sanctioned string for the model. Narrow by construction.

    * Downstream denial -> ``"Access denied to <resource>."`` and nothing else.
    * Upstream failure   -> a non-permission failure notice with no detail.
    * Identity failure   -> ``None``. There is no model turn: the middle tier
      refuses before the model is invoked, because a model asked to handle
      "we do not know who you are" has nothing useful to contribute and every
      opportunity to reassure the user that it does.
    """
    if isinstance(error, IdentityAcquisitionError):
        return None

    if isinstance(error, DownstreamAuthorizationDenied):
        text = MODEL_DENIAL_TEMPLATE.format(resource=error.resource)
        _assert_no_leak(text, error.raw_message, allowed=(error.resource,))
        return text

    if isinstance(error, UpstreamUnavailable):
        text = MODEL_UPSTREAM_TEMPLATE
        _assert_no_leak(text, error.detail)
        return text

    return MODEL_UPSTREAM_TEMPLATE


# ==========================================================================
# Interception
# ==========================================================================


def _log_level_for(error: MiddleTierError) -> int:
    if isinstance(error, IdentityAcquisitionError) and error.design_fatal:
        # A 403 that named the credential or principal type. If this ever
        # fires in production the workforce-identity design is wrong, and it
        # must not be discoverable only by reading a WARNING among thousands.
        return logging.CRITICAL
    if isinstance(error, DownstreamAuthorizationDenied):
        # An unrecognised 403 shape: a human needs to read the text the
        # classifier could not place, before the two-403s distinction rots.
        return logging.ERROR if not error.confidently_classified else logging.WARNING
    if isinstance(error, UpstreamUnavailable):
        return logging.ERROR
    return logging.WARNING


def _emit_log(error: MiddleTierError, level: int, extra: Mapping[str, Any]) -> None:
    """Log everything, with the raw text intact.

    Uses ``app.logging_utils.log_event`` when it is importable (it redacts
    tokens and emits structured JSON) and falls back to stdlib logging
    otherwise, so this module never fails to import because a sibling is
    mid-refactor.
    """
    fields = {**error.log_fields(), **extra}
    try:  # pragma: no cover - exercised only when the sibling module exists
        from ..logging_utils import log_event  # type: ignore

        log_event(logger, level, f"tool boundary: {type(error).__name__}", **fields)
    except Exception:  # pragma: no cover - fallback path
        logger.log(level, "tool boundary: %s %s", type(error).__name__, fields)


def intercept(
    exc: Exception,
    *,
    resource: str | None = None,
    action: str | None = None,
    request_id: str | None = None,
    signin_url: str | None = None,
    support_contact: str | None = None,
    connection_name: str | None = None,
    user_display: str | None = None,
) -> BoundaryOutcome:
    """Catch a downstream failure at the tool boundary and split the audiences.

    This is the function ADR 004 path 2 describes. Everything it knows goes to
    the log; the user gets the template; the model gets one sentence.

    :param resource: what the tool was reaching for. Used when the upstream
        text does not name a resource itself, which is common for
        project-level and API-enablement denials.
    """
    error = classify_exception(exc, resource_hint=resource, action_hint=action, request_id=request_id)

    level = _log_level_for(error)
    _emit_log(error, level, {"tool_resource_hint": resource, "tool_action_hint": action})

    user_activity = templates.render(
        error,
        signin_url=signin_url,
        support_contact=support_contact,
        connection_name=connection_name,
        user_display=user_display,
    )
    message_for_model = model_facing(error)

    return BoundaryOutcome(
        error=error,
        user_activity=user_activity,
        model_message=message_for_model,
        # Identity failures end the turn. A single denied tool does not, by
        # itself, end a conversation.
        refused=isinstance(error, IdentityAcquisitionError),
        log_level=level,
        log_fields=error.log_fields(),
    )


async def guard_tool_call(
    call: Callable[[], Awaitable[T]],
    *,
    resource: str,
    action: str | None = None,
    request_id: str | None = None,
    **render_kwargs: Any,
) -> tuple[T | None, BoundaryOutcome | None]:
    """Run a tool call inside the boundary.

    :returns: ``(result, None)`` on success, ``(None, outcome)`` on failure.
        There is no third case, and in particular no "retried it another way"
        case: on failure the tool produced no data and the outcome says so.
    """
    try:
        return await call(), None
    except Exception as exc:  # noqa: BLE001 - the boundary is the point
        return None, intercept(
            exc, resource=resource, action=action, request_id=request_id, **render_kwargs
        )


# ==========================================================================
# Identity acquisition (ADR 004 path 1)
# ==========================================================================


async def acquire_credential_or_refuse(
    acquire: Callable[[], Awaitable[str]],
    *,
    signin_url: str | None = None,
    support_contact: str | None = None,
    connection_name: str | None = None,
) -> tuple[str | None, BoundaryOutcome | None]:
    """Acquire the user's Google credential, or refuse the turn. No third option.

    :returns: ``(token, None)`` when the broker returned the user's own
        credential, ``(None, outcome)`` when it did not.

    The ``None`` in the failure case is load-bearing. There is no branch in
    this function that reaches for ambient credentials, the metadata server, a
    key file, or an impersonated principal. ADR 004 records the
    service-account fallback as explicitly rejected and predicts it will be
    proposed here, as the obvious expedient fix, by someone who has not read
    ADR 002. It stays rejected: falling back would answer the user with data
    they may have no right to see, under an identity that is not theirs, which
    is the precise failure the whole design exists to prevent.
    """
    try:
        token = await acquire()
    except Exception as exc:  # noqa: BLE001
        from .taxonomy import from_identity_error

        error = (
            exc
            if isinstance(exc, IdentityAcquisitionError)
            else from_identity_error(exc)
            if _looks_like_identity_failure(exc)
            else classify_exception(exc)
        )
        level = _log_level_for(error)
        _emit_log(error, level, {"phase": "identity_acquisition"})
        user_activity = templates.render(
            error,
            signin_url=signin_url,
            support_contact=support_contact,
            connection_name=connection_name,
        )
        return None, BoundaryOutcome(
            error=error,
            user_activity=user_activity,
            model_message=model_facing(error),
            refused=True,
            log_level=level,
            log_fields=error.log_fields(),
        )

    if not token:
        # A broker that returns nothing is a broker that failed; serving the
        # turn with an empty credential would be a silent downgrade.
        error = IdentityAcquisitionError(
            stage="sts", reason_code="empty_credential", detail="broker returned no token"
        )
        _emit_log(error, logging.ERROR, {"phase": "identity_acquisition"})
        return None, BoundaryOutcome(
            error=error,
            user_activity=templates.render(
                error, signin_url=signin_url, support_contact=support_contact
            ),
            model_message=None,
            refused=True,
            log_level=logging.ERROR,
            log_fields=error.log_fields(),
        )

    return token, None


def _looks_like_identity_failure(exc: Exception) -> bool:
    """Duck-typed: does this exception come from the identity broker?

    ``app.identity.errors`` is owned by another component and is in flux, so we
    match on shape (a ``stage`` attribute, or a class name in its hierarchy)
    rather than importing it and coupling importability of the error layer to
    the current state of that module.
    """
    if isinstance(exc, (IdentityAcquisitionError, MissingEntraObjectId)):
        return True
    if hasattr(exc, "stage"):
        return True
    name = type(exc).__name__
    return name.startswith(("Obo", "Sts", "Identity", "MissingEntra", "Precondition"))
