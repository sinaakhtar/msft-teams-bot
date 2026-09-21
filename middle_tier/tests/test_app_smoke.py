"""Smoke tests for the HTTP layer, router and error templates.

Not required by the build brief, but written so NOTES.md can distinguish
"this code exists" from "this code was executed". Without these, `main.py`,
`routing.py` and `errors.py` would be unexecuted text.

Everything here runs against a real aiohttp server on loopback with a real
JWKS server. No external network.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import errors  # noqa: E402
from app.auth.inbound import AuthenticatedCaller  # noqa: E402
from app.config import ConfigError, SecretResolver, Settings  # noqa: E402
from app.logging_utils import fingerprint, redact  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routing import Dependencies, is_reset_command, route_activity  # noqa: E402

from .conftest import BOT_APP_ID, KeyMaterial, mint_token  # noqa: E402

TENANT = "00000000-0000-0000-0000-000000000000"


def _settings(**overrides) -> Settings:
    base = dict(
        gcp_project_id="example-project",
        gcp_project_number="000000000000",
        location="us-central1",
        entra_tenant_id=TENANT,
        microsoft_app_id=BOT_APP_ID,
        microsoft_app_password="not-a-real-password",
        signin_url="https://example.invalid/signin",
    )
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class RecordingSender:
    """Stands in for the Bot Connector and remembers what was posted.

    Replies are delivered by an outbound POST to the connector, not returned
    in the HTTP response, so "did the user see anything" is only answerable by
    looking at what reached this object. Asserting on the response body
    instead is what let the bot ship replying to nobody: every template was
    built correctly, returned with a 200, and discarded by the Bot Framework.
    """

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def __call__(self, activity, conversation_ref) -> dict:
        self.sent.append(dict(activity))
        return {"id": f"posted-{len(self.sent)}"}

    @property
    def only(self) -> dict:
        """The single delivered activity, asserting there was exactly one."""
        assert len(self.sent) == 1, f"expected exactly 1 reply, got {len(self.sent)}"
        return self.sent[0]


@pytest.fixture
def sender() -> RecordingSender:
    return RecordingSender()


@pytest.fixture
async def client(authenticator, aiohttp_client_factory, sender):
    settings = _settings()
    app = create_app(
        settings=settings,
        authenticator=authenticator,
        deps=Dependencies(reply_sender=sender),
    )
    return await aiohttp_client_factory(app)


@pytest.fixture
async def aiohttp_client_factory():
    """Minimal replacement for pytest-aiohttp's client fixture."""
    import aiohttp

    runners: list[web.AppRunner] = []
    sessions: list[aiohttp.ClientSession] = []

    class Client:
        def __init__(self, base: str, session: aiohttp.ClientSession) -> None:
            self._base = base
            self._session = session

        def get(self, path: str, **kw):
            return self._session.get(self._base + path, **kw)

        def post(self, path: str, **kw):
            return self._session.post(self._base + path, **kw)

    async def factory(app: web.Application) -> Client:
        runner = web.AppRunner(app)
        runners.append(runner)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        host, port = runner.addresses[0][0], runner.addresses[0][1]
        session = aiohttp.ClientSession()
        sessions.append(session)
        return Client(f"http://{host}:{port}", session)

    try:
        yield factory
    finally:
        for s in sessions:
            await s.close()
        for r in runners:
            await r.cleanup()


# ==========================================================================
# Endpoints
# ==========================================================================


async def test_healthz_is_dependency_free(client):
    async with client.get("/healthz") as resp:
        assert resp.status == 200
        assert (await resp.json())["status"] == "ok"


async def test_messages_rejects_missing_authorization_with_401_and_no_detail(client):
    async with client.post("/api/messages", json={"type": "message"}) as resp:
        assert resp.status == 401
        body = await resp.text()
    # No oracle: the response must not say WHY.
    assert body == ""


async def test_messages_rejects_forged_token_with_401(client, keys: KeyMaterial):
    forged = mint_token(keys.rogue, kid="test-key-1")
    async with client.post(
        "/api/messages",
        json={"type": "message", "serviceUrl": "https://smba.trafficmanager.net/emea/"},
        headers={"Authorization": f"Bearer {forged}"},
    ) as resp:
        assert resp.status == 401


async def test_messages_rejects_non_json_body(client):
    async with client.post(
        "/api/messages", data="not json", headers={"Content-Type": "application/json"}
    ) as resp:
        assert resp.status == 400


