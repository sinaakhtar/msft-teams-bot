"""aiohttp application: the Bot Framework endpoint plus Cloud Run probes.

Endpoints
---------
``POST /api/messages``  the Bot Framework endpoint. Validates, routes, replies.
``GET  /healthz``       liveness. Cheap, no dependencies, always 200 if alive.
``GET  /readyz``        readiness. Fails until config and the JWKS are usable.

The distinction matters on Cloud Run: a liveness probe that touches a
dependency turns a downstream blip into a container restart loop, and a
readiness probe that touches nothing routes traffic to an instance that cannot
serve it. So `/healthz` is a constant and `/readyz` does real work.

Response discipline on /api/messages
------------------------------------
  * 401 for any authentication failure, with NO body detail. "Wrong audience"
    vs "bad signature" vs "expired" is a free oracle for an attacker tuning a
    forgery, so the reason is logged and never returned.
  * 200 for a handled turn, including a refusal. A refusal is a successful
    delivery of a refusal message; a non-2xx would make Azure Bot Service retry
    the same doomed activity.
  * 400 only for a body that is not JSON.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import aiohttp
from aiohttp import web

from . import errors  # noqa: F401  (re-exported for convenience in tests)
from .auth.inbound import (
    InboundActivityAuthenticator,
    InboundAuthError,
    JwksCache,
    build_authenticator,
)
from .config import ConfigError, Settings, get_settings
from .logging_utils import configure_logging, log_event, redact
from .routing import Dependencies, route_activity

logger = logging.getLogger(__name__)

APP_SETTINGS = web.AppKey[Settings]("settings")
APP_AUTHENTICATOR = web.AppKey[InboundActivityAuthenticator]("authenticator")
APP_DEPS = web.AppKey[Dependencies]("deps")
APP_STARTED_AT = web.AppKey[float]("started_at")
APP_HTTP = web.AppKey[aiohttp.ClientSession]("http")


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------


async def healthz(request: web.Request) -> web.Response:
    """Liveness. Deliberately dependency-free."""
    return web.json_response({"status": "ok"})


async def readyz(request: web.Request) -> web.Response:
    """Readiness: config is loaded and the channel JWKS is reachable.

    Warming the JWKS here means the first real activity does not pay the
    metadata + keys round trip, and means an instance with no egress to
    login.botframework.com is never marked ready. That instance would 401
    every single request, so keeping it out of the load balancer is exactly
    right.
    """
    settings: Settings = request.app[APP_SETTINGS]
    auth: InboundActivityAuthenticator = request.app[APP_AUTHENTICATOR]

    checks: dict[str, Any] = {
        "config": "ok",
        "app_id_configured": bool(settings.microsoft_app_id),
    }

    try:
        # Any kid will do; we only care that the documents fetch. An
        # `unknown_kid` outcome still proves reachability, so it counts as OK.
        await auth.jwks_cache.get_key(
            "https://login.botframework.com/v1/.well-known/openidconfiguration",
            "__readiness_probe__",
        )
        checks["jwks"] = "ok"
    except InboundAuthError as exc:
        if exc.reason == "unknown_kid":
            checks["jwks"] = "ok"
        else:
            checks["jwks"] = f"unavailable:{exc.reason}"
            return web.json_response({"status": "not-ready", "checks": checks}, status=503)
    except Exception as exc:  # pragma: no cover - defensive
        checks["jwks"] = f"error:{type(exc).__name__}"
        return web.json_response({"status": "not-ready", "checks": checks}, status=503)

    uptime = time.monotonic() - request.app[APP_STARTED_AT]
    return web.json_response(
        {"status": "ready", "checks": checks, "uptime_seconds": round(uptime, 1)}
    )


async def messages(request: web.Request) -> web.Response:
    """`POST /api/messages`. The only endpoint that matters."""
    settings: Settings = request.app[APP_SETTINGS]
    auth: InboundActivityAuthenticator = request.app[APP_AUTHENTICATOR]
    deps: Dependencies = request.app[APP_DEPS]

    if request.content_type and "json" not in request.content_type.lower():
        return web.json_response({"error": "unsupported media type"}, status=415)

    try:
        body = await request.json()
    except Exception:
        # Body is not parseable. Do not log the body; it may contain anything.
        log_event(logger, logging.WARNING, "rejected non-JSON body on /api/messages")
        return web.json_response({"error": "invalid json"}, status=400)

    if not isinstance(body, dict):
        return web.json_response({"error": "invalid activity"}, status=400)

    # ---- AUTHENTICATION. Nothing below this line may read identity fields
    # ---- until this returns an AuthenticatedCaller.
    try:
        caller = await auth.authenticate(
            auth_header=request.headers.get("Authorization"), activity=body
        )
    except InboundAuthError as exc:
        log_event(
            logger,
            logging.WARNING,
            "inbound activity rejected",
            reason=exc.reason,
            # `detail` can contain claim values; redact() is applied by the
            # formatter, but keep it short regardless.
            detail=redact(exc.detail)[:200],
            activity_type=body.get("type"),
            channel_id=body.get("channelId"),
            remote=request.remote,
        )
        # No body detail. See module docstring.
        return web.Response(status=401)

    try:
        result = await route_activity(
            body,
            caller=caller,
            deps=deps,
            expected_tenant_id=settings.entra_tenant_id,
        )
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "unhandled error routing activity",
            error_type=type(exc).__name__,
            activity_type=body.get("type"),
        )
        logger.exception("routing failure")
        return web.json_response({"error": "internal"}, status=500)

    # NOTE: `result.reply` has already been delivered to the conversation over
    # the Bot Connector by the router. It deliberately does NOT come back here.
    # The Bot Framework ignores the body of a message/conversationUpdate POST
    # and reads only the status code, so anything written here is invisible to
    # the user. `result.body` is reserved for `invoke`, whose response body is
    # the protocol payload. See routing.RouteResult.
    if result.body is None:
        return web.Response(status=result.status)
    return web.json_response(dict(result.body), status=result.status)


# --------------------------------------------------------------------------
# Application factory
# --------------------------------------------------------------------------


def create_app(
    *,
    settings: Settings | None = None,
    authenticator: InboundActivityAuthenticator | None = None,
    deps: Dependencies | None = None,
) -> web.Application:
    """Build the aiohttp application.

    Injectable on purpose: tests construct it with a fake authenticator and no
    Secret Manager, and the production path constructs it from
    :func:`app.config.get_settings`.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    authenticator = authenticator or build_authenticator(
        app_id=settings.microsoft_app_id,
        tenant_id=settings.entra_tenant_id,
        allow_emulator=settings.allow_emulator,
        jwks_cache=JwksCache(),
    )

    # `deps` is injected by tests, which supply fakes and want no network. When
    # it is not injected we assemble the real thing at startup, from
    # `app.composition`. Assembly needs a running event loop for the shared
    # aiohttp session, so it happens in an `on_startup` hook rather than here.
    assemble = deps is None
    deps = deps or Dependencies(
        signin_url=settings.signin_url,
        support_contact=settings.support_contact,
    )

    app = web.Application(client_max_size=1024 * 1024)  # 1 MiB is generous for an activity
    app[APP_SETTINGS] = settings
    app[APP_AUTHENTICATOR] = authenticator
    app[APP_DEPS] = deps
    app[APP_STARTED_AT] = time.monotonic()

    app.router.add_post("/api/messages", messages)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/readyz", readyz)

    if assemble:

        async def _assemble_dependencies(app_: web.Application) -> None:
            """Build the four collaborators, or refuse to serve.

            An exception here aborts startup, which is the entire point. The
            previous behaviour was to carry on with four `None` collaborators:
            the container came up, `/healthz` and `/readyz` went green, and
            every message got the "temporarily unavailable" template. A
            deployment that is misconfigured must not be indistinguishable
            from one that works.
            """
            from .composition import build_dependencies

            http = aiohttp.ClientSession()
            app_[APP_HTTP] = http
            try:
                app_[APP_DEPS] = build_dependencies(settings, http=http)
            except Exception as exc:
                await http.close()
                log_event(
                    logger,
                    logging.CRITICAL,
                    "refusing to start: the middle tier could not be assembled",
                    error_type=type(exc).__name__,
                    detail=str(exc),
                )
                raise

        async def _close_http(app_: web.Application) -> None:
            http = app_.get(APP_HTTP)
            if http is not None and not http.closed:
                await http.close()

        app.on_startup.append(_assemble_dependencies)
        app.on_cleanup.append(_close_http)

    async def _close_authenticator(_app: web.Application) -> None:
        await authenticator.close()

    app.on_cleanup.append(_close_authenticator)

    log_event(
        logger,
        logging.INFO,
        "middle tier initialised",
        project=settings.gcp_project_id,
        location=settings.location,
        tenant=settings.entra_tenant_id,
        app_type=settings.microsoft_app_type,
        dev_mode=settings.dev_mode,
        emulator_trusted=settings.allow_emulator,
    )
    return app


def main() -> None:  # pragma: no cover - process entry point
    # Configure logging FIRST, before any secret is read.
    #
    # `get_settings()` emits the CRITICAL "DEV-ONLY SECRET FALLBACK" lines, and
    # those are precisely the lines an operator needs to alert on. If logging
    # is configured afterwards they are emitted through the root logger's
    # default handler as unstructured text, which Cloud Logging files as a
    # plain blob with no `severity` - so a log-based alert on
    # `severity=CRITICAL` silently never fires. Observed in a live run before
    # this was moved; see NOTES.md.
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))

    try:
        settings = get_settings()
    except ConfigError as exc:
        log_event(
            logger, logging.CRITICAL, "startup configuration failed", detail=str(exc)
        )
        raise SystemExit(2) from exc

    app = create_app(settings=settings)
    # Cloud Run contract: bind 0.0.0.0 on $PORT.
    web.run_app(app, host="0.0.0.0", port=settings.port, access_log=None)


if __name__ == "__main__":  # pragma: no cover
    main()
