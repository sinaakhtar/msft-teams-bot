"""Local harness: prove the agent answers through the MCP tool AS A GIVEN USER.

WHAT THIS ASSERTS, AND WHY IT ASSERTS IT THAT WAY
================================================================================
The evidence is the RAW TOOL RESPONSE, never the model's prose. This is a rule
carried over from the Layer 3 concurrency spike and it matters: an LLM that
garbles a tool argument produces a turn with no identity in it, which is
indistinguishable from an identity defect if you only read the final text.
Conflating those two would have us "discover" a security bug that is really a
model flake, or worse, miss a real one behind fluent prose. So:

  * PASS  = the workforce-principal / user identity string appears in a raw
            function_response, and no OTHER user's identity appears in it.
  * TOOL-FAILED = no identity anywhere and the tool reported an error. Not a
            pass and not an identity failure. Reported as its own category.
  * FAIL  = an identity string that belongs to a different user turns up in
            this invocation's tool responses. That is a cross-user leak.

MODES
  --selftest    OFFLINE. No Google credentials, no model, no network. Drives
                `header_provider` directly under concurrency with fake
                invocation contexts and asserts every concurrent caller gets
                its own token back. This is the part that can actually be run
                in a sandbox, and it is the part that proves the threading.

  (default)     LIVE. Requires real tokens and a reachable Vertex AI + BigQuery.
                Runs the real agent through the real MCP endpoint.

LIVE USAGE
  Single user:
    export GOOGLE_CLOUD_PROJECT=example-project
    export GOOGLE_CLOUD_LOCATION=us-central1
    export GOOGLE_GENAI_USE_VERTEXAI=True
    export BQ_USER_ACCESS_TOKEN="$(...workforce principal access token...)"
    python test_local.py

  Two users concurrently (the leak test):
    export BQ_USER_ACCESS_TOKEN_A=... BQ_USER_SUBJECT_A='principal://...'
    export BQ_USER_ACCESS_TOKEN_B=... BQ_USER_SUBJECT_B='someone@example.com'
    python test_local.py --fanout 3

  Tokens can be minted with the sibling spike helper:
    python ../layer3/tokens.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from typing import Any, Optional

# --------------------------------------------------------------------------
# The probe question. SESSION_USER() is the zero-setup identity probe: it needs
# no table, no dataset and no row-access policy, so it isolates identity from
# every other variable.
# --------------------------------------------------------------------------
PROBE_PROMPT = (
    "Run this exact query and report the single value it returns, nothing "
    "else: SELECT SESSION_USER() AS who"
)

DATA_PROMPT = (
    "How many rows can I see in the demo dataset? Discover the tables first, "
    "then count rows in the largest one."
)

IDENTITY_RE = re.compile(
    r"principal://iam\.googleapis\.com/[\w/\-]+/subject/[0-9a-zA-Z\-]+"
    r"|[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.(?:com|net|org|io|onmicrosoft\.com)"
)


# ==========================================================================
# OFFLINE SELFTEST
# ==========================================================================


class _FakeReadonlyContext:
    """Stands in for ADK's ReadonlyContext. Only `.state` / `.invocation_id`
    are touched by `header_provider`, so this is a faithful stub."""

    def __init__(self, invocation_id: str, state: dict[str, Any]):
        self.invocation_id = invocation_id
        self.state = state


class _FakeInvocationContext:
    def __init__(self, invocation_id: str, state: dict[str, Any]):
        self.invocation_id = invocation_id
        self.session = type("_S", (), {"state": state})()


async def selftest(concurrency: int = 12) -> int:
    """Concurrency test of the credential path. Runs anywhere, no cloud."""
    from bq_agent import credentials as cred

    print(f"=== OFFLINE SELFTEST: {concurrency} concurrent invocations ===")
    failures: list[str] = []

    # ---- 1. header_provider under a contextvar, concurrently --------------
    barrier = asyncio.Barrier(concurrency)

    async def one(index: int) -> tuple[str, str]:
        token = f"tok-user-{index}"
        with cred.bind_user_credential(
            cred.UserCredential(access_token=token, subject=f"user-{index}")
        ):
            # Force genuine overlap: every task holds its binding while all the
            # others acquire theirs. Sequential execution cannot pass this.
            await barrier.wait()
            headers = await cred.header_provider(
                _FakeReadonlyContext(f"inv-{index}", {})
            )
        return token, headers["Authorization"]

    results = await asyncio.gather(*[one(i) for i in range(concurrency)])
    for expected, got in results:
        if got != f"Bearer {expected}":
            failures.append(f"contextvar bleed: expected {expected}, header was {got!r}")
    print(f"[{'PASS' if not failures else 'FAIL'}] contextvar: "
          f"{len(results)} concurrent callers, each got its own token")

    # ---- 2. the temp: state fallback --------------------------------------
    headers = await cred.header_provider(
        _FakeReadonlyContext("inv-x", {cred.TEMP_STATE_KEY: "tok-from-temp-state"})
    )
    if headers.get("Authorization") != "Bearer tok-from-temp-state":
        failures.append("temp: state fallback did not produce the token")
    if headers.get(cred.USER_PROJECT_HEADER) != cred.USER_PROJECT:
        failures.append("X-Goog-User-Project header missing or wrong")
    print(f"[{'PASS' if len(failures) == 0 else 'FAIL'}] temp: state fallback "
          f"+ mandatory {cred.USER_PROJECT_HEADER} header")

    # ---- 3. a token under a PERSISTED key must be refused -----------------
    persisted = {"bq_token": "ya29.persisted-and-therefore-forbidden"}
    try:
        await cred.header_provider(_FakeReadonlyContext("inv-y", persisted))
        failures.append("header_provider accepted a token from PERSISTED state")
        print("[FAIL] persisted-state token was accepted")
    except cred.MissingUserCredential:
        print("[PASS] token under a non-temp: key refused, failed closed")

    # ---- 4. no credential at all -> fail closed, no ambient fallback ------
    try:
        await cred.header_provider(_FakeReadonlyContext("inv-z", {}))
        failures.append("header_provider returned headers with NO credential")
        print("[FAIL] no-credential case did not raise")
    except cred.MissingUserCredential:
        print("[PASS] no credential -> MissingUserCredential (no service-account fallback)")

    # ---- 5. the invocation-boundary plugin binds and, crucially, UNBINDS --
    plugin = cred.UserCredentialPlugin()
    ic = _FakeInvocationContext("inv-p", {cred.TEMP_STATE_KEY: "tok-plugin"})
    await plugin.before_run_callback(invocation_context=ic)
    bound = cred.current_user_credential()
    if bound is None or bound.access_token != "tok-plugin":
        failures.append("plugin did not bind the credential from temp: state")
    await plugin.after_run_callback(invocation_context=ic)
    if cred.current_user_credential() is not None:
        failures.append("plugin did not UNBIND: a Tool Identity outlived its turn")
    if cred.TEMP_STATE_KEY in ic.session.state:
        failures.append("plugin left the live token in the in-memory session state")
    print(f"[{'PASS' if not failures else 'FAIL'}] plugin binds at invocation "
          f"start and unbinds at invocation end")

    # ---- 6. a credential must never stringify to its token ----------------
    c = cred.UserCredential(access_token="ya29.super-secret", subject="s")
    if "super-secret" in repr(c) or "super-secret" in str(c):
        failures.append("UserCredential leaks its token via repr/str")
    print(f"[{'PASS' if not failures else 'FAIL'}] credential never renders its token")

    print()
    if failures:
        print("SELFTEST FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("SELFTEST PASSED")
    return 0


# ==========================================================================
# LIVE HARNESS
# ==========================================================================


def _load_identities() -> dict[str, dict[str, Optional[str]]]:
    """Identities from the environment. Nothing is minted or invented here."""
    identities: dict[str, dict[str, Optional[str]]] = {}
    single = os.environ.get("BQ_USER_ACCESS_TOKEN")
    if single:
        identities["user"] = {
            "token": single,
            "expect": os.environ.get("BQ_USER_SUBJECT"),
        }
    for suffix in ("A", "B", "C"):
        token = os.environ.get(f"BQ_USER_ACCESS_TOKEN_{suffix}")
        if token:
            identities[suffix] = {
                "token": token,
                "expect": os.environ.get(f"BQ_USER_SUBJECT_{suffix}"),
            }
    return identities


async def _one_invocation(runner, label: str, token: str, prompt: str,
                          barrier: Optional[asyncio.Barrier]) -> dict:
    """One user's turn. Collects raw tool evidence, not prose."""
    from google.genai import types

    from bq_agent import credentials as cred

    user_id = f"local-harness-{label}"
    session = await runner.session_service.create_session(
        app_name=runner.app_name, user_id=user_id
    )

    final_text = ""
    tool_identities: list[str] = []
    tool_errors: list[str] = []
    tool_calls: list[str] = []

    with cred.bind_user_credential(
        cred.UserCredential(access_token=token, subject=label)
    ):
        if barrier is not None:
            await asyncio.wait_for(barrier.wait(), timeout=120)
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=prompt)]),
        ):
            if not (event.content and event.content.parts):
                continue
            for part in event.content.parts:
                if part.text:
                    final_text += part.text
                call = getattr(part, "function_call", None)
                if call is not None:
                    tool_calls.append(f"{call.name}({sorted((call.args or {}).keys())})")
                response = getattr(part, "function_response", None)
                if response is not None and response.response is not None:
                    raw = json.dumps(response.response, default=str)
                    tool_identities.extend(IDENTITY_RE.findall(raw))
                    if "error" in raw.lower() or "denied" in raw.lower():
                        tool_errors.append(raw[:400])

    return {
        "label": label,
        "final_text": final_text.strip(),
        "tool_calls": tool_calls,
        "tool_identities": sorted(set(tool_identities)),
        "tool_errors": tool_errors,
    }


