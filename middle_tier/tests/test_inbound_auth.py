"""Inbound JWT validation tests. These are the tests that matter.

Every case below is an attack an unauthenticated `POST /api/messages` endpoint
is exposed to in production. Each one asserts that the validator FAILS CLOSED,
and asserts on the specific rejection reason so a refactor cannot accidentally
turn "bad signature" into "expired" and still pass.

Real RSA keys, real signatures, real HTTP JWKS fetch over loopback. No mocks in
the crypto or transport path. No external network.
"""

from __future__ import annotations

import time

import pytest

from app.auth.inbound import (
    ALLOWED_ALGORITHMS,
    BOT_FRAMEWORK_TOKEN_ISSUER,
    ChannelProfile,
    InboundActivityAuthenticator,
    InboundAuthError,
    JwksCache,
    default_channel_profiles,
)

from .conftest import (
    BOT_APP_ID,
    ROGUE_KID,
    SERVICE_URL,
    KeyMaterial,
    mint_alg_none_token,
    mint_hs256_token,
    mint_token,
    tamper_payload,
)


# ==========================================================================
# The happy path. If this does not pass, every rejection below is meaningless
# because a validator that rejects everything is trivially "secure".
# ==========================================================================


async def test_valid_token_is_accepted(authenticator, keys: KeyMaterial, activity):
    token = mint_token(keys.trusted)

    caller = await authenticator.authenticate(
        auth_header=f"Bearer {token}", activity=activity
    )

    assert caller.app_id == BOT_APP_ID
    assert caller.issuer == BOT_FRAMEWORK_TOKEN_ISSUER
    assert caller.profile_name == "bot_connector"
    assert caller.claims["aud"] == BOT_APP_ID


async def test_valid_token_accepts_lowercase_bearer_scheme(
    authenticator, keys: KeyMaterial, activity
):
    """RFC 7235 says the scheme is case-insensitive; some proxies lowercase it."""
    token = mint_token(keys.trusted)
    caller = await authenticator.authenticate(
        auth_header=f"bearer {token}", activity=activity
    )
    assert caller.app_id == BOT_APP_ID


# ==========================================================================
# Audience. A token minted for a DIFFERENT bot is a valid, correctly signed,
# unexpired Bot Connector token. Only `aud` distinguishes it.
# ==========================================================================


async def test_wrong_audience_is_rejected(authenticator, keys: KeyMaterial, activity):
    token = mint_token(keys.trusted, audience="99999999-8888-7777-6666-555555555555")

    with pytest.raises(InboundAuthError) as exc:
        await authenticator.authenticate(auth_header=f"Bearer {token}", activity=activity)

    assert exc.value.reason == "bad_audience"


async def test_missing_audience_claim_is_rejected(
    authenticator, keys: KeyMaterial, activity
):
    token = mint_token(keys.trusted, omit_claims=("aud",))

    with pytest.raises(InboundAuthError) as exc:
        await authenticator.authenticate(auth_header=f"Bearer {token}", activity=activity)

    # PyJWT raises MissingRequiredClaim for a required-but-absent claim.
    assert exc.value.reason in {"missing_required_claim", "bad_audience", "invalid_token"}


# ==========================================================================
# Expiry / nbf.
# ==========================================================================


async def test_expired_token_is_rejected(authenticator, keys: KeyMaterial, activity):
    # Well past the 5 minute skew allowance.
    token = mint_token(keys.trusted, expires_in=-3600, not_before_offset=-7200)

    with pytest.raises(InboundAuthError) as exc:
        await authenticator.authenticate(auth_header=f"Bearer {token}", activity=activity)

    assert exc.value.reason == "expired"


async def test_token_expired_within_clock_skew_is_accepted(
    authenticator, keys: KeyMaterial, activity
):
    """60 seconds stale is inside the documented 5-minute industry skew.

    Asserted explicitly because a future "tighten the skew to zero" change
    would break real Teams traffic on clock drift, and this test says so.
    """
    token = mint_token(keys.trusted, expires_in=-60, not_before_offset=-3600)
    caller = await authenticator.authenticate(
        auth_header=f"Bearer {token}", activity=activity
    )
    assert caller.app_id == BOT_APP_ID


