"""Stage 1: the Entra On-Behalf-Of exchange. Re-audiencing, nothing else.

WHY OBO IS IN THIS DESIGN (READ THIS BEFORE "FIXING" IT)
--------------------------------------------------------
OBO is here to RE-AUDIENCE a token across the Microsoft -> Google trust
boundary. It is **NOT** here to reach Microsoft Graph. Nothing in this package
calls Graph, wants to call Graph, or should be edited towards calling Graph.
If you are reading this because you were about to add ``User.Read`` or
``https://graph.microsoft.com/.default`` to the scope, stop: that request
returns a Graph token, whose ``aud`` is Graph, which Google will refuse.

The reason is a single sentence in Google's own API reference for a workforce
pool OIDC provider (``Oidc.clientId``):

    "Required. The client ID. Must match the audience claim of the JWT issued
     by the identity provider."
    https://cloud.google.com/iam/docs/reference/rest/v1/locations.workforcePools.providers

There is exactly one acceptable ``aud`` value, and on our pool it is the
FEDERATION app ``11111111-1111-1111-1111-111111111111``. A Teams SSO token
carries the BOT app's audience instead, because that is what Teams issues it
for. So the Teams token cannot be sent to Google as-is, at all, ever. OBO is
the mechanism Entra provides for turning a token audienced to app A into a
token audienced to app B for the same user, and that - only that - is why the
hop exists.

Note also what is NOT available to us: the workforce-pool ``Oidc`` message has
fields ``issuerUri``, ``clientId``, ``clientSecret``, ``webSsoConfig``,
``jwksJson`` and **no ``allowedAudiences``**. Workload identity pool providers
do have ``oidc.allowedAudiences`` and people reach for it from memory; it does
not exist on the workforce side. One audience. Exact match. No list.

THE AUDIENCE RISK - MEASURED LIVE, AND IT IS THE ISSUER
--------------------------------------------------------
The STS hop is verified working with an Entra **ID token** (aud = bare
federation client-ID GUID, iss = ``.../v2.0``). An OBO exchange returns an
**access token** instead, so the question is what claims that carries.

On 2026-09-07 this was measured rather than reasoned about. An Entra access
token for the federation app was minted and fed to the real Google STS
endpoint. Both tokens, side by side, same user, same moment:

    ID token       aud = 11111111-1111-1111-1111-111111111111   ver 2.0
                   iss = https://login.microsoftonline.com/{tid}/v2.0
                   => STS ACCEPTED. BigQuery SESSION_USER() confirmed the
                      workforce principal.

    ACCESS token   aud = 11111111-1111-1111-1111-111111111111   ver 1.0
                   iss = https://sts.windows.net/{tid}/
                   => STS REFUSED, HTTP 400 invalid_grant:
                      "The issuer in ID Token https://sts.windows.net/{tid}
                       does not match the expected one in config:
                       https://login.microsoftonline.com/{tid}/v2.0"

Read that carefully, because the received wisdom is wrong: **the audience was
already correct**. The v1 access token's ``aud`` was the bare client-ID GUID,
not an ``api://`` URI. What breaks the chain is the **issuer**. The federation
app is currently on ``api.requestedAccessTokenVersion`` 1/null, so Entra
stamps v1 access tokens with the legacy ``sts.windows.net`` issuer, and the
workforce pool provider's ``issuerUri`` is the v2.0 one.

The fix is a one-field change on the FEDERATION app registration:

    api.requestedAccessTokenVersion = 2

which flips ``iss`` to ``https://login.microsoftonline.com/{tid}/v2.0`` and
leaves ``aud`` as the bare GUID. It has NOT yet been applied or verified - see
NOTES.md, which carries the exact command and the decisive test. Until it is,
the OBO hop cannot feed STS, and this module's pre-flight check is what makes
that legible instead of mysterious.

Also measured: ``subject_token_type`` is not the lever. The same access token
was rejected identically as ``...:id_token`` and as ``...:jwt``, with the same
issuer error. Google validates the JWT the same way either way.

"CAN WE JUST ASK OBO FOR AN id_token INSTEAD?" - NO, AND HERE IS WHY
---------------------------------------------------------------------
Adding ``openid`` to the OBO scope does make Entra return an ``id_token``
alongside the access token. It does not help, and believing it does is the
trap this comment exists to spring:

  * An ID token's ``aud`` is, by OIDC Core 1.0 s2, the ``client_id`` of the
    client the token was issued TO - never the resource being called.
    https://openid.net/specs/openid-connect-core-1_0.html#IDToken
  * In an OBO request the client is the middle tier authenticating with its
    own credentials, i.e. the **BOT** app. So the returned ``id_token`` has
    ``aud`` = the bot app: the exact audience we invoked OBO to get away from.
  * Measured corroboration, from the same 2026-09-07 probe: a token request
    made with ``client_id`` = the FEDERATION app returned an ``id_token``
    whose ``aud`` was the federation app, while the ``access_token`` in the
    very same response was audienced at the federation app as the *resource*.
    The ID token's audience tracked the CLIENT. Point the client at the bot
    app - which is what OBO does - and the ID token's audience follows it
    there.
  * ``layer3/tokens.py`` in this repo is the same evidence from the other
    direction: the ID token it mints is Google-acceptable precisely because
    it passes ``client_id = FEDERATION_CLIENT_ID``.

The only way an OBO-issued ``id_token`` would carry the federation audience is
if the federation app itself performed the OBO - which would require the Teams
SSO assertion to already be audienced to the federation app, which is the
premise we do not have. The access-token route with
``requestedAccessTokenVersion = 2`` is therefore the only viable path here,
and this module implements that route and refuses to guess about the other.

RESIDUAL UNCERTAINTY, STATED PLAINLY
------------------------------------
What is established by execution: a v1 access token is refused, on the issuer,
and the token-type label makes no difference.

What is NOT established: that a **v2** access token is accepted. Nobody has
minted one, because that needs the app-registration change above and admin
rights on the tenant. It is highly likely - a v2 access token for a custom API
is an RS256 JWT signed by the same tenant keys, published at the same JWKS
URI, carrying the same ``iss`` and ``oid`` as the ID token that IS accepted -
but "highly likely" is not "verified", and this comment will not pretend
otherwise. NOTES.md carries the exact decisive test.
:func:`describe_assertion` exists to make that test a thirty-second job.

WHAT THIS MODULE LOGS
---------------------
Fingerprints (``app.logging_utils.fingerprint``: SHA-256, first 12 hex) and
claim values that are not credentials - ``aud``, ``iss``, ``ver``, ``oid``,
``exp``. Never a token, never a secret, not even at DEBUG.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Mapping

import aiohttp

from ..logging_utils import fingerprint, log_event
from .errors import (
    OboAudienceMismatch,
    OboConsentRequired,
    OboExchangeError,
    OboTransientError,
    PreconditionError,
)

_log = logging.getLogger(__name__)

#: OAuth 2.0 On-Behalf-Of, RFC 7523 s2.1 JWT bearer grant.
OBO_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"

#: Tells Entra the assertion is an app-issued access token being delegated.
OBO_REQUESTED_TOKEN_USE = "on_behalf_of"

#: Entra error codes that mean "the human must do something".
#: AADSTS65001 - no consent recorded for this app/scope.
#: AADSTS50076/50079 - MFA required. AADSTS50105 - not assigned to the app.
_INTERACTION_SUBERRORS = frozenset({"consent_required", "interaction_required", "basic_action"})
_INTERACTION_AADSTS = ("AADSTS65001", "AADSTS50076", "AADSTS50079", "AADSTS50105", "AADSTS53000")


@dataclass(frozen=True)
class OboConfig:
    """Everything the OBO hop needs. Constructed once at startup.

    :param tenant_id: Entra tenant GUID. The token endpoint is tenant-scoped
        on purpose: ``/common`` would accept assertions from any tenant, and
        the workforce pool provider trusts exactly one issuer.
    :param client_id: the **BOT** app registration - the confidential client
        that holds the secret and is the audience of the Teams SSO assertion.
    :param client_secret: the bot app's secret. From Secret Manager only
        (see ``app.config``); never from disk, never from an env var in prod.
    :param federation_app_id: the **FEDERATION** app - the resource we are
        re-audiencing towards, and the value the workforce pool provider's
        ``clientId`` is set to.
    :param scope: the delegated scope requested from the federation app.
        ``{federation_app_id}/.default`` asks for every statically-consented
        delegated permission on that app, which is what a middle tier wants:
        no incremental-consent surprises at runtime.
    """

    tenant_id: str
    client_id: str
    client_secret: str
    federation_app_id: str
    scope: str | None = None
    timeout_seconds: float = 10.0
    #: Fail fast if the returned token is not usable against Google. Off only
    #: for a deliberate diagnostic run; never off in production.
    enforce_audience: bool = True

    @property
    def token_endpoint(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"

    @property
    def effective_scope(self) -> str:
        # NOT "https://graph.microsoft.com/.default". See module docstring.
        return self.scope or f"{self.federation_app_id}/.default"

    @property
    def expected_issuer(self) -> str:
        """The v2.0 issuer. A v1.0 token would say ``sts.windows.net`` here."""
        return f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"OboConfig(tenant_id={self.tenant_id!r}, client_id={self.client_id!r}, "
            f"federation_app_id={self.federation_app_id!r}, "
            f"client_secret=[REDACTED], scope={self.effective_scope!r})"
        )


@dataclass(frozen=True)
class OboResult:
    """The re-audienced token plus the claim facts worth logging."""

    #: The token handed to STS. Never logged, never persisted.
    token: str
    #: Which OAuth field it came out of, "access_token" or "id_token".
    token_kind: str
    expires_in: int
    aud: Any
    iss: str | None
    oid: str | None
    ver: str | None

    @property
    def token_fingerprint(self) -> str:
        return fingerprint(self.token)


# --------------------------------------------------------------------------
# JWT introspection - unverified, diagnostic only
# --------------------------------------------------------------------------


def decode_claims_unverified(token: str) -> dict[str, Any]:
    """Base64url-decode a JWT payload WITHOUT verifying the signature.

    Legitimate here and nowhere else: we are the client, not the resource
    server. Google verifies this token properly a few milliseconds later, and
    the only decisions taken on these claims are (a) refuse early on an
    audience mismatch and (b) write ``aud``/``iss`` into a log line. Neither
    grants access to anything. ``app.auth.inbound`` is where real verification
    lives, and it does full RS256 signature checking against a JWKS.

    :raises PreconditionError: the value is not a decodable JWT.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise PreconditionError(
            f"value is not a decodable JWT ({type(exc).__name__})",
        ) from None
    if not isinstance(claims, dict):
        raise PreconditionError("JWT payload is not a JSON object")
    return claims


