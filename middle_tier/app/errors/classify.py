"""Classify a downstream failure from status code AND message text.

THE TWO-403s DISTINCTION
------------------------
Both authorization failures observed in the earlier spike were HTTP 403 and
they meant completely different things. The status code carries none of that
information. **The message text is the only signal.**

  * A 403 naming a **missing role** is an ordinary permission fix. The
    verified example, quoted in ADR 004 and kept below as
    :data:`SERVICE_USAGE_CONSUMER_403`::

        Caller does not have required permission to use project example-project.
        Grant the caller the roles/serviceusage.serviceUsageConsumer role...

    This is ADR 004 path 2: intercept at the tool boundary, name the resource,
    say nothing else.

  * A 403 naming the **credential or principal type** would have been fatal to
    the design. It would mean Google had rejected the workforce-pool principal
    as a kind of caller, not this caller's access to a thing. No role grant
    fixes it, the sign-in loop does not fix it, and quietly rendering it as
    "you lack permission to X" would send an admin hunting for a role that was
    never the problem. It is classified as an identity-plane failure and
    flagged ``design_fatal`` so it is logged at CRITICAL.

PRECEDENCE, AND WHY IT IS THIS WAY ROUND
----------------------------------------
Real missing-role messages sometimes mention a service account in passing
("the service account foo@... needs roles/x"), so a naive "does the text
contain 'service account'" check misfires. The strong role-grant signatures
(``Grant the caller the roles/...``, ``does not have required permission``)
are therefore tested FIRST, and the credential/principal-type patterns are
tested only on text that did not match one. Both orders are covered by tests.

NOTHING IS SWALLOWED
--------------------
Every value this module returns carries the full upstream text in a log-only
field (``raw_message`` / ``detail``). ADR 004: log the full text, render the
template. A classifier that discarded the text would make the two-403s
distinction unauditable after the fact.
"""

from __future__ import annotations

import re
from typing import Any, Final, Mapping, Pattern

from .taxonomy import (
    UNNAMED_RESOURCE,
    DownstreamAuthorizationDenied,
    IdentityAcquisitionError,
    IdentityStage,
    MiddleTierError,
    UpstreamUnavailable,
)

__all__ = [
    "classify",
    "classify_exception",
    "extract_resource",
    "extract_named_role",
    "SERVICE_USAGE_CONSUMER_403",
    "BIGQUERY_TABLE_DENIED_403",
    "BIGQUERY_DATASET_DENIED_403",
    "CREDENTIAL_TYPE_403_SYNTHETIC",
    "MIXED_ROLE_AND_SERVICE_ACCOUNT_403_SYNTHETIC",
]


# ==========================================================================
# Fixtures
# ==========================================================================

#: VERIFIED. Observed live during the identity spike against project
#: ``example-project``. The load-bearing part - everything up to and including
#: ``Grant the caller the roles/serviceusage.serviceUsageConsumer role`` - is
#: verbatim as quoted in ADR 004. The trailing sentence is Google's standard
#: boilerplate continuation of that message and is NOT load-bearing for
#: classification; no pattern below depends on it.
SERVICE_USAGE_CONSUMER_403: Final[str] = (
    "Caller does not have required permission to use project example-project. "
    "Grant the caller the roles/serviceusage.serviceUsageConsumer role, or a "
    "custom role with the serviceusage.services.use permission, by visiting "
    "https://console.developers.google.com/iam-admin/iam/project?project=example-project "
    "and then retry. Propagation of the new permission may take a few minutes."
)

#: SYNTHETIC (standard BigQuery wording, not captured in the spike). Used only
#: to check that a table-scoped denial still classifies as a missing-role
#: permission problem and that the table name is extracted.
BIGQUERY_TABLE_DENIED_403: Final[str] = (
    "Access Denied: Table example-project:sales.orders: User does not have "
    "permission to query table example-project:sales.orders, or perhaps it does "
    "not exist."
)

