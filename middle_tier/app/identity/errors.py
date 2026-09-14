"""Typed identity-acquisition failures, each naming the stage that failed.

WHY THE STAGE IS PART OF THE TYPE
---------------------------------
ADR 004 says a turn with no usable user identity is REFUSED, with an explicit
message. "Explicit" is doing real work in that sentence. These four failures
are indistinguishable from the outside and have four different remedies:

    stage=obo   / consent_required   -> the USER must consent. Sign-in card.
    stage=obo   / audience_mismatch  -> an ADMIN must fix the Entra app
                                        registration. A sign-in card sends the
                                        user round a loop that cannot end.
    stage=sts   / permission_denied  -> an ADMIN must fix IAM or the workforce
                                        pool. Again, not the user's problem.
    stage=sts   / transient          -> nobody must do anything. Retry.

If all four collapse into one "sorry, sign in again" the operator learns
nothing and the user is asked to fix a problem they do not have. So the stage
travels with the exception and ends up in the log line and in the short
``reason_code`` printed on the refusal card.

WHAT IS *NOT* HERE
------------------
There is no ``FallbackCredentialUsed`` and there is no error that carries a
credential. Every class below is a dead end: the caller either propagates it
or renders a refusal. Returning a service-account token instead of raising is
precisely the anti-pattern ADR 002 exists to prevent.
"""

from __future__ import annotations

import enum


class IdentityStage(str, enum.Enum):
    """Where in the chain the acquisition died."""

    #: Local checks before any network call: config, user-key shape, inputs.
    PRECONDITION = "precondition"
    #: The Entra On-Behalf-Of exchange.
    OBO = "obo"
    #: The OBO call succeeded, but the token it returned cannot be used
    #: against Google. Split out from OBO because it is a *configuration*
    #: fault with a completely different remedy - see the module docstring
    #: of ``obo.py``.
    OBO_AUDIENCE = "obo_audience"
    #: The Google STS token exchange.
    STS = "sts"
    #: The token cache itself (should be unreachable; present so a cache bug
    #: cannot be silently reported as an Entra outage).
    CACHE = "cache"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class IdentityAcquisitionError(Exception):
    """Base class. Identity could not be acquired, so the turn is refused.

    :param detail: operator-facing detail. MUST NOT contain a token. Callers
        pass this through ``app.logging_utils.redact`` before logging anyway,
        but do not rely on that: construct the message from claim values and
        HTTP status codes, never from raw credential material.
    :param retryable: true only for genuinely transient conditions (5xx,
        timeout). A retryable error is still a refusal for *this* turn; it
        only tells the caller whether an immediate retry is sensible.
    """

    #: Overridden by every concrete subclass.
    stage: IdentityStage = IdentityStage.PRECONDITION
    #: Short machine token appended to the stage to build ``reason_code``.
    code: str = "failed"

    def __init__(
        self,
        detail: str = "",
        *,
        user_key: str | None = None,
        retryable: bool = False,
        http_status: int | None = None,
        upstream_code: str | None = None,
    ) -> None:
        self.detail = detail
        self.user_key = user_key
        self.retryable = retryable
        self.http_status = http_status
        #: e.g. Entra's ``invalid_grant`` / ``AADSTS65001``, or STS's
        #: ``PERMISSION_DENIED``. Verbatim from the provider, for grepping.
        self.upstream_code = upstream_code
        super().__init__(self._summary())

    @property
    def reason_code(self) -> str:
        """Stable short token for the refusal card and the log line.

        Format ``identity.<stage>.<code>``. Support can grep one string and
        land on the exact branch that fired.
        """
        return f"identity.{self.stage.value}.{self.code}"

    def _summary(self) -> str:
        bits = [self.reason_code]
        if self.http_status is not None:
            bits.append(f"http={self.http_status}")
        if self.upstream_code:
            bits.append(f"upstream={self.upstream_code}")
        if self.detail:
            bits.append(self.detail)
        return " ".join(bits)

    def as_log_fields(self) -> dict[str, object]:
        """Structured fields for ``log_event``. Never includes a token."""
        return {
            "stage": self.stage.value,
            "reason_code": self.reason_code,
            "retryable": self.retryable,
            "http_status": self.http_status,
            "upstream_code": self.upstream_code,
            "detail": self.detail,
            # The user key is `entra:{tid}:{oid}` - two GUIDs. Not a secret,
            # and without it a support ticket cannot be correlated at all.
            "user_key": self.user_key,
        }


# --------------------------------------------------------------------------
# Preconditions
# --------------------------------------------------------------------------


class PreconditionError(IdentityAcquisitionError):
    """Refused before any network call: bad config, bad user key, no token.

    Distinct from the network stages because it is deterministic. Retrying
    changes nothing and the sign-in card is usually the wrong answer.
    """

    stage = IdentityStage.PRECONDITION
    code = "invalid_request"


# --------------------------------------------------------------------------
# Stage 1: Entra On-Behalf-Of
# --------------------------------------------------------------------------


class OboExchangeError(IdentityAcquisitionError):
    """The Entra OBO exchange failed."""

    stage = IdentityStage.OBO
    code = "failed"


