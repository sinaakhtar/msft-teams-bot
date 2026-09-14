"""Get a real Entra ID token for a real user, without needing a Teams bot yet.

Device code flow, split into two steps so the code can be handed to a human
between them.

    python entra_device_login.py start
    ... human signs in ...
    python entra_device_login.py poll --out token.json

The ID token this produces has `aud` equal to the app's client ID, which is
exactly what the Workforce Pool provider is configured to accept.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import sys
import time

import httpx


def _env(name: str) -> str:
    """Required environment value. See .env.example at the repository root."""
    value = os.environ.get(name, "")
    if not value:
        sys.exit(
            f"{name} is not set. Source your .env first:\n"
            f"    set -a && . ../.env && set +a"
        )
    return value


TENANT_ID = _env("ENTRA_TENANT_ID")
CLIENT_ID = _env("FEDERATION_APP_CLIENT_ID")
BASE = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0"
STATE = pathlib.Path("/tmp/entra_device_flow.json")


def start() -> int:
    r = httpx.post(
        f"{BASE}/devicecode",
        data={"client_id": CLIENT_ID, "scope": "openid profile offline_access"},
        timeout=30.0,
    )
    r.raise_for_status()
    flow = r.json()
    STATE.write_text(json.dumps(flow))

    print("Go to:   ", flow["verification_uri"])
    print("Enter:   ", flow["user_code"])
    print(f"Expires in {flow['expires_in'] // 60} minutes.")
    return 0


def _decode_claims(jwt: str) -> dict:
    payload = jwt.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def poll(out: str | None) -> int:
    flow = json.loads(STATE.read_text())
    interval = int(flow.get("interval", 5))
    deadline = time.time() + int(flow.get("expires_in", 900))

    while time.time() < deadline:
        r = httpx.post(
            f"{BASE}/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": CLIENT_ID,
                "device_code": flow["device_code"],
            },
            timeout=30.0,
        )
        body = r.json()
        if r.status_code == 200:
            claims = _decode_claims(body["id_token"])
            # These four decide whether the Google exchange can possibly work.
            print("signed in as:", claims.get("preferred_username"))
            print("  oid:", claims.get("oid"))
            print("  tid:", claims.get("tid"))
            print("  aud:", claims.get("aud"))
            print("  iss:", claims.get("iss"))
            if out:
                pathlib.Path(out).write_text(json.dumps(body))
                print(f"\ntoken written to {out}")
            return 0

        err = body.get("error")
        if err == "authorization_pending":
            time.sleep(interval)
            continue
        if err == "slow_down":
            interval += 5
            time.sleep(interval)
            continue
        print(f"device flow failed: {json.dumps(body, indent=2)}", file=sys.stderr)
        return 1

    print("device code expired before sign-in completed", file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start")
    p = sub.add_parser("poll")
    p.add_argument("--out", help="write the raw token response here")
    args = ap.parse_args()
    return start() if args.cmd == "start" else poll(args.out)


if __name__ == "__main__":
    sys.exit(main())
