"""Inbound Bot Framework activity authentication. THE CROWN JEWELS.

THE THREAT
==========
`POST /api/messages` is a public, unauthenticated-by-network endpoint on the
open internet. The only thing standing between an attacker and full identity
impersonation is this module.

Downstream of here, `app.caller_identity` reads `from.aadObjectId` out of the activity
body and turns it into the Agent Runtime session key `entra:{tid}:{oid}`. That
key is what the identity broker exchanges for a Google access token, and that
token is what BigQuery MCP runs queries as. So:

    An activity that reaches the router without having been cryptographically
    proven to originate from Azure Bot Service is an attacker asserting an
    arbitrary Entra user, and reading that user's data out of BigQuery.

There is no second gate. `aadObjectId` is an attacker-controlled string in the
request body until this module says the body came from the Bot Connector. If
validation here is weak, every other identity control in the system is
decoration.

Consequences of that framing, which this module enforces:

  * FAIL CLOSED on every error path. Not one code path returns "unverified but
    probably fine". Any doubt raises :class:`InboundAuthError` and the caller
    turns that into a 401 with no body detail.
  * NO BYPASS SWITCH. There is deliberately no `allow_anonymous`,
    no `skip_validation`, no "if not app_id: return". The Bot Framework REST
    docs say implementers should not expose a way to disable JWT validation,
    and a dev-convenience flag is exactly how that gets exposed.
  * NO CLAIM IS READ FOR A SECURITY DECISION BEFORE THE SIGNATURE IS CHECKED.
    We read the *unverified* `iss` for one purpose only - selecting which
    OpenID metadata document to fetch keys from. The chosen document's issuer
    is then re-asserted as a hard `issuer=` constraint inside `jwt.decode`, so
    a lying `iss` can only route itself to a JWKS that will not validate it.

WHAT IS CHECKED (per the Bot Connector authentication spec)
===========================================================
  1. `Authorization: Bearer <token>` present and well formed.
  2. Header `alg` is on an explicit RSA allow-list. `none` is rejected before
     any key is fetched. `kid` must be present.
  3. Signing key resolved by `kid` from the JWKS advertised by the channel's
     OpenID metadata document. Unknown `kid` triggers exactly one forced cache
     refresh (key rollover is real), then rejection.
  4. Signature verified with the algorithms the metadata document advertises
     in `id_token_signing_alg_values_supported`, intersected with our
     allow-list. Never the algorithm the token asked for.
  5. `iss` equals the channel's expected issuer. `aud` equals the bot's
     Microsoft App ID, exactly.
  6. `exp` and `nbf` within a 5-minute clock skew (the industry-standard value
     the Bot Framework docs specify).
  7. `serviceUrl` claim, where the channel supplies one, must match the
     activity's `serviceUrl`. This is what stops a captured token being
     replayed to redirect a conversation at an attacker-controlled endpoint.
  8. For Entra-issued (emulator / single-tenant) tokens, the app id also
     appears in `appid` (v1) or `azp` (v2) and is checked there too.

SDK-vs-hand-rolled: see NOTES.md, ADR-006. Short version: the Bot Framework
Python SDK is archived and unmaintained, and the maintained successor's
validator has verified gaps. This module is deliberately small and fully
covered by offline tests so it can actually be audited.

References
----------
Authenticate requests with the Bot Connector API (Microsoft Learn):
https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-authentication
Bot Framework security FAQ (serviceUrl binding rationale):
https://learn.microsoft.com/en-us/azure/bot-service/bot-service-security-baseline
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

import aiohttp
import jwt
from jwt import PyJWK, PyJWTError

from ..logging_utils import fingerprint, log_event

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Constants. Values mirror Microsoft's AuthenticationConstants; they were read
# out of an installed `microsoft-agents-hosting-core` 1.5.0 rather than copied
# from a blog post.
# --------------------------------------------------------------------------

#: OpenID metadata for tokens minted by the Bot Connector for the public cloud.
TO_BOT_FROM_CHANNEL_OPENID_METADATA_URL = (
    "https://login.botframework.com/v1/.well-known/openidconfiguration"
)

#: OpenID metadata for tokens from the Bot Framework Emulator and from
#: single-tenant / managed-identity app types, which are Entra-issued.
TO_BOT_FROM_EMULATOR_OPENID_METADATA_URL = (
    "https://login.microsoftonline.com/common/v2.0/.well-known/openid-configuration"
)

#: Issuer stamped into Bot Connector channel tokens.
BOT_FRAMEWORK_TOKEN_ISSUER = "https://api.botframework.com"

#: Entra v1 / v2 issuer templates, for the single-tenant app type.
ENTRA_V1_ISSUER_TEMPLATE = "https://sts.windows.net/{tenant_id}/"
ENTRA_V2_ISSUER_TEMPLATE = "https://login.microsoftonline.com/{tenant_id}/v2.0"

#: The only signature algorithms we will ever accept. Asymmetric only: an HMAC
#: algorithm here would let anyone who can read the JWKS forge a token, which
#: is the classic `alg` confusion attack.
ALLOWED_ALGORITHMS: frozenset[str] = frozenset({"RS256", "RS384", "RS512"})

#: Bot Framework docs: "Industry-standard clock-skew is 5 minutes."
DEFAULT_CLOCK_SKEW_SECONDS = 300

#: How long a fetched metadata/JWKS pair is trusted before refetch.
DEFAULT_JWKS_TTL_SECONDS = 3600

#: Hard ceiling on how often an unknown `kid` may force a refetch, so a flood
#: of tokens with random `kid`s cannot be used to hammer login.botframework.com
#: (or to stall our own event loop).
MIN_FORCED_REFRESH_INTERVAL_SECONDS = 60


# --------------------------------------------------------------------------
# Errors. One type, several reasons. The reason is for OUR logs; the caller
# must not echo it to the client, because "wrong audience" vs "bad signature"
# is a free oracle.
# --------------------------------------------------------------------------


class InboundAuthError(Exception):
    """Inbound activity failed authentication. Always terminal, always 401.

    :param reason: short machine-ish token for logs/metrics, e.g. ``bad_audience``.
    :param detail: human detail for logs only. NEVER returned to the caller.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


