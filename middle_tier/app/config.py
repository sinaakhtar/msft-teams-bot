"""Configuration and Secret Manager wiring.

RULES
-----
1. Secrets come from Google Secret Manager. Not from disk, not from a mounted
   file, not from a `.env` next to the code. Cloud Run's own secret-as-file
   mount is also deliberately unused: a secret on a filesystem is a secret in
   a container layer diff, a core dump, and every `find / -name '*.json'` a
   future debugger runs.
2. There IS a local-dev environment-variable fallback, because the alternative
   is developers inventing worse ones. It is gated behind an explicit
   ``MIDDLE_TIER_DEV_MODE=true`` and it is LOUD: a CRITICAL log line per
   secret read, naming the secret. If those lines ever appear in a deployed
   environment's logs, that is an incident and it is trivially alertable.
3. Dev mode cannot be entered by accident. It requires the explicit flag AND
   the absence of ``K_SERVICE`` (which Cloud Run always sets). Setting the flag
   on Cloud Run raises at startup rather than degrading.
4. Nothing in this module ever logs a secret VALUE. It logs names and lengths.

Note the asymmetry with :mod:`app.auth.inbound`, which has no dev bypass at
all. Reading a secret from an env var during local development is a
convenience with a bounded blast radius. Skipping JWT validation is not.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from .logging_utils import log_event

logger = logging.getLogger(__name__)


class ConfigError(RuntimeError):
    """Startup configuration is invalid. Always fatal - never start degraded."""


# --------------------------------------------------------------------------
# Secret access
# --------------------------------------------------------------------------


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _running_on_cloud_run() -> bool:
    # Cloud Run always injects K_SERVICE. Its presence is the most reliable
    # "you are in production" signal available inside the container.
    return bool(os.environ.get("K_SERVICE"))


class SecretResolver:
    """Reads secrets from Secret Manager, with a loud dev-only env fallback.

    :param project_id: GCP project hosting the secrets.
    :param dev_mode: enable the env-var fallback. Refused on Cloud Run.
    """

    def __init__(self, *, project_id: str, dev_mode: bool = False) -> None:
        if dev_mode and _running_on_cloud_run():
            raise ConfigError(
                "MIDDLE_TIER_DEV_MODE=true is set on Cloud Run (K_SERVICE present). "
                "The env-var secret fallback is local-development only. Refusing "
                "to start."
            )
        self._project_id = project_id
        self._dev_mode = dev_mode
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            # Imported lazily so local dev and unit tests do not need the
            # google-cloud-secret-manager import path to be healthy.
            from google.cloud import secretmanager  # type: ignore[import-untyped]

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def get(self, secret_id: str, *, env_fallback: str, version: str = "latest") -> str:
        """Resolve one secret.

        :param secret_id: Secret Manager secret id (not the full resource name).
        :param env_fallback: env var consulted ONLY in dev mode.
        :raises ConfigError: if the secret cannot be resolved. Never returns a
            placeholder or an empty string.
        """
        if self._dev_mode:
            value = os.environ.get(env_fallback)
            if value:
                # Deliberately CRITICAL. This must be impossible to miss in a
                # log search and must page if it ever fires in a deployed env.
                log_event(
                    logger,
                    logging.CRITICAL,
                    "DEV-ONLY SECRET FALLBACK IN USE - reading a secret from an "
                    "environment variable instead of Secret Manager. This MUST "
                    "NOT happen outside local development.",
                    secret_id=secret_id,
                    env_var=env_fallback,
                    value_length=len(value),
                )
                return value
            log_event(
                logger,
                logging.WARNING,
                "dev mode is on but the env fallback is unset; falling through "
                "to Secret Manager",
                secret_id=secret_id,
                env_var=env_fallback,
            )

        name = f"projects/{self._project_id}/secrets/{secret_id}/versions/{version}"
        try:
            response = self._get_client().access_secret_version(request={"name": name})
            value = response.payload.data.decode("utf-8").strip()
        except Exception as exc:
            raise ConfigError(
                f"could not read secret {secret_id!r} from project "
                f"{self._project_id!r}: {type(exc).__name__}"
            ) from exc

        if not value:
            raise ConfigError(f"secret {secret_id!r} resolved to an empty value")

        log_event(
            logger,
            logging.INFO,
            "secret loaded from Secret Manager",
            secret_id=secret_id,
            version=version,
            value_length=len(value),
        )
        return value


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """Everything the process needs, resolved once at startup.

    Secrets live in this object in memory and nowhere else. `__repr__` is
    overridden so an accidental `print(settings)` or an exception repr cannot
    dump the bot password.
    """

    # --- identity / platform -------------------------------------------
    gcp_project_id: str
    gcp_project_number: str
    location: str
    entra_tenant_id: str

    # --- bot registration ----------------------------------------------
    microsoft_app_id: str
    microsoft_app_password: str = field(repr=False)
    microsoft_app_type: str = "SingleTenant"

    # --- Entra app used for the OBO exchange (owned by another component,
    #     loaded here so there is one secret-loading path in the service) ---
    entra_client_secret: str = field(default="", repr=False)

    # --- agent runtime ---------------------------------------------------
    reasoning_engine_id: str = ""

    #: The key the middle tier puts the user's Google token under in the
    #: invocation's `authorizations` map. The runtime turns it into the session
    #: state key `temp:{id}`, which is what `agent/bq_agent/credentials.py`
    #: reads. The two sides must agree; see `AUTHORIZATION_ID` there.
    runtime_authorization_id: str = "bigquery_user"

    # --- identity federation (ADR 002, Tool Identity plane) ---------------
    #: The Entra app the workforce pool provider's `clientId` is set to. The
    #: OBO exchange re-audiences the Teams SSO token towards this app, because
    #: Google validates the assertion's `aud` against it.
    federation_app_id: str = ""
    workforce_pool_id: str = ""
    workforce_provider_id: str = ""

    # --- behaviour --------------------------------------------------------
    allow_emulator: bool = False
    dev_mode: bool = False
    port: int = 8080
    log_level: str = "INFO"
    signin_url: str | None = None
    support_contact: str | None = None
    #: Name of the OAuth Connection Setting on the Azure Bot resource
    #: (runbook 11, step 5a). Teams will only perform the SILENT token
    #: exchange if an OAuthCard names a connection whose **Token Exchange
    #: URL** is set to the App ID URI. Leave this empty and the bot falls
    #: back to the visible sign-in card, which never yields a token.
    oauth_connection_name: str = ""

    @property
    def token_exchange_uri(self) -> str:
        """The App ID URI Teams mints the user assertion against.

        Must equal the `webApplicationInfo.resource` in the Teams manifest and
        the Token Exchange URL on the OAuth connection. If these three
        disagree, Teams either does not attempt SSO at all or returns a token
        with the wrong `aud`, and the OBO exchange then fails downstream where
        the cause is no longer visible.
        """
        return f"api://botid-{self.microsoft_app_id}"

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"Settings(project={self.gcp_project_id!r}, location={self.location!r}, "
            f"tenant={self.entra_tenant_id!r}, app_id={self.microsoft_app_id!r}, "
            f"app_type={self.microsoft_app_type!r}, dev_mode={self.dev_mode}, "
            f"secrets=<redacted>)"
        )

    @property
    def reasoning_engine_name(self) -> str:
        if not self.reasoning_engine_id:
            raise ConfigError("REASONING_ENGINE_ID is not configured")
        return (
            f"projects/{self.gcp_project_number}/locations/{self.location}"
            f"/reasoningEngines/{self.reasoning_engine_id}"
        )


def _require_env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default if default is not None else "")
    if not value:
        raise ConfigError(f"required environment variable {name} is not set")
    return value


def load_settings() -> Settings:
    """Build :class:`Settings`, reading secrets at startup.

    Fails hard and early. A middle tier that starts without a valid app id
    would serve `/healthz` while rejecting every real request, which is the
    worst possible failure shape: green dashboard, broken product.
    """
    dev_mode = _env_flag("MIDDLE_TIER_DEV_MODE", False)
    # No defaults. These three identify the Google project and the Entra
    # tenant, and there is no value we could guess that would be anything but
    # wrong. A missing one raises here, at startup, which is the cheapest
    # place for it to fail. See .env.example at the repository root.
    project_id = _require_env("GCP_PROJECT_ID")
    project_number = _require_env("GCP_PROJECT_NUMBER")
    location = os.environ.get("GCP_LOCATION", "us-central1")
    tenant_id = _require_env("ENTRA_TENANT_ID")

    resolver = SecretResolver(project_id=project_id, dev_mode=dev_mode)

    # The app id is not a secret (it is a public client identifier and appears
    # in every token's `aud`), so it comes from plain config. The password is.
    app_id = _require_env("MICROSOFT_APP_ID")
    app_password = resolver.get(
        os.environ.get("MICROSOFT_APP_PASSWORD_SECRET", "teams-bot-app-password"),
        env_fallback="MICROSOFT_APP_PASSWORD",
    )
    entra_client_secret = resolver.get(
        os.environ.get("ENTRA_CLIENT_SECRET_SECRET", "entra-obo-client-secret"),
        env_fallback="ENTRA_CLIENT_SECRET",
    )

    app_type = os.environ.get("MICROSOFT_APP_TYPE", "SingleTenant")
    if app_type not in {"SingleTenant", "MultiTenant", "UserAssignedMSI"}:
        raise ConfigError(f"unsupported MICROSOFT_APP_TYPE {app_type!r}")
    if app_type == "MultiTenant":
        # Azure stopped supporting new multi-tenant bot creation after
        # 2025-07-31. Allowed, but it should be a conscious choice.
        log_event(
            logger,
            logging.WARNING,
            "MICROSOFT_APP_TYPE=MultiTenant; new multi-tenant bot registrations "
            "are no longer supported by Azure. Prefer SingleTenant or "
            "UserAssignedMSI.",
        )

    settings = Settings(
        gcp_project_id=project_id,
        gcp_project_number=project_number,
        location=location,
        entra_tenant_id=tenant_id,
        microsoft_app_id=app_id,
        microsoft_app_password=app_password,
        microsoft_app_type=app_type,
        entra_client_secret=entra_client_secret,
        reasoning_engine_id=os.environ.get("REASONING_ENGINE_ID", ""),
        runtime_authorization_id=os.environ.get(
            "RUNTIME_AUTHORIZATION_ID", "bigquery_user"
        ),
        # FEDERATION_APP_ID has no default: it is an identifier your Entra
        # tenant issues, so any literal here would be somebody else's app.
        # It is not a secret (it appears as `aud` in every token), but a
        # wrong value fails at the STS exchange rather than degrading.
        #
        # WORKFORCE_POOL_ID and WORKFORCE_PROVIDER_ID DO default, because
        # they are names this repo chooses rather than identifiers the
        # environment imposes. The defaults match terraform/variables.tf
        # (pool_id, provider_id); change them in both places or not at all.
        federation_app_id=os.environ.get("FEDERATION_APP_ID", ""),
        workforce_pool_id=os.environ.get("WORKFORCE_POOL_ID", "teams-bot-demo"),
        workforce_provider_id=os.environ.get("WORKFORCE_PROVIDER_ID", "entra"),
        allow_emulator=_env_flag("ALLOW_BOT_EMULATOR", False) and dev_mode,
        dev_mode=dev_mode,
        # Cloud Run contract: listen on $PORT, default 8080.
        port=int(os.environ.get("PORT", "8080")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        signin_url=os.environ.get("SIGNIN_URL") or None,
        support_contact=os.environ.get("SUPPORT_CONTACT") or None,
        oauth_connection_name=os.environ.get("OAUTH_CONNECTION_NAME", ""),
    )

    if _env_flag("ALLOW_BOT_EMULATOR", False) and not dev_mode:
        raise ConfigError(
            "ALLOW_BOT_EMULATOR=true requires MIDDLE_TIER_DEV_MODE=true. The "
            "emulator issuer must never be trusted in a deployed environment."
        )

    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide cached settings. Secrets are read exactly once."""
    return load_settings()


__all__ = [
    "ConfigError",
    "SecretResolver",
    "Settings",
    "get_settings",
    "load_settings",
]