async def test_not_yet_valid_token_is_rejected(authenticator, keys: KeyMaterial, activity):
    token = mint_token(keys.trusted, not_before_offset=3600, expires_in=7200)

    with pytest.raises(InboundAuthError) as exc:
        await authenticator.authenticate(auth_header=f"Bearer {token}", activity=activity)

    assert exc.value.reason == "not_yet_valid"


async def test_token_without_exp_is_rejected(authenticator, keys: KeyMaterial, activity):
    """A token with no `exp` never expires. Absent is as bad as wrong."""
    token = mint_token(keys.trusted, omit_claims=("exp",))

    with pytest.raises(InboundAuthError) as exc:
        await authenticator.authenticate(auth_header=f"Bearer {token}", activity=activity)

    assert exc.value.reason == "missing_required_claim"


# ==========================================================================
# alg: none - the unsecured JWS attack.
# ==========================================================================


async def test_alg_none_is_rejected(authenticator, activity):
    token = mint_alg_none_token()

    with pytest.raises(InboundAuthError) as exc:
        await authenticator.authenticate(auth_header=f"Bearer {token}", activity=activity)

    assert exc.value.reason == "alg_none_rejected"


async def test_alg_none_is_rejected_before_any_network_call(
    authenticator, jwks_server, activity
):
    """The rejection must not cost us a JWKS fetch.

    Otherwise a flood of `alg: none` tokens is a free amplification vector
    against login.botframework.com, using our egress.
    """
    before = jwks_server.metadata_hits
    with pytest.raises(InboundAuthError):
        await authenticator.authenticate(
            auth_header=f"Bearer {mint_alg_none_token()}", activity=activity
        )
    assert jwks_server.metadata_hits == before


async def test_symmetric_alg_is_rejected(
    authenticator, jwks_server, keys: KeyMaterial, activity
):
    """HS256 signed with the PUBLIC key - classic algorithm confusion.

    Our JWKS is public by design. If the validator picked its verification
    algorithm from the token's own `alg` header, it would fetch our published
    RSA public key, treat those bytes as an HMAC secret, and this token would
    verify. Anyone who can read a URL could then impersonate Azure Bot Service.

    Note the token is hand-built: PyJWT's encoder refuses to use a PEM as an
    HMAC secret. That is PyJWT's protection on the signing side and tells us
    nothing about our validator, so we bypass it.
    """
    from cryptography.hazmat.primitives import serialization

    public_pem = keys.trusted.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    forged = mint_hs256_token(public_pem, kid="test-key-1")

    before = jwks_server.metadata_hits
    with pytest.raises(InboundAuthError) as exc:
        await authenticator.authenticate(auth_header=f"Bearer {forged}", activity=activity)

    assert exc.value.reason == "disallowed_alg"
    assert "HS256" not in ALLOWED_ALGORITHMS
    # Rejected on the header check, before a key was ever fetched.
    assert jwks_server.metadata_hits == before


# ==========================================================================
# Signature: unknown key, and tampering.
# ==========================================================================


async def test_token_signed_by_unknown_key_is_rejected(
    authenticator, keys: KeyMaterial, activity
):
    """Attacker's own RSA key, advertising a `kid` we do not publish."""
    token = mint_token(keys.rogue, kid=ROGUE_KID)

    with pytest.raises(InboundAuthError) as exc:
        await authenticator.authenticate(auth_header=f"Bearer {token}", activity=activity)

    assert exc.value.reason == "unknown_kid"


async def test_token_signed_by_unknown_key_reusing_a_published_kid_is_rejected(
    authenticator, keys: KeyMaterial, activity
):
    """The nastier version: attacker's key, but claiming OUR `kid`.

    Key lookup succeeds and returns the real public key; only the signature
    check catches this. This is the test that proves verification is real.
    """
    token = mint_token(keys.rogue, kid="test-key-1")

    with pytest.raises(InboundAuthError) as exc:
        await authenticator.authenticate(auth_header=f"Bearer {token}", activity=activity)

    assert exc.value.reason == "bad_signature"


async def test_tampered_payload_is_rejected(authenticator, keys: KeyMaterial, activity):
    """Take a genuinely valid token and edit a claim, keeping the signature."""
    good = mint_token(keys.trusted)
    tampered = tamper_payload(good, aud="99999999-8888-7777-6666-555555555555")

    with pytest.raises(InboundAuthError) as exc:
        await authenticator.authenticate(
            auth_header=f"Bearer {tampered}", activity=activity
        )

    assert exc.value.reason == "bad_signature"


