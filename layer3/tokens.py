"""Mint two genuinely different live identities for the Layer 3 concurrency spike.

Identity A: analyst@example.onmicrosoft.com, an Entra user with NO Google account.
  Entra refresh_token -> fresh Entra ID token (aud = federation app)
  -> Google STS token exchange -> Workforce Principal access token.
  BigQuery SESSION_USER() returns
  principal://iam.googleapis.com/locations/global/workforcePools/teams-bot-demo/subject/<oid>

Identity B: admin@example.com, a real Google account via authorized_user ADC.
  BigQuery SESSION_USER() returns admin@example.com

Two identities that BigQuery reports differently is the whole point: it makes a
cross-user leak visible rather than theoretical. Nothing here is mocked; if a
token cannot be minted this raises, and the spike must report BLOCKED.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import urllib.parse
import urllib.request


def _env(name: str) -> str:
    """Required environment value. See .env.example at the repository root."""
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(
            f"{name} is not set. This spike talks to live services and has no\n"
            f"safe default. Source your .env first:\n"
            f"    set -a && . ../.env && set +a"
        )
    return value


TENANT_ID = _env("ENTRA_TENANT_ID")
FEDERATION_CLIENT_ID = _env("FEDERATION_APP_CLIENT_ID")
PROJECT = _env("GCP_PROJECT_ID")
POOL = f"locations/global/workforcePools/{os.environ.get('WORKFORCE_POOL_ID', 'teams-bot-demo')}"
PROVIDER = os.environ.get("WORKFORCE_PROVIDER_ID", "entra")
STS_AUDIENCE = f"//iam.googleapis.com/{POOL}/providers/{PROVIDER}"

#: Identity A: the Entra-only user. Its Entra object ID.
ANALYST_OID = _env("ANALYST_OBJECT_ID")
EXPECT_ANALYST = f"principal://iam.googleapis.com/{POOL}/subject/{ANALYST_OID}"
#: Identity B: a first-party Google account reachable via the local ADC file.
EXPECT_ADMIN = _env("GOOGLE_ADMIN_ACCOUNT")

ENTRA_TOKEN_FILE = pathlib.Path("/tmp/entra_token.json")
ADC_FILE = pathlib.Path.home() / ".config/gcloud/application_default_credentials.json"


def _form_post(url: str, fields: dict) -> dict:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def jwt_claims(token: str) -> dict:
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


def entra_id_token() -> str:
    """Fresh Entra ID token for analyst@, via the stored refresh token."""
    stored = json.loads(ENTRA_TOKEN_FILE.read_text())
    got = _form_post(
        f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token",
        {
            "client_id": FEDERATION_CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": stored["refresh_token"],
            "scope": "openid profile email offline_access",
        },
    )
    # Persist the rotated refresh token so repeated runs keep working.
    ENTRA_TOKEN_FILE.write_text(json.dumps(got))
    claims = jwt_claims(got["id_token"])
    assert claims["aud"] == FEDERATION_CLIENT_ID, f"unexpected aud {claims['aud']}"
    assert claims["oid"] == ANALYST_OID, f"unexpected oid {claims['oid']}"
    return got["id_token"]


def workforce_token() -> str:
    """Identity A: Entra ID token -> Google STS -> Workforce Principal token."""
    got = _form_post(
        "https://sts.googleapis.com/v1/token",
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "audience": STS_AUDIENCE,
            "scope": "https://www.googleapis.com/auth/cloud-platform",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "subject_token": entra_id_token(),
            "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
            "options": json.dumps({"userProject": PROJECT}),
        },
    )
    return got["access_token"]


def adc_token() -> str:
    """Identity B: a real Google account, from the local authorized_user ADC."""
    d = json.loads(ADC_FILE.read_text())
    got = _form_post(
        "https://oauth2.googleapis.com/token",
        {
            "client_id": d["client_id"],
            "client_secret": d["client_secret"],
            "refresh_token": d["refresh_token"],
            "grant_type": "refresh_token",
        },
    )
    return got["access_token"]


def both() -> dict[str, dict[str, str]]:
    """Returns {label: {token, expect}} for the two identities."""
    return {
        "analyst": {"token": workforce_token(), "expect": EXPECT_ANALYST},
        "admin": {"token": adc_token(), "expect": EXPECT_ADMIN},
    }
