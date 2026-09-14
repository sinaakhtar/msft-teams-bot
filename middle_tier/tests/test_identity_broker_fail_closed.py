"""ADR 004 for the Identity Broker specifically: it raises, it never substitutes.

FILENAME AND SCOPE NOTE
-----------------------
This content was written for ``tests/test_no_service_account_fallback.py``. A
concurrent piece of work claimed that filename for a repo-wide scanner that
walks ``middle_tier`` and ``agent`` with an allowlist mechanism.

That scanner covers ``app/identity/`` too, so the *textual source scan* half of
the requirement lives there and is deliberately NOT duplicated here. I tried
duplicating it and it was actively harmful: a second copy of the forbidden-
pattern list is itself a file full of the forbidden patterns, and it tripped
the repo-wide scanner. Verified by mutation test that the remaining coverage is
real - injecting an ADC fallback into ``broker.py`` turns
``test_no_unallowlisted_service_identity_in_code`` red over there and seven
tests red here.

What the repo-wide scanner does NOT cover, because it is not about this
component, is the behavioural half: that :class:`ChainedIdentityBroker` RAISES
when the OBO hop or the STS hop fails, rather than returning any credential at
all. That, plus two structural checks that need no dangerous literals, is what
this file is for. See NOTES.md §6.

WHY THIS TEST EXISTS
--------------------
The fallback is the anti-pattern the design exists to prevent, and it is the
one somebody proposes in a stand-up when staging is red: "just fall back so the
demo works". ADR 002 splits the Bot Identity plane from the Tool Identity plane
precisely so no user-scoped query can run as a shared identity. A fallback does
not degrade gracefully - it *succeeds*, silently, as the wrong principal, and
every row-level policy written against the human evaluates against something
else. The user sees an answer. Nobody sees the breach.

If this file fails, delete the fallback. Do not loosen the test.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.identity.broker import ChainedIdentityBroker, parse_user_key  # noqa: E402
from app.identity.cache import PerUserTokenCache  # noqa: E402
from app.identity.errors import (  # noqa: E402
    IdentityAcquisitionError,
    OboAudienceMismatch,
    OboConsentRequired,
    PreconditionError,
    StsPermissionDenied,
    StsTransientError,
)
from app.identity.obo import OboResult  # noqa: E402
from app.identity.sts import StsResult  # noqa: E402

IDENTITY_PACKAGE = Path(__file__).resolve().parents[1] / "app" / "identity"

USER_KEY = "entra:00000000-0000-0000-0000-000000000000:33333333-3333-3333-3333-333333333333"
TEAMS_SSO_TOKEN = "eyJhbGciOiJSUzI1NiJ9.eyJhdWQiOiJib3QtYXBwIn0.sig"


def _identity_sources() -> list[Path]:
    files = sorted(p for p in IDENTITY_PACKAGE.glob("*.py"))
    assert files, f"no sources found under {IDENTITY_PACKAGE}"
    return files


# --------------------------------------------------------------------------
# Structural checks (AST only - no pattern literals, so the repo-wide
# scanner has nothing to trip over)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", _identity_sources(), ids=lambda p: p.name)
def test_identity_package_imports_no_credential_library(path: Path) -> None:
    """Nothing in this package may import a library that yields an ambient
    identity. Checked structurally rather than textually, so a docstring
    explaining the rule cannot be mistaken for a violation of it."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    banned_roots = {"google", "googleapiclient", "oauth2client"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            root = name.split(".")[0]
            assert root not in banned_roots, (
                f"{path.name}:{node.lineno} imports {name!r}. The identity path "
                f"must not be able to obtain an ambient Google credential."
            )


def test_no_bare_return_none_in_the_acquisition_path() -> None:
    """``google_access_token`` must return ``str`` - never an optional.

    A ``-> str | None`` signature is how a fallback gets introduced without
    anyone noticing: the caller adds ``or something_else`` at the call site and
    the type checker agrees.
    """
    tree = ast.parse((IDENTITY_PACKAGE / "broker.py").read_text(encoding="utf-8"))
    found = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in (
            "google_access_token",
            "get_google_access_token",
        ):
            found += 1
            assert isinstance(node.returns, ast.Name) and node.returns.id == "str", (
                f"{node.name} must be annotated `-> str`, got "
                f"{ast.unparse(node.returns) if node.returns else 'nothing'}"
            )
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return):
                    assert not (
                        isinstance(sub.value, ast.Constant) and sub.value.value is None
                    ), f"{node.name} returns None at line {sub.lineno}"
    # Three definitions are expected: the Protocol declaration plus the two
    # spellings on ChainedIdentityBroker. The floor is what matters - the point
    # is that every definition found is `-> str`.
    assert found >= 2, f"expected the accessor to be defined, found {found}"


# --------------------------------------------------------------------------
# Behavioural: the broker raises rather than substituting
# --------------------------------------------------------------------------


class FakeObo:
    """Stands in for the network call, nothing else."""

    def __init__(self, *, raises: Exception | None = None, token: str = "obo-token") -> None:
        self.raises = raises
        self.token = token
        self.calls = 0

    async def exchange(self, *, teams_sso_token: str, user_key: str | None = None) -> OboResult:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return OboResult(
            token=self.token,
            token_kind="access_token",
            expires_in=3600,
            aud="11111111-1111-1111-1111-111111111111",
            iss="https://login.microsoftonline.com/00000000-0000-0000-0000-000000000000/v2.0",
            oid="33333333-3333-3333-3333-333333333333",
            ver="2.0",
        )


