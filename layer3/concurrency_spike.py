"""Layer 3 spike: is per-user token threading in ADK safe under CONCURRENCY?

THE RISK
--------
One deployed Agent instance in the Agent Runtime serves every user. If a
per-user credential is captured once and reused, user B's question is answered
with user A's authority. That is a cross-user data leak, not a bug.

THE ACCEPTANCE TEST IS CONCURRENCY, NOT CORRECTNESS
---------------------------------------------------
A sequential test passes even when the design is broken, because the second
invocation simply overwrites shared state after the first has finished. This
harness therefore installs an ``asyncio.Barrier`` inside the credential
resolution path: NEITHER invocation may proceed until BOTH have arrived. The
two tool calls are consequently in flight at the same instant, which is the
maximally adversarial interleaving for any shared-state bug.

If the barrier times out, the invocations did NOT overlap and the run is
reported INCONCLUSIVE rather than passing. A test that cannot prove it
overlapped has not tested concurrency.

WHAT IS REAL HERE
-----------------
Two genuinely different live identities, two real tokens, real calls to the
managed BigQuery MCP server. ``SELECT SESSION_USER()`` is the probe: BigQuery
itself reports who it believes the caller is, so a leak is observable rather
than argued. Nothing is mocked or simulated.

APPROACHES UNDER TEST
---------------------
A. ``contextvar`` set per invocation, read by an async ``header_provider``.
B. Token carried in Agent Runtime Session state, read from ``ReadonlyContext``.
C. Hand-rolled MCP client as a plain ADK ``FunctionTool`` reading ``ToolContext``.

Note on ADK 2.8.0: ``MCPToolset`` accepts a ``header_provider`` callable that is
invoked at TOOL CALL time with the live invocation's context (see
``mcp_tool.py``: ``self._header_provider(ReadonlyContext(tool_context._invocation_context))``),
and MCP sessions are pooled on a hash of the merged headers. The premise that
``MCPToolset`` can only take headers at construction time is outdated for this
version. That is what makes approaches A and B viable at all.
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import os
import re
import sys
import traceback
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tokens as tk  # noqa: E402

from google.adk.agents import LlmAgent  # noqa: E402
from google.adk.agents.readonly_context import ReadonlyContext  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.adk.tools.mcp_tool.mcp_session_manager import (  # noqa: E402
    StreamableHTTPConnectionParams,
)
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset  # noqa: E402
from google.adk.tools.tool_context import ToolContext  # noqa: E402
from google.genai import types  # noqa: E402

import handrolled_mcp  # noqa: E402

MCP_URL = "https://bigquery.googleapis.com/mcp"
#: Billing/quota project for the MCP call. Required: workforce principals
#: have no project of their own, so BigQuery cannot infer one.
PROJECT = os.environ.get("GCP_PROJECT_ID", "")
if not PROJECT:
    raise SystemExit(
        "GCP_PROJECT_ID is not set. This spike talks to live BigQuery and has\n"
        "no safe default. Source your .env first:\n"
        "    set -a && . ../.env && set +a"
    )
APP_NAME = "layer3_spike"
MODEL = "gemini-2.5-flash"

PROMPT = (
    "Call the execute_sql_readonly tool exactly once. You MUST supply BOTH "
    f'arguments: projectId must be the string "{PROJECT}", and query must be '
    "the string \"SELECT SESSION_USER() AS whoami\". Both are required; a call "
    "missing either one will fail. Then reply with ONLY the raw value of the "
    "whoami column and nothing else."
)

INSTRUCTION = (
    "You are an identity probe. When asked, call the execute_sql_readonly tool "
    "exactly once and report the raw value it returns verbatim. Never invent a "
    "value. If the tool fails, say TOOL_ERROR followed by the error text."
)

# --- Approach A state -------------------------------------------------------
CURRENT_TOKEN: contextvars.ContextVar[str] = contextvars.ContextVar("CURRENT_TOKEN")

# --- Concurrency enforcement ------------------------------------------------
_barrier: asyncio.Barrier | None = None
_overlap_confirmed = False
_resolutions: list[tuple[str, str]] = []  # (invocation_label, token_fingerprint)
_rendezvoused: set[str] = set()  # header_provider fires more than once per turn
_rz_lock: asyncio.Lock | None = None


def fp(token: str) -> str:
    """Short, non-secret fingerprint of a token, for evidence without leaking it."""
    return f"...{token[-12:]}"


async def _rendezvous(label: str) -> None:
    """Block until every concurrent invocation has reached credential resolution.

    Only the FIRST credential resolution per invocation participates. ADK calls
    ``header_provider`` more than once per turn (tool listing, then the call),
    and a barrier sized to the number of invocations would otherwise stall for
    every subsequent call once its partners had moved on.
    """
    global _overlap_confirmed
    if _barrier is None or _rz_lock is None:
        return
    async with _rz_lock:
        if label in _rendezvoused:
            return
        _rendezvoused.add(label)
    try:
        await asyncio.wait_for(_barrier.wait(), timeout=90)
        _overlap_confirmed = True
    except (TimeoutError, asyncio.TimeoutError, asyncio.BrokenBarrierError) as e:
        print(f"  [{label}] BARRIER NOT MET ({type(e).__name__}): "
              f"invocations did not overlap; result is INCONCLUSIVE", flush=True)


def _headers(token: str) -> dict[str, str]:
    # X-Goog-User-Project is mandatory for workforce principals: they have no
    # project of their own to bill. It is why roles/serviceusage.serviceUsageConsumer
    # is required on the quota project.
    return {
        "Authorization": f"Bearer {token}",
        "X-Goog-User-Project": PROJECT,
    }


# ---------------------------------------------------------------- approach A
async def header_provider_contextvar(ctx: ReadonlyContext) -> dict[str, str]:
    label = ctx.state.get("label", "?")
    await _rendezvous(label)
    # Read the contextvar AFTER the rendezvous, under maximum contention. If
    # ADK's async execution does not preserve the per-invocation context, this
    # is where the wrong token surfaces.
    token = CURRENT_TOKEN.get()
    _resolutions.append((label, fp(token)))
    print(f"  [{label}] approach A resolved token {fp(token)}", flush=True)
    return _headers(token)


# ---------------------------------------------------------------- approach B
async def header_provider_state(ctx: ReadonlyContext) -> dict[str, str]:
    label = ctx.state.get("label", "?")
    await _rendezvous(label)
    # The token travels with the invocation's own session state, so there is no
    # ambient state to get confused. ADK hands us the right context explicitly.
    token = ctx.state["google_access_token"]
    _resolutions.append((label, fp(token)))
    print(f"  [{label}] approach B resolved token {fp(token)}", flush=True)
    return _headers(token)


def build_mcp_agent(header_provider) -> LlmAgent:
    """ONE agent instance, ONE toolset instance: the production topology."""
    toolset = MCPToolset(
        connection_params=StreamableHTTPConnectionParams(url=MCP_URL, timeout=60),
        tool_filter=["execute_sql_readonly"],
        header_provider=header_provider,
    )
    return LlmAgent(
        name="identity_probe",
        model=MODEL,
        instruction=INSTRUCTION,
        tools=[toolset],
    )


# ---------------------------------------------------------------- approach C
def build_handrolled_agent() -> LlmAgent:
    """A plain ADK tool that reads the credential from ToolContext itself.

    Loses MCPToolset ergonomics, gains total control of the credential: the
    token is a function argument, never ambient, so there is no shared state to
    leak in the first place.
    """
    async def execute_sql_readonly(query: str, tool_context: ToolContext) -> str:
        """Runs a read-only BigQuery SQL query as the signed-in user.

        Args:
            query: The SQL to execute.
        """
        state = tool_context._invocation_context.session.state
        label = state.get("label", "?")
        await _rendezvous(label)
        token = state["google_access_token"]
        _resolutions.append((label, fp(token)))
        print(f"  [{label}] approach C resolved token {fp(token)}", flush=True)
        return await handrolled_mcp.execute_sql_readonly(token, PROJECT, query)

    return LlmAgent(
        name="identity_probe",
        model=MODEL,
        instruction=INSTRUCTION,
        tools=[execute_sql_readonly],
    )


#: Matches either identity shape SESSION_USER() can return: a workforce
#: principal URI (Identity A) or an ordinary Google account address
#: (Identity B). ORG_DOMAIN scopes the second so an unrelated address in
#: the model output cannot be mistaken for the answer.
_ORG_DOMAIN = re.escape(os.environ.get("ORG_DOMAIN", "example.com"))
IDENTITY_RE = re.compile(
    r"principal://iam\.googleapis\.com/[\w/\-]+/subject/[0-9a-f\-]+"
    r"|[A-Za-z0-9._%+\-]+@" + _ORG_DOMAIN
)


async def one_invocation(runner: Runner, label: str, token: str,
                         approach: str) -> dict:
    """Run one user's turn end to end.

    Returns both the model's final text AND every identity string observed in
    raw tool responses. The tool response is the authoritative evidence: it is
    what BigQuery actually said. The model's prose is secondary, because an LLM
    that garbles a tool argument would otherwise be indistinguishable from an
    identity defect, and those two failures must never be confused.
    """
    user_id = f"entra-test-{label}"
    session = await runner.session_service.create_session(
        app_name=APP_NAME,
        user_id=user_id,
        state={"label": label, "google_access_token": token},
    )
    if approach == "A":
        CURRENT_TOKEN.set(token)

    final = ""
    tool_identities: list[str] = []
    tool_errors: list[str] = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session.id,
        new_message=types.Content(role="user", parts=[types.Part(text=PROMPT)]),
    ):
        if not (event.content and event.content.parts):
            continue
        for p in event.content.parts:
            if p.text:
                final += p.text
            fr = getattr(p, "function_response", None)
            if fr is not None and fr.response is not None:
                raw = json.dumps(fr.response, default=str)
                tool_identities.extend(IDENTITY_RE.findall(raw))
                if "error" in raw.lower():
                    tool_errors.append(raw[:300])
    return {
        "final_text": final.strip(),
        "tool_identities": sorted(set(tool_identities)),
        "tool_errors": tool_errors,
    }


async def run_approach(approach: str, identities: dict, fanout: int = 1) -> dict:
    """Run every identity CONCURRENTLY through ONE shared agent instance.

    ``fanout`` repeats each identity, so the same agent serves several
    invocations of the same person alongside several of another. That is a
    harsher test than one-each: it makes any last-writer-wins bug far likelier
    to surface.
    """
    global _barrier, _overlap_confirmed, _resolutions, _rendezvoused, _rz_lock

    # Expand identities into concrete invocations.
    invocations: list[tuple[str, dict]] = []
    for rep in range(fanout):
        for label, spec in identities.items():
            invocations.append((f"{label}#{rep}" if fanout > 1 else label, spec))

    _barrier = asyncio.Barrier(len(invocations))
    _rz_lock = asyncio.Lock()
    _rendezvoused = set()
    _overlap_confirmed = False
    _resolutions = []

    if approach == "A":
        agent = build_mcp_agent(header_provider_contextvar)
    elif approach == "B":
        agent = build_mcp_agent(header_provider_state)
    elif approach == "C":
        agent = build_handrolled_agent()
    else:
        raise ValueError(approach)

    runner = Runner(
        app_name=APP_NAME,
        agent=agent,
        session_service=InMemorySessionService(),
    )

    print(f"\n=== APPROACH {approach}: {len(invocations)} SIMULTANEOUS invocations ===",
          flush=True)

    results = await asyncio.gather(
        *[one_invocation(runner, label, spec["token"], approach)
          for label, spec in invocations],
        return_exceptions=True,
    )

    report = {"approach": approach, "overlap_confirmed": _overlap_confirmed,
              "resolutions": _resolutions, "per_identity": {},
              "leaks": [], "tool_failures": [], "verdict": None}

    leak_found = False
    tool_failed = False
    identity_verified = 0

    for (label, spec), got in zip(invocations, results):
        if isinstance(got, BaseException):
            entry = {"error": f"{type(got).__name__}: {got}"}
            tool_failed = True
            report["tool_failures"].append(label)
        else:
            expect = spec["expect"]
            seen = got["tool_identities"]
            others = {s["expect"] for lbl, s in identities.items()
                      if s["expect"] != expect}
            # A LEAK is the specific, serious case: this invocation's tool call
            # came back as SOMEBODY ELSE. Distinguish it sharply from a tool
            # that simply failed, which is a harness/LLM problem, not a
            # security problem.
            leaked = sorted(set(seen) & others)
            correct = expect in seen
            if leaked:
                leak_found = True
                report["leaks"].append({"label": label, "leaked": leaked})
            if correct:
                identity_verified += 1
            if not seen:
                tool_failed = True
                report["tool_failures"].append(label)
            entry = {"expected": expect, "tool_identities": seen,
                     "identity_correct": correct,
                     "leaked_identity_of": leaked or None,
                     "tool_errors": got["tool_errors"][:1],
                     "final_text": got["final_text"][:120]}
        report["per_identity"][label] = entry
        print(f"  [{label}] correct={entry.get('identity_correct')} "
              f"leaked={entry.get('leaked_identity_of')} "
              f"seen={entry.get('tool_identities')}", flush=True)

    report["identity_verified_count"] = identity_verified
    report["invocation_count"] = len(invocations)

    # Verdict logic, in priority order. A leak outranks everything.
    if leak_found:
        report["verdict"] = "FAIL (CROSS-USER IDENTITY LEAK)"
    elif not _overlap_confirmed:
        report["verdict"] = "INCONCLUSIVE (invocations did not provably overlap)"
    elif identity_verified == len(invocations):
        report["verdict"] = "PASS"
    elif identity_verified > 0 and tool_failed:
        report["verdict"] = (f"PARTIAL (no leak; {identity_verified}/"
                             f"{len(invocations)} verified, rest tool-failed)")
    else:
        report["verdict"] = "FAIL (no identity observed)"
    print(f"  VERDICT: {report['verdict']}", flush=True)
    return report


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--approaches", default="A,B,C")
    ap.add_argument("--repeat", type=int, default=1,
                    help="Repeat each approach; concurrency bugs are often flaky.")
    ap.add_argument("--fanout", type=int, default=1,
                    help="Concurrent invocations per identity.")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "1")
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", PROJECT)
    os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")

    print("Minting two live identities...", flush=True)
    try:
        identities = tk.both()
    except Exception as e:
        print(f"BLOCKED: could not mint live identities: {type(e).__name__}: {e}")
        traceback.print_exc()
        return 2
    for label, spec in identities.items():
        print(f"  {label}: token {fp(spec['token'])} expects {spec['expect']}",
              flush=True)

    reports = []
    for approach in args.approaches.split(","):
        for i in range(args.repeat):
            try:
                reports.append(await run_approach(approach.strip(), identities,
                                                  fanout=args.fanout))
            except Exception as e:
                traceback.print_exc()
                reports.append({"approach": approach, "verdict": "ERROR",
                                "error": f"{type(e).__name__}: {e}"})

    print("\n================ SUMMARY ================")
    for r in reports:
        print(f"  approach {r['approach']}: {r['verdict']}"
              f"  (overlap_confirmed={r.get('overlap_confirmed')},"
              f" verified={r.get('identity_verified_count')}/"
              f"{r.get('invocation_count')}, leaks={len(r.get('leaks') or [])})")

    if args.out:
        with open(args.out, "w") as f:
            json.dump(reports, f, indent=2)
        print(f"\nwrote {args.out}")

    return 0 if all(r.get("verdict") == "PASS" for r in reports) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
