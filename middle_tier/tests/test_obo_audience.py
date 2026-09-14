"""The audience pre-flight check: the likeliest failure point, tested offline.

These tests need no network and no Entra secret. They build real JWT-shaped
strings locally (header.payload.signature, base64url) with the exact claim
sets Entra produces in each registration configuration, and assert that the
pre-flight either passes them or refuses them with a message that names both
the value we got and the value the workforce pool provider requires.

They do NOT prove that Google's STS accepts an Entra access token - nothing
executable here can prove that, and NOTES.md records it as BLOCKED. What they
do prove is that a v1-shaped token is caught locally, immediately, with an
actionable message, instead of surfacing as an STS 400 that names no claim.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.identity.errors import OboAudienceMismatch, PreconditionError  # noqa: E402
from app.identity.obo import (  # noqa: E402
    OboConfig,
    check_google_audience,
    describe_assertion,
)

TENANT = "00000000-0000-0000-0000-000000000000"
FEDERATION_APP = "11111111-1111-1111-1111-111111111111"
BOT_APP = "11111111-2222-3333-4444-555555555555"
ANALYST_OID = "33333333-3333-3333-3333-333333333333"

V2_ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
V1_ISSUER = f"https://sts.windows.net/{TENANT}/"


def _jwt(claims: dict) -> str:
    """A JWT-shaped string. Unsigned - the pre-flight is deliberately a claim
    inspection, not a verification; Google does the verification."""

    def seg(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'RS256', 'typ': 'JWT'})}.{seg(claims)}.not-a-real-signature"


def _v2_access_token_for_federation_app() -> str:
    """What Entra issues when the federation app has
    ``api.requestedAccessTokenVersion = 2``. This is the shape that works."""
    return _jwt(
        {
            "aud": FEDERATION_APP,
            "iss": V2_ISSUER,
            "ver": "2.0",
            "oid": ANALYST_OID,
            "tid": TENANT,
            "azp": BOT_APP,
            "exp": int(time.time()) + 3600,
        }
    )


def _v1_access_token_as_actually_observed() -> str:
    """The REAL shape, captured from Entra on 2026-09-07.

    This is not a guess. A live access token for the federation app came back
    with the bare-GUID audience (correct!) and the legacy v1 issuer (fatal).
    Google STS refused it with:

        "The issuer in ID Token https://sts.windows.net/{tid} does not match
         the expected one in config: https://login.microsoftonline.com/{tid}/v2.0"
    """
    return _jwt(
        {
            "aud": FEDERATION_APP,  # already correct - the trap is below
            "iss": V1_ISSUER,  # this is what breaks the chain
            "ver": "1.0",
            "oid": ANALYST_OID,
            "tid": TENANT,
            "appid": FEDERATION_APP,
            "exp": int(time.time()) + 3600,
        }
    )


def _v1_access_token_with_app_id_uri_audience() -> str:
    """The failure everyone expects but this tenant did not produce.

    Would occur if the federation app had a custom App ID URI. Kept as a
    test case because the pre-flight has to catch it too, and because a
    future app-registration edit could introduce it."""
    return _jwt(
        {
            "aud": f"api://{FEDERATION_APP}",
            "iss": V1_ISSUER,
            "ver": "1.0",
            "oid": ANALYST_OID,
            "tid": TENANT,
            "exp": int(time.time()) + 3600,
        }
    )


def _teams_sso_token() -> str:
    """The inbound token: audienced to the BOT app. This is why OBO exists."""
    return _jwt(
        {
            "aud": BOT_APP,
            "iss": V2_ISSUER,
            "ver": "2.0",
            "oid": ANALYST_OID,
            "tid": TENANT,
            "exp": int(time.time()) + 3600,
        }
    )


CONFIG = OboConfig(
    tenant_id=TENANT,
    client_id=BOT_APP,
    client_secret="not-used-by-these-tests",
    federation_app_id=FEDERATION_APP,
)


def test_v2_access_token_passes_the_preflight() -> None:
    claims = check_google_audience(
        _v2_access_token_for_federation_app(),
        expected_aud=FEDERATION_APP,
        expected_iss=CONFIG.expected_issuer,
    )
    assert claims["aud"] == FEDERATION_APP
    assert claims["oid"] == ANALYST_OID


def test_the_real_v1_access_token_is_refused_on_the_ISSUER_not_the_audience() -> None:
    """Regression test for the live finding that inverted the assumption.

    The whole risk was written up as an *audience* risk. Measured, it is an
    *issuer* risk: the audience was already right. If the pre-flight ever
    reports this as an audience problem, an operator will go and edit the
    wrong field.
    """
    with pytest.raises(OboAudienceMismatch) as caught:
        check_google_audience(
            _v1_access_token_as_actually_observed(),
            expected_aud=FEDERATION_APP,
            expected_iss=CONFIG.expected_issuer,
        )

    wrong = caught.value.wrong_claims
    assert len(wrong) == 1, f"only the issuer should be flagged, got {wrong}"
    assert wrong[0].startswith("iss"), wrong
    assert "sts.windows.net" in caught.value.detail
    assert "requestedAccessTokenVersion" in caught.value.detail, "must name the actual fix"
    assert caught.value.reason_code == "identity.obo_audience.mismatch"


def test_app_id_uri_audience_is_also_refused_and_reported_as_an_audience_fault() -> None:
    with pytest.raises(OboAudienceMismatch) as caught:
        check_google_audience(
            _v1_access_token_with_app_id_uri_audience(),
            expected_aud=FEDERATION_APP,
            expected_iss=CONFIG.expected_issuer,
        )

    wrong = caught.value.wrong_claims
    assert len(wrong) == 2, f"both claims are wrong in this shape, got {wrong}"
    assert any(w.startswith("aud") for w in wrong)
    assert any(w.startswith("iss") for w in wrong)
    assert f"api://{FEDERATION_APP}" in caught.value.detail


def test_the_teams_token_itself_would_be_refused() -> None:
    """Sanity check on the premise of the whole design: the inbound Teams SSO
    token cannot be sent to Google directly."""
    with pytest.raises(OboAudienceMismatch) as caught:
        check_google_audience(
            _teams_sso_token(),
            expected_aud=FEDERATION_APP,
            expected_iss=CONFIG.expected_issuer,
        )
    assert caught.value.got_aud == BOT_APP


def test_an_audience_list_matches_on_any_member() -> None:
    """RFC 7519 s4.1.3 allows `aud` to be an array; Google matches any entry."""
    token = _jwt(
        {
            "aud": ["some-other-app", FEDERATION_APP],
            "iss": V2_ISSUER,
            "oid": ANALYST_OID,
            "exp": int(time.time()) + 3600,
        }
    )
    claims = check_google_audience(
        token, expected_aud=FEDERATION_APP, expected_iss=CONFIG.expected_issuer
    )
    assert claims["oid"] == ANALYST_OID


def test_wrong_tenant_issuer_is_refused() -> None:
    """A token from another tenant must not be accepted just because the
    audience happens to match."""
    token = _jwt(
        {
            "aud": FEDERATION_APP,
            "iss": "https://login.microsoftonline.com/99999999-9999-9999-9999-999999999999/v2.0",
            "oid": ANALYST_OID,
            "exp": int(time.time()) + 3600,
        }
    )
    with pytest.raises(OboAudienceMismatch):
        check_google_audience(
            token, expected_aud=FEDERATION_APP, expected_iss=CONFIG.expected_issuer
        )


def test_garbage_is_refused_as_a_precondition_not_a_crash() -> None:
    for bad in ("", "not-a-jwt", "a.b", "a.!!!.c"):
        with pytest.raises((PreconditionError, OboAudienceMismatch)):
            check_google_audience(
                bad, expected_aud=FEDERATION_APP, expected_iss=CONFIG.expected_issuer
            )


def test_describe_assertion_is_safe_to_paste_into_a_ticket() -> None:
    """The diagnostic helper must not echo the token back."""
    token = _v2_access_token_for_federation_app()
    described = describe_assertion(token)

    assert described["aud"] == FEDERATION_APP
    assert described["iss"] == V2_ISSUER
    assert described["ver"] == "2.0"
    assert described["oid"] == ANALYST_OID
    assert 3590 <= described["expires_in_seconds"] <= 3600

    rendered = json.dumps(described)
    assert token not in rendered
    assert token.split(".")[1] not in rendered, "the payload segment must not appear verbatim"


def test_config_never_points_the_scope_at_microsoft_graph() -> None:
    """The comment in obo.py says not to. This makes it enforceable."""
    assert CONFIG.effective_scope == f"{FEDERATION_APP}/.default"
    assert "graph.microsoft.com" not in CONFIG.effective_scope


def test_config_repr_does_not_leak_the_client_secret() -> None:
    config = OboConfig(
        tenant_id=TENANT,
        client_id=BOT_APP,
        client_secret="hunter2-the-real-bot-secret",
        federation_app_id=FEDERATION_APP,
    )
    assert "hunter2-the-real-bot-secret" not in repr(config)
    assert "[REDACTED]" in repr(config)


def test_expected_issuer_is_the_v2_endpoint() -> None:
    """If this ever says sts.windows.net, the provider config and the code
    have drifted apart."""
    assert CONFIG.expected_issuer == V2_ISSUER
    assert "sts.windows.net" not in CONFIG.expected_issuer