def describe_assertion(token: str) -> dict[str, Any]:
    """Non-sensitive claim summary for diagnostics. Contains no credential.

    This is the fast way to answer the audience question for real:

        >>> describe_assertion(obo_access_token)
        {'aud': '11111111-...', 'iss': 'https://login.microsoftonline.com/00000000-.../v2.0',
         'ver': '2.0', 'oid': '33333333-...', 'tid': '00000000-...', ...}

    Safe to print and paste into a ticket. Contains no signature, no scopes
    that could identify a secret, and no token material.
    """
    claims = decode_claims_unverified(token)
    out = {
        key: claims.get(key)
        for key in ("aud", "iss", "ver", "oid", "tid", "sub", "appid", "azp", "typ")
    }
    exp = claims.get("exp")
    out["exp"] = exp
    if isinstance(exp, (int, float)):
        out["expires_in_seconds"] = int(exp - time.time())
    out["fingerprint"] = fingerprint(token)
    return out


def check_google_audience(
    token: str,
    *,
    expected_aud: str,
    expected_iss: str,
    user_key: str | None = None,
) -> dict[str, Any]:
    """Pre-flight: will Google's STS accept this token's ``aud`` and ``iss``?

    Replicates the two checks the workforce pool provider performs that we can
    perform locally, and raises :class:`OboAudienceMismatch` naming both the
    value we got and the value the provider wants. Without this, the same
    misconfiguration surfaces as an STS ``400 invalid_request`` that mentions
    neither claim and sends people to read Google's IAM docs about a
    Microsoft app-registration problem.

    ``aud`` may legitimately be a list (RFC 7519 s4.1.3); a match on any entry
    is a match, which is also how Google treats it.

    :returns: the claim summary, so the caller can log it on the happy path.
    """
    claims = decode_claims_unverified(token)
    aud = claims.get("aud")
    iss = claims.get("iss")
    aud_values = aud if isinstance(aud, list) else [aud]

    if expected_aud not in aud_values or iss != expected_iss:
        raise OboAudienceMismatch(
            got_aud=aud,
            expected_aud=expected_aud,
            got_iss=iss,
            expected_iss=expected_iss,
            user_key=user_key,
        )
    return claims


