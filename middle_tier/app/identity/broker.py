"""The Identity Broker: OBO -> STS -> cache, behind one method.

    Teams SSO token (aud = BOT app)
      --[OBO re-audiencing]-->  token for the FEDERATION app
      --[Google STS exchange]-> Workforce Principal access token
      --[per-user cache]------> reused for ~48 minutes, refreshed proactively

FAIL CLOSED (ADR 004)
---------------------
Every path out of :meth:`ChainedIdentityBroker.google_access_token` is either
a token belonging to the calling human, or a raised
:class:`~app.identity.errors.IdentityAcquisitionError`. There is no third
outcome. Specifically there is:

* no ``return None``,
* no ``except: pass``,
* no Application Default Credentials,
* no service-account impersonation,
* no "degraded mode" flag that would enable any of the above.

If you are about to add one because staging is failing: the failure IS the
feature. ADR 002 splits the Bot Identity plane (a service identity, used only
to talk to Teams and to the Agent Runtime control plane) from the Tool
Identity plane (always the human). A fallback here silently converts the
second into the first, and nothing downstream can tell - BigQuery would
happily answer as the service account and every row-level policy written
against the user would evaluate against the wrong principal. The failure mode
is not an outage, it is undetected over-disclosure.

``tests/test_no_service_account_fallback.py`` enforces this by reading this
package's source. That test failing is a design review, not a flake.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol, runtime_checkable

import aiohttp

from ..logging_utils import log_event
from .cache import DEFAULT_MAX_ENTRIES, DEFAULT_REFRESH_RATIO, MintedToken, PerUserTokenCache
from .errors import IdentityAcquisitionError, PreconditionError
from .obo import OboConfig, OboExchanger
from .sts import StsConfig, StsExchanger

_log = logging.getLogger(__name__)

#: ADR 003. Two GUIDs, colon-separated, with a fixed prefix. Validated rather
#: than trusted: a malformed key would partition the cache on an attacker-
#: influenced string, and a stray ``:`` could forge a different user's key.
_USER_KEY_RE = re.compile(
    r"^entra:"
    r"(?P<tid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}):"
    r"(?P<oid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)


@runtime_checkable
class IdentityBroker(Protocol):
    """The contract other components code against.

    Implementations MUST NOT return a service-account token under any
    circumstance, including as a fallback when the exchange fails.
    """

    async def google_access_token(self, *, user_key: str, teams_sso_token: str) -> str:
        """Return a Google access token for the human identified by ``user_key``.

        :raises IdentityAcquisitionError: on any failure, with the failing
            stage named. Never returns ``None``, never returns a credential
            belonging to anything other than that human.
        """
        ...


def parse_user_key(user_key: str) -> tuple[str, str]:
    """Split ``entra:{tid}:{oid}`` into ``(tid, oid)``, or refuse.

    :raises PreconditionError: the key is absent or malformed. ADR 003 says a
        missing ``aadObjectId`` fails closed at the edge; this is the second
        line of that defence, for anything that constructs a key by hand.
    """
    if not user_key:
        raise PreconditionError("no user_key supplied; ADR 003 requires entra:{tid}:{oid}")
    match = _USER_KEY_RE.match(user_key)
    if not match:
        raise PreconditionError(
            "user_key is not of the form entra:{tid}:{oid} with two GUIDs",
            user_key=user_key,
        )
    return match.group("tid"), match.group("oid")


class ChainedIdentityBroker:
    """Composes the OBO hop, the STS hop and the per-user cache."""

    def __init__(
        self,
        *,
        obo: OboExchanger,
        sts: StsExchanger,
        cache: PerUserTokenCache | None = None,
    ) -> None:
        self._obo = obo
        self._sts = sts
        self._cache = cache if cache is not None else PerUserTokenCache()

    @property
    def cache(self) -> PerUserTokenCache:
        return self._cache

    async def google_access_token(self, *, user_key: str, teams_sso_token: str) -> str:
        """Acquire (or reuse) a Google access token for this human.

        The cache is consulted first; on a miss, one - and only one, per user,
        per refresh - full OBO+STS chain runs.

        :raises IdentityAcquisitionError: any stage failing. The exception
            carries ``stage`` and ``reason_code`` so ADR 004's refusal message
            can say something true and specific.
        """
        # Validate before touching the cache: an unvalidated key would create
        # a cache partition named by an untrusted string.
        parse_user_key(user_key)
        if not teams_sso_token:
            raise PreconditionError("no Teams SSO token supplied", user_key=user_key)

        async def mint() -> MintedToken:
            obo_result = await self._obo.exchange(
                teams_sso_token=teams_sso_token, user_key=user_key
            )
            sts_result = await self._sts.exchange(
                subject_token=obo_result.token, user_key=user_key
            )
            return MintedToken(
                token=sts_result.access_token,
                lifetime_seconds=float(sts_result.expires_in),
            )

        try:
            return await self._cache.get_or_mint(user_key, mint)
        except IdentityAcquisitionError as exc:
            # Log with the stage attached, then re-raise UNCHANGED. No
            # fallback, no substitute credential, no None.
            log_event(_log, logging.WARNING, "identity.acquisition.failed", **exc.as_log_fields())
            raise

    # -- compatibility with app.ports.IdentityBroker ----------------------
    #
    # `app/ports.py` declares the seam with positional arguments and the name
    # `get_google_access_token`. The task contract for this component specifies
    # the keyword-only `google_access_token`. Rather than pick a winner and
    # break somebody, both names exist and share one implementation. Flagged in
    # NOTES.md for a human to reconcile; the duplication is deliberate and
    # cheap, whereas guessing wrong costs an integration afternoon.

    async def get_google_access_token(self, user_key: str, teams_sso_token: str) -> str:
        """``app.ports.IdentityBroker`` spelling. Same behaviour."""
        return await self.google_access_token(
            user_key=user_key, teams_sso_token=teams_sso_token
        )

    async def invalidate(self, user_key: str) -> None:
        """Drop the cached credential for one user. Idempotent, never raises.

        Call after a downstream 401: the token may have been revoked before
        its ``exp``, and replaying it just produces more 401s.
        """
        if self._cache.invalidate(user_key):
            log_event(_log, logging.INFO, "identity.cache.invalidated", user_key=user_key)


def build_identity_broker(
    *,
    session: aiohttp.ClientSession,
    tenant_id: str,
    bot_client_id: str,
    bot_client_secret: str,
    federation_app_id: str,
    workforce_pool_id: str,
    workforce_provider_id: str,
    user_project: str,
    max_cache_entries: int = DEFAULT_MAX_ENTRIES,
    refresh_ratio: float = DEFAULT_REFRESH_RATIO,
) -> ChainedIdentityBroker:
    """Wire the whole chain from flat configuration values.

    :param session: shared aiohttp session, owned by the application. One
        session for the process, not one per request.
    :param bot_client_id: the app the Teams SSO token is audienced TO, and
        the confidential client that performs the OBO.
    :param federation_app_id: the app the workforce pool provider's
        ``clientId`` is set to; the audience we re-audience TOWARDS.
    :param user_project: the mandatory STS quota project. See ``sts.py``.
    """
    obo = OboExchanger(
        OboConfig(
            tenant_id=tenant_id,
            client_id=bot_client_id,
            client_secret=bot_client_secret,
            federation_app_id=federation_app_id,
        ),
        session,
    )
    sts = StsExchanger(
        StsConfig.for_workforce_pool(
            pool_id=workforce_pool_id,
            provider_id=workforce_provider_id,
            user_project=user_project,
        ),
        session,
    )
    cache = PerUserTokenCache(max_entries=max_cache_entries, refresh_ratio=refresh_ratio)
    return ChainedIdentityBroker(obo=obo, sts=sts, cache=cache)


__all__ = [
    "IdentityBroker",
    "ChainedIdentityBroker",
    "build_identity_broker",
    "parse_user_key",
]
