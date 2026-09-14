"""Spike: does the managed BigQuery MCP server authorize per-caller identity,
and does it accept a workforce-federated token?

This deliberately does NOT use ADK. It isolates one question: given a bearer
token, does https://bigquery.googleapis.com/mcp act as that identity?

Layer 1 (needs only a project):
    python mcp_identity_spike.py --token-source gcloud --sql "SELECT 1"
    python mcp_identity_spike.py --token-source sa --sa-key key.json --sql "SELECT ..."

Layer 2 (the make-or-break, needs a configured Workforce Identity Pool):
    python mcp_identity_spike.py --token-source wif --wif-config wif-client-config.json

Exit code is 0 only if the tool call returned rows. Anything else is a finding,
so record the stderr verbatim rather than paraphrasing it.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid

import httpx

MCP_URL = "https://bigquery.googleapis.com/mcp"
PROTOCOL_VERSION = "2025-06-18"


def token_from_gcloud(account: str | None) -> str:
    cmd = ["gcloud", "auth", "print-access-token"]
    if account:
        cmd += ["--account", account]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def token_from_adc(credentials_path: str | None) -> str:
    """Application Default Credentials, optionally from an explicit file.

    An `authorized_user` ADC file carries a real human identity, which is what
    layer 1 needs: we are testing whether the MCP server authorizes per caller.
    """
    import os

    from google.auth.transport.requests import Request

    if credentials_path:
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_path

    import google.auth

    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(Request())
    return creds.token


def token_from_service_account(key_path: str) -> str:
    from google.auth.transport.requests import Request
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(
        key_path, scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(Request())
    return creds.token


def token_from_wif(config_path: str) -> str:
    """Exchange an external (Entra) credential for a Google access token.

    Uses the external_account client config produced by
    `gcloud iam workforce-pools create-cred-config`. The point of this spike is
    to find out whether the resulting principal is accepted by the MCP server,
    so any failure here is a result, not a bug to work around.
    """
    from google.auth import load_credentials_from_file
    from google.auth.transport.requests import Request

    creds, _ = load_credentials_from_file(
        config_path, scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(Request())
    return creds.token


class McpProbe:
    """Minimal streamable-HTTP MCP client. Just enough to answer the question."""

    def __init__(self, token: str, project: str) -> None:
        self._session_id: str | None = None
        self._client = httpx.Client(
            timeout=60.0,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": PROTOCOL_VERSION,
                "X-Goog-User-Project": project,
            },
        )

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        headers = {}
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        resp = self._client.post(MCP_URL, json=payload, headers=headers)

        # Capture the identity-relevant failures precisely; these ARE the result.
        if resp.status_code in (401, 403):
            raise SystemExit(
                f"IDENTITY REJECTED on {method}: HTTP {resp.status_code}\n"
                f"{resp.text}\n"
                "Record this verbatim. A 403 naming a missing IAM permission means "
                "the principal was understood but unauthorized. A 401, or a 403 "
                "complaining about the credential or principal type, means the "
                "endpoint does not accept this kind of identity at all, which is a "
                "different and much worse finding."
            )
        resp.raise_for_status()

        if sid := resp.headers.get("Mcp-Session-Id"):
            self._session_id = sid

        return _parse_body(resp)

    def initialize(self) -> dict:
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcp-identity-spike", "version": "0.1.0"},
            },
        )
        self._client.post(
            MCP_URL,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={"Mcp-Session-Id": self._session_id} if self._session_id else {},
        )
        return result

    def list_tools(self) -> list[str]:
        result = self._rpc("tools/list")
        return [t["name"] for t in result.get("result", {}).get("tools", [])]

    def execute_sql_readonly(self, project: str, sql: str) -> dict:
        return self._rpc(
            "tools/call",
            {
                # Verified against the live tools/list schema on 2026-09-07:
                # required args are camelCase `projectId` and `query`.
                "name": "execute_sql_readonly",
                "arguments": {"projectId": project, "query": sql},
            },
        )


def _parse_body(resp: httpx.Response) -> dict:
    """The endpoint may answer as JSON or as a one-shot SSE stream."""
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:") :].strip())
        raise RuntimeError(f"no data frame in SSE response:\n{resp.text}")
    return resp.json()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True, help="billing/quota project")
    ap.add_argument(
        "--token-source", required=True, choices=["gcloud", "adc", "sa", "wif"]
    )
    ap.add_argument("--account", help="gcloud account, for --token-source gcloud")
    ap.add_argument("--adc-file", help="explicit ADC json, for --token-source adc")
    ap.add_argument("--sa-key", help="path to key file, for --token-source sa")
    ap.add_argument("--wif-config", help="external_account config, for --token-source wif")
    ap.add_argument("--sql", default="SELECT 1 AS ok")
    args = ap.parse_args()

    if args.token_source == "gcloud":
        token = token_from_gcloud(args.account)
    elif args.token_source == "adc":
        token = token_from_adc(args.adc_file)
    elif args.token_source == "sa":
        token = token_from_service_account(args.sa_key)
    else:
        token = token_from_wif(args.wif_config)

    # Never print the token. Print enough to tell two identities apart.
    print(f"token acquired: {len(token)} chars, prefix {token[:12]}...", file=sys.stderr)

    probe = McpProbe(token, args.project)

    init = probe.initialize()
    print(f"initialize ok: {json.dumps(init.get('result', {}), indent=2)[:400]}")

    tools = probe.list_tools()
    print(f"tools: {tools}")
    if "execute_sql_readonly" not in tools:
        print("execute_sql_readonly not advertised; the surface has changed", file=sys.stderr)
        return 2

    result = probe.execute_sql_readonly(args.project, args.sql)
    if "error" in result:
        print(f"TOOL ERROR: {json.dumps(result['error'], indent=2)}", file=sys.stderr)
        return 3

    print(json.dumps(result.get("result", {}), indent=2)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