# --------------------------------------------------------------------------
# The exchange
# --------------------------------------------------------------------------


class OboExchanger:
    """Performs the Entra OBO exchange over a caller-supplied aiohttp session.

    The session is injected rather than created per call: TLS handshakes to
    ``login.microsoftonline.com`` on every turn would dominate latency, and a
    per-call session is the classic aiohttp file-descriptor leak.
    """

    def __init__(self, config: OboConfig, session: aiohttp.ClientSession) -> None:
        self._config = config
        self._session = session

    async def exchange(self, *, teams_sso_token: str, user_key: str | None = None) -> OboResult:
        """Re-audience a Teams SSO token towards the federation app.

        THE EXACT REQUEST BODY (form-encoded, POST to the tenant v2.0 token
        endpoint). Every field is load-bearing:

            grant_type            urn:ietf:params:oauth:grant-type:jwt-bearer
            client_id             <BOT app id>          the confidential client
            client_secret         <BOT app secret>      from Secret Manager
            assertion             <Teams SSO token>     aud = BOT app
            scope                 <FED app id>/.default the re-audiencing target
            requested_token_use   on_behalf_of

        Reference: "Microsoft identity platform and OAuth2.0 On-Behalf-Of
        flow", https://learn.microsoft.com/entra/identity-platform/v2-oauth2-on-behalf-of-flow

        DO NOT add ``https://graph.microsoft.com/.default`` to ``scope``. This
        hop has nothing to do with Graph; see the module docstring.

        :raises OboConsentRequired: the user must consent / complete MFA.
        :raises OboAudienceMismatch: the token came back audienced somewhere
            Google will not accept. Configuration fault, not a user fault.
        :raises OboTransientError: Entra 5xx, throttle, or timeout.
        :raises OboExchangeError: anything else.
        """
        if not teams_sso_token:
            raise PreconditionError("no Teams SSO assertion supplied", user_key=user_key)

        form = {
            "grant_type": OBO_GRANT_TYPE,
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "assertion": teams_sso_token,
            "scope": self._config.effective_scope,
            "requested_token_use": OBO_REQUESTED_TOKEN_USE,
        }

        try:
            async with self._session.post(
                self._config.token_endpoint,
                data=form,
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=self._config.timeout_seconds),
            ) as response:
                status = response.status
                body_text = await response.text()
        except aiohttp.ClientError as exc:
            raise OboTransientError(
                f"transport failure talking to Entra: {type(exc).__name__}",
                user_key=user_key,
            ) from None
        except TimeoutError:
            raise OboTransientError(
                f"Entra token endpoint timed out after {self._config.timeout_seconds}s",
                user_key=user_key,
            ) from None

        if status != 200:
            raise _classify_obo_failure(status, body_text, user_key=user_key)

        try:
            payload: Mapping[str, Any] = json.loads(body_text)
        except ValueError:
            raise OboExchangeError(
                "Entra returned HTTP 200 with a non-JSON body", http_status=status, user_key=user_key
            ) from None

        token = payload.get("access_token")
        token_kind = "access_token"
        if not token:
            raise OboExchangeError(
                "Entra returned HTTP 200 with no access_token field",
                http_status=status,
                user_key=user_key,
            )

        if self._config.enforce_audience:
            # Fail here, loudly and specifically, rather than 200ms later at
            # STS with a message that names neither claim.
            claims = check_google_audience(
                token,
                expected_aud=self._config.federation_app_id,
                expected_iss=self._config.expected_issuer,
                user_key=user_key,
            )
        else:
            claims = decode_claims_unverified(token)

        result = OboResult(
            token=token,
            token_kind=token_kind,
            expires_in=int(payload.get("expires_in") or 0),
            aud=claims.get("aud"),
            iss=claims.get("iss"),
            oid=claims.get("oid"),
            ver=claims.get("ver"),
        )

        log_event(
            _log,
            logging.INFO,
            "obo.exchange.ok",
            user_key=user_key,
            token_fp=result.token_fingerprint,
            aud=result.aud,
            iss=result.iss,
            ver=result.ver,
            expires_in=result.expires_in,
        )
        return result


