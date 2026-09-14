"""The typed error hierarchy for ADR 004. Two paths, two templates.

WHY A TAXONOMY AND NOT `Exception("403")`
-----------------------------------------
ADR 004 defines exactly two authorization failure paths and gives each one a
fixed behaviour:

  1. **Identity acquisition failed** -> refuse the turn, say it is an identity
     problem, attach a sign-in card. (:class:`IdentityAcquisitionError`,
     :class:`MissingEntraObjectId`.)
  2. **Downstream authorization denied** -> intercept at the tool boundary,
     render a template that NAMES the refused resource, and tell the model
     nothing except that name. (:class:`DownstreamAuthorizationDenied`.)

Anything that is neither of those is not an authorization event and must not be
worded like one (:class:`UpstreamUnavailable`). Telling a user they lack
permission when the backend was merely down sends them to an admin who will
find nothing wrong and will not believe the next report.

THE ONE RULE THIS MODULE ENFORCES
---------------------------------
Every error here separates two audiences that are deliberately NOT symmetric:

  * :meth:`template_fields` - the minimum the user-facing template needs.
  * :meth:`log_fields`      - everything, including the full raw upstream text.

The raw text is never dropped (ADR 004: "Log the full text; render the template
to the user") and never widened into the template or into the model context.
:mod:`app.errors.boundary` is where that asymmetry is enforced at runtime; this
module is where it is expressed in the type.

WHAT IS NOT HERE, ON PURPOSE
----------------------------
There is no ``FallbackCredentialUsed``, no ``ServiceAccountRetry``, and no
error that carries a credential of any kind. The service-account fallback is
recorded in ADR 004 as **explicitly rejected and must not be reintroduced**;
an error type that could express it would be the first step back towards it.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Final

__all__ = [
    "IdentityStage",
    "MiddleTierError",
    "IdentityAcquisitionError",
    "MissingEntraObjectId",
    "DownstreamAuthorizationDenied",
    "UpstreamUnavailable",
    "from_port_error",
    "from_identity_error",
    "UNNAMED_RESOURCE",
]


# The literal used when a denial genuinely cannot be attributed to a named
# resource. It is never empty: an empty resource is a programming error
# (the templates raise), but a denial that reached a user must still say
# something falsifiable rather than "access denied" with no object.
UNNAMED_RESOURCE: Final[str] = "an unnamed resource (see the reference id)"


class IdentityStage(str, Enum):
    """Where in the identity chain the acquisition failed.

    The chain is: Teams SSO assertion -> Entra On-Behalf-Of re-audiencing ->
    Google STS token exchange. The three stages fail for different reasons and
    have different remedies, so the stage travels with the exception into the
    log line and into the short reference code shown to the user.

    Only these three exist because only these three are stages of *acquiring
    the user's identity*. A failure after acquisition is a downstream denial,
    not an identity failure, and mixing them is the mistake this enum prevents.
    """

    TEAMS_SSO = "teams_sso"
    OBO = "obo"
    STS = "sts"


class MiddleTierError(Exception):
    """Base class. Carries a reason code, a retryable flag, and two views.

    Subclasses override :meth:`template_fields` (narrow, user-facing) and
    :meth:`log_fields` (wide, operator-facing). Nothing in this layer ever
    produces a third view for the model; the model's view is derived by
    :mod:`app.errors.boundary` and is narrower than both.
    """

    reason_code: str = "error"
    retryable: bool = False

    def template_fields(self) -> dict[str, Any]:
        """What the user-facing template is allowed to interpolate."""
        return {"reason_code": self.reason_code}

    def log_fields(self) -> dict[str, Any]:
        """Everything an operator needs, including raw upstream text."""
        return {
            "error_type": type(self).__name__,
            "reason_code": self.reason_code,
            "retryable": self.retryable,
        }


# ==========================================================================
# Path 1: identity acquisition failure -> refuse the turn + sign-in card
# ==========================================================================


class IdentityAcquisitionError(MiddleTierError):
    """No usable *user* identity, so the turn is refused. ADR 004 path 1.

    Raised when the OBO exchange is refused, when Google's STS rejects the
    assertion, or when the user has been removed from the tenant. All three
    end the turn. None of them fall back to anything.

    :param stage: which hop failed (:class:`IdentityStage`). Required, because
        "sign in again" is the right advice for a revoked consent and the
        wrong advice for a misconfigured audience.
    :param reason_code: short stable token, shown to the user only as a
        correlation reference and never as an explanation.
    :param detail: the raw upstream text. **Log-only.** Never rendered, never
        given to the model.
    :param retryable: a transient failure (5xx, timeout) is still a refusal
        for *this* turn; the flag only tells the caller whether retrying the
        acquisition later is meaningful.
    :param signin_will_help: False when a sign-in card cannot fix the cause
        (bad configuration, guest user with no directory identity). The
        template still refuses; it just does not send the user round a loop
        that cannot terminate.
    :param design_fatal: set by :mod:`app.errors.classify` when the upstream
        message rejected the *credential or principal type* rather than a
        permission. That case would invalidate the whole workforce-identity
        design, so it is flagged here and logged at CRITICAL rather than
        being quietly folded in with ordinary sign-in failures.
    """

    reason_code = "identity_unavailable"

    def __init__(
        self,
        *,
        stage: IdentityStage | str,
        reason_code: str | None = None,
        detail: str = "",
        retryable: bool = False,
        signin_will_help: bool = True,
        design_fatal: bool = False,
        status_code: int | None = None,
    ) -> None:
        self.stage = IdentityStage(stage) if not isinstance(stage, IdentityStage) else stage
        if reason_code:
            self.reason_code = reason_code
        self.detail = detail
        self.retryable = retryable
        self.signin_will_help = signin_will_help
        self.design_fatal = design_fatal
        self.status_code = status_code
        super().__init__(f"identity acquisition failed at {self.stage.value}: {self.reason_code}")

    def template_fields(self) -> dict[str, Any]:
        # Note what is absent: `detail`. The user gets a reason code they can
        # quote in a ticket, not an upstream error they cannot action.
        return {
            "reason_code": self.reason_code,
            "stage": self.stage.value,
            "signin_will_help": self.signin_will_help,
        }

    def log_fields(self) -> dict[str, Any]:
        return {
            **super().log_fields(),
            "stage": self.stage.value,
            "status_code": self.status_code,
            "design_fatal": self.design_fatal,
            "detail": self.detail,
        }


class MissingEntraObjectId(IdentityAcquisitionError):
    """The activity carried no ``from.aadObjectId``. ADR 003 + ADR 004.

    This is an identity failure that happens *before* any network call. Teams
    also offers ``from.id`` (the Teams MRI, e.g. ``29:1a2b...``) and it is
    always present, which is exactly what makes it dangerous: serving the turn
    under the MRI would mint a second, unfederated identity for a person who
    already has an Entra object id, and the two would never reconcile.

    So the turn is refused. This class deliberately does **not** carry the MRI:
    an attribute holding a usable substitute identifier is an invitation to
    substitute it. Correlation is done with channel and conversation ids,
    which are not identities.
    """

    reason_code = "missing_aad_object_id"

    def __init__(
        self,
        *,
        channel_id: str | None = None,
        conversation_id: str | None = None,
        detail: str = "",
    ) -> None:
        super().__init__(
            stage=IdentityStage.TEAMS_SSO,
            reason_code="missing_aad_object_id",
            detail=detail,
            retryable=False,
            # A guest or anonymous participant has no directory identity to
            # sign in *to*. A sign-in button here loops forever.
            signin_will_help=False,
        )
        self.channel_id = channel_id
        self.conversation_id = conversation_id

    def log_fields(self) -> dict[str, Any]:
        return {
            **super().log_fields(),
            "channel_id": self.channel_id,
            "conversation_id": self.conversation_id,
        }


# ==========================================================================
# Path 2: downstream authorization denial -> template naming the resource
# ==========================================================================


class DownstreamAuthorizationDenied(MiddleTierError):
    """The user's own token was valid; IAM/BigQuery refused this resource.

    ADR 004 path 2. The user is told which resource was refused, because that
    turns "the bot says access denied" into a thirty-second grant. The model is
    told the same one fact and nothing else.

    :param resource: what was refused, e.g. ``example-project.sales.orders``.
        Required and non-empty: an unattributed denial is the failure mode
        this path exists to prevent.
    :param action: optional permission or verb, e.g.
        ``bigquery.tables.getData``.
    :param named_role: a role name lifted **verbatim** from the upstream
        message (``roles/serviceusage.serviceUsageConsumer``). Extraction is a
        regex over the error text, never an inference: if the text does not
        name a role, this stays ``None`` rather than guessing one.
    :param raw_message: the full upstream error text. **Log-only.** This is
        the field the whole ADR is about. It is never rendered and never
        reaches the model.
    :param confidently_classified: False when the classifier could not match
        the 403 to a known shape. The turn is still refused; the log line is
        raised so a human reads the text the classifier could not place.
    """

    reason_code = "authorization_denied"

    def __init__(
        self,
        *,
        resource: str,
        action: str | None = None,
        named_role: str | None = None,
        raw_message: str = "",
        status_code: int | None = None,
        request_id: str | None = None,
        confidently_classified: bool = True,
    ) -> None:
        if not resource or not resource.strip():
            raise ValueError(
                "DownstreamAuthorizationDenied requires a resource name; ADR 004 "
                "forbids an unattributed denial. Use UNNAMED_RESOURCE if the "
                "upstream error genuinely did not name one."
            )
        self.resource = resource.strip()
        self.action = action
        self.named_role = named_role
        self.raw_message = raw_message
        self.status_code = status_code
        self.request_id = request_id
        self.confidently_classified = confidently_classified
        super().__init__(f"denied: {self.resource}")

    # The single sanctioned sentence the model may be given. It is a method on
    # the error rather than a string built at the call site so that there is
    # exactly one place to audit. See boundary.model_facing().
    def model_facing_summary(self) -> str:
        return f"Access denied to {self.resource}."

    def template_fields(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "action": self.action,
            "named_role": self.named_role,
            "request_id": self.request_id,
        }

    def log_fields(self) -> dict[str, Any]:
        return {
            **super().log_fields(),
            "resource": self.resource,
            "action": self.action,
            "named_role": self.named_role,
            "status_code": self.status_code,
            "request_id": self.request_id,
            "confidently_classified": self.confidently_classified,
            # The point of the ADR: the text is kept, in full, here and only
            # here.
            "raw_message": self.raw_message,
        }


# ==========================================================================
# Neither path: the backend failed. Not an authorization event.
# ==========================================================================


class UpstreamUnavailable(MiddleTierError):
    """5xx, timeout, quota, or a 404 we refuse to interpret.

    A 404 lands here rather than in :class:`DownstreamAuthorizationDenied` on
    purpose. BigQuery answers "Not found: Table x" both for a table that does
    not exist and, in some configurations, for one the caller cannot see. ADR
    004's central claim is that a language model cannot distinguish a missing
    role from a non-existent table from a typo. Neither can this classifier,
    and it is not going to pretend otherwise by rendering a denial template
    over an ambiguous 404.
    """

    reason_code = "upstream_unavailable"

    def __init__(
        self,
        *,
        reason_code: str | None = None,
        status_code: int | None = None,
        retryable: bool = True,
        retry_after: float | None = None,
        request_id: str | None = None,
        detail: str = "",
    ) -> None:
        if reason_code:
            self.reason_code = reason_code
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after
        self.request_id = request_id
        self.detail = detail
        super().__init__(f"upstream unavailable: {self.reason_code}")

    def template_fields(self) -> dict[str, Any]:
        return {"reason_code": self.reason_code, "request_id": self.request_id}

    def log_fields(self) -> dict[str, Any]:
        return {
            **super().log_fields(),
            "status_code": self.status_code,
            "retry_after": self.retry_after,
            "request_id": self.request_id,
            "detail": self.detail,
        }


# ==========================================================================
# Adapters for the exception types other components raise
# ==========================================================================
#
# These are DUCK-TYPED on purpose. `app.ports` and `app.identity.errors` are
# owned by other people and are in flux; importing them here would couple this
# layer's importability to their current state, and an error-handling module
# that fails to import is worse than useless. We inspect attributes instead.

_IDENTITY_STAGE_ALIASES: Final[dict[str, IdentityStage]] = {
    # app.identity.errors uses a finer-grained stage enum. Map it onto the
    # three stages ADR 004 names. Unknown values fall back to OBO, the
    # middle hop, and are recorded verbatim in the reason code.
    "teams_sso": IdentityStage.TEAMS_SSO,
    "precondition": IdentityStage.TEAMS_SSO,
    "obo": IdentityStage.OBO,
    "obo_audience": IdentityStage.OBO,
    "sts": IdentityStage.STS,
    "cache": IdentityStage.STS,
}


def _stage_of(exc: Exception) -> IdentityStage:
    raw = getattr(exc, "stage", None)
    value = getattr(raw, "value", raw)
    if isinstance(value, str):
        return _IDENTITY_STAGE_ALIASES.get(value.lower(), IdentityStage.OBO)
    return IdentityStage.OBO


def from_identity_error(exc: Exception) -> IdentityAcquisitionError:
    """Adapt any identity-broker exception into this taxonomy.

    Accepts the broker's own hierarchy (anything carrying ``stage`` and
    optionally ``code``/``retryable``) and anything else that means "we could
    not establish who this is". The result is always a refusal: there is no
    input to this function that produces a credential.
    """
    if isinstance(exc, IdentityAcquisitionError):
        return exc
    code = getattr(exc, "code", None) or "identity_unavailable"
    return IdentityAcquisitionError(
        stage=_stage_of(exc),
        reason_code=str(code),
        detail=str(exc),
        retryable=bool(getattr(exc, "retryable", False)),
        signin_will_help=bool(getattr(exc, "signin_will_help", True)),
    )


def from_port_error(exc: Exception, *, resource_hint: str | None = None) -> MiddleTierError:
    """Adapt an ``app.ports`` error (or lookalike) into this taxonomy.

    ``AuthorizationDenied`` carries ``resource``; ``IdentityUnavailable`` and
    ``TransientBackendError`` carry nothing but a message. Matching is on
    class name plus attributes so this keeps working if ports.py moves.
    """
    if isinstance(exc, MiddleTierError):
        return exc

    name = type(exc).__name__
    if name == "AuthorizationDenied" or hasattr(exc, "resource"):
        resource = str(getattr(exc, "resource", "") or resource_hint or UNNAMED_RESOURCE)
        return DownstreamAuthorizationDenied(
            resource=resource or UNNAMED_RESOURCE,
            raw_message=str(exc),
            status_code=getattr(exc, "status_code", None),
        )
    if name == "IdentityUnavailable":
        return IdentityAcquisitionError(
            stage=_stage_of(exc), reason_code="identity_unavailable", detail=str(exc)
        )
    if name in ("TransientBackendError", "SessionNotFound"):
        return UpstreamUnavailable(
            reason_code="backend_transient" if name == "TransientBackendError" else "session_not_found",
            retryable=name == "TransientBackendError",
            detail=str(exc),
        )
    return UpstreamUnavailable(reason_code="unclassified", retryable=False, detail=str(exc))
