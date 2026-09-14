"""Offline tests for the ADR 004 tool boundary: the two 403s and the arg repair.

Runs anywhere:  python test_errors.py
"""

from __future__ import annotations

import asyncio
import sys

from bq_agent.credentials import MissingUserCredential
from bq_agent.errors import (
    DenialKind,
    FailClosedToolPlugin,
    build_denial,
    classify,
    extract_resource,
    looks_like_denial,
    render,
)

failures: list[str] = []


def check(condition: bool, label: str) -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {label}")
    if not condition:
        failures.append(label)


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


# --------------------------------------------------------------------------
# The two 403s
# --------------------------------------------------------------------------

PERMISSION_403 = (
    "403 Access Denied: Table example-project:teams_bot_demo.orders: User does not "
    "have bigquery.tables.getData permission for table "
    "example-project:teams_bot_demo.orders."
)

CREDENTIAL_403 = (
    "403 PERMISSION_DENIED: Your application is authenticating by using local "
    "Application Default Credentials. Caller does not have permission to use "
    "project example-project: serviceusage.services.use denied."
)

CREDENTIAL_401 = (
    "401 UNAUTHENTICATED: Request had invalid authentication credentials. "
    "Expected OAuth 2 access token."
)

ROW_POLICY_403 = (
    "403 Access Denied: BigQuery BigQuery: User does not have permission to "
    "query table example-project:teams_bot_demo.sales, or perhaps it does not exist."
)


def test_classification() -> None:
    print("=== the two 403s ===")
    check(classify(PERMISSION_403) is DenialKind.PERMISSION,
          "403 naming a missing ROLE/permission -> PERMISSION (a grant fixes it)")
    check(classify(CREDENTIAL_403) is DenialKind.CREDENTIAL,
          "403 naming the CREDENTIAL/user-project -> CREDENTIAL (fatal, no grant fixes it)")
    check(classify(CREDENTIAL_401) is DenialKind.CREDENTIAL,
          "401 -> CREDENTIAL")
    check(classify(ROW_POLICY_403) is DenialKind.PERMISSION,
          "row-access-policy refusal -> PERMISSION")
    check(not looks_like_denial("Query error: Syntax error at [1:8]"),
          "an ordinary SQL error is NOT treated as a denial")
    check(looks_like_denial(PERMISSION_403), "a real denial is detected")


def test_resource_naming() -> None:
    print()
    print("=== ADR 004: the message must NAME the refused resource ===")
    check(extract_resource(PERMISSION_403) == "example-project:teams_bot_demo.orders",
          "resource extracted from the error text")
    named = extract_resource(
        "403 Access Denied.",
        fallback_args={"query": "SELECT * FROM `example-project.teams_bot_demo.orders`"},
    )
    check(named == "example-project.teams_bot_demo.orders",
          "resource recovered from the SQL when the error does not name it")


def test_rendering_never_swallows_text() -> None:
    print()
    print("=== the verbatim server text survives into the rendered message ===")
    for text, must_contain in (
        (PERMISSION_403, "bigquery.tables.getData"),
        (CREDENTIAL_403, "serviceusage.services.use"),
        (CREDENTIAL_401, "invalid authentication credentials"),
    ):
        rendered = render(build_denial(text))
        check(must_contain in rendered,
              f"rendered message still quotes {must_contain!r}")

    permission_msg = render(build_denial(PERMISSION_403))
    credential_msg = render(build_denial(CREDENTIAL_403))
    check("ACCESS DENIED" in permission_msg and "not a sign-in problem" in permission_msg,
          "permission denial tells the user signing in again will not help")
    check("SIGN-IN REQUIRED" in credential_msg and "not a permissions problem" in credential_msg,
          "credential denial tells the user to sign in again")
    check("Do NOT speculate" in permission_msg and "invent" in permission_msg,
          "the model is instructed to relay, not to explain or invent")


# --------------------------------------------------------------------------
# Plugin behaviour
# --------------------------------------------------------------------------


async def test_plugin() -> None:
    print()
    print("=== tool-boundary plugin ===")
    plugin = FailClosedToolPlugin(default_project="example-project")
    tool = _Tool("execute_sql_readonly")

    # 1. a 403 in the RESULT is intercepted, not passed through
    result = await plugin.after_tool_callback(
        tool=tool,
        tool_args={"projectId": "example-project", "query": "SELECT 1"},
        tool_context=None,
        result={"error": PERMISSION_403},
    )
    check(result is not None and "ACCESS DENIED" in result["error"],
          "a denial in the tool result is replaced by the template")
    check("teams_bot_demo.orders" in result["error"],
          "the template names the refused table")

    # 2. a raised denial is intercepted too
    raised = await plugin.on_tool_error_callback(
        tool=tool, tool_args={}, tool_context=None,
        error=RuntimeError(CREDENTIAL_401),
    )
    check(raised is not None and "SIGN-IN REQUIRED" in raised["error"],
          "a raised denial is intercepted at the tool boundary")

    # 3. a missing credential fails closed with its own template
    missing = await plugin.on_tool_error_callback(
        tool=tool, tool_args={}, tool_context=None,
        error=MissingUserCredential("nothing bound"),
    )
    check(missing is not None
          and "will not fall back to a service account" in missing["error"],
          "MissingUserCredential renders the no-service-account template")

    # 4. an ordinary error is NOT swallowed (the model should see real errors)
    ordinary = await plugin.after_tool_callback(
        tool=tool, tool_args={}, tool_context=None,
        result={"error": "Syntax error: Unexpected keyword SELCT at [1:1]"},
    )
    check(ordinary is None, "a non-authorization error is left alone")

    # 5. THE 17% MODEL FLAKE: `query` omitted
    args = {"projectId": "example-project"}
    corrective = await plugin.before_tool_callback(
        tool=tool, tool_args=args, tool_context=None
    )
    check(corrective is not None and "query" in corrective["error"],
          "a call missing `query` is short-circuited with a message naming the field")
    check(corrective.get("retryable") is True, "the corrective response is marked retryable")

    # 6. camelCase repair + projectId default
    args = {"statement": "SELECT SESSION_USER()"}
    passthrough = await plugin.before_tool_callback(
        tool=tool, tool_args=args, tool_context=None
    )
    check(passthrough is None, "a repairable call proceeds to the tool")
    check(args == {"query": "SELECT SESSION_USER()", "projectId": "example-project"},
          f"snake_case/alias args repaired to camelCase and projectId defaulted: {args}")


async def main() -> int:
    test_classification()
    test_resource_naming()
    test_rendering_never_swallows_text()
    await test_plugin()
    print()
    if failures:
        print(f"FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print("ALL ERROR-BOUNDARY ASSERTIONS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