async def test_valid_token_reaches_the_router(
    client, keys: KeyMaterial, activity, sender
):
    """End to end: signed token -> validation -> routing -> refusal template.

    No IdentityBroker is wired, so the honest outcome is a 200 carrying a
    "not a permissions problem" message - NOT a fabricated agent answer.
    """
    token = mint_token(keys.trusted)
    async with client.post(
        "/api/messages", json=activity, headers={"Authorization": f"Bearer {token}"}
    ) as resp:
        assert resp.status == 200
        # The response body is NOT the reply. The Bot Framework reads the
        # status code and discards the body, so this must stay empty and the
        # assertion that matters is on what was posted to the connector.
        assert await resp.text() == ""

    body = sender.only
    assert body["type"] == "message"
    assert "not a permissions problem" in body["text"]


async def test_guest_user_without_aad_object_id_gets_the_fail_closed_message(
    client, keys: KeyMaterial, activity, sender
):
    """ADR 003 end to end through the HTTP layer."""
    activity["from"] = {"id": "29:guest", "name": "Guest"}
    token = mint_token(keys.trusted)

    async with client.post(
        "/api/messages", json=activity, headers={"Authorization": f"Bearer {token}"}
    ) as resp:
        assert resp.status == 200
        assert await resp.text() == ""

    body = sender.only
    assert "missing_aad_object_id" in body["text"]
    assert "29:guest" not in body["text"]


# ==========================================================================
# Routing
# ==========================================================================


@pytest.mark.parametrize("cmd", ["/new", "/New", " /reset ", "/clear"])
def test_reset_commands_recognised(cmd):
    assert is_reset_command(cmd)


@pytest.mark.parametrize("text", ["new", "/newsletter", "what is /new", ""])
def test_non_reset_text_not_treated_as_command(text):
    assert not is_reset_command(text)


async def test_unknown_activity_type_is_ignored_safely(activity):
    caller = AuthenticatedCaller(
        app_id=BOT_APP_ID,
        issuer="https://api.botframework.com",
        profile_name="bot_connector",
        service_url="https://smba.trafficmanager.net/emea/",
    )
    for unknown in ("typing", "messageReaction", "installationUpdate", "somethingNew"):
        result = await route_activity(
            {**activity, "type": unknown}, caller=caller, deps=Dependencies()
        )
        assert result.status == 200
        assert result.handled is False


def _bot_caller() -> AuthenticatedCaller:
    return AuthenticatedCaller(
        app_id=BOT_APP_ID,
        issuer="https://api.botframework.com",
        profile_name="bot_connector",
        service_url="https://smba.trafficmanager.net/emea/",
    )


async def test_token_exchange_without_sso_configured_fails_with_412_not_200(activity):
    """A 200 would tell Teams the exchange succeeded and leave the user
    waiting for a reply that never comes.

    412 is the code Teams reads as "consent needed", so it falls back to the
    visible card instead of silently stranding the turn.
    """
    result = await route_activity(
        {
            **activity,
            "type": "invoke",
            "name": "signin/tokenExchange",
            "value": {"id": "exchange-1", "token": "an-assertion"},
        },
        caller=_bot_caller(),
        deps=Dependencies(),
    )
    assert result.status == 412
    assert result.body is not None
    assert result.body["id"] == "exchange-1"
    # The failure detail is operator-facing and must never carry the token.
    assert "an-assertion" not in json.dumps(result.body)


async def test_token_exchange_with_no_token_is_refused(activity):
    result = await route_activity(
        {
            **activity,
            "type": "invoke",
            "name": "signin/tokenExchange",
            "value": {"id": "exchange-2"},
        },
        caller=_bot_caller(),
        deps=Dependencies(),
    )
    assert result.status == 412


async def test_an_invoke_reply_stays_in_the_body_and_is_never_posted(activity):
    """The one activity type whose response body IS the protocol payload.

    Posting it to the connector instead would both lose the protocol response
    and put a raw failure object in the user's chat.
    """
    sender = RecordingSender()
    result = await route_activity(
        {
            **activity,
            "type": "invoke",
            "name": "signin/tokenExchange",
            "value": {"id": "exchange-3", "token": "an-assertion"},
        },
        caller=_bot_caller(),
        deps=Dependencies(reply_sender=sender),
    )
    assert result.status == 412
    assert result.reply is None
    assert sender.sent == []


async def test_conversation_update_welcomes_only_when_the_bot_is_added(activity):
    caller = AuthenticatedCaller(
        app_id=BOT_APP_ID,
        issuer="https://api.botframework.com",
        profile_name="bot_connector",
        service_url="https://smba.trafficmanager.net/emea/",
    )
    base = {
        **activity,
        "type": "conversationUpdate",
        "recipient": {"id": "28:bot"},
    }

    sender = RecordingSender()
    deps = Dependencies(reply_sender=sender)

    human_joined = await route_activity(
        {**base, "membersAdded": [{"id": "29:human"}]}, caller=caller, deps=deps
    )
    assert human_joined.handled is False
    assert human_joined.reply is None
    assert sender.sent == [], "greeting every human who joins is how a bot gets muted"

    bot_joined = await route_activity(
        {**base, "membersAdded": [{"id": "28:bot"}]}, caller=caller, deps=deps
    )
    assert bot_joined.reply is not None
    assert "**as you**" in bot_joined.reply["text"]
    # And it was actually delivered, not just constructed.
    assert "**as you**" in sender.only["text"]


