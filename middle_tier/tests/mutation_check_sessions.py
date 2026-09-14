"""Mutation check for the Agent Runtime Session manager (app/sessions/*).

Companion to ``mutation_check.py``, which covers the inbound-auth and caller
identity paths. This one covers session lifecycle: LRO unwrapping, the ADR 003
identity key, and the 60-minute idle boundary.

WHY THIS EXISTS
---------------
``43 passed`` is not evidence that the tests guard anything. It is only
evidence that the tests agree with the code. The question that matters is
whether the suite FAILS when the behaviour is broken. Each mutation below
encodes a bug a reviewer could plausibly ship.

This found a real hole. MS1 -- deriving the session id from the LRO's
*operation* name instead of from ``response.name`` -- originally SURVIVED the
entire suite, because every LRO fixture embedded the same session id in both
places and so could not tell the two implementations apart.
``test_session_id_comes_from_response_name_when_the_two_disagree`` was added to
close it. Do not delete that test; it is the only thing standing between this
codebase and the single most likely bug in the whole client.

SAFETY
------
Mutations are applied to a THROWAWAY COPY in a temp directory, never to the
real tree. The copy is deleted on the way out. Run it from ``middle_tier/``:

    .venv/bin/python tests/mutation_check_sessions.py

Exit status is 0 only if every mutation was killed.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile

#: The real tree. Read from, copied, never written to.
SOURCE = pathlib.Path(__file__).resolve().parents[1]
#: Absolute, because the throwaway copy deliberately has no .venv of its own.
PY = sys.executable

#: (label, file relative to the tree, exact text to replace, replacement)
MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "MS1: LRO -- take the session id from the OPERATION name instead of "
        "response.name",
        "app/sessions/client.py",
        """                if isinstance(response, Mapping) and response.get("name"):
                    if polls:
                        logger.info(
                            "sessions.create LRO completed after %d poll(s)", polls
                        )
                    return Session.from_api(response)
""",
        """                if isinstance(response, Mapping) and response.get("name"):
                    op_name_ = str(operation.get("name") or "")
                    sess_name_ = op_name_.split("/operations/", 1)[0]
                    return Session(
                        name=sess_name_,
                        session_id=sess_name_.rsplit("/", 1)[-1],
                        user_id=str(response.get("userId") or ""),
                    )
""",
    ),
    (
        "MS2: LRO -- drop the guard that refuses an operation name as a session",
        "app/sessions/client.py",
        '        if "/operations/" in name or not _SESSION_NAME_RE.match(name):',
        "        if False:",
    ),
    (
        "MS3: ADR 003 -- stop validating the user key, so a Teams MRI sails "
        "through as an identity",
        "app/sessions/manager.py",
        "    if not _USER_KEY_RE.match(user_key):",
        "    if False:",
    ),
    (
        "MS4: idle expiry -- move the 60-minute boundary off by one (>= becomes >)",
        "app/sessions/manager.py",
        "                if idle < self._idle_timeout:",
        "                if idle <= self._idle_timeout:",
    ),
]

TARGET_TESTS = "tests/test_session_manager.py"


def run_suite(root: pathlib.Path) -> tuple[int, str]:
    proc = subprocess.run(
        [PY, "-m", "pytest", TARGET_TESTS, "-q"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def last_line(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else "(no output)"


def main() -> int:
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="session-mutation-"))
    root = tmp / "middle_tier"
    try:
        shutil.copytree(
            SOURCE,
            root,
            ignore=shutil.ignore_patterns(
                ".venv", ".pytest_cache", "__pycache__", "*.pyc"
            ),
        )
        print(f"throwaway copy: {root}")

        rc, out = run_suite(root)
        print("=" * 72)
        print(f"BASELINE: {last_line(out)}")
        print("=" * 72)
        if rc != 0:
            print("baseline is not green; aborting without mutating")
            return 1

        survivors: list[str] = []
        for label, relpath, old, new in MUTATIONS:
            path = root / relpath
            original = path.read_text()
            if old not in original:
                print(f"\n!! {label}")
                print(f"   PATTERN NOT FOUND in {relpath}; treating as a survivor")
                survivors.append(label)
                continue
            try:
                path.write_text(original.replace(old, new, 1))
                rc, out = run_suite(root)
                print(f"\n{label}")
                print(f"  file:   {relpath}")
                print(f"  result: {last_line(out)}")
                if rc == 0:
                    print("  VERDICT: SURVIVED -- the suite does NOT catch this.")
                    survivors.append(label)
                else:
                    failed = [
                        ln.split(" - ")[0]
                        for ln in out.splitlines()
                        if ln.startswith("FAILED")
                    ]
                    print(f"  VERDICT: KILLED by {len(failed)} test(s)")
                    for ln in failed[:8]:
                        print(f"    {ln}")
            finally:
                path.write_text(original)

        rc, out = run_suite(root)
        print("\n" + "=" * 72)
        print(f"AFTER RESTORE: {last_line(out)}")
        print(f"mutations: {len(MUTATIONS)}, survivors: {len(survivors)}")
        print("=" * 72)
        for label in survivors:
            print(f"SURVIVOR: {label}")
        return 1 if survivors or rc != 0 else 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
