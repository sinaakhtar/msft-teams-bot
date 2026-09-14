"""Test fixtures: a real RSA keypair, a real local JWKS server, real tokens.

Nothing here is mocked at the crypto layer. We generate an actual RSA-2048 key,
sign actual tokens with it, and serve an actual OpenID metadata document and
JWKS over an actual HTTP server on 127.0.0.1. The code under test performs its
normal HTTP fetch and its normal signature verification.

That matters because the failure mode we are guarding against is "the test
mocked out the exact line that was broken". Mocking `jwt.decode` would let a
validator that accepts `alg: none` pass a test suite that claims to reject it.

No external network is required or used - only loopback.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from cryptography.hazmat.primitives.asymmetric import rsa

# Make `app` importable when running pytest from the middle_tier directory
# without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import jwt  # noqa: E402
from app.auth.inbound import (  # noqa: E402
    BOT_FRAMEWORK_TOKEN_ISSUER,
    ChannelProfile,
    InboundActivityAuthenticator,
    JwksCache,
)

BOT_APP_ID = "11111111-2222-3333-4444-555555555555"
SERVICE_URL = "https://smba.trafficmanager.net/emea/"
PRIMARY_KID = "test-key-1"
ROGUE_KID = "rogue-key-1"


def _b64u_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _public_jwk(private_key: rsa.RSAPrivateKey, kid: str) -> dict[str, Any]:
    numbers = private_key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64u_uint(numbers.n),
        "e": _b64u_uint(numbers.e),
    }


class KeyMaterial:
    """A trusted keypair and an untrusted one, plus their JWKS document."""

    def __init__(self) -> None:
        self.trusted = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.rogue = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    @property
    def jwks(self) -> dict[str, Any]:
        # Only the trusted key is published. The rogue key exists but the
        # world does not know about it - exactly the attacker's position.
        return {"keys": [_public_jwk(self.trusted, PRIMARY_KID)]}


@pytest.fixture(scope="session")
def keys() -> KeyMaterial:
    return KeyMaterial()


class JwksServer:
    """A real aiohttp server on an ephemeral loopback port.

    Started and torn down per test. Counts hits so the cache tests can assert
    on fetch behaviour rather than guessing at it.
    """

    def __init__(self, keys: KeyMaterial) -> None:
        self._keys = keys
        self.metadata_hits = 0
        self.jwks_hits = 0
        self.published_kid = PRIMARY_KID
        self.advertised_algs: list[str] | None = ["RS256"]
        self._runner: web.AppRunner | None = None
        self.base_url = ""

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/openidconfiguration", self._metadata)
        app.router.add_get("/keys", self._jwks)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        sockets = list(self._runner.addresses)
        host, port = sockets[0][0], sockets[0][1]
        self.base_url = f"http://{host}:{port}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    @property
    def metadata_url(self) -> str:
        return f"{self.base_url}/openidconfiguration"

    async def _metadata(self, request: web.Request) -> web.Response:
        self.metadata_hits += 1
        doc: dict[str, Any] = {
            "issuer": BOT_FRAMEWORK_TOKEN_ISSUER,
            "jwks_uri": f"{self.base_url}/keys",
        }
        if self.advertised_algs is not None:
            doc["id_token_signing_alg_values_supported"] = self.advertised_algs
        return web.json_response(doc)

    async def _jwks(self, request: web.Request) -> web.Response:
        self.jwks_hits += 1
        return web.json_response(
            {"keys": [_public_jwk(self._keys.trusted, self.published_kid)]}
        )


@pytest.fixture
async def jwks_server(keys: KeyMaterial):
    server = JwksServer(keys)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


@pytest.fixture
def metadata_url(jwks_server: JwksServer) -> str:
    return jwks_server.metadata_url


@pytest.fixture
def profile(metadata_url: str) -> ChannelProfile:
    return ChannelProfile(
        name="bot_connector",
        issuer=BOT_FRAMEWORK_TOKEN_ISSUER,
        metadata_url=metadata_url,
        require_service_url=True,
    )


@pytest.fixture
async def authenticator(profile: ChannelProfile):
    """An authenticator wired to the local JWKS server.

    Closed on teardown so the shared aiohttp ClientSession does not leak. An
    unclosed session is only a ResourceWarning in tests, but the same leak in
    the Cloud Run process is a slow file-descriptor exhaustion, so the fixture
    exercises the real close path rather than ignoring the warning.
    """
    auth = InboundActivityAuthenticator(
        app_id=BOT_APP_ID,
        profiles=[profile],
        jwks_cache=JwksCache(ttl_seconds=3600),
    )
    try:
        yield auth
    finally:
        await auth.close()


@pytest.fixture
def activity() -> dict[str, Any]:
    """A realistic Teams message activity."""
    return {
        "type": "message",
        "id": "1700000000000",
        "channelId": "msteams",
        "serviceUrl": SERVICE_URL,
        "text": "who am I?",
        "from": {
            "id": "29:1a2b3c4d5e6f7a8b9c0d",
            "name": "Sina Nek Akhtar",
            "aadObjectId": "33333333-3333-3333-3333-333333333333",
        },
        "recipient": {"id": f"28:{BOT_APP_ID}", "name": "Data Assistant"},
        "conversation": {"id": "a:1abcdef", "tenantId": "00000000-0000-0000-0000-000000000000"},
        "channelData": {"tenant": {"id": "00000000-0000-0000-0000-000000000000"}},
    }


def mint_token(
    private_key: rsa.RSAPrivateKey,
    *,
    kid: str = PRIMARY_KID,
    audience: str = BOT_APP_ID,
    issuer: str = BOT_FRAMEWORK_TOKEN_ISSUER,
    service_url: str | None = SERVICE_URL,
    expires_in: int = 3600,
    not_before_offset: int = -60,
    algorithm: str = "RS256",
    extra_claims: dict[str, Any] | None = None,
    omit_claims: tuple[str, ...] = (),
) -> str:
    """Mint a token with the given properties. Used to build every attack case."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "nbf": now + not_before_offset,
        "exp": now + expires_in,
    }
    if service_url is not None:
        payload["serviceurl"] = service_url
    if extra_claims:
        payload.update(extra_claims)
    for claim in omit_claims:
        payload.pop(claim, None)

    return jwt.encode(payload, private_key, algorithm=algorithm, headers={"kid": kid})


