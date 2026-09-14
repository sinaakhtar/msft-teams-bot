"""Mutation check: break the validator on purpose, confirm the suite catches it.

A test suite that passes is worthless evidence unless it also FAILS when the
thing it guards is broken. Each mutation below is applied to a real file, the
real suite is run, and the file is restored unconditionally.
"""
import pathlib
import subprocess
import sys

# Resolved relative to this file so the harness works from any cwd and on any
# machine. Run it as:  .venv/bin/python tests/mutation_check.py
ROOT = pathlib.Path(__file__).resolve().parents[1]
PY = ROOT / ".venv/bin/python"

MUTATIONS = [
    (
        "M1: accept alg:none (remove the unsecured-JWS guard)",
        "app/auth/inbound.py",
        '        if alg.lower() == "none":\n            # Explicit, loud, and before any I/O. The unsecured-JWS attack.\n            raise InboundAuthError("alg_none_rejected")\n',
        '        if False:\n            raise InboundAuthError("alg_none_rejected")\n',
    ),
    (
        "M2: stop verifying the audience",
        "app/auth/inbound.py",
        '                    "verify_aud": True,',
        '                    "verify_aud": False,',
    ),
    (
        "M3: stop verifying the signature",
        "app/auth/inbound.py",
        '                    "verify_signature": True,',
        '                    "verify_signature": False,',
    ),
    (
        "M4: stop verifying expiry",
        "app/auth/inbound.py",
        '                    "verify_exp": True,',
        '                    "verify_exp": False,',
    ),
    (
        "M5: downgrade serviceUrl mismatch to a warning (the SDK's behaviour)",
        "app/auth/inbound.py",
        '            raise InboundAuthError(\n                "service_url_mismatch",',
        '            logger.warning("service url mismatch")\n            return claim_url\n            raise InboundAuthError(\n                "service_url_mismatch",',
    ),
    (
        "M6: ADR 003 violation - fall back to the Teams MRI",
        "app/caller_identity.py",
        '    object_id = sender.get("aadObjectId")\n',
        '    object_id = sender.get("aadObjectId") or sender.get("id")\n',
    ),
]


def run_suite() -> tuple[int, str]:
    proc = subprocess.run(
        [str(PY), "-m", "pytest", "-q", "--no-header", "-p", "no:warnings"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    tail = [l for l in proc.stdout.strip().splitlines() if l.strip()]
    return proc.returncode, tail[-1] if tail else "(no output)"


def main() -> int:
    print("=== BASELINE (unmutated) ===")
    rc, summary = run_suite()
    print(f"exit={rc}  {summary}\n")
    if rc != 0:
        print("baseline is not green; aborting mutation run")
        return 1

    results = []
    for label, relpath, old, new in MUTATIONS:
        path = ROOT / relpath
        original = path.read_text()
        if old not in original:
            results.append((label, "SKIPPED", "anchor text not found"))
            print(f"--- {label}\n    SKIPPED: anchor not found\n")
            continue
        try:
            path.write_text(original.replace(old, new, 1))
            rc, summary = run_suite()
            verdict = "CAUGHT" if rc != 0 else "NOT CAUGHT"
            results.append((label, verdict, summary))
            print(f"--- {label}\n    {verdict}: exit={rc}  {summary}\n")
        finally:
            path.write_text(original)

    print("=== RESTORED; re-running baseline ===")
    rc, summary = run_suite()
    print(f"exit={rc}  {summary}\n")

    print("=== SUMMARY ===")
    for label, verdict, summary in results:
        print(f"{verdict:<11} {label}")
        print(f"            {summary}")
    missed = [r for r in results if r[1] == "NOT CAUGHT"]
    return 1 if missed or rc != 0 else 0


if __name__ == "__main__":
    sys.exit(main())
