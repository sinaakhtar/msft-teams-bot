"""Tests for the outbound Bot Connector transport.

This module was written during the integration pass and is the only newly
written code that sits on the reply path, so it gets its own tests rather than
riding on the end-to-end wiring test.

No network. The aiohttp session is replaced with a recorder, so what is
asserted is what would have gone on the wire: the URL, the addressing fields
merged onto the activity, and the credential. Whether Microsoft accepts it has
NOT been tested and cannot be from here -- see INTEGRATION.md.
"""

from __future__ import annotations

import pathlib
import sys
from typing import Any, Mapping

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.streaming.connector import (  # noqa: E402
    BOT_CONNECTOR_SCOPE,
    BotConnectorTransport,
    ConnectorError,
    UnsafeServiceUrl,
    assert_safe_service_url,
)

SERVICE_URL = "https://smba.trafficmanager.net/emea/"
CONVERSATION_ID = "a:1p9YyEA-conversation"

CONVERSATION_REF: dict[str, Any] = {
    "serviceUrl": SERVICE_URL,
    "channelId": "msteams",
    "conversation": {"id": CONVERSATION_ID},
    "recipient": {"id": "29:the-human"},
    "bot": {"id": "28:the-bot"},
    "activityId": "activity-0001",
}


# ==========================================================================
# Recording transport
# ==========================================================================


class _Response:
    def __init__(self, status: int, payload: Any, text: str = "") -> None:
        self.status = status
        self._payload = payload
        self._text = text if text else ("{}" if payload is not None else "")

    async def text(self) -> str:
        return self._text

    async def json(self, content_type: Any = None) -> Any:
        return self._payload

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class RecordingHttp:
    """Stands in for `aiohttp.ClientSession`, recording every POST."""

    def __init__(self, responses: list[_Response] | None = None) -> None:
        self.posts: list[dict[str, Any]] = []
        self.responses = responses or []
        self.closed = False

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.posts.append({"url": url, **kwargs})
        if self.responses:
            return self.responses.pop(0)
        return _Response(200, {"id": "1700000000000"}, text='{"id":"1700000000000"}')


def transport(http: RecordingHttp, **overrides: Any) -> BotConnectorTransport:
    kwargs: dict[str, Any] = dict(
        app_id="00000000-1111-2222-3333-444444444444",
        app_password="fake-bot-password",
        tenant_id="00000000-0000-0000-0000-000000000000",
        single_tenant=True,
        http=http,
    )
    kwargs.update(overrides)
    return BotConnectorTransport(**kwargs)


# ==========================================================================
# serviceUrl safety: the path that would leak a credential
# ==========================================================================


def test_a_bot_framework_https_url_is_accepted():
    assert_safe_service_url(SERVICE_URL)
    assert_safe_service_url("https://smba.trafficmanager.net/amer/")
    assert_safe_service_url("https://europe.botframework.com/")


@pytest.mark.parametrize(
    "url",
    [
        "http://smba.trafficmanager.net/emea/",  # plaintext
        "https://evil.example.com/",  # attacker host
        "https://botframework.com.evil.example/",  # suffix confusion
        "",  # absent
        "not-a-url",
    ],
)
def test_an_untrusted_service_url_is_refused(url: str):
    """The bot's bearer token is POSTed to this host. Getting it wrong is a
    credential handover, and it fails silently if unchecked."""
    with pytest.raises(UnsafeServiceUrl):
        assert_safe_service_url(url)


async def test_send_refuses_an_untrusted_service_url_before_any_request():
    http = RecordingHttp()
    with pytest.raises(UnsafeServiceUrl):
        transport(http).sender_for(
            {**CONVERSATION_REF, "serviceUrl": "https://evil.example.com/"}
        )
    assert http.posts == [], "a request was made despite the unsafe URL"


# ==========================================================================
# The credential
# ==========================================================================


def test_a_single_tenant_bot_uses_its_own_tenant_authority():
    endpoint = transport(RecordingHttp()).token_endpoint
    assert endpoint == (
        "https://login.microsoftonline.com/"
        "00000000-0000-0000-0000-000000000000/oauth2/v2.0/token"
    )


def test_a_multi_tenant_bot_uses_the_shared_authority():
    """Getting this backwards produces an AADSTS700016 that reads like a
    wrong client secret, which sends debugging in the wrong direction."""
    endpoint = transport(RecordingHttp(), single_tenant=False).token_endpoint
    assert endpoint == (
        "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
    )


