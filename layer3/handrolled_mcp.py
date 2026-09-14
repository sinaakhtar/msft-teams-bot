"""A minimal streamable-HTTP MCP client, for approach C.

The point of approach C is that the credential is an explicit function
argument. There is no ambient state, no construction-time capture and no
pooling keyed on anything, so there is nothing to leak between users by
construction. The cost is that MCPToolset's ergonomics (tool discovery,
schema generation) are lost and must be hand-maintained.

Async so it can be driven concurrently, which is the whole subject of the spike.
"""
from __future__ import annotations

import json
import uuid

import httpx

MCP_URL = "https://bigquery.googleapis.com/mcp"
PROTOCOL_VERSION = "2025-06-18"


def _parse(resp: httpx.Response) -> dict:
    """Streamable HTTP may answer as JSON or as a one-shot SSE frame."""
    text = resp.text
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype or text.lstrip().startswith("event:"):
        for line in text.splitlines():
            if line.startswith("data: "):
                return json.loads(line[6:])
        raise ValueError(f"no data frame in SSE response: {text[:300]}")
    return json.loads(text)


async def _rpc(client: httpx.AsyncClient, method: str, params: dict | None = None) -> dict:
    body: dict = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method}
    if params is not None:
        body["params"] = params
    resp = await client.post(MCP_URL, json=body)
    if resp.status_code >= 400:
        # ADR 004: surface the real error text. A 403 naming a missing role is a
        # permission fix; a 403 naming the credential type is fatal. Only the
        # message distinguishes them, so never swallow it.
        raise RuntimeError(f"MCP HTTP {resp.status_code}: {resp.text[:600]}")
    return _parse(resp)


async def execute_sql_readonly(token: str, project: str, query: str) -> str:
    """Run a read-only query as whoever `token` identifies."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": PROTOCOL_VERSION,
        "X-Goog-User-Project": project,
    }
    async with httpx.AsyncClient(timeout=60.0, headers=headers) as client:
        await _rpc(client, "initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "layer3-spike", "version": "0"},
        })
        # NOTE camelCase. `project_id`/`statement` returns a bare
        # "Request contains an invalid argument" with no hint which one.
        res = await _rpc(client, "tools/call", {
            "name": "execute_sql_readonly",
            "arguments": {"projectId": project, "query": query},
        })
    if "error" in res:
        raise RuntimeError(f"MCP tool error: {json.dumps(res['error'])[:600]}")
    return json.dumps(res.get("result", res))[:4000]