class OboConsentRequired(OboExchangeError):
    """Entra needs the USER to interact: consent, MFA, conditional access.

    This is the one identity failure that the sign-in card actually fixes.
    Entra signals it with ``invalid_grant`` plus ``AADSTS65001`` (no consent)
    or an ``interaction_required`` suberror.
    """

    code = "consent_required"


class OboTransientError(OboExchangeError):
    """Entra 5xx, throttling, or a network timeout. Retry is meaningful."""

    code = "transient"

    def __init__(self, detail: str = "", **kw: object) -> None:
        kw.setdefault("retryable", True)
        super().__init__(detail, **kw)  # type: ignore[arg-type]


class OboAudienceMismatch(IdentityAcquisitionError):
    """OBO returned a token whose ``aud``/``iss`` Google's STS will refuse.

    Raised by a local pre-flight check, deliberately BEFORE the STS call, so
    the operator sees exactly which claim is wrong and what to change,
    instead of an opaque 400 arriving from a different cloud.

    MEASURED, not assumed. On 2026-09-07 an Entra access token for the
    federation app was fed to the real STS endpoint and refused with:

        The issuer in ID Token https://sts.windows.net/{tid} does not match
        the expected one in config:
        https://login.microsoftonline.com/{tid}/v2.0

    So the claim that actually breaks in this tenant today is ``iss``, not
    ``aud`` - the v1 access token's audience was already the bare client-ID
    GUID. Both are checked here anyway: a custom App ID URI on the federation
    app would break the audience too, and the two faults have one fix.
    """

    stage = IdentityStage.OBO_AUDIENCE
    code = "mismatch"

    #: The one-line remedy, kept in one place because it appears in the error
    #: message, the README and NOTES.md.
    REMEDY = (
        "Fix: set api.requestedAccessTokenVersion=2 on the FEDERATION app "
        "registration. That flips iss to the v2.0 endpoint and keeps aud as "
        "the bare client-ID GUID. Note a workforce-pool OIDC provider has no "
        "allowedAudiences list (unlike a workload pool), so exactly one "
        "audience value is ever accepted and there is no Google-side widening."
    )

    def __init__(
        self,
        *,
        got_aud: object = None,
        expected_aud: str = "",
        got_iss: str | None = None,
        expected_iss: str | None = None,
        user_key: str | None = None,
    ) -> None:
        self.got_aud = got_aud
        self.expected_aud = expected_aud
        self.got_iss = got_iss
        self.expected_iss = expected_iss

        # Name the claim that is actually wrong. "aud/iss mismatch" sends
        # people to check the audience when the issuer is the problem, which
        # is exactly what happened during the live probe.
        aud_values = got_aud if isinstance(got_aud, list) else [got_aud]
        wrong = []
        if expected_aud not in aud_values:
            wrong.append(f"aud (got {got_aud!r}, provider clientId is {expected_aud!r})")
        if expected_iss is not None and got_iss != expected_iss:
            wrong.append(f"iss (got {got_iss!r}, provider issuerUri is {expected_iss!r})")
        self.wrong_claims = wrong

        detail = (
            f"the re-audienced token will be refused by Google STS - "
            f"{'; '.join(wrong) if wrong else 'claim mismatch'}. {self.REMEDY}"
        )
        super().__init__(detail, user_key=user_key)

    def as_log_fields(self) -> dict[str, object]:
        fields = super().as_log_fields()
        fields.update(
            {
                "got_aud": self.got_aud,
                "expected_aud": self.expected_aud,
                "got_iss": self.got_iss,
                "expected_iss": self.expected_iss,
            }
        )
        return fields


# --------------------------------------------------------------------------
# Stage 2: Google STS
# --------------------------------------------------------------------------


class StsExchangeError(IdentityAcquisitionError):
    """The Google STS token exchange failed."""

    stage = IdentityStage.STS
    code = "failed"


class StsSubjectTokenRejected(StsExchangeError):
    """STS 400: the subject token was not acceptable.

    In practice this is almost always audience, issuer, expiry, or the
    provider's attribute condition - and almost never a "real" 400. The
    pre-flight check in ``obo.py`` exists to catch the audience case earlier
    and more legibly than this.
    """

    code = "subject_token_rejected"


class StsPermissionDenied(StsExchangeError):
    """STS/Google 403.

    Two very different faults share this status and the message is the only
    way to tell them apart (spike FINDINGS.md documents both):

    * the workforce principal lacks a role on the resource, or
    * the *quota project* named in ``options.userProject`` has not granted
      ``roles/serviceusage.serviceUsageConsumer`` to the principal.

    The second is the surprising one, because it is provoked by the very
    ``userProject`` that workforce principals are required to send.
    """

    code = "permission_denied"


class StsTransientError(StsExchangeError):
    """STS 5xx or a network timeout. Retry is meaningful."""

    code = "transient"

    def __init__(self, detail: str = "", **kw: object) -> None:
        kw.setdefault("retryable", True)
        super().__init__(detail, **kw)  # type: ignore[arg-type]


__all__ = [
    "IdentityStage",
    "IdentityAcquisitionError",
    "PreconditionError",
    "OboExchangeError",
    "OboConsentRequired",
    "OboTransientError",
    "OboAudienceMismatch",
    "StsExchangeError",
    "StsSubjectTokenRejected",
    "StsPermissionDenied",
    "StsTransientError",
]