async def test_the_token_is_requested_with_the_connector_scope():
    http = RecordingHttp(
        [_Response(200, {"access_token": "bot-token", "expires_in": 3600})]
    )
    token = await transport(http).access_token()

    assert token == "bot-token"
    assert len(http.posts) == 1
    form = http.posts[0]["data"]
    assert form["grant_type"] == "client_credentials"
    assert form["scope"] == BOT_CONNECTOR_SCOPE


async def test_the_token_is_cached_across_calls():
    """One token request per hour, not one per streamed chunk."""
    http = RecordingHttp(
        [_Response(200, {"access_token": "bot-token", "expires_in": 3600})]
    )
    subject = transport(http)

    assert await subject.access_token() == "bot-token"
    assert await subject.access_token() == "bot-token"
    assert len(http.posts) == 1


async def test_a_refused_token_request_raises_rather_than_returning_empty():
    http = RecordingHttp([_Response(401, None, text="AADSTS7000215: bad secret")])
    with pytest.raises(ConnectorError) as caught:
        await transport(http).access_token()
    assert "401" in str(caught.value)


async def test_a_token_response_with_no_token_is_refused():
    """An empty string here would be sent as `Bearer ` and 401 downstream,
    which is a much harder failure to read than this one."""
    http = RecordingHttp([_Response(200, {"expires_in": 3600})])
    with pytest.raises(ConnectorError):
        await transport(http).access_token()


# ==========================================================================
# Delivery
# ==========================================================================


async def test_the_activity_is_posted_to_the_conversation_with_addressing():
    http = RecordingHttp(
        [
            _Response(200, {"access_token": "bot-token", "expires_in": 3600}),
            _Response(200, {"id": "1700000000000"}, text='{"id":"1700000000000"}'),
        ]
    )
    send = transport(http).sender_for(CONVERSATION_REF)

    response = await send({"type": "message", "text": "hello"})

    assert response == {"id": "1700000000000"}
    post = http.posts[-1]
    assert post["url"] == (
        f"https://smba.trafficmanager.net/emea/v3/conversations/"
        f"{CONVERSATION_ID}/activities"
    )
    assert post["headers"]["Authorization"] == "Bearer bot-token"

    sent = post["json"]
    # The renderer supplies the content; the transport supplies the address.
    assert sent["text"] == "hello"
    assert sent["conversation"] == {"id": CONVERSATION_ID}
    assert sent["from"] == {"id": "28:the-bot"}
    assert sent["recipient"] == {"id": "29:the-human"}
    assert sent["replyToId"] == "activity-0001"
    assert sent["channelId"] == "msteams"
    assert sent["serviceUrl"] == SERVICE_URL


async def test_the_transport_does_not_overwrite_fields_the_renderer_set():
    """The sink owns streamId/sequence and the streaminfo entity. If the
    transport clobbered any of it, streaming would silently restart."""
    http = RecordingHttp(
        [
            _Response(200, {"access_token": "bot-token", "expires_in": 3600}),
            _Response(200, {"id": "x"}, text='{"id":"x"}'),
        ]
    )
    send = transport(http).sender_for(CONVERSATION_REF)

    await send(
        {
            "type": "typing",
            "text": "Working on it...",
            "channelData": {"streamId": "abc", "streamSequence": 3},
            "entities": [{"type": "streaminfo", "streamSequence": 3}],
        }
    )

    sent = http.posts[-1]["json"]
    assert sent["channelData"] == {"streamId": "abc", "streamSequence": 3}
    assert sent["entities"] == [{"type": "streaminfo", "streamSequence": 3}]


async def test_a_rejected_delivery_raises():
    http = RecordingHttp(
        [
            _Response(200, {"access_token": "bot-token", "expires_in": 3600}),
            _Response(403, None, text="BotNotInConversationRoster"),
        ]
    )
    send = transport(http).sender_for(CONVERSATION_REF)

    with pytest.raises(ConnectorError) as caught:
        await send({"type": "message", "text": "hello"})
    assert "403" in str(caught.value)


def test_construction_refuses_missing_credentials():
    """Fail at assembly, not on the first user's first message."""
    with pytest.raises(ConnectorError):
        BotConnectorTransport(app_id="", app_password="pw", tenant_id="t")
    with pytest.raises(ConnectorError):
        BotConnectorTransport(app_id="a", app_password="", tenant_id="t")
    with pytest.raises(ConnectorError):
        BotConnectorTransport(app_id="a", app_password="pw", tenant_id="")