async def test_tampered_expiry_is_rejected(authenticator, keys: KeyMaterial, activity):
    """Expired token with `exp` pushed into the future. Signature must catch it."""
    expired = mint_token(keys.trusted, expires_in=-3600, not_before_offset=-7200)
    revived = tamper_payload(expired, exp=int(time.time()) + 3600)

    with pytest.raises(InboundAuthError) as exc:
        await authenticator.authenticate(auth_header=f"Bearer {revived}", activity=activity)

    assert exc.value.reason == "bad_signature"


async def test_tampered_service_url_is_rejected(authenticator, keys: KeyMaterial, activity):
    """Redirecting the conversation to an attacker host requires forging the
    `serviceurl` claim, which breaks the signature."""
    good = mint_token(keys.trusted)
    hijacked = tamper_payload(good, serviceurl="https://evil.example.com/")

    with pytest.raises(InboundAuthError) as exc:
        await authenticator.authenticate(auth_header=f"Bearer {hijacked}", activity=activity)

    assert exc.value.reason == "bad_signature"


# ==========================================================================
# Issuer.
# ==========================================================================


async def test_untrusted_issuer_is_rejected(authenticator, keys: KeyMaterial, activity):
    token = mint_token(keys.trusted, issuer="https://evil.example.com/")

    with pytest.raises(InboundAuthError) as exc:
        await authenticator.authenticate(auth_header=f"Bearer {token}", activity=activity)

    assert exc.value.reason == "untrusted_issuer"


async def test_missing_issuer_is_rejected(authenticator, keys: KeyMaterial, activity):
    token = mint_token(keys.trusted, omit_claims=("iss",))

    with pytest.raises(InboundAuthError) as exc:
        await authenticator.authenticate(auth_header=f"Bearer {token}", activity=activity)

    assert exc.value.reason == "missing_issuer"


# ==========================================================================
# serviceUrl binding (the token-replay-redirect defence).
# ==========================================================================


async def test_service_url_mismatch_is_rejected(authenticator, keys: KeyMaterial, activity):
    """Valid token, but the body points replies somewhere else."""
    token = mint_token(keys.trusted, service_url="https://smba.trafficmanager.net/emea/")
    activity["serviceUrl"] = "https://attacker.example.net/"

    with pytest.raises(InboundAuthError) as exc:
        await authenticator.authenticate(auth_header=f"Bearer {token}", activity=activity)

    assert exc.value.reason == "service_url_mismatch"


async def test_service_url_trailing_slash_difference_is_accepted(
    authenticator, keys: KeyMaterial, activity
):
    """Azure is inconsistent about the trailing slash; that is not an attack."""
    token = mint_token(keys.trusted, service_url="https://smba.trafficmanager.net/emea/")
    activity["serviceUrl"] = "https://smba.trafficmanager.net/emea"

    caller = await authenticator.authenticate(
        auth_header=f"Bearer {token}", activity=activity
    )
    assert caller.app_id == BOT_APP_ID


async def test_missing_service_url_claim_is_rejected(
    authenticator, keys: KeyMaterial, activity
):
    token = mint_token(keys.trusted, service_url=None)

    with pytest.raises(InboundAuthError) as exc:
        await authenticator.authenticate(auth_header=f"Bearer {token}", activity=activity)

    assert exc.value.reason == "missing_service_url_claim"


# ==========================================================================
# Header handling.
# ==========================================================================


@pytest.mark.parametrize(
    "header,expected",
    [
        (None, "missing_authorization_header"),
        ("", "missing_authorization_header"),
        ("Bearer", "malformed_authorization_header"),
        ("Basic dXNlcjpwYXNz", "malformed_authorization_header"),
        ("Bearer a b c", "malformed_authorization_header"),
        ("Bearer not-a-jwt", "malformed_token"),
        ("Bearer aaa.bbb.ccc", "malformed_token"),
    ],
)
async def test_bad_authorization_headers_are_rejected(authenticator, activity, header, expected):
    with pytest.raises(InboundAuthError) as exc:
        await authenticator.authenticate(auth_header=header, activity=activity)
    assert exc.value.reason == expected


