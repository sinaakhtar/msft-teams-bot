"""The headline test: prove there is no service-account fallback anywhere.

ADR 002 puts the user's own identity on the data plane. ADR 004 records the
service-account fallback as **explicitly rejected**, and predicts that someone
who has not read ADR 002 will propose it as the obvious expedient fix the
first time an OBO exchange fails in a demo. This file is the tripwire.

One grep is weak evidence, so this runs in three layers:

  (a) STATIC. Walk the whole ``middle_tier/`` and ``agent/`` source and look
      for the danger surface: ambient credentials, key files, impersonation,
      the metadata server. Python files are scanned with comments and
      docstrings STRIPPED, so a docstring that says "there is no service
      account fallback here" does not fail the build while
      ``from google.oauth2 import service_account`` does. Anything found in
      real code must be in :data:`ALLOWLIST` *and* carry an inline
      ``sa-allow:`` justification comment. Anything else fails.

  (b) BEHAVIOURAL. When the identity broker raises, the turn is refused and
      NO credential of any kind comes back. A static scan cannot prove the
      absence of a fallback that is assembled at runtime; this can.

  (c) IDENTITY SUBSTITUTION. A turn with no ``from.aadObjectId`` is refused,
      and ``from.id`` (the Teams MRI) is never used in its place. Serving the
      turn under the MRI would be a different silent degradation with the same
      shape: a second, unfederated identity for a person who already has one.

HONESTY ABOUT COVERAGE
----------------------
The scan reports exactly which trees it walked. Trees that are not present in
this workspace are reported as NOT SCANNED rather than quietly counted as
clean - see :func:`test_scan_coverage_is_reported`, whose output is printed
with ``-s`` and recorded in NOTES.md.
"""

from __future__ import annotations

import ast
import io
import re
import sys
import tokenize
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Iterable, Iterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import errors  # noqa: E402
from app.errors import boundary, taxonomy  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
MIDDLE_TIER = Path(__file__).resolve().parents[1]

#: Trees that MUST exist and MUST be clean. The middle tier holds the identity
#: broker; the agent holds the tools that run under the user's credential.
REQUIRED_TREES = ("middle_tier", "agent")

#: Scanned and reported, but not fatal. Spikes are throwaway exploration and
#: are expected to contain service-account code - that is what a spike is for.
#: Terraform provisions service identities as infrastructure, which is a
#: different thing from a runtime credential fallback.
INFORMATIONAL_TREES = ("spikes", "layer3", "terraform", "bigquery", "entra")

SKIP_DIR_NAMES = {
    ".venv",
    "venv",
    "env",
    "__pycache__",
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".terraform",
    "node_modules",
    "site-packages",
    "dist",
    "build",
    "htmlcov",
    ".egg-info",
}

PY_SUFFIXES = {".py", ".pyi"}
OTHER_SCANNED_SUFFIXES = {".sh", ".bash", ".toml", ".cfg", ".ini", ".yaml", ".yml", ".json", ".tf", ".env"}
SCANNED_FILENAMES = {"Dockerfile", "Procfile", "entrypoint.sh"}

#: The danger surface. Each entry is (name, regex). These are the ways a
#: process acquires an identity that is NOT the signed-in Entra user.
DANGER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("google_auth_default", re.compile(r"google\.auth\.default")),
    ("bare_default_call", re.compile(r"\bauth\.default\s*\(|(?<![\w.])default\s*\(\s*\)")),
    ("app_default_credentials_env", re.compile(r"GOOGLE_APPLICATION_CREDENTIALS")),
    ("service_account_identifier", re.compile(r"service_account")),
    ("from_service_account", re.compile(r"from_service_account")),
    ("impersonated_credentials", re.compile(r"impersonated_credentials")),
    ("compute_engine_credentials", re.compile(r"compute_engine\.Credentials|compute_engine\b")),
    ("metadata_server_host", re.compile(r"metadata\.google\.internal")),
    ("metadata_server_ip", re.compile(r"169\.254\.169\.254")),
    ("gcloud_adc_file", re.compile(r"application_default_credentials\.json")),
)