async def live(fanout: int, prompt: str) -> int:
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService

    from bq_agent.agent import build_agent, build_plugins

    identities = _load_identities()
    if not identities:
        print(
            "BLOCKED: no per-user access token in the environment.\n"
            "  Set BQ_USER_ACCESS_TOKEN (single user), or\n"
            "  BQ_USER_ACCESS_TOKEN_A / _B (concurrent leak test).\n"
            "  Mint one with: python ../layer3/tokens.py",
            file=sys.stderr,
        )
        return 2

    invocations = [
        (f"{label}#{rep}" if fanout > 1 else label, spec)
        for rep in range(fanout)
        for label, spec in identities.items()
    ]

    # ONE shared agent instance for every user. That is the deployed topology,
    # and a per-user instance would not test anything worth testing.
    runner = Runner(
        app_name="bq_teams_analyst_local",
        agent=build_agent(),
        plugins=build_plugins(),
        session_service=InMemorySessionService(),
    )

    barrier = asyncio.Barrier(len(invocations)) if len(invocations) > 1 else None
    print(f"=== LIVE: {len(invocations)} invocation(s) through ONE agent instance ===")

    results = await asyncio.gather(
        *[
            _one_invocation(runner, label, spec["token"], prompt, barrier)
            for label, spec in invocations
        ],
        return_exceptions=True,
    )

    verdict = 0
    seen_by_label: dict[str, list[str]] = {}
    for result in results:
        if isinstance(result, BaseException):
            print(f"[ERROR] {type(result).__name__}: {result}")
            verdict = 1
            continue
        label = result["label"]
        base = label.split("#")[0]
        expect = identities[base]["expect"]
        seen_by_label.setdefault(base, []).extend(result["tool_identities"])

        print(f"\n--- {label} ---")
        print(f"  tool calls        : {result['tool_calls']}")
        print(f"  raw tool identity : {result['tool_identities']}")
        if result["tool_errors"]:
            print(f"  tool errors       : {result['tool_errors'][:1]}")
        print(f"  model text        : {result['final_text'][:200]}")

        if not result["tool_identities"]:
            print("  VERDICT: TOOL-FAILED (no identity observed; NOT an identity pass)")
            verdict = max(verdict, 1)
        elif expect and not any(expect in i for i in result["tool_identities"]):
            print(f"  VERDICT: FAIL (expected {expect!r}, tool said "
                  f"{result['tool_identities']})")
            verdict = 1
        else:
            print("  VERDICT: PASS (raw tool response carries this user's identity)")

    # Cross-user leak check across the whole run.
    for label, identities_seen in seen_by_label.items():
        others = {
            other
            for other_label, seen in seen_by_label.items()
            if other_label != label
            for other in seen
        }
        overlap = set(identities_seen) & others
        if overlap:
            print(f"\n*** CROSS-USER IDENTITY LEAK: {label} and another user "
                  f"both saw {overlap} ***")
            verdict = 1

    print("\n" + ("LIVE RUN PASSED" if verdict == 0 else "LIVE RUN NOT CLEAN"))
    return verdict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true",
                        help="offline credential-threading test; no cloud needed")
    parser.add_argument("--concurrency", type=int, default=12,
                        help="concurrent callers in --selftest")
    parser.add_argument("--fanout", type=int, default=1,
                        help="repeat each live identity N times")
    parser.add_argument("--data", action="store_true",
                        help="ask a real data question instead of the identity probe")
    args = parser.parse_args()

    if args.selftest:
        return asyncio.run(selftest(args.concurrency))
    return asyncio.run(live(args.fanout, DATA_PROMPT if args.data else PROBE_PROMPT))


if __name__ == "__main__":
    raise SystemExit(main())
