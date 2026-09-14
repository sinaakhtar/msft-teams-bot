"""Deploy the BigQuery agent to Vertex AI Agent Runtime (Reasoning Engine).

API NOTE (this surface moves; here is what was checked, and where)
================================================================================
Verified by reading the INSTALLED SDK, google-cloud-aiplatform 2.1.0, not from
memory or documentation:

  * `vertexai.agent_engines.create(agent_engine=..., requirements=...,
    display_name=..., description=..., extra_packages=..., env_vars=...,
    service_account=..., min_instances=..., max_instances=...)`
    -> delegates to `AgentEngine.create`. Project, location and staging bucket
    are NOT parameters; they come from `vertexai.init(...)`, and `create`
    raises ValueError if any of the three is unset.
  * The deployable object is `vertexai.agent_engines.AdkApp(agent=...,
    plugins=[...], app_name=..., enable_tracing=...)`.
  * `AdkApp` exposes `streaming_agent_run_with_events(request_json)`, which
    ADR 005 commits the middle tier to. It is a method on the template, so
    deploying an `AdkApp` at all is what exposes it; there is no separate
    opt-in. This script asserts the method is present before uploading, so a
    future SDK that drops or renames it fails here rather than in Teams.
  * `vertexai.agent_engines.list()` / `.delete(resource_name, force=...)` for
    rollback.

SAFETY
--------------------------------------------------------------------------------
`reasoningEngines/9000000000000000001` ("data_science_agent", us-central1) is
NOT ours. This script never updates or deletes an existing engine: it only
CREATEs, and `--rollback` refuses to touch any resource id on the protected
list. Rollback of our own engine is opt-in and prints the id it is about to
delete.

USAGE
    # dry run: builds the app locally and prints everything it would send
    python deploy.py --dry-run

    # real deploy
    export GOOGLE_CLOUD_PROJECT=example-project
    export GOOGLE_CLOUD_LOCATION=us-central1
    export STAGING_BUCKET=gs://example-project-agent-staging
    python deploy.py

    # what is deployed
    python deploy.py --list

    # roll back a deploy of OURS
    python deploy.py --rollback projects/example-project/locations/us-central1/reasoningEngines/<id>
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from typing import Iterable

# --------------------------------------------------------------------------
# Configuration. All overridable by environment so the script has no secrets
# and no environment-specific literals buried in code.
# --------------------------------------------------------------------------

def _require_env(name: str) -> str:
    """Read a required env var, or exit with a message naming it.

    No defaults. Guessing a project ID here means deploying an agent into
    somebody else's project, which is not a failure you want to discover
    after the fact. See .env.example at the repository root.
    """
    value = os.environ.get(name, "")
    if not value:
        sys.exit(
            f"{name} is not set. Export it, or source your .env:\n"
            f"    set -a && . ../.env && set +a"
        )
    return value


PROJECT = _require_env("GOOGLE_CLOUD_PROJECT")

#: us-central1, not global. Verified: `global` returns an empty engine list for
#: this project, so deploying there would produce an engine the middle tier
#: cannot find.
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")

STAGING_BUCKET = os.environ.get("STAGING_BUCKET", f"gs://{PROJECT}-agent-staging")

DISPLAY_NAME = os.environ.get("AGENT_DISPLAY_NAME", "teams-bot-bq-analyst")

DESCRIPTION = (
    "Queries BigQuery as the signed-in Microsoft user. Every tool call carries "
    "the user's Workforce Principal token, threaded per invocation; the runtime "
    "service identity is never used as a Tool Identity."
)

#: DO NOT DEPLOY OVER, UPDATE, OR DELETE THESE. Not ours.
#:
#: 9000000000000000001 is the one named in the brief. The other two were found
#: by listing us-central1 on 2026-09-07: all three carry displayName
#: "data_science_agent" and were created 2026-03-08/2026-03-10, months before
#: this project existed, so all three are somebody else's. Listing only one of
#: them would let `--rollback` delete the other two by id without complaint.
#: Engines this script must REFUSE to delete, as a comma-separated list of
#: bare numeric IDs in the PROTECTED_ENGINE_IDS env var.
#:
#: This exists because the project used to build this repo already hosted
#: unrelated reasoning engines belonging to someone else, and `--rollback`
#: takes a resource name that is one fat-finger away from one of them.
#:
#: The default is EMPTY, which means the guard is inert. That is the honest
#: default for a public repo -- we cannot know your engine IDs -- but it is
#: NOT a safe one. If your project hosts any engine you did not deploy from
#: here, list them. Find them with: python deploy.py --list
PROTECTED_ENGINE_IDS = frozenset(
    e.strip()
    for e in os.environ.get("PROTECTED_ENGINE_IDS", "").split(",")
    if e.strip()
)

HERE = pathlib.Path(__file__).resolve().parent
REQUIREMENTS_FILE = HERE / "requirements.txt"


def read_requirements() -> list[str]:
    """Requirement lines from requirements.txt, comments stripped.

    Read from the file rather than duplicated here so the deployed environment
    cannot drift from the one the tests ran against. `mcp<2` in particular is
    load-bearing: google-adk 2.8.0 imports `mcp.shared.session.ProgressFnT`,
    which MCP 2.x removed, so an unpinned resolve breaks the deploy at import.
    """
    lines = []
    for raw in REQUIREMENTS_FILE.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    if not any(spec.startswith("mcp") for spec in lines):
        raise RuntimeError(
            "requirements.txt has no `mcp` pin. google-adk 2.8.0 needs mcp<2; "
            "without the pin the deployed agent dies at import."
        )
    return lines


def build_app():
    """The deployable AdkApp: root agent + the two plugins.

    Both plugins are essential, not decoration:
      * UserCredentialPlugin binds the per-user credential for exactly one
        invocation. Without it, a deployed turn has no Tool Identity and every
        tool call fails closed.
      * FailClosedToolPlugin implements ADR 004 and repairs the malformed tool
        calls the model emits ~17% of the time.
    """
    from vertexai.agent_engines import AdkApp

    from bq_agent.agent import build_agent, build_plugins

    app = AdkApp(agent=build_agent(), plugins=build_plugins(), enable_tracing=True)

    # ADR 005 contract check, done locally before anything is uploaded.
    if not hasattr(app, "streaming_agent_run_with_events"):
        raise RuntimeError(
            "This AdkApp does not expose streaming_agent_run_with_events, which "
            "ADR 005 requires. Do not deploy; the middle tier cannot drive it."
        )
    return app


def _engine_id(resource_name: str) -> str:
    return resource_name.rstrip("/").rsplit("/", 1)[-1]


def do_deploy(*, dry_run: bool) -> int:
    requirements = read_requirements()
    env_vars = {
        key: os.environ[key]
        for key in (
            "BQ_MCP_URL",
            "BQ_AGENT_DATASET",
            "BQ_AGENT_MODEL",
            "BQ_AGENT_USER_PROJECT",
            "BQ_AGENT_AUTHORIZATION_ID",
        )
        if key in os.environ
    }

    plan = {
        "project": PROJECT,
        "location": LOCATION,
        "staging_bucket": STAGING_BUCKET,
        "display_name": DISPLAY_NAME,
        "requirements": requirements,
        "extra_packages": ["./bq_agent"],
        "env_vars": env_vars,
        "protected_engines_untouched": sorted(PROTECTED_ENGINE_IDS),
    }
    print("DEPLOYMENT PLAN")
    print(json.dumps(plan, indent=2))

    app = build_app()
    print("\nLocal build OK: AdkApp constructed and "
          "streaming_agent_run_with_events is present.")

    if dry_run:
        print("\n--dry-run: nothing was uploaded and no Google Cloud call was made.")
        return 0

    import vertexai
    from vertexai import agent_engines

    vertexai.init(project=PROJECT, location=LOCATION, staging_bucket=STAGING_BUCKET)

    remote = agent_engines.create(
        agent_engine=app,
        requirements=requirements,
        display_name=DISPLAY_NAME,
        description=DESCRIPTION,
        # The package is imported by the deployed process, so it must be shipped.
        extra_packages=["./bq_agent"],
        env_vars=env_vars or None,
    )
    print(f"\nCREATED: {remote.resource_name}")
    print("Record this id. The middle tier addresses it directly (ADR 001), and "
          "`deploy.py --rollback <resource_name>` removes it.")
    return 0


def do_list() -> int:
    import vertexai
    from vertexai import agent_engines

    vertexai.init(project=PROJECT, location=LOCATION)
    print(f"Agent Engines in {PROJECT}/{LOCATION}:")
    for engine in agent_engines.list():
        marker = " [PROTECTED - NOT OURS]" if _engine_id(engine.resource_name) in PROTECTED_ENGINE_IDS else ""
        print(f"  {engine.resource_name}{marker}")
    return 0


def do_rollback(resource_name: str, *, yes: bool) -> int:
    engine_id = _engine_id(resource_name)
    if engine_id in PROTECTED_ENGINE_IDS:
        print(
            f"REFUSING: {engine_id} is on the protected list. It is not ours "
            "and must not be modified or deleted.",
            file=sys.stderr,
        )
        return 2
    print(f"About to DELETE {resource_name}")
    if not yes:
        print("Re-run with --yes to actually delete it.")
        return 0

    import vertexai
    from vertexai import agent_engines

    vertexai.init(project=PROJECT, location=LOCATION)
    agent_engines.delete(resource_name, force=True)
    print(f"DELETED {resource_name}")
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="build locally and print the plan; no cloud calls")
    parser.add_argument("--list", action="store_true", help="list Agent Engines")
    parser.add_argument("--rollback", metavar="RESOURCE_NAME",
                        help="delete one of OUR engines")
    parser.add_argument("--yes", action="store_true", help="confirm --rollback")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.list:
        return do_list()
    if args.rollback:
        return do_rollback(args.rollback, yes=args.yes)
    return do_deploy(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
