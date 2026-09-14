"""Identity extraction tests. ADR 003.

The single most important assertion in this file is
:func:`test_missing_aad_object_id_never_falls_back_to_teams_mri`. Everything
else is supporting detail.

Why that one matters: a fallback from `aadObjectId` to `from.id` does not
produce a slightly-degraded session. It produces a SECOND, PERMANENT,
UNFEDERATED identity for a person who already has one. Their history splits,
their token exchange fails in a way that looks like an Entra problem, and
nothing after the fact can map a Teams MRI back to an Entra object id to
reconcile the two. So the test does not merely check that an exception is
raised - it checks that the MRI value does not appear anywhere in the outcome.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.auth.inbound import AuthenticatedCaller  # noqa: E402
from app.caller_identity import (  # noqa: E402
    CallerIdentity,
    MalformedEntraIdentifier,
    MissingEntraObjectId,
    MissingTenantId,
    build_user_key,
    caller_from_activity,
)

TENANT = "00000000-0000-0000-0000-000000000000"
OID = "33333333-3333-3333-3333-333333333333"
MRI = "29:1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"
EXPECTED_KEY = f"entra:{TENANT}:{OID}"


@pytest.fixture
def caller() -> AuthenticatedCaller:
    """Stand-in for proof of channel authenticity.

    Constructing this by hand in a test is fine. Constructing it by hand in
    production code is the bug the type is designed to make visible in review.
    """
    return AuthenticatedCaller(
        app_id="11111111-2222-3333-4444-555555555555",
        issuer="https://api.botframework.com",
        profile_name="bot_connector",
        service_url="https://smba.trafficmanager.net/emea/",
        claims={},
    )


def _activity(**overrides) -> dict:
    activity = {
        "type": "message",
        "channelId": "msteams",
        "serviceUrl": "https://smba.trafficmanager.net/emea/",
        "text": "hello",
        "from": {"id": MRI, "name": "Sina Nek Akhtar", "aadObjectId": OID},
        "conversation": {"id": "a:1abc", "tenantId": TENANT},
        "channelData": {"tenant": {"id": TENANT}},
    }
    activity.update(overrides)
    return activity


# ==========================================================================
# The key format. Fixed by ADR 003 and by the worked example in the design.
# ==========================================================================


def test_user_key_is_entra_tid_oid(caller):
    identity = caller_from_activity(_activity(), caller=caller)

    assert identity.user_key == EXPECTED_KEY
    assert identity.user_key == (
        "entra:00000000-0000-0000-0000-000000000000:"
        "33333333-3333-3333-3333-333333333333"
    )
    assert identity.tenant_id == TENANT
    assert identity.object_id == OID


def test_user_key_has_exactly_three_colon_separated_parts(caller):
    identity = caller_from_activity(_activity(), caller=caller)
    parts = identity.user_key.split(":")
    assert len(parts) == 3
    assert parts[0] == "entra"


def test_user_key_is_lowercased_so_one_person_gets_one_key(caller):
    """Channels are inconsistent about GUID case. Two cases must not become
    two sessions for the same human."""
    upper = _activity()
    upper["from"] = {"id": MRI, "aadObjectId": OID.upper()}
    upper["channelData"] = {"tenant": {"id": TENANT.upper()}}

    assert caller_from_activity(upper, caller=caller).user_key == EXPECTED_KEY


def test_build_user_key_rejects_non_guid_input():
    with pytest.raises(MalformedEntraIdentifier):
        build_user_key(TENANT, "not-a-guid")
    with pytest.raises(MalformedEntraIdentifier):
        build_user_key("not-a-guid", OID)


def test_build_user_key_rejects_injected_colons():
    """A `:` in either half would forge a different composite key."""
    with pytest.raises(MalformedEntraIdentifier):
        build_user_key(TENANT, f"{OID}:extra")


# ==========================================================================
# THE CRITICAL TEST. Missing aadObjectId must fail closed.
# ==========================================================================


@pytest.mark.parametrize(
    "sender",
    [
        {"id": MRI, "name": "Guest User"},                 # absent
        {"id": MRI, "name": "Guest User", "aadObjectId": None},   # null
        {"id": MRI, "name": "Guest User", "aadObjectId": ""},     # empty
        {"id": MRI, "name": "Guest User", "aadObjectId": "   "},  # whitespace
    ],
    ids=["absent", "null", "empty", "whitespace"],
)
def test_missing_aad_object_id_fails_closed(caller, sender):
    activity = _activity(**{"from": sender})

    with pytest.raises(MissingEntraObjectId):
        caller_from_activity(activity, caller=caller)


def test_missing_aad_object_id_never_falls_back_to_teams_mri(caller):
    """THE test. No CallerIdentity is produced, and the MRI leaks nowhere.

    Asserting only `pytest.raises` would still pass if someone later "fixed"
    the exception by returning an identity built from `from.id`. So this
    inspects the raised exception and confirms the MRI is not in it, and
    separately confirms no identity object was produced.
    """
    activity = _activity(**{"from": {"id": MRI, "name": "Guest User"}})

    result: CallerIdentity | None = None
    with pytest.raises(MissingEntraObjectId) as exc_info:
        result = caller_from_activity(activity, caller=caller)

    assert result is None, "an identity was constructed despite a missing aadObjectId"

    # The MRI must not have been used, echoed, or smuggled into the error.
    assert MRI not in str(exc_info.value)
    assert "29:" not in str(exc_info.value)


def test_teams_mri_is_never_the_user_key_even_on_the_happy_path(caller):
    """On success the MRI is carried, but only as an opaque correlation
    handle. It must never be the session key or any part of it."""
    identity = caller_from_activity(_activity(), caller=caller)

    assert identity.teams_mri == MRI
    assert MRI not in identity.user_key
    assert "29:" not in identity.user_key
    assert identity.user_key.split(":")[2] == OID
    assert identity.object_id != MRI


def test_identity_module_never_reads_from_id_for_identity():
    """AST tripwire: `from.id` must not feed the object id or the user key.

    A future well-meaning "make guests work" change would most naturally be
    written as `sender.get("aadObjectId") or sender.get("id")`. This test
    exists so that line cannot land silently.
    """
    import ast
    import inspect

    from app import caller_identity as identity_module

    tree = ast.parse(inspect.getsource(identity_module))

    # Find any BoolOp (`or`) whose operands include a subscript/get of "id".
    offending: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            continue
        for value in node.values:
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "get"
                and value.args
                and isinstance(value.args[0], ast.Constant)
                and value.args[0].value == "id"
            ):
                offending.append(ast.dump(node)[:120])

    assert offending == [], f"possible MRI fallback introduced: {offending}"


# ==========================================================================
# Tenant resolution.
# ==========================================================================


def test_tenant_read_from_channel_data(caller):
    activity = _activity()
    activity["conversation"].pop("tenantId", None)
    assert caller_from_activity(activity, caller=caller).tenant_id == TENANT


def test_tenant_falls_back_to_conversation_tenant_id(caller):
    activity = _activity(channelData={})
    assert caller_from_activity(activity, caller=caller).tenant_id == TENANT


def test_tenant_falls_back_to_configured_tenant(caller):
    activity = _activity(channelData={}, conversation={"id": "a:1abc"})
    identity = caller_from_activity(
        activity, caller=caller, expected_tenant_id=TENANT
    )
    assert identity.tenant_id == TENANT


def test_no_tenant_anywhere_fails_closed(caller):
    activity = _activity(channelData={}, conversation={"id": "a:1abc"})
    with pytest.raises(MissingTenantId):
        caller_from_activity(activity, caller=caller)


def test_cross_tenant_activity_is_refused(caller):
    """A single-tenant bot receiving another tenant's activity is refused,
    not silently served under the configured tenant."""
    other = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    activity = _activity(channelData={"tenant": {"id": other}})

    with pytest.raises(MissingTenantId):
        caller_from_activity(activity, caller=caller, expected_tenant_id=TENANT)


def test_cross_tenant_check_is_case_insensitive(caller):
    activity = _activity(channelData={"tenant": {"id": TENANT.upper()}})
    identity = caller_from_activity(
        activity, caller=caller, expected_tenant_id=TENANT
    )
    assert identity.user_key == EXPECTED_KEY


# ==========================================================================
# Malformed inputs.
# ==========================================================================


def test_missing_from_object_fails_closed(caller):
    activity = _activity()
    activity.pop("from")
    with pytest.raises(MissingEntraObjectId):
        caller_from_activity(activity, caller=caller)


def test_non_guid_object_id_fails_closed(caller):
    """An injected value must not become part of a session key."""
    activity = _activity(**{"from": {"id": MRI, "aadObjectId": "../../admin"}})
    with pytest.raises(MalformedEntraIdentifier):
        caller_from_activity(activity, caller=caller)


def test_display_name_is_carried_for_rendering_only(caller):
    identity = caller_from_activity(_activity(), caller=caller)
    assert identity.display_name == "Sina Nek Akhtar"
    assert identity.display_name not in identity.user_key