async def test_token_without_kid_is_rejected(authenticator, keys: KeyMaterial, activity):
    import jwt as pyjwt

    now = int(time.time())
    token = pyjwt.encode(
        {
            "iss": BOT_FRAMEWORK_TOKEN_ISSUER,
            "aud": BOT_APP_ID,
            "iat": now,
            "nbf": now - 60,
            "exp": now + 3600,
            "serviceurl": SERVICE_URL,
        },
        keys.trusted,
        algorithm="RS256",
    )

    with pytest.raises(InboundAuthError) as exc:
        await authenticator.authenticate(auth_header=f"Bearer {token}", activity=activity)

    assert exc.value.reason == "missing_kid"


# ==========================================================================
# Construction-time refusals. Fail closed at startup, not at request time.
# ==========================================================================


def test_authenticator_refuses_empty_app_id():
    with pytest.raises(ValueError, match="app_id is required"):
        InboundActivityAuthenticator(app_id="", profiles=default_channel_profiles())


def test_authenticator_refuses_empty_profile_list():
    with pytest.raises(ValueError, match="at least one ChannelProfile"):
        InboundActivityAuthenticator(app_id=BOT_APP_ID, profiles=[])


def test_emulator_profile_requires_an_explicit_tenant():
    """A wildcard Entra issuer would trust every tenant on earth."""
    with pytest.raises(ValueError, match="requires an explicit tenant_id"):
        default_channel_profiles(allow_emulator=True)


def test_default_profiles_do_not_trust_entra_unless_asked():
    profiles = default_channel_profiles()
    assert [p.issuer for p in profiles] == [BOT_FRAMEWORK_TOKEN_ISSUER]
    assert profiles[0].require_service_url is True


def test_emulator_profiles_are_tenant_pinned():
    tenant = "00000000-0000-0000-0000-000000000000"
    profiles = default_channel_profiles(tenant_id=tenant, allow_emulator=True)
    issuers = [p.issuer for p in profiles]
    assert BOT_FRAMEWORK_TOKEN_ISSUER in issuers
    assert f"https://sts.windows.net/{tenant}/" in issuers
    assert f"https://login.microsoftonline.com/{tenant}/v2.0" in issuers
    # No wildcard/common issuer anywhere.
    assert not any("/common/" in i for i in issuers)


def test_allowed_algorithms_are_asymmetric_only():
    assert ALLOWED_ALGORITHMS == frozenset({"RS256", "RS384", "RS512"})
    assert not any(a.startswith("HS") for a in ALLOWED_ALGORITHMS)
    assert "none" not in {a.lower() for a in ALLOWED_ALGORITHMS}


# ==========================================================================
# JWKS cache behaviour.
# ==========================================================================


async def test_jwks_is_fetched_once_and_cached(
    authenticator, jwks_server, keys: KeyMaterial, activity
):
    token = mint_token(keys.trusted)
    await authenticator.authenticate(auth_header=f"Bearer {token}", activity=activity)
    await authenticator.authenticate(auth_header=f"Bearer {token}", activity=activity)
    await authenticator.authenticate(auth_header=f"Bearer {token}", activity=activity)

    assert jwks_server.metadata_hits == 1
    assert jwks_server.jwks_hits == 1


async def test_unknown_kid_triggers_exactly_one_refresh(
    authenticator, jwks_server, keys: KeyMaterial, activity
):
    """Key rollover must be survivable, but not a free DoS amplifier."""
    good = mint_token(keys.trusted)
    await authenticator.authenticate(auth_header=f"Bearer {good}", activity=activity)
    assert jwks_server.jwks_hits == 1

    rogue = mint_token(keys.rogue, kid=ROGUE_KID)
    with pytest.raises(InboundAuthError):
        await authenticator.authenticate(auth_header=f"Bearer {rogue}", activity=activity)
    assert jwks_server.jwks_hits == 2

    # A second unknown kid inside the rate-limit window must NOT refetch.
    with pytest.raises(InboundAuthError):
        await authenticator.authenticate(auth_header=f"Bearer {rogue}", activity=activity)
    assert jwks_server.jwks_hits == 2


async def test_key_rollover_is_picked_up_on_refresh(
    authenticator, jwks_server, keys: KeyMaterial, activity
):
    """After the IdP rotates its kid, a token under the new kid must validate."""
    await authenticator.authenticate(
        auth_header=f"Bearer {mint_token(keys.trusted)}", activity=activity
    )

    jwks_server.published_kid = "test-key-2"
    rolled = mint_token(keys.trusted, kid="test-key-2")

    caller = await authenticator.authenticate(
        auth_header=f"Bearer {rolled}", activity=activity
    )
    assert caller.app_id == BOT_APP_ID