def mint_hs256_token(
    secret: bytes,
    *,
    audience: str = BOT_APP_ID,
    issuer: str = BOT_FRAMEWORK_TOKEN_ISSUER,
    kid: str = PRIMARY_KID,
) -> str:
    """Hand-build an HS256 token. The algorithm-confusion attack.

    Built by hand because PyJWT's *encoder* refuses to use a PEM public key as
    an HMAC secret (a good defence, but it is PyJWT's defence, not ours). If we
    let that refusal stand in for a test, we would be asserting that PyJWT
    protects us on the signing side and learning nothing about our validator.

    The attacker's premise: our JWKS is public. If a validator selected its
    verification algorithm from the token's own `alg` header, it would take our
    published RSA public key, treat those bytes as an HMAC secret, and this
    token would verify. Our validator must reject it on the header check,
    before a key is ever fetched.
    """
    import hashlib
    import hmac

    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT", "kid": kid}
    payload = {
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "nbf": now - 60,
        "exp": now + 3600,
        "serviceurl": SERVICE_URL,
    }

    def seg(obj: dict[str, Any]) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode())
            .rstrip(b"=")
            .decode("ascii")
        )

    signing_input = f"{seg(header)}.{seg(payload)}".encode("ascii")
    mac = hmac.new(secret, signing_input, hashlib.sha256).digest()
    signature = base64.urlsafe_b64encode(mac).rstrip(b"=").decode("ascii")
    return f"{signing_input.decode('ascii')}.{signature}"


def mint_alg_none_token(
    *,
    audience: str = BOT_APP_ID,
    issuer: str = BOT_FRAMEWORK_TOKEN_ISSUER,
    kid: str = PRIMARY_KID,
) -> str:
    """Hand-build an unsecured JWS (`alg: none`, empty signature).

    PyJWT refuses to *encode* one without opting in, so we assemble it by hand.
    This is precisely the token a naive validator accepts.
    """
    now = int(time.time())
    header = {"alg": "none", "typ": "JWT", "kid": kid}
    payload = {
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "nbf": now - 60,
        "exp": now + 3600,
        "serviceurl": SERVICE_URL,
    }

    def seg(obj: dict[str, Any]) -> str:
        return (
            base64.urlsafe_b64encode(json.dumps(obj, separators=(",", ":")).encode())
            .rstrip(b"=")
            .decode("ascii")
        )

    return f"{seg(header)}.{seg(payload)}."


def tamper_payload(token: str, **overrides: Any) -> str:
    """Rewrite payload claims, keeping header and the ORIGINAL signature.

    The signature no longer matches the payload. Any validator that verifies
    the signature will reject this; any validator that decodes first and checks
    later will not.
    """
    header_b64, payload_b64, signature_b64 = token.split(".")
    pad = "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(payload_b64 + pad))
    payload.update(overrides)
    new_payload = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
        .rstrip(b"=")
        .decode("ascii")
    )
    return f"{header_b64}.{new_payload}.{signature_b64}"