# --------------------------------------------------------------------------
# Channel profiles
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelProfile:
    """One trusted token source: an issuer bound to an OpenID metadata document.

    :param name: label for logs, e.g. ``bot_connector``.
    :param issuer: the exact ``iss`` value this profile accepts. Enforced
        cryptographically by ``jwt.decode(issuer=...)`` after key resolution,
        not merely inspected.
    :param metadata_url: OpenID metadata document to pull ``jwks_uri`` from.
    :param require_service_url: whether a ``serviceUrl`` claim must be present
        and must match the activity's ``serviceUrl``. True for Bot Connector
        tokens, which always carry one.
    :param require_app_id_claim: whether the app id must also appear in
        ``appid``/``azp``. True for Entra-issued tokens (emulator,
        single-tenant), which is where that convention comes from.
    """

    name: str
    issuer: str
    metadata_url: str
    require_service_url: bool = False
    require_app_id_claim: bool = False


def default_channel_profiles(
    *,
    tenant_id: str | None = None,
    allow_emulator: bool = False,
) -> tuple[ChannelProfile, ...]:
    """Build the trusted-issuer set.

    The Bot Connector profile is always present. Entra-issued profiles (used by
    the Bot Framework Emulator and by the single-tenant / user-assigned managed
    identity `MicrosoftAppType`s) are only added when explicitly enabled, and
    are always pinned to a single tenant. A wildcard Entra issuer would let any
    Entra tenant on earth mint a token for our bot.
    """
    profiles: list[ChannelProfile] = [
        ChannelProfile(
            name="bot_connector",
            issuer=BOT_FRAMEWORK_TOKEN_ISSUER,
            metadata_url=TO_BOT_FROM_CHANNEL_OPENID_METADATA_URL,
            require_service_url=True,
            require_app_id_claim=False,
        )
    ]
    if allow_emulator:
        if not tenant_id:
            raise ValueError(
                "allow_emulator=True requires an explicit tenant_id; a wildcard "
                "Entra issuer would trust every tenant in the world."
            )
        for template in (ENTRA_V1_ISSUER_TEMPLATE, ENTRA_V2_ISSUER_TEMPLATE):
            profiles.append(
                ChannelProfile(
                    name="entra_single_tenant",
                    issuer=template.format(tenant_id=tenant_id),
                    metadata_url=TO_BOT_FROM_EMULATOR_OPENID_METADATA_URL,
                    require_service_url=False,
                    require_app_id_claim=True,
                )
            )
    return tuple(profiles)