#: Prose forms. Reported for information only: a comment or a user-facing
#: string saying "I did not fall back to a service account" is the design
#: being documented, not violated.
PROSE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("service_account_prose", re.compile(r"service[ -]account", re.IGNORECASE)),
)

#: Marker that must appear on, or within three lines above, an allowlisted hit.
JUSTIFICATION_MARKER = re.compile(r"sa-allow:\s*\S+", re.IGNORECASE)


@dataclass(frozen=True)
class AllowEntry:
    """One legitimate, NON user-scoped use of a service identity.

    Legitimate means: it authenticates the *process* to something that is not
    the user's data. Reading a bot password from Secret Manager at startup
    qualifies. Reaching BigQuery on a user's behalf never does.
    """

    path_glob: str
    pattern: str
    reason: str


#: Empty on purpose, today. Entries are expected over time (Secret Manager at
#: startup, the agent's own model calls) and each one narrows the claim this
#: file makes, so each one must be argued for in review.
ALLOWLIST: tuple[AllowEntry, ...] = ()

#: This file names every dangerous pattern by construction, so it excludes
#: itself. Nothing else is self-excluded.
SELF = Path(__file__).resolve()


def _rel(path: Path) -> str:
    """Repo-relative when possible; absolute otherwise (planted test trees)."""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


@dataclass(frozen=True)
class Hit:
    path: Path
    line_no: int
    pattern: str
    text: str

    def __str__(self) -> str:
        return f"{_rel(self.path)}:{self.line_no}: [{self.pattern}] {self.text.strip()[:110]}"


# ==========================================================================
# Scanning
# ==========================================================================


def _walk(tree: Path) -> Iterator[Path]:
    for path in sorted(tree.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIR_NAMES or part.endswith(".egg-info") for part in path.parts):
            continue
        if path.resolve() == SELF:
            continue
        if path.suffix in PY_SUFFIXES or path.suffix in OTHER_SCANNED_SUFFIXES or path.name in SCANNED_FILENAMES:
            yield path