async def test_unreachable_jwks_fails_closed(keys: KeyMaterial, activity):
    """No keys means no trust. Never 'accept and hope'."""
    profile = ChannelProfile(
        name="bot_connector",
        issuer=BOT_FRAMEWORK_TOKEN_ISSUER,
        # Port 1 on loopback: reliably nothing listening, no external network.
        metadata_url="http://127.0.0.1:1/openidconfiguration",
        require_service_url=True,
    )
    auth = InboundActivityAuthenticator(
        app_id=BOT_APP_ID, profiles=[profile], jwks_cache=JwksCache()
    )
    try:
        with pytest.raises(InboundAuthError) as exc:
            await auth.authenticate(
                auth_header=f"Bearer {mint_token(keys.trusted)}", activity=activity
            )
        assert exc.value.reason == "jwks_unavailable"
    finally:
        await auth.close()


async def test_metadata_advertising_only_bad_algs_fails_closed(
    jwks_server, keys: KeyMaterial, activity
):
    """If the IdP advertises nothing we accept, refuse rather than guess."""
    jwks_server.advertised_algs = ["HS256", "ES256"]
    profile = ChannelProfile(
        name="bot_connector",
        issuer=BOT_FRAMEWORK_TOKEN_ISSUER,
        metadata_url=jwks_server.metadata_url,
        require_service_url=True,
    )
    auth = InboundActivityAuthenticator(
        app_id=BOT_APP_ID, profiles=[profile], jwks_cache=JwksCache()
    )
    try:
        with pytest.raises(InboundAuthError) as exc:
            await auth.authenticate(
                auth_header=f"Bearer {mint_token(keys.trusted)}", activity=activity
            )
        assert exc.value.reason == "jwks_unavailable"
    finally:
        await auth.close()


# ==========================================================================
# There must be no bypass. This test exists so that adding one breaks CI.
# ==========================================================================


def test_there_is_no_validation_bypass_switch():
    """No parameter, attribute or variable named like a validation bypass.

    The Bot Connector docs say implementers should not expose a way to disable
    JWT validation. This is a tripwire, not a proof: if someone adds
    `allow_anonymous`, CI fails and they have to argue for it in review rather
    than landing it quietly.

    Uses the AST rather than a text grep, because a text grep also matches the
    module docstring that explains these flags deliberately do not exist -
    which would make the test fail on its own documentation.
    """
    import ast
    import inspect

    from app.auth import inbound

    tree = ast.parse(inspect.getsource(inbound))
    banned = {"allow_anonymous", "skip_validation", "disable_auth", "bypass_auth",
              "insecure", "trust_unverified"}

    identifiers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.arg):
            identifiers.add(node.arg)
        elif isinstance(node, ast.keyword) and node.arg:
            identifiers.add(node.arg)
        elif isinstance(node, ast.Name):
            identifiers.add(node.id)
        elif isinstance(node, ast.Attribute):
            identifiers.add(node.attr)

    offenders = sorted(identifiers & banned)
    assert offenders == [], f"validation bypass introduced: {offenders}"


def test_signature_verification_is_never_disabled_in_the_verify_path():
    """`verify_signature: False` must appear exactly once, in `_select_profile`.

    That one use is the unverified issuer read used ONLY to choose which JWKS
    to fetch; the issuer is re-imposed cryptographically afterwards. Any second
    occurrence means someone decoded a token without checking it, which is the
    whole vulnerability class this module exists to prevent.
    """
    import ast
    import inspect

    from app.auth import inbound

    tree = ast.parse(inspect.getsource(inbound))
    unverified_sites: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) and not isinstance(
            node, ast.AsyncFunctionDef
        ):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Dict):
                continue
            for key, value in zip(inner.keys, inner.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "verify_signature"
                    and isinstance(value, ast.Constant)
                    and value.value is False
                ):
                    unverified_sites.append(node.name)

    assert unverified_sites == ["_select_profile"], (
        "signature verification disabled outside the documented issuer-routing "
        f"read: {unverified_sites}"
    )