#: SYNTHETIC (standard BigQuery wording).
BIGQUERY_DATASET_DENIED_403: Final[str] = (
    "Access Denied: Dataset example-project:sales: Permission bigquery.tables.list "
    "denied on dataset example-project:sales (or it may not exist)."
)

#: SYNTHETIC AND NEVER OBSERVED. This is the shape that would have been fatal
#: to the design: Google rejecting the *kind* of credential rather than this
#: caller's access to a resource. Kept as a fixture so the classifier's
#: handling of it is tested, NOT as evidence that it occurs.
CREDENTIAL_TYPE_403_SYNTHETIC: Final[str] = (
    "Request had invalid authentication credentials. The credential type "
    "'external_account' is not supported for this API. Expected a service "
    "account or end user credential."
)

#: SYNTHETIC. The precedence trap: a genuine missing-role message that also
#: mentions a service account. Must classify as a permission problem, not as
#: a credential-type rejection.
MIXED_ROLE_AND_SERVICE_ACCOUNT_403_SYNTHETIC: Final[str] = (
    "Caller does not have required permission to use project example-project. "
    "Grant the caller the roles/serviceusage.serviceUsageConsumer role. The "
    "service account example-project@appspot.gserviceaccount.com already has it."
)


# ==========================================================================
# Signatures
# ==========================================================================