# ==========================================================================
# Error templates (ADR 004)
# ==========================================================================


def test_denial_names_the_refused_resource():
    body = errors.downstream_denial(
        resource="example-project.sales.orders", action="bigquery.tables.getData"
    )
    assert "example-project.sales.orders" in body["text"]
    assert "bigquery.tables.getData" in body["text"]
    assert "service account" in body["text"]


def test_denial_without_a_resource_is_a_programming_error():
    with pytest.raises(ValueError):
        errors.downstream_denial(resource="")


def test_identity_failure_carries_a_signin_card():
    body = errors.identity_failure(signin_url="https://example.invalid/signin")
    assert body["attachments"][0]["contentType"] == errors.SIGNIN_CARD_CONTENT_TYPE
    assert (
        body["attachments"][0]["content"]["buttons"][0]["value"]
        == "https://example.invalid/signin"
    )


def test_identity_failure_without_a_url_has_no_broken_button():
    body = errors.identity_failure(signin_url=None)
    assert "attachments" not in body
    assert "not available right now" in body["text"]


def test_transient_failure_is_not_worded_as_a_permission_problem():
    body = errors.transient_failure()
    assert "not a permissions problem" in body["text"]
    assert "denied" not in body["text"].lower()


# ==========================================================================
# Redaction
# ==========================================================================


def test_jwt_is_redacted_from_free_text():
    token = (
        "eyJhbGciOiJSUzI1NiIsImtpZCI6ImFiYyJ9."
        "eyJpc3MiOiJodHRwczovL2FwaS5ib3RmcmFtZXdvcmsuY29tIn0.c2lnbmF0dXJl"
    )
    out = redact(f"failed to verify {token} from channel")
    assert token not in out
    assert "[REDACTED]" in out


def test_authorization_header_value_is_redacted_in_dicts():
    out = redact({"Authorization": "Bearer abc.def.ghi", "channelId": "msteams"})
    assert out["Authorization"] == "[REDACTED]"
    assert out["channelId"] == "msteams"


def test_bearer_scheme_redacted_even_for_opaque_tokens():
    out = redact("header was: Bearer sk-not-a-jwt-but-still-secret")
    assert "sk-not-a-jwt-but-still-secret" not in out


def test_nested_secrets_are_redacted():
    out = redact({"value": {"token": "abc", "id": "exchange-1"}})
    assert out["value"]["token"] == "[REDACTED]"
    assert out["value"]["id"] == "exchange-1"


def test_fingerprint_is_stable_and_not_reversible():
    a = fingerprint("some-token-value")
    assert a == fingerprint("some-token-value")
    assert a != fingerprint("some-other-token")
    assert "some-token-value" not in a
    assert len(a) == 12


def test_settings_repr_does_not_leak_secrets():
    s = _settings(microsoft_app_password="SUPER-SECRET", entra_client_secret="ALSO-SECRET")
    assert "SUPER-SECRET" not in repr(s)
    assert "ALSO-SECRET" not in repr(s)
    assert "<redacted>" in repr(s)


# ==========================================================================
# Config guardrails
# ==========================================================================


def test_dev_mode_is_refused_on_cloud_run(monkeypatch):
    """`MIDDLE_TIER_DEV_MODE` on Cloud Run must fail startup, not degrade."""
    monkeypatch.setenv("K_SERVICE", "middle-tier")
    with pytest.raises(ConfigError, match="Cloud Run"):
        SecretResolver(project_id="example-project", dev_mode=True)


def test_dev_mode_allowed_off_cloud_run(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    resolver = SecretResolver(project_id="example-project", dev_mode=True)
    monkeypatch.setenv("MY_SECRET_ENV", "local-dev-value")
    assert resolver.get("some-secret", env_fallback="MY_SECRET_ENV") == "local-dev-value"


def test_dev_fallback_logs_at_critical(monkeypatch, caplog):
    """The dev fallback must be impossible to miss in a log search."""
    import logging

    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.setenv("MY_SECRET_ENV", "local-dev-value")
    resolver = SecretResolver(project_id="example-project", dev_mode=True)

    with caplog.at_level(logging.CRITICAL):
        resolver.get("some-secret", env_fallback="MY_SECRET_ENV")

    critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert critical, "dev secret fallback did not log at CRITICAL"
    assert "DEV-ONLY SECRET FALLBACK" in critical[0].getMessage()
    # The value itself must not be in the log.
    assert "local-dev-value" not in caplog.text
