"""Structured JSON logging for Cloud Run, plus token redaction.

Cloud Run scrapes stdout. A line that is valid JSON with a `severity` field is
picked up by Cloud Logging as a structured entry; anything else lands as an
unparsed text blob. So: one JSON object per line, on stdout, always.

REDACTION IS NOT OPTIONAL. Two things must never reach a log sink:

  * inbound Bot Framework channel tokens (they authenticate a caller as Azure
    Bot Service, and are replayable until `exp`),
  * Teams SSO / Entra tokens and any Google access token minted from them
    (they authenticate a caller as a specific human).

Every helper that touches a header, an activity, or an exception message runs
through :func:`redact` first. Use :func:`redact` even when you are "sure" the
value is safe - the cost of being wrong is a credential in a log bucket that
a wide set of principals can read.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any, Mapping

# A JWT is three base64url segments separated by dots. We match greedily on
# shape rather than on context, so a token pasted into a free-text error
# message is caught just as reliably as one in an Authorization header.
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")

# `Bearer <anything non-space>` - catches opaque (non-JWT) bearer tokens too.
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[^\s\"']+")

# Keys whose *values* are always replaced wholesale, regardless of shape.
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "auth_header",
        "token",
        "access_token",
        "id_token",
        "refresh_token",
        "client_secret",
        "app_password",
        "microsoftapppassword",
        "password",
        "secret",
        "assertion",
        "code",
        "connectionname_token",
        "x-ms-token",
    }
)

_REDACTED = "[REDACTED]"


def redact(value: Any) -> Any:
    """Return `value` with anything that looks like a credential removed.

    Recurses into dicts and lists. Never raises - a redaction helper that can
    throw is a redaction helper that gets wrapped in a bare `except` and then
    bypassed.
    """
    try:
        if isinstance(value, str):
            out = _JWT_RE.sub(_REDACTED, value)
            out = _BEARER_RE.sub(r"\1 " + _REDACTED, out)
            return out
        if isinstance(value, Mapping):
            return {
                k: (_REDACTED if str(k).lower() in _SENSITIVE_KEYS else redact(v))
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [redact(v) for v in value]
        return value
    except Exception:  # pragma: no cover - defensive
        return _REDACTED


def fingerprint(token: str | None) -> str:
    """A stable, non-reversible handle for a token, safe to log.

    Lets you correlate "the same bad token retried 400 times" without ever
    writing the token itself. SHA-256, first 12 hex chars.
    """
    if not token:
        return "none"
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


class _CloudRunJsonFormatter(logging.Formatter):
    """Render a LogRecord as a single-line JSON object for Cloud Logging."""

    _SEVERITY = {
        logging.DEBUG: "DEBUG",
        logging.INFO: "INFO",
        logging.WARNING: "WARNING",
        logging.ERROR: "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": self._SEVERITY.get(record.levelno, "DEFAULT"),
            "message": redact(record.getMessage()),
            "logger": record.name,
        }
        extra = getattr(record, "json_fields", None)
        if isinstance(extra, Mapping):
            payload.update(redact(dict(extra)))
        if record.exc_info:
            # The formatted traceback can contain a token if some library
            # helpfully embedded one in an exception message.
            payload["exception"] = redact(self.formatException(record.exc_info))
        trace = getattr(record, "trace", None)
        if trace:
            payload["logging.googleapis.com/trace"] = trace
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger, writing to stdout."""
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(_CloudRunJsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # aiohttp's access logger duplicates what Cloud Run's request log already
    # gives us, and it logs full URLs. Quiet it.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def log_event(
    logger: logging.Logger,
    level: int,
    message: str,
    /,
    **fields: Any,
) -> None:
    """Emit a structured event. All `fields` are redacted before serialization."""
    logger.log(level, message, extra={"json_fields": fields})