class FakeSts:
    def __init__(self, *, raises: Exception | None = None, token: str = "google-token") -> None:
        self.raises = raises
        self.token = token
        self.calls = 0

    async def exchange(self, *, subject_token: str, user_key: str | None = None) -> StsResult:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return StsResult(access_token=self.token, expires_in=3598)


def _broker(obo: FakeObo, sts: FakeSts) -> ChainedIdentityBroker:
    return ChainedIdentityBroker(obo=obo, sts=sts, cache=PerUserTokenCache())


async def test_happy_path_returns_the_users_google_token() -> None:
    """Control case. Without this, "it raises" proves nothing."""
    obo, sts = FakeObo(), FakeSts()
    token = await _broker(obo, sts).google_access_token(
        user_key=USER_KEY, teams_sso_token=TEAMS_SSO_TOKEN
    )
    assert token == "google-token"
    assert (obo.calls, sts.calls) == (1, 1)


@pytest.mark.parametrize(
    "failure",
    [
        OboConsentRequired("AADSTS65001: user has not consented", http_status=400),
        OboAudienceMismatch(
            got_aud="11111111-1111-1111-1111-111111111111",
            expected_aud="11111111-1111-1111-1111-111111111111",
            got_iss="https://sts.windows.net/00000000-0000-0000-0000-000000000000/",
            expected_iss="https://login.microsoftonline.com/00000000-0000-0000-0000-000000000000/v2.0",
        ),
    ],
    ids=["consent_required", "audience_mismatch"],
)
async def test_obo_failure_raises_and_never_yields_a_credential(
    failure: IdentityAcquisitionError,
) -> None:
    obo = FakeObo(raises=failure)
    sts = FakeSts()
    broker = _broker(obo, sts)

    with pytest.raises(IdentityAcquisitionError) as caught:
        await broker.google_access_token(user_key=USER_KEY, teams_sso_token=TEAMS_SSO_TOKEN)

    assert caught.value is failure
    assert sts.calls == 0, "a failed OBO must not proceed to STS"
    assert len(broker.cache) == 0, "nothing may be cached on failure"


@pytest.mark.parametrize(
    "failure",
    [
        StsPermissionDenied(
            "Caller does not have required permission to use project", http_status=403
        ),
        StsTransientError("STS returned 503", http_status=503),
    ],
    ids=["permission_denied", "transient"],
)
async def test_sts_failure_raises_and_never_yields_a_credential(
    failure: IdentityAcquisitionError,
) -> None:
    broker = _broker(FakeObo(), FakeSts(raises=failure))

    with pytest.raises(IdentityAcquisitionError) as caught:
        await broker.google_access_token(user_key=USER_KEY, teams_sso_token=TEAMS_SSO_TOKEN)

    assert caught.value is failure
    assert len(broker.cache) == 0


async def test_every_failure_names_its_stage() -> None:
    """ADR 004 wants a specific refusal message, which needs a specific cause."""
    cases = {
        "obo": OboConsentRequired("x"),
        "sts": StsPermissionDenied("x"),
        "obo_audience": OboAudienceMismatch(got_aud="a", expected_aud="b"),
        "precondition": PreconditionError("x"),
    }
    for expected_stage, exc in cases.items():
        assert exc.stage.value == expected_stage
        assert exc.reason_code.startswith(f"identity.{expected_stage}.")
        assert exc.as_log_fields()["stage"] == expected_stage


async def test_missing_or_malformed_user_key_is_refused_before_any_network_call() -> None:
    """ADR 003: no aadObjectId, no turn. Not 'best effort with the MRI'."""
    obo, sts = FakeObo(), FakeSts()
    broker = _broker(obo, sts)

    for bad_key in (
        "",
        "29:1a2b3c4d-teams-mri",
        "entra:not-a-guid:33333333-3333-3333-3333-333333333333",
        "entra:00000000-0000-0000-0000-000000000000",
        "conversation:a:b",
    ):
        with pytest.raises(PreconditionError):
            await broker.google_access_token(user_key=bad_key, teams_sso_token=TEAMS_SSO_TOKEN)

    assert (obo.calls, sts.calls) == (0, 0)


async def test_missing_teams_token_is_refused_rather_than_substituted() -> None:
    obo, sts = FakeObo(), FakeSts()
    broker = _broker(obo, sts)

    with pytest.raises(PreconditionError):
        await broker.google_access_token(user_key=USER_KEY, teams_sso_token="")

    assert (obo.calls, sts.calls) == (0, 0)


async def test_a_failing_turn_does_not_poison_a_later_successful_one() -> None:
    """Fail closed must not mean fail permanently: once consent is granted the
    next turn has to work."""
    obo = FakeObo(raises=OboConsentRequired("AADSTS65001"))
    sts = FakeSts()
    broker = _broker(obo, sts)

    with pytest.raises(OboConsentRequired):
        await broker.google_access_token(user_key=USER_KEY, teams_sso_token=TEAMS_SSO_TOKEN)

    obo.raises = None  # the user consented
    assert (
        await broker.google_access_token(user_key=USER_KEY, teams_sso_token=TEAMS_SSO_TOKEN)
        == "google-token"
    )


async def test_parse_user_key_extracts_tenant_and_object_id() -> None:
    tid, oid = parse_user_key(USER_KEY)
    assert tid == "00000000-0000-0000-0000-000000000000"
    assert oid == "33333333-3333-3333-3333-333333333333"
