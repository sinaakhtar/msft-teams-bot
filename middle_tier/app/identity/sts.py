"""Stage 2: the Google STS token exchange. Verified parameter set - do not trim.

WHAT IS VERIFIED, AND WHEN
--------------------------
The exchange below was executed live against the real endpoint on 2026-09-07
and returned a working access token. The principal it produced was confirmed
from inside BigQuery with ``SELECT SESSION_USER()``:

    principal://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/subject/33333333-3333-3333-3333-333333333333

where the trailing GUID is the Entra ``oid`` of ``analyst@example.onmicrosoft.com``,
a plain tenant user with no Google identity of any kind. See
``spikes/FINDINGS.md`` (Layer 2) for the full record.

THE VERIFIED PARAMETER SET (all six fields are load-bearing)
------------------------------------------------------------
POST https://sts.googleapis.com/v1/token   (application/x-www-form-urlencoded)

    grant_type            urn:ietf:params:oauth:grant-type:token-exchange
    audience              //iam.googleapis.com/locations/global/workforcePools/
                          teams-bot-demo/providers/entra
    scope                 https://www.googleapis.com/auth/cloud-platform
    requested_token_type  urn:ietf:params:oauth:token-type:access_token
    subject_token         <Entra ID token>          NOT an Entra access token
                                                    in the verified run
    subject_token_type    urn:ietf:params:oauth:token-type:id_token
    options               {"userProject": "example-project"}

Response: ``access_token``, ``expires_in`` 3598. Treat the Google credential
as a ~1 hour lifetime; the workforce pool ``sessionDuration`` is 3600s and the
exchange cannot mint anything longer than the pool allows.

WHY ``options``/``userProject`` IS NOT OPTIONAL
-----------------------------------------------
It looks like decoration. It is not. A workforce principal has no project of
its own, so it has nothing to bill API usage to, and Google requires a quota
project to be named. Drop ``options`` and the call still succeeds - and then
the *downstream* API call fails with a 403 that talks about BigQuery, sending
you to debug the wrong service.

The second-order trap, from the spike: naming the quota project makes that
project demand its own permission. The first live attempt failed with

    "Caller does not have required permission to use project example-project.
     Grant the caller the roles/serviceusage.serviceUsageConsumer role..."

So the required role set is four, not the three the BigQuery MCP docs list:
``roles/mcp.toolUser``, ``roles/bigquery.jobUser``, ``roles/bigquery.dataViewer``,
and ``roles/serviceusage.serviceUsageConsumer``. If you are here because you
simplified ``options`` away and rediscovered a 403: put it back.

SUBJECT TOKEN TYPE
------------------
``subject_token_type`` stays ``...:id_token`` because that is the value that
was verified. It is configurable on :class:`StsConfig` for exactly one reason:
the OBO hop hands us an *access* token, and if Google turns out to want
``...:jwt`` for that, the fix must be a config change and not a patch to a
module whose docstring claims to be verified. Whichever value is used, the
provider still validates issuer, signature and audience the same way - see
the audience discussion in ``obo.py``.

Reference: https://cloud.google.com/iam/docs/reference/sts/rest/v1/TopLevel/token
Workforce federation how-to: https://cloud.google.com/iam/docs/workforce-obtaining-short-lived-credentials
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping

import aiohttp

from ..logging_utils import fingerprint, log_event
from .errors import (
    PreconditionError,
    StsExchangeError,
    StsPermissionDenied,
    StsSubjectTokenRejected,
    StsTransientError,
)

_log = logging.getLogger(__name__)

STS_ENDPOINT = "https://sts.googleapis.com/v1/token"
TOKEN_EXCHANGE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:token-exchange"
REQUESTED_TOKEN_TYPE_ACCESS = "urn:ietf:params:oauth:token-type:access_token"
SUBJECT_TOKEN_TYPE_ID_TOKEN = "urn:ietf:params:oauth:token-type:id_token"
SUBJECT_TOKEN_TYPE_JWT = "urn:ietf:params:oauth:token-type:jwt"
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

#: The pool's ``sessionDuration``. The STS response is authoritative; this is
#: only the floor used when a response omits ``expires_in``, and a sanity
#: bound so a surprising upstream value cannot extend a cache entry past the
#: pool's own limit.
WORKFORCE_SESSION_DURATION_SECONDS = 3600


@dataclass(frozen=True)
class StsConfig:
    """The verified STS parameters, as configuration rather than literals.

    :param audience: the full provider resource name, ``//iam.googleapis.com/
        locations/global/workforcePools/{pool}/providers/{provider}``. Note the
        leading double slash - it is part of the value, not a typo.
    :param user_project: the quota project. MANDATORY. See module docstring.
    :param scope: OAuth scope for the returned Google token.
    :param subject_token_type: see module docstring. Default is the verified
        value.
    """

    audience: str
    user_project: str
    scope: str = CLOUD_PLATFORM_SCOPE
    subject_token_type: str = SUBJECT_TOKEN_TYPE_ID_TOKEN
    endpoint: str = STS_ENDPOINT
    timeout_seconds: float = 10.0

    @classmethod
    def for_workforce_pool(
        cls,
        *,
        pool_id: str,
        provider_id: str,
        user_project: str,
        location: str = "global",
        **kw: Any,
    ) -> "StsConfig":
        """Build the audience from its parts so nobody hand-types it wrong."""
        return cls(
            audience=(
                f"//iam.googleapis.com/locations/{location}/workforcePools/"
                f"{pool_id}/providers/{provider_id}"
            ),
            user_project=user_project,
            **kw,
        )


@dataclass(frozen=True)
class StsResult:
    """A Google access token and how long it is good for."""

    #: In memory only. Never logged, never written to disk, never placed in
    #: Agent Runtime Session state - that state is PERSISTED, and a live
    #: bearer token in durable conversation history is a breach with a
    #: retention policy attached to it.
    access_token: str
    expires_in: int
    token_type: str = "Bearer"

    @property
    def token_fingerprint(self) -> str:
        return fingerprint(self.access_token)


class StsExchanger:
    """Performs the Google STS token exchange over an injected aiohttp session."""

    def __init__(self, config: StsConfig, session: aiohttp.ClientSession) -> None:
        self._config = config
        self._session = session

    async def exchange(self, *, subject_token: str, user_key: str | None = None) -> StsResult:
        """Exchange an Entra assertion for a Workforce Principal access token.

        :param subject_token: the re-audienced Entra token from stage 1. Its
            ``aud`` must equal the workforce pool provider's ``clientId``.
        :raises StsSubjectTokenRejected: 400 - audience, issuer, expiry or
            attribute condition.
        :raises StsPermissionDenied: 403 - IAM, or the ``userProject`` quota
            project missing ``serviceUsageConsumer``.
        :raises StsTransientError: 5xx or timeout.
        """
        if not subject_token:
            raise PreconditionError("no subject token supplied to STS", user_key=user_key)

        form = {
            "grant_type": TOKEN_EXCHANGE_GRANT_TYPE,
            "audience": self._config.audience,
            "scope": self._config.scope,
            "requested_token_type": REQUESTED_TOKEN_TYPE_ACCESS,
            "subject_token": subject_token,
            "subject_token_type": self._config.subject_token_type,
            # MANDATORY. Workforce principals have no project to bill.
            "options": json.dumps({"userProject": self._config.user_project}),
        }

        try:
            async with self._session.post(
                self._config.endpoint,
                data=form,
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=self._config.timeout_seconds),
            ) as response:
                status = response.status
                body_text = await response.text()
        except aiohttp.ClientError as exc:
            raise StsTransientError(
                f"transport failure talking to STS: {type(exc).__name__}", user_key=user_key
            ) from None
        except TimeoutError:
            raise StsTransientError(
                f"STS timed out after {self._config.timeout_seconds}s", user_key=user_key
            ) from None

        if status != 200:
            raise _classify_sts_failure(status, body_text, user_key=user_key)

        try:
            payload: Mapping[str, Any] = json.loads(body_text)
        except ValueError:
            raise StsExchangeError(
                "STS returned HTTP 200 with a non-JSON body", http_status=status, user_key=user_key
            ) from None

        access_token = payload.get("access_token")
        if not access_token:
            raise StsExchangeError(
                "STS returned HTTP 200 with no access_token field",
                http_status=status,
                user_key=user_key,
            )

        expires_in = int(payload.get("expires_in") or WORKFORCE_SESSION_DURATION_SECONDS)
        # A value longer than the pool's sessionDuration would mean we cache a
        # credential past the point the pool stops honouring it.
        expires_in = min(expires_in, WORKFORCE_SESSION_DURATION_SECONDS)

        result = StsResult(
            access_token=access_token,
            expires_in=expires_in,
            token_type=str(payload.get("token_type") or "Bearer"),
        )
        log_event(
            _log,
            logging.INFO,
            "sts.exchange.ok",
            user_key=user_key,
            token_fp=result.token_fingerprint,
            expires_in=result.expires_in,
            audience=self._config.audience,
            user_project=self._config.user_project,
        )
        return result


def _classify_sts_failure(
    status: int, body_text: str, *, user_key: str | None
) -> StsExchangeError:
    """Map an STS error body onto the typed hierarchy.

    STS speaks two dialects depending on how far the request got: the OAuth
    shape ``{"error", "error_description"}`` and the Google API shape
    ``{"error": {"code", "message", "status"}}``. Handle both; the useful text
    is in different places.
    """
    error_code = ""
    message = ""
    try:
        body: Any = json.loads(body_text)
    except ValueError:
        body = None

    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            error_code = str(err.get("status") or err.get("code") or "")
            message = str(err.get("message") or "")
        else:
            error_code = str(err or "")
            message = str(body.get("error_description") or "")
    if not message:
        message = body_text[:400]

    if status >= 500 or status == 429:
        return StsTransientError(
            f"STS returned {status}: {message[:200]}",
            http_status=status,
            upstream_code=error_code or None,
            user_key=user_key,
        )
    if status == 403:
        return StsPermissionDenied(
            message[:400], http_status=status, upstream_code=error_code or None, user_key=user_key
        )
    if status in (400, 401):
        return StsSubjectTokenRejected(
            message[:400], http_status=status, upstream_code=error_code or None, user_key=user_key
        )
    return StsExchangeError(
        message[:400], http_status=status, upstream_code=error_code or None, user_key=user_key
    )


__all__ = [
    "StsConfig",
    "StsResult",
    "StsExchanger",
    "STS_ENDPOINT",
    "TOKEN_EXCHANGE_GRANT_TYPE",
    "REQUESTED_TOKEN_TYPE_ACCESS",
    "SUBJECT_TOKEN_TYPE_ID_TOKEN",
    "SUBJECT_TOKEN_TYPE_JWT",
    "CLOUD_PLATFORM_SCOPE",
    "WORKFORCE_SESSION_DURATION_SECONDS",
]