def _strip_python_comments_and_docstrings(source: str) -> dict[int, str]:
    """Return ``{line_no: code_text}`` with comments and docstrings removed.

    String literals that are *not* docstrings are kept, because
    ``os.environ["GOOGLE_APPLICATION_CREDENTIALS"]`` is executable code that
    happens to be spelled as a string. Falls back to the raw source if the file
    does not parse - a syntax error must not silently disable the scan.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {i: line for i, line in enumerate(source.splitlines(), start=1)}

    docstring_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    start = body[0].lineno
                    end = body[0].end_lineno or start
                    docstring_lines.update(range(start, end + 1))

    lines = {i: line for i, line in enumerate(source.splitlines(), start=1) if i not in docstring_lines}

    # Remove comment tokens.
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                line_no = token.start[0]
                if line_no in lines:
                    lines[line_no] = lines[line_no].replace(token.string, "")
    except (tokenize.TokenError, IndentationError):
        pass

    return lines


def _strip_hash_comments(source: str) -> dict[int, str]:
    out: dict[int, str] = {}
    for i, line in enumerate(source.splitlines(), start=1):
        stripped = re.sub(r"(?<!:)#.*$", "", line)
        out[i] = stripped
    return out


def _code_lines(path: Path) -> dict[int, str]:
    source = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix in PY_SUFFIXES:
        return _strip_python_comments_and_docstrings(source)
    if path.suffix == ".json":
        return {i: line for i, line in enumerate(source.splitlines(), start=1)}
    return _strip_hash_comments(source)


_DECLARATION_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+(?P<name>\w+)")


def is_declaration_only(text: str, pattern: re.Pattern[str]) -> bool:
    """True when the only match is inside a ``def``/``class`` NAME.

    ``async def test_missing_access_token_fails_closed_no_service_account_fallback()``
    is a test asserting the absence of the thing. A name cannot acquire a
    credential; only the body below it can, and the body is scanned normally.
    This carve-out is narrow by construction: blank the declared name, and if
    the match disappears the line was a declaration and nothing else.

    Declaration-only hits are still counted and printed by the coverage
    report, so the carve-out is visible rather than silent.
    """
    match = _DECLARATION_RE.match(text)
    if not match:
        return False
    without_name = text.replace(match.group("name"), "", 1)
    return not pattern.search(without_name)


def scan_tree(tree: Path, patterns=DANGER_PATTERNS) -> tuple[list[Hit], list[Hit]]:
    """Return ``(hits, declaration_only_hits)`` for one tree."""
    hits: list[Hit] = []
    declarations: list[Hit] = []
    for path in _walk(tree):
        for line_no, text in _code_lines(path).items():
            if not text:
                continue
            for name, pattern in patterns:
                if not pattern.search(text):
                    continue
                hit = Hit(path=path, line_no=line_no, pattern=name, text=text)
                (declarations if is_declaration_only(text, pattern) else hits).append(hit)
    return hits, declarations


def scan_tree_raw(tree: Path, patterns) -> list[Hit]:
    """Same walk, but over the raw text including comments and docstrings."""
    hits: list[Hit] = []
    for path in _walk(tree):
        source = path.read_text(encoding="utf-8", errors="replace")
        for line_no, text in enumerate(source.splitlines(), start=1):
            for name, pattern in patterns:
                if pattern.search(text):
                    hits.append(Hit(path=path, line_no=line_no, pattern=name, text=text))
    return hits


def _is_allowlisted(hit: Hit, allowlist: tuple[AllowEntry, ...] = ALLOWLIST) -> AllowEntry | None:
    rel = _rel(hit.path)
    for entry in allowlist:
        if fnmatch(rel, entry.path_glob) and entry.pattern == hit.pattern:
            return entry
    return None


def _has_justification(hit: Hit) -> bool:
    """An allowlisted hit must say, in the code, why it is not a fallback."""
    lines = hit.path.read_text(encoding="utf-8", errors="replace").splitlines()
    window = lines[max(0, hit.line_no - 4) : hit.line_no]
    return any(JUSTIFICATION_MARKER.search(line) for line in window)


def present_trees() -> tuple[list[Path], list[str]]:
    present, missing = [], []
    for name in REQUIRED_TREES:
        tree = REPO_ROOT / name
        (present if tree.is_dir() else missing).append(tree if tree.is_dir() else name)
    return present, missing


# ==========================================================================
# (a) Static scan
# ==========================================================================


def test_required_trees_exist():
    """If a tree is missing, the scan below is not evidence about it."""
    _, missing = present_trees()
    assert not missing, (
        f"cannot scan {missing}: tree not present in this workspace. The "
        "fallback claim covers only the trees actually walked."
    )


def test_no_unallowlisted_service_identity_in_code():
    """The headline assertion. Real code, comments and docstrings stripped."""
    present, _ = present_trees()
    offenders: list[str] = []

    for tree in present:
        hits, _declarations = scan_tree(tree)
        for hit in hits:
            entry = _is_allowlisted(hit)
            if entry is None:
                offenders.append(f"UNLISTED  {hit}")
            elif not _has_justification(hit):
                offenders.append(f"NO REASON {hit}  (allowlisted for: {entry.reason})")

    assert not offenders, (
        "service-identity surface found in user-scoped code. ADR 004 records "
        "the service-account fallback as explicitly rejected; if one of these "
        "is a legitimate non-user-scoped use, add an ALLOWLIST entry AND an "
        "inline 'sa-allow:' comment saying why.\n  " + "\n  ".join(offenders)
    )


def test_error_layer_itself_imports_no_credential_library():
    """The tool boundary is exactly where the expedient fix gets proposed."""
    layer = MIDDLE_TIER / "app" / "errors"
    assert layer.is_dir()
    for path in sorted(layer.glob("*.py")):
        code = "\n".join(_code_lines(path).values())
        for forbidden in ("google.auth", "google.oauth2", "from_service_account", "impersonated"):
            assert forbidden not in code, f"{path.name} reaches for a credential library"


def test_scan_coverage_is_reported(capsys):
    """Print what was actually walked. Coverage claimed = coverage measured."""
    present, missing = present_trees()
    report = ["", "=== service-account fallback scan coverage ==="]

    total_files = 0
    declaration_only: list[Hit] = []
    for tree in present:
        files = list(_walk(tree))
        total_files += len(files)
        hits, declarations = scan_tree(tree)
        declaration_only += declarations
        report.append(
            f"SCANNED (fatal)      {tree.relative_to(REPO_ROOT)}/  files={len(files)}  "
            f"fatal-surface hits={len(hits)}  declaration-only={len(declarations)}"
        )

    for name in missing:
        report.append(f"NOT SCANNED (absent) {name}/")

    for name in INFORMATIONAL_TREES:
        tree = REPO_ROOT / name
        if not tree.is_dir():
            report.append(f"NOT SCANNED (absent) {name}/")
            continue
        info_hits, info_declarations = scan_tree(tree)
        report.append(
            f"SCANNED (report-only) {name}/  files={len(list(_walk(tree)))} "
            f"danger-surface hits={len(info_hits)} declaration-only={len(info_declarations)}"
        )
        for hit in info_hits[:12]:
            report.append(f"    {hit}")

    report.append(f"total files scanned in fatal mode: {total_files}")
    report.append(
        "declaration-only hits in required trees (names of tests asserting the "
        f"absence; not fatal): {len(declaration_only)}"
    )
    for hit in declaration_only:
        report.append(f"    {hit}")

    prose = [h for tree in present for h in scan_tree_raw(tree, PROSE_PATTERNS)]
    report.append(
        f"prose mentions of 'service account' in required trees (not fatal): {len(prose)}"
    )
    for hit in prose[:12]:
        report.append(f"    {hit}")

    with capsys.disabled():
        print("\n".join(report))

    assert total_files > 0


# ==========================================================================
# (b) Behavioural: broker raises -> refused, and no credential comes back
# ==========================================================================


class _BrokerExploded(Exception):
    """Stands in for any identity-broker failure: OBO refused, STS refused."""

    stage = "sts"
    code = "sts_rejected_assertion"


async def test_broker_failure_refuses_the_turn_and_returns_no_credential():
    async def broker():
        raise _BrokerExploded("STS rejected the subject token for workforce pool teams-bot-demo")

    token, outcome = await errors.acquire_credential_or_refuse(
        broker, signin_url="https://example.invalid/signin"
    )

    assert token is None
    assert outcome is not None
    assert outcome.refused is True
    assert outcome.credential is None
    # The user is told it is an identity problem and offered a sign-in card.
    assert "identity problem" in outcome.user_activity["text"]
    assert outcome.user_activity["attachments"][0]["contentType"].endswith(".signin")
    # The model is not invited to explain an identity failure.
    assert outcome.model_message is None


async def test_broker_returning_nothing_is_a_failure_not_a_downgrade():
    async def empty_broker():
        return ""

    token, outcome = await errors.acquire_credential_or_refuse(empty_broker)
    assert token is None
    assert outcome is not None and outcome.refused is True


async def test_no_outcome_field_can_carry_a_credential():
    """Structural: the refusal object has nowhere to put a fallback token."""

    async def broker():
        raise _BrokerExploded("nope")

    _, outcome = await errors.acquire_credential_or_refuse(broker)
    assert outcome is not None

    suspicious = re.compile(r"token|credential|secret|key|principal|service", re.IGNORECASE)
    for name in vars(outcome):
        if suspicious.search(name):
            assert getattr(outcome, name) in (None, "", {}, [])
    # `credential` exists only so its emptiness is assertable.
    assert outcome.credential is None


async def test_a_denied_tool_is_never_retried_under_another_identity():
    calls: list[str] = []

    class Denied(Exception):
        status_code = 403

    async def tool():
        calls.append("attempt")
        raise Denied(
            "Caller does not have required permission to use project example-project. "
            "Grant the caller the roles/serviceusage.serviceUsageConsumer role."
        )

    result, outcome = await boundary.guard_tool_call(tool, resource="project example-project")

    assert result is None
    assert outcome is not None
    assert calls == ["attempt"], "the boundary retried a refused call"


def test_taxonomy_has_no_type_that_can_express_a_fallback():
    names = [n for n in dir(taxonomy) if not n.startswith("_")]
    for name in names:
        assert "Fallback" not in name
        assert "ServiceAccount" not in name


# ==========================================================================
# (c) aadObjectId absent -> refused; from.id never substituted
# ==========================================================================

TENANT = "00000000-0000-0000-0000-000000000000"
OID = "4c2f6f0e-9e33-4d0e-9d1a-1f9a2b3c4d5e"
TEAMS_MRI = "29:1a2b3c4d5e6f7g8h9i0j-teams-mri-not-an-identity"


def _caller_extractor():
    """Locate the caller-identity extractor wherever it currently lives."""
    for module_name in ("app.caller_identity", "app.identity"):
        try:
            module = __import__(module_name, fromlist=["*"])
        except Exception:
            continue
        func = getattr(module, "caller_from_activity", None)
        exc = getattr(module, "MissingEntraObjectId", None)
        if func and exc:
            return module_name, func, exc
    return None, None, None


def _authenticated_caller():
    from app.auth.inbound import AuthenticatedCaller

    return AuthenticatedCaller(
        app_id="11111111-2222-3333-4444-555555555555",
        issuer="https://api.botframework.com",
        profile_name="bot_connector",
        service_url="https://smba.trafficmanager.net/emea/",
    )


def _activity(*, with_oid: bool) -> dict:
    sender = {"id": TEAMS_MRI, "name": "Sina"}
    if with_oid:
        sender["aadObjectId"] = OID
    return {
        "type": "message",
        "text": "how many orders last week",
        "channelId": "msteams",
        "conversation": {"id": "19:conversation@thread.tacv2"},
        "channelData": {"tenant": {"id": TENANT}},
        "from": sender,
        "recipient": {"id": "28:bot"},
        "serviceUrl": "https://smba.trafficmanager.net/emea/",
    }


def test_missing_aad_object_id_is_refused_end_to_end():
    module_name, extractor, missing_exc = _caller_extractor()
    if extractor is None:
        pytest.skip(
            "no caller-identity extractor found (app.caller_identity / app.identity); "
            "ADR 003 substitution is covered only at the template layer here"
        )

    with pytest.raises(missing_exc):
        extractor(_activity(with_oid=False), caller=_authenticated_caller())

    identity = extractor(_activity(with_oid=True), caller=_authenticated_caller())
    user_key = getattr(identity, "user_key")
    assert user_key == f"entra:{TENANT}:{OID}"
    assert TEAMS_MRI not in user_key, "the Teams MRI was substituted into the user key"
    print(f"\n[c] caller-identity extractor used: {module_name}")


def test_refusal_for_a_missing_oid_never_shows_or_uses_the_mri():
    err = taxonomy.MissingEntraObjectId(channel_id="msteams", conversation_id="19:conversation")
    body = errors.render(err, signin_url="https://example.invalid/signin")

    assert TEAMS_MRI not in body["text"]
    assert "missing_aad_object_id" in body["text"]
    # The exception itself cannot carry the MRI: there is no field for it, so
    # no later code can reach for one.
    assert not any("mri" in name.lower() or name == "teams_id" for name in vars(err))


def test_user_key_format_is_the_adr_003_shape():
    """entra:{tid}:{oid} - built only from directory identifiers."""
    key = f"entra:{TENANT}:{OID}"
    assert re.fullmatch(r"entra:[0-9a-f-]{36}:[0-9a-f-]{36}", key)
    assert not key.startswith("29:")


# ==========================================================================
# Meta: prove the scanner is not passing vacuously
# ==========================================================================
#
# A green scan over a clean tree is indistinguishable from a green scan by a
# broken scanner. These tests plant the exact violations the scan claims to
# catch, in a throwaway tree, and check they are caught.


def _plant(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_scanner_catches_a_planted_adc_fallback(tmp_path: Path):
    _plant(
        tmp_path,
        "app/broker.py",
        "import google.auth\n"
        "\n"
        "async def google_token(user_key):\n"
        "    try:\n"
        "        return await obo_then_sts(user_key)\n"
        "    except Exception:\n"
        "        creds, _ = google.auth.default()\n"
        "        return creds.token\n",
    )
    hits, declarations = scan_tree(tmp_path)
    patterns = {hit.pattern for hit in hits}
    assert "google_auth_default" in patterns
    assert declarations == []


@pytest.mark.parametrize(
    "line,expected",
    [
        ("from google.oauth2 import service_account", "service_account_identifier"),
        ("creds = service_account.Credentials.from_service_account_file(p)", "from_service_account"),
        ('os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/k.json"', "app_default_credentials_env"),
        ("from google.auth import impersonated_credentials", "impersonated_credentials"),
        ("from google.auth import compute_engine", "compute_engine_credentials"),
        ('r = get("http://metadata.google.internal/computeMetadata/v1/token")', "metadata_server_host"),
        ('r = get("http://169.254.169.254/computeMetadata/v1/token")', "metadata_server_ip"),
    ],
)
def test_scanner_catches_every_documented_danger_pattern(tmp_path: Path, line: str, expected: str):
    _plant(tmp_path, "app/sneaky.py", f"def f():\n    {line}\n")
    hits, _ = scan_tree(tmp_path)
    assert expected in {hit.pattern for hit in hits}, f"{expected} not detected in {line!r}"


def test_scanner_ignores_prose_but_not_code_on_the_same_topic(tmp_path: Path):
    _plant(
        tmp_path,
        "app/documented.py",
        '"""There is no service_account fallback here, and never will be."""\n'
        "\n"
        "# We deliberately do not call google.auth.default() anywhere.\n"
        "VALUE = 1\n",
    )
    hits, _ = scan_tree(tmp_path)
    assert hits == [], f"prose was treated as code: {[str(h) for h in hits]}"


def test_declaration_carveout_does_not_hide_the_body(tmp_path: Path):
    """A dangerous NAME is tolerated; a dangerous BODY under it is not."""
    _plant(
        tmp_path,
        "tests/test_thing.py",
        "def test_no_service_account_fallback():\n"
        "    from google.oauth2 import service_account\n"
        "    assert service_account\n",
    )
    hits, declarations = scan_tree(tmp_path)
    assert [h.line_no for h in declarations] == [1]
    assert 2 in [h.line_no for h in hits], "the import inside the body escaped the scan"


def test_allowlisted_hit_still_fails_without_an_inline_justification(tmp_path: Path):
    """The allowlist alone is not enough; the code must say why."""
    unjustified = _plant(
        tmp_path,
        "app/secrets.py",
        "def load():\n    creds, _ = google.auth.default()\n    return creds\n",
    )
    justified = _plant(
        tmp_path,
        "app/secrets_ok.py",
        "def load():\n"
        "    # sa-allow: process identity reading Secret Manager at startup;\n"
        "    # never used for a user-scoped call. ADR 002.\n"
        "    creds, _ = google.auth.default()\n"
        "    return creds\n",
    )
    allowlist = (
        AllowEntry(
            path_glob=(tmp_path / "app/*.py").as_posix(),
            pattern="google_auth_default",
            reason="startup secret read",
        ),
    )

    hits, _ = scan_tree(tmp_path)
    by_file = {hit.path: hit for hit in hits if hit.pattern == "google_auth_default"}

    assert _is_allowlisted(by_file[unjustified], allowlist) is not None
    assert _has_justification(by_file[unjustified]) is False

    assert _is_allowlisted(by_file[justified], allowlist) is not None
    assert _has_justification(by_file[justified]) is True


def test_unlisted_path_is_never_allowlisted(tmp_path: Path):
    planted = _plant(tmp_path, "app/other.py", "def f():\n    creds, _ = google.auth.default()\n")
    hits, _ = scan_tree(tmp_path)
    hit = next(h for h in hits if h.path == planted)
    allowlist = (
        AllowEntry(path_glob="somewhere/else/*.py", pattern="google_auth_default", reason="n/a"),
    )
    assert _is_allowlisted(hit, allowlist) is None
