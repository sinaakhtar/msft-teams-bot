"""Outbound Bot Connector transport: the other thing that did not exist.

``ConnectorTeamsSink`` takes a single injected callable, ``send(activity)``.
That injection is what lets the renderer suite drive the whole streaming
protocol with no network at all, and it is a good design. But it means the
real callable had to be written somewhere, and it had not been: nothing in the
tree posted anything back to Teams, and nothing acquired an outbound
credential. Search for ``api.botframework.com`` before this file and the only
hit is the inbound token *issuer* constant.

So: this module acquires an app credential from Entra and posts activities to
the Bot Connector.


DIRECTION OF TRAVEL, AND WHY THIS IS NOT ADR 002 BACKSLIDING
------------------------------------------------------------
Every other credential in this system belongs to the human. This one does not,
and that is correct. This token authenticates *the bot to Microsoft* so that a
message appears in the right conversation. It is the postal service, not the
author. It never reaches Google, it is never used to read data, and it is not
substitutable for the user's token anywhere -- the Agent Runtime call in
``app/runtime/client.py`` takes the user's token and refuses to run without it.

The distinction to hold on to: this credential answers "may this bot post to
this Teams conversation", never "may this person see this data".


THE serviceUrl QUESTION
-----------------------
``serviceUrl`` comes from the inbound activity body, and this module posts a
bot credential to it. That would be an obvious credential-exfiltration hole if
the body were untrusted -- an attacker who could set ``serviceUrl`` would
receive the bot's bearer token.

It is not untrusted: ``app/auth/inbound.py`` binds ``serviceUrl`` to the
verified JWT and refuses the activity on mismatch, and that check has no bypass
switch. This module additionally refuses any ``serviceUrl`` that is not HTTPS
and not on a recognised Bot Framework host, because two independent checks on
the one path that leaks a credential is cheap, and because the failure mode of
getting this wrong is silent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Mapping
from urllib.parse import urlparse

import aiohttp

from ..logging_utils import log_event

logger = logging.getLogger(__name__)

#: The resource the outbound token is audienced to.
BOT_CONNECTOR_SCOPE = "https://api.botframework.com/.default"

#: Where a multi-tenant bot gets its outbound token. Single-tenant bots use
#: their own tenant instead; the app registration type decides.
MULTI_TENANT_AUTHORITY_TENANT = "botframework.com"

#: Hosts we will post a bot credential to. Bot Framework service URLs are
#: regional (``smba.trafficmanager.net/emea/``, ``/amer/``, ...) plus the older
#: ``*.botframework.com`` forms.
ALLOWED_SERVICE_URL_SUFFIXES = (
    ".botframework.com",
    "botframework.com",
    ".trafficmanager.net",
)

#: Refresh this many seconds before the token actually expires, so a token
#: does not die between the check and the request.
_EXPIRY_SKEW_SECONDS = 300.0


class ConnectorError(RuntimeError):
    """Outbound Teams delivery failed or was refused as unsafe."""


class UnsafeServiceUrl(ConnectorError):
    """A serviceUrl we will not post a bot credential to."""


def assert_safe_service_url(service_url: str) -> None:
    """Refuse anything that is not HTTPS on a known Bot Framework host."""
    if not service_url:
        raise UnsafeServiceUrl("activity carried no serviceUrl")
    parsed = urlparse(service_url)
    if parsed.scheme != "https":
        raise UnsafeServiceUrl(f"serviceUrl is not https: {parsed.scheme!r}")
    host = (parsed.hostname or "").lower()
    if not host:
        raise UnsafeServiceUrl("serviceUrl has no host")
    if not any(host.endswith(suffix) for suffix in ALLOWED_SERVICE_URL_SUFFIXES):
        raise UnsafeServiceUrl(f"serviceUrl host is not a Bot Framework host: {host}")


class BotConnectorTransport:
    """Acquires an outbound app token and posts activities to a conversation.

    :param app_id: the bot's Entra application (client) id.
    :param app_password: its client secret. Never logged; see
        ``app.logging_utils.redact``.
    :param tenant_id: the bot's home tenant, used for a SingleTenant app.
    :param single_tenant: whether the app registration is SingleTenant. A
        MultiTenant bot authenticates against the shared ``botframework.com``
        authority instead of its own tenant, and getting this backwards
        produces an ``AADSTS700016`` that reads like a wrong secret.
    :param http: shared session, owned by the application.
    """

    def __init__(
        self,
        *,
        app_id: str,
        app_password: str,
        tenant_id: str,
        single_tenant: bool = True,
        http: aiohttp.ClientSession | None = None,
        timeout_seconds: float = 30.0,
        clock: Any = time.monotonic,
    ) -> None:
        if not app_id:
            raise ConnectorError("app_id is required to post back to Teams")
        if not app_password:
            raise ConnectorError("app_password is required to post back to Teams")
        if single_tenant and not tenant_id:
            raise ConnectorError("a SingleTenant bot needs its tenant id")

        self._app_id = app_id
        self._app_password = app_password
        self._tenant_id = tenant_id
        self._single_tenant = single_tenant
        self._http = http
        self._owns_http = http is None
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._clock = clock

        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    @property
    def token_endpoint(self) -> str:
        tenant = self._tenant_id if self._single_tenant else MULTI_TENANT_AUTHORITY_TENANT
        return f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

    async def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
            self._owns_http = True
        return self._http

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None and not self._http.closed:
            await self._http.close()

    # -- credential ---------------------------------------------------------

    async def access_token(self) -> str:
        """Cached client-credentials token for the Bot Connector."""
        now = self._clock()
        if self._token and now < self._token_expires_at:
            return self._token

        async with self._token_lock:
            now = self._clock()
            if self._token and now < self._token_expires_at:
                return self._token

            http = await self._session()
            form = {
                "grant_type": "client_credentials",
                "client_id": self._app_id,
                "client_secret": self._app_password,
                "scope": BOT_CONNECTOR_SCOPE,
            }
            try:
                async with http.post(
                    self.token_endpoint, data=form, timeout=self._timeout
                ) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        raise ConnectorError(
                            f"bot connector token request failed with "
                            f"{resp.status}: {text[:400]}"
                        )
                    payload = await resp.json(content_type=None)
            except aiohttp.ClientError as exc:
                raise ConnectorError(
                    f"bot connector token transport error: {type(exc).__name__}"
                ) from exc

            token = payload.get("access_token")
            if not token:
                raise ConnectorError("bot connector token response carried no token")
            expires_in = float(payload.get("expires_in") or 3600.0)
            self._token = str(token)
            self._token_expires_at = self._clock() + max(
                60.0, expires_in - _EXPIRY_SKEW_SECONDS
            )
            log_event(
                logger,
                logging.INFO,
                "acquired an outbound Bot Connector token",
                expires_in=expires_in,
            )
            return self._token

    # -- delivery -----------------------------------------------------------

    def sender_for(self, conversation_ref: Mapping[str, Any]):
        """Return a ``send(activity)`` callable bound to one conversation.

        This is the shape ``ConnectorTeamsSink`` wants: it builds the streaming
        activity (text, streamId, sequence) and knows nothing about addressing.
        Addressing is filled in here, from the reference the router extracted
        from the inbound activity.
        """
        service_url = str(conversation_ref.get("serviceUrl") or "")
        assert_safe_service_url(service_url)

        conversation = conversation_ref.get("conversation") or {}
        conversation_id = str(
            conversation.get("id") if isinstance(conversation, Mapping) else ""
        )
        if not conversation_id:
            raise ConnectorError("conversation reference carried no conversation id")

        async def send(activity: Mapping[str, Any]) -> Any:
            return await self.send_activity(
                activity, conversation_ref=conversation_ref
            )

        return send

    async def send_activity(
        self, activity: Mapping[str, Any], *, conversation_ref: Mapping[str, Any]
    ) -> Any:
        """POST one activity to the conversation. Returns the decoded response.

        The response matters: ``ConnectorTeamsSink`` reads ``streamId`` (or
        ``id``) out of it to number the rest of the stream, so returning
        ``None`` here would silently break streaming continuity.
        """
        service_url = str(conversation_ref.get("serviceUrl") or "")
        assert_safe_service_url(service_url)

        conversation = conversation_ref.get("conversation") or {}
        conversation_id = (
            str(conversation.get("id")) if isinstance(conversation, Mapping) else ""
        )
        if not conversation_id:
            raise ConnectorError("conversation reference carried no conversation id")

        addressed: dict[str, Any] = dict(activity)
        addressed.setdefault("serviceUrl", service_url)
        if conversation_ref.get("channelId"):
            addressed.setdefault("channelId", conversation_ref["channelId"])
        addressed.setdefault("conversation", conversation)
        if conversation_ref.get("bot"):
            addressed.setdefault("from", conversation_ref["bot"])
        if conversation_ref.get("recipient"):
            addressed.setdefault("recipient", conversation_ref["recipient"])
        if conversation_ref.get("activityId"):
            addressed.setdefault("replyToId", conversation_ref["activityId"])

        url = (
            f"{service_url.rstrip('/')}/v3/conversations/"
            f"{conversation_id}/activities"
        )
        token = await self.access_token()
        http = await self._session()

        try:
            async with http.post(
                url,
                json=addressed,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=self._timeout,
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise ConnectorError(
                        f"bot connector POST failed with {resp.status}: {text[:400]}"
                    )
                if not text:
                    return None
                try:
                    return await resp.json(content_type=None)
                except Exception:  # pragma: no cover - non-JSON 2xx
                    return None
        except aiohttp.ClientError as exc:
            raise ConnectorError(
                f"bot connector transport error: {type(exc).__name__}"
            ) from exc


__all__ = [
    "ALLOWED_SERVICE_URL_SUFFIXES",
    "BOT_CONNECTOR_SCOPE",
    "BotConnectorTransport",
    "ConnectorError",
    "UnsafeServiceUrl",
    "assert_safe_service_url",
]