def _classify_obo_failure(
    status: int, body_text: str, *, user_key: str | None
) -> OboExchangeError:
    """Turn an Entra error body into the right typed error.

    Entra's error shape is ``{"error", "error_description", "error_codes",
    "suberror"}``. ``error_description`` contains the AADSTS code AND, on some
    failures, a correlation id and a timestamp. It does not contain a token,
    but it is user-supplied-adjacent text, so it is truncated rather than
    propagated wholesale.
    """
    try:
        body: Mapping[str, Any] = json.loads(body_text)
    except ValueError:
        body = {}

    error = str(body.get("error") or "")
    suberror = str(body.get("suberror") or "")
    description = str(body.get("error_description") or "")[:400]
    aadsts = next((code for code in _INTERACTION_AADSTS if code in description), None)

    if 500 <= status or status == 429:
        return OboTransientError(
            f"Entra returned {status}", http_status=status, upstream_code=error, user_key=user_key
        )

    if aadsts or suberror in _INTERACTION_SUBERRORS:
        return OboConsentRequired(
            description or "user interaction required",
            http_status=status,
            upstream_code=aadsts or suberror or error,
            user_key=user_key,
        )

    return OboExchangeError(
        description or "OBO exchange refused",
        http_status=status,
        upstream_code=error or None,
        user_key=user_key,
    )


__all__ = [
    "OboConfig",
    "OboResult",
    "OboExchanger",
    "decode_claims_unverified",
    "describe_assertion",
    "check_google_audience",
    "OBO_GRANT_TYPE",
    "OBO_REQUESTED_TOKEN_USE",
]