# --------------------------------------------------------------------------
# JWKS cache
# --------------------------------------------------------------------------


@dataclass
class _CachedKeys:
    keys_by_kid: dict[str, PyJWK]
    algorithms: frozenset[str]
    fetched_at: float
    last_forced_refresh: float = 0.0


class JwksCache:
    """Fetches and caches OpenID metadata + JWKS per metadata URL.

    Concurrency: one asyncio lock per metadata URL, so a burst of first
    requests results in one fetch, not N.

    Every network failure is fatal to the request that triggered it. A stale
    cache is NOT served past its TTL on error: "we could not reach the IdP" is
    not a reason to start trusting tokens we cannot verify.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_JWKS_TTL_SECONDS,
        session_factory=None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        self._cache: dict[str, _CachedKeys] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._session_factory = session_factory
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session_factory is not None:
            return self._session_factory()
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._timeout)
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    def _lock(self, url: str) -> asyncio.Lock:
        lock = self._locks.get(url)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[url] = lock
        return lock

    async def _fetch(self, metadata_url: str) -> _CachedKeys:
        session = await self._get_session()
        owns_session = self._session_factory is not None
        try:
            if owns_session:
                async with session:
                    return await self._fetch_with(session, metadata_url)
            return await self._fetch_with(session, metadata_url)
        except InboundAuthError:
            raise
        except Exception as exc:
            raise InboundAuthError(
                "jwks_unavailable", f"{type(exc).__name__} fetching {metadata_url}"
            ) from exc

    async def _fetch_with(
        self, session: aiohttp.ClientSession, metadata_url: str
    ) -> _CachedKeys:
        async with session.get(metadata_url) as resp:
            if resp.status != 200:
                raise InboundAuthError(
                    "jwks_unavailable",
                    f"metadata {metadata_url} returned HTTP {resp.status}",
                )
            metadata = await resp.json(content_type=None)

        jwks_uri = metadata.get("jwks_uri")
        if not jwks_uri:
            raise InboundAuthError(
                "jwks_unavailable", f"metadata {metadata_url} has no jwks_uri"
            )

        # Only accept the algorithms the IdP itself advertises, intersected
        # with our own allow-list. If the document advertises nothing usable we
        # do NOT silently fall back to "all of ours" - we refuse.
        advertised = metadata.get("id_token_signing_alg_values_supported") or []
        algorithms = frozenset(a for a in advertised if a in ALLOWED_ALGORITHMS)
        if not algorithms:
            if advertised:
                raise InboundAuthError(
                    "jwks_unavailable",
                    f"metadata {metadata_url} advertises no acceptable alg "
                    f"(got {sorted(advertised)})",
                )
            # Some Bot Framework metadata documents omit the field entirely.
            # Falling back to our own conservative allow-list is safe: it is
            # RSA-only, so there is no algorithm-confusion path.
            algorithms = ALLOWED_ALGORITHMS

        async with session.get(jwks_uri) as resp:
            if resp.status != 200:
                raise InboundAuthError(
                    "jwks_unavailable", f"jwks {jwks_uri} returned HTTP {resp.status}"
                )
            jwks = await resp.json(content_type=None)

        keys_by_kid: dict[str, PyJWK] = {}
        for raw in jwks.get("keys", []):
            kid = raw.get("kid")
            if not kid:
                continue
            # Skip keys we structurally cannot use rather than exploding: a
            # single odd entry in the document must not deny service.
            if raw.get("kty") != "RSA":
                continue
            try:
                keys_by_kid[kid] = PyJWK.from_dict(raw)
            except Exception:  # pragma: no cover - malformed key entry
                continue

        if not keys_by_kid:
            raise InboundAuthError(
                "jwks_unavailable", f"jwks {jwks_uri} contained no usable RSA keys"
            )

        return _CachedKeys(
            keys_by_kid=keys_by_kid,
            algorithms=algorithms,
            fetched_at=time.monotonic(),
        )

    async def get_key(self, metadata_url: str, kid: str) -> tuple[PyJWK, frozenset[str]]:
        """Resolve `kid` for `metadata_url`, refreshing at most once on a miss.

        :raises InboundAuthError: ``jwks_unavailable`` if the documents cannot
            be fetched, ``unknown_kid`` if the key is genuinely not published.
        """
        async with self._lock(metadata_url):
            cached = self._cache.get(metadata_url)
            now = time.monotonic()

            if cached is None or (now - cached.fetched_at) > self._ttl:
                cached = await self._fetch(metadata_url)
                self._cache[metadata_url] = cached

            key = cached.keys_by_kid.get(kid)
            if key is not None:
                return key, cached.algorithms

            # Unknown kid. Could be a legitimate key rollover, could be an
            # attacker probing. Allow one forced refresh, rate limited.
            if (now - cached.last_forced_refresh) >= MIN_FORCED_REFRESH_INTERVAL_SECONDS:
                refreshed = await self._fetch(metadata_url)
                refreshed.last_forced_refresh = now
                self._cache[metadata_url] = refreshed
                key = refreshed.keys_by_kid.get(kid)
                if key is not None:
                    return key, refreshed.algorithms

            raise InboundAuthError("unknown_kid", f"kid not published by {metadata_url}")


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthenticatedCaller:
    """Proof that an activity body came from a trusted channel.

    Holding one of these is the ONLY licence to read identity fields out of the
    activity. `app.caller_identity.caller_from_activity` demands one.
    """

    app_id: str
    issuer: str
    profile_name: str
    service_url: str | None
    claims: Mapping[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# The authenticator
# --------------------------------------------------------------------------


class InboundActivityAuthenticator:
    """Validates the `Authorization` header of an inbound Bot Framework POST.

    :param app_id: the bot's Microsoft App ID. Every accepted token's ``aud``
        must equal this exactly.
    :param profiles: trusted issuers. Empty is a configuration error, not an
        "allow everything" shortcut.
    """

    def __init__(
        self,
        *,
        app_id: str,
        profiles: Sequence[ChannelProfile],
        jwks_cache: JwksCache | None = None,
        clock_skew_seconds: int = DEFAULT_CLOCK_SKEW_SECONDS,
    ) -> None:
        if not app_id:
            # Deliberate: no app id means we cannot check `aud`, and a bot that
            # cannot check `aud` accepts tokens minted for any other bot.
            raise ValueError("app_id is required; refusing to run without audience validation")
        if not profiles:
            raise ValueError("at least one ChannelProfile is required")

        self._app_id = app_id
        self._profiles_by_issuer: dict[str, ChannelProfile] = {p.issuer: p for p in profiles}
        self._jwks = jwks_cache or JwksCache()
        self._skew = clock_skew_seconds

    @property
    def jwks_cache(self) -> JwksCache:
        return self._jwks

    async def close(self) -> None:
        await self._jwks.close()

    # -- step 1 -----------------------------------------------------------
    @staticmethod
    def _extract_bearer(auth_header: str | None) -> str:
        if not auth_header:
            raise InboundAuthError("missing_authorization_header")
        parts = auth_header.strip().split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            raise InboundAuthError("malformed_authorization_header")
        token = parts[1]
        if not token:
            raise InboundAuthError("malformed_authorization_header", "empty bearer value")
        return token

    # -- step 2 -----------------------------------------------------------
    @staticmethod
    def _inspect_header(token: str) -> tuple[str, str]:
        """Return ``(kid, alg)`` from the JOSE header, rejecting junk early.

        Reading the header before verifying is unavoidable - you cannot pick a
        key without `kid`. What matters is that nothing here is *trusted*: the
        `alg` we read is only used to reject, never to select a verifier.
        """
        try:
            header = jwt.get_unverified_header(token)
        except PyJWTError as exc:
            raise InboundAuthError("malformed_token", type(exc).__name__) from exc

        alg = header.get("alg")
        if not isinstance(alg, str):
            raise InboundAuthError("missing_alg")
        if alg.lower() == "none":
            # Explicit, loud, and before any I/O. The unsecured-JWS attack.
            raise InboundAuthError("alg_none_rejected")
        if alg not in ALLOWED_ALGORITHMS:
            raise InboundAuthError("disallowed_alg", alg)

        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise InboundAuthError("missing_kid")
        return kid, alg

    # -- step 3 -----------------------------------------------------------
    def _select_profile(self, token: str) -> ChannelProfile:
        """Pick the metadata document by the token's *unverified* issuer.

        This is routing, not authorization. The selected profile's issuer is
        re-imposed as a hard constraint during `jwt.decode`, so a forged `iss`
        can only route the token to a JWKS under which it will fail to verify.
        """
        try:
            unverified = jwt.decode(
                token, options={"verify_signature": False, "verify_exp": False}
            )
        except PyJWTError as exc:
            raise InboundAuthError("malformed_token", type(exc).__name__) from exc

        issuer = unverified.get("iss")
        if not isinstance(issuer, str):
            raise InboundAuthError("missing_issuer")
        profile = self._profiles_by_issuer.get(issuer)
        if profile is None:
            raise InboundAuthError("untrusted_issuer", issuer)
        return profile

    # -- steps 4-8 --------------------------------------------------------
    async def authenticate(
        self,
        *,
        auth_header: str | None,
        activity: Mapping[str, Any] | None,
    ) -> AuthenticatedCaller:
        """Validate the header against the activity. Raise or return proof.

        :raises InboundAuthError: on ANY failure. There is no partial success
            and no unverified return value.
        """
        token = self._extract_bearer(auth_header)
        token_fp = fingerprint(token)

        kid, _alg = self._inspect_header(token)
        profile = self._select_profile(token)

        key, metadata_algorithms = await self._jwks.get_key(profile.metadata_url, kid)

        try:
            claims = jwt.decode(
                token,
                key=key.key,
                algorithms=sorted(metadata_algorithms),
                audience=self._app_id,
                issuer=profile.issuer,
                leeway=self._skew,
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iat": False,
                    "verify_aud": True,
                    "verify_iss": True,
                    # Absent claims are as bad as wrong ones. A token with no
                    # `exp` never expires.
                    "require": ["exp", "iss", "aud"],
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise InboundAuthError("expired", str(exc)) from exc
        except jwt.ImmatureSignatureError as exc:
            raise InboundAuthError("not_yet_valid", str(exc)) from exc
        except jwt.InvalidAudienceError as exc:
            raise InboundAuthError("bad_audience", str(exc)) from exc
        except jwt.InvalidIssuerError as exc:
            raise InboundAuthError("bad_issuer", str(exc)) from exc
        except jwt.MissingRequiredClaimError as exc:
            raise InboundAuthError("missing_required_claim", str(exc)) from exc
        except jwt.InvalidSignatureError as exc:
            raise InboundAuthError("bad_signature", str(exc)) from exc
        except PyJWTError as exc:
            # Catch-all so a new PyJWT error subclass can never become an
            # accidental success.
            raise InboundAuthError("invalid_token", type(exc).__name__) from exc

        self._check_app_id_claim(claims, profile)
        service_url = self._check_service_url(claims, activity, profile)

        log_event(
            logger,
            logging.INFO,
            "inbound activity authenticated",
            profile=profile.name,
            issuer=profile.issuer,
            token_fingerprint=token_fp,
            service_url_host=_host_of(service_url),
        )

        return AuthenticatedCaller(
            app_id=self._app_id,
            issuer=profile.issuer,
            profile_name=profile.name,
            service_url=service_url,
            claims=dict(claims),
        )

    # ---------------------------------------------------------------------
    def _check_app_id_claim(
        self, claims: Mapping[str, Any], profile: ChannelProfile
    ) -> None:
        """Entra-issued tokens carry the app id in `appid` (v1) or `azp` (v2)."""
        if not profile.require_app_id_claim:
            return
        candidate = claims.get("appid") or claims.get("azp")
        if candidate != self._app_id:
            raise InboundAuthError("bad_app_id_claim", "appid/azp does not match app_id")

    def _check_service_url(
        self,
        claims: Mapping[str, Any],
        activity: Mapping[str, Any] | None,
        profile: ChannelProfile,
    ) -> str | None:
        """Bind the token to the activity's `serviceUrl`.

        Without this, a token captured from one conversation can be replayed
        with a body pointing at an attacker-controlled `serviceUrl`, and the
        bot will happily post its replies there.
        """
        claim_url = claims.get("serviceurl") or claims.get("serviceUrl")

        if not profile.require_service_url:
            return claim_url if isinstance(claim_url, str) else None

        if not isinstance(claim_url, str) or not claim_url:
            raise InboundAuthError("missing_service_url_claim")

        activity_url = (activity or {}).get("serviceUrl")
        if not isinstance(activity_url, str) or not activity_url:
            raise InboundAuthError("missing_activity_service_url")

        if _normalize_service_url(claim_url) != _normalize_service_url(activity_url):
            # Deliberately NOT a warn-and-continue. The maintained Microsoft
            # SDK downgrades this to a log line unless a host validator is
            # configured; we do not.
            raise InboundAuthError(
                "service_url_mismatch",
                f"claim host {_host_of(claim_url)} != activity host {_host_of(activity_url)}",
            )
        return claim_url


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _normalize_service_url(url: str) -> str:
    """Compare scheme+host+port, case-insensitively, ignoring a trailing slash.

    Azure Bot Service is inconsistent about the trailing slash between the
    claim and the activity body, so an exact string compare produces false
    rejections. Path is intentionally excluded from the comparison: the
    security property we need is "replies go to the same host", and the host
    is what an attacker would have to change to redirect a conversation.
    """
    parts = urlsplit(url.strip())
    return f"{parts.scheme.lower()}://{(parts.netloc or '').lower()}"


def _host_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        return urlsplit(url).netloc.lower() or None
    except Exception:  # pragma: no cover
        return None


def build_authenticator(
    *,
    app_id: str,
    tenant_id: str | None = None,
    allow_emulator: bool = False,
    jwks_cache: JwksCache | None = None,
) -> InboundActivityAuthenticator:
    """Convenience constructor used by `app.main`."""
    return InboundActivityAuthenticator(
        app_id=app_id,
        profiles=default_channel_profiles(
            tenant_id=tenant_id, allow_emulator=allow_emulator
        ),
        jwks_cache=jwks_cache,
    )


__all__: Iterable[str] = (
    "ALLOWED_ALGORITHMS",
    "AuthenticatedCaller",
    "ChannelProfile",
    "InboundActivityAuthenticator",
    "InboundAuthError",
    "JwksCache",
    "build_authenticator",
    "default_channel_profiles",
)