def _compile(patterns: list[str]) -> tuple[Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


#: Unambiguous "you are missing a role" wording. Tested first; a match here
#: short-circuits the credential-type check.
_STRONG_MISSING_ROLE: Final = _compile(
    [
        r"grant the caller the roles/",
        r"does not have required permission",
        r"\bgrant\b[^.]{0,60}\broles/[a-z0-9_.\-]+",
    ]
)

#: Weaker permission wording. Still a permission problem, but generic enough
#: that the credential-type check runs first.
_WEAK_MISSING_ROLE: Final = _compile(
    [
        r"permission\s+'?[a-z][a-z0-9]*\.[a-zA-Z0-9.]+'?\s+denied",
        r"user does not have permission",
        r"does not have [a-z][a-z0-9]*\.[a-zA-Z0-9.]+ permission",
        r"caller does not have permission",
        r"access denied:",
        r"the user does not have access",
        r"permission denied on resource",
        r"iam_permission_denied",
    ]
)

#: The shape that would invalidate the design: the principal or credential
#: TYPE was refused, not this principal's access to a resource.
_CREDENTIAL_TYPE_REJECTED: Final = _compile(
    [
        r"credential type",
        r"credential_type",
        r"access_token_type_unsupported",
        r"principal type",
        r"principal_type",
        r"is not supported for this api",
        r"not allowed to impersonate",
        r"external_account.{0,40}not (supported|allowed)",
        r"workforce (pool|identity).{0,40}(disabled|not (allowed|permitted|supported))",
        r"expected a service account",
        r"service account credentials are required",
        r"reauthentication is needed",
    ]
)

#: 400/401 wording that means the identity exchange itself failed.
_IDENTITY_EXCHANGE_FAILURE: Final = _compile(
    [
        r"invalid_grant",
        r"invalid_client",
        r"invalid subject token",
        r"subject token",
        r"assertion",
        r"aadsts\d+",
        r"consent",
        r"audience",
        r"token exchange",
        r"invalid authentication credentials",
        r"unauthenticated",
    ]
)

_STS_HINTS: Final = _compile([r"\bsts\b", r"securetoken", r"token exchange", r"workforce", r"external_account", r"subject token"])
_OBO_HINTS: Final = _compile([r"aadsts\d+", r"on-behalf-of", r"\bobo\b", r"login\.microsoftonline\.com", r"consent"])


def _matches(patterns: tuple[Pattern[str], ...], text: str) -> str | None:
    for pattern in patterns:
        found = pattern.search(text)
        if found:
            return pattern.pattern
    return None


# ==========================================================================
# Extraction (regex over the text, never inference)
# ==========================================================================

_RESOURCE_PATTERNS: Final = _compile(
    [
        r"permission to use project ([A-Za-z0-9\-_.]+)",
        r"Access Denied: (?:Table|Dataset|Project|View|Routine|Model) ([A-Za-z0-9\-_.:$]+)",
        r"permission to query table ([A-Za-z0-9\-_.:$]+)",
        r"denied on (?:resource|dataset|table|project) '?([A-Za-z0-9\-_.:$/]+)'?",
        r"on resource '?([A-Za-z0-9\-_.:$/]+)'?",
    ]
)

_ROLE_PATTERN: Final[Pattern[str]] = re.compile(r"roles/[A-Za-z0-9_.\-]+")


def extract_resource(message: str) -> str | None:
    """Lift a resource name out of an error message, or return None.

    Deliberately conservative. If the text does not name a resource in a shape
    we have actually seen, this returns None and the caller must supply the
    name it already knows (the tool call's own target). Guessing a resource
    name would reintroduce, in regex form, exactly the fabrication ADR 004
    forbids the model from doing in prose.
    """
    if not message:
        return None
    for pattern in _RESOURCE_PATTERNS:
        found = pattern.search(message)
        if found:
            captured = found.group(1).rstrip(".,;:")
            if pattern.pattern.startswith("permission to use project"):
                return f"project {captured}"
            return captured
    return None


def extract_named_role(message: str) -> str | None:
    """Lift a ``roles/...`` name out of the text, verbatim, or return None."""
    if not message:
        return None
    found = _ROLE_PATTERN.search(message)
    return found.group(0).rstrip(".,;:") if found else None


# ==========================================================================
# The classifier
# ==========================================================================


def classify(
    status_code: int | None,
    message: str,
    *,
    resource_hint: str | None = None,
    action_hint: str | None = None,
    request_id: str | None = None,
) -> MiddleTierError:
    """Map (status code, message text) onto the ADR 004 taxonomy.

    :param status_code: HTTP status, or None if the failure was not HTTP.
    :param message: the full upstream error text. Never modified, never
        discarded: it is carried into the returned error's log-only field.
    :param resource_hint: what the tool was trying to reach. Used when the
        message does not name a resource itself, which is common for
        project-level and API-enablement denials.
    :returns: a :class:`~app.errors.taxonomy.MiddleTierError` subclass. Never
        raises for unrecognised input; unrecognised input is a classification
        with ``confidently_classified=False``, which is a louder log line, not
        a crash in the error path.
    """
    text = message or ""

    if status_code == 403:
        return _classify_403(
            text, resource_hint=resource_hint, action_hint=action_hint, request_id=request_id
        )

    if status_code in (401, 407):
        return IdentityAcquisitionError(
            stage=_stage_from_text(text),
            reason_code="unauthenticated",
            detail=text,
            status_code=status_code,
        )

    if status_code == 400 and _matches(_IDENTITY_EXCHANGE_FAILURE, text):
        # A 400 from an OAuth/STS endpoint is an identity failure, not a bad
        # user request: invalid_grant, wrong audience, expired assertion.
        return IdentityAcquisitionError(
            stage=_stage_from_text(text),
            reason_code="token_exchange_rejected",
            detail=text,
            status_code=400,
        )

    if status_code == 404:
        # Deliberately NOT a denial. See UpstreamUnavailable's docstring: we
        # cannot tell "absent" from "hidden" and will not pretend to.
        return UpstreamUnavailable(
            reason_code="not_found",
            status_code=404,
            retryable=False,
            request_id=request_id,
            detail=text,
        )

    if status_code == 429 or (status_code is not None and 500 <= status_code <= 599):
        return UpstreamUnavailable(
            reason_code="rate_limited" if status_code == 429 else "backend_error",
            status_code=status_code,
            retryable=True,
            request_id=request_id,
            detail=text,
        )

    if status_code is None and _matches(_CREDENTIAL_TYPE_REJECTED, text):
        # A local google-auth failure ("could not determine credentials")
        # reaches here with no status code. It is still an identity failure
        # and still must not be answered with a fallback credential.
        return IdentityAcquisitionError(
            stage=IdentityStage.STS,
            reason_code="credential_type_rejected",
            detail=text,
            signin_will_help=False,
            design_fatal=True,
        )

    return UpstreamUnavailable(
        reason_code="unclassified",
        status_code=status_code,
        retryable=status_code is None,
        request_id=request_id,
        detail=text,
    )


def _classify_403(
    text: str,
    *,
    resource_hint: str | None,
    action_hint: str | None,
    request_id: str | None,
) -> MiddleTierError:
    """The two-403s distinction, in order of precedence."""

    strong = _matches(_STRONG_MISSING_ROLE, text)
    if strong:
        # Unambiguous permission problem, even if the text also happens to
        # mention a service account somewhere.
        return _denial(text, resource_hint, action_hint, request_id, confident=True)

    credential_type = _matches(_CREDENTIAL_TYPE_REJECTED, text)
    if credential_type:
        # FATAL TO THE DESIGN if it ever fires in production. Not a resource
        # denial: Google refused the kind of principal, so there is no role to
        # grant and no sign-in that helps. Surfaced as an identity failure and
        # logged at CRITICAL by the boundary.
        return IdentityAcquisitionError(
            stage=IdentityStage.STS,
            reason_code="credential_type_rejected",
            detail=text,
            signin_will_help=False,
            design_fatal=True,
            status_code=403,
        )

    if _matches(_WEAK_MISSING_ROLE, text):
        return _denial(text, resource_hint, action_hint, request_id, confident=True)

    # An unrecognised 403. It is rendered as a denial - that is the safe
    # user-facing behaviour, since the request was refused either way - but
    # flagged so the log line demands a human reads the text we could not
    # place. Silently treating it as a known shape is how the two-403s
    # distinction would rot.
    return _denial(text, resource_hint, action_hint, request_id, confident=False)


def _denial(
    text: str,
    resource_hint: str | None,
    action_hint: str | None,
    request_id: str | None,
    *,
    confident: bool,
) -> DownstreamAuthorizationDenied:
    resource = extract_resource(text) or resource_hint or UNNAMED_RESOURCE
    return DownstreamAuthorizationDenied(
        resource=resource,
        action=action_hint,
        named_role=extract_named_role(text),
        raw_message=text,
        status_code=403,
        request_id=request_id,
        confidently_classified=confident,
    )


def _stage_from_text(text: str) -> IdentityStage:
    if _matches(_OBO_HINTS, text):
        return IdentityStage.OBO
    if _matches(_STS_HINTS, text):
        return IdentityStage.STS
    return IdentityStage.OBO


# ==========================================================================
# Exception adapter
# ==========================================================================

_STATUS_ATTRS: Final[tuple[str, ...]] = ("status_code", "code", "status", "resp_status")


def _status_of(exc: Exception) -> int | None:
    for attr in _STATUS_ATTRS:
        value = getattr(exc, attr, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    response = getattr(exc, "response", None)
    for attr in ("status_code", "status"):
        value = getattr(response, attr, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    return None


def classify_exception(
    exc: Exception,
    *,
    resource_hint: str | None = None,
    action_hint: str | None = None,
    request_id: str | None = None,
) -> MiddleTierError:
    """Classify a raised exception rather than a (status, message) pair.

    Duck-typed over ``google.api_core.exceptions``, ``aiohttp``,
    ``httpx`` and plain exceptions: anything exposing a status-ish attribute
    is used, otherwise the text alone decides. No SDK is imported, so this
    module has no dependency on which client library a tool happens to use.
    """
    if isinstance(exc, MiddleTierError):
        return exc
    return classify(
        _status_of(exc),
        str(exc),
        resource_hint=resource_hint or getattr(exc, "resource", None),
        action_hint=action_hint,
        request_id=request_id,
    )
