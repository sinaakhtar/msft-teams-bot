"""Identity Broker: the component that crosses the Microsoft -> Google trust boundary.

    Teams SSO token (aud = BOT app)
        -> OBO re-audiencing (Entra)        -> token for the FEDERATION app
        -> STS token exchange (Google)      -> Workforce Principal access token
        -> plain bearer against Google Cloud

Public surface is deliberately tiny: build a broker with
:func:`app.identity.broker.build_identity_broker` and call
``await broker.google_access_token(user_key=..., teams_sso_token=...)``.

Everything fails closed (ADR 004). There is no service-account fallback in
this package and adding one is a design violation, not an enhancement - see
``tests/test_no_service_account_fallback.py``, which fails the build if the
string ``default()`` or ``service_account`` appears in this source tree.
"""

from __future__ import annotations

from .errors import (
    IdentityAcquisitionError,
    IdentityStage,
    OboAudienceMismatch,
    OboConsentRequired,
    OboExchangeError,
    OboTransientError,
    PreconditionError,
    StsExchangeError,
    StsPermissionDenied,
    StsSubjectTokenRejected,
    StsTransientError,
)

__all__ = [
    "IdentityAcquisitionError",
    "IdentityStage",
    "PreconditionError",
    "OboExchangeError",
    "OboConsentRequired",
    "OboAudienceMismatch",
    "OboTransientError",
    "StsExchangeError",
    "StsSubjectTokenRejected",
    "StsPermissionDenied",
    "StsTransientError",
]
