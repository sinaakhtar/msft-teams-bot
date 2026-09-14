"""ADR 004 error layer: the two paths, the two templates, the two 403s.

Everything here runs offline. No network, no mocks of the code under test -
the real classifier is fed real error strings and the real templates are
rendered and inspected.

The tests are grouped by the claim they defend:

  * path 1 - identity failure yields an explicit message AND a sign-in card
  * path 2 - downstream denial NAMES the refused resource
  * the model's view is strictly narrower than the log's, always
  * the verified serviceUsageConsumer 403 classifies as a missing-role
    permission problem, and a credential-type 403 does not
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import errors  # noqa: E402
from app.errors import boundary, taxonomy, templates  # noqa: E402
from app.errors import classify as classify_mod  # noqa: E402  (the module, not the function)

VERIFIED_403 = classify_mod.SERVICE_USAGE_CONSUMER_403


# ==========================================================================
# Path 1: identity failure -> message + sign-in card
# ==========================================================================


def test_identity_failure_names_the_problem_and_carries_a_signin_card():
    body = errors.identity_failure(signin_url="https://example.invalid/signin")

    assert body["type"] == "message"
    assert "identity problem" in body["text"]
    # It must not be worded as a permissions problem: that sends the user to
    # an admin who will find nothing wrong.
    assert "not a permissions problem" in body["text"]

    card = body["attachments"][0]
    assert card["contentType"] == "application/vnd.microsoft.card.signin"
    button = card["content"]["buttons"][0]
    assert button["type"] == "signin"
    assert button["value"] == "https://example.invalid/signin"


def test_identity_failure_without_a_url_refuses_without_a_broken_button():
    body = errors.identity_failure(signin_url=None)
    assert "attachments" not in body
    assert "not available right now" in body["text"]


def test_identity_failure_never_leaks_upstream_detail_into_the_message():
    err = taxonomy.IdentityAcquisitionError(
        stage="sts", reason_code="sts_rejected_assertion", detail=VERIFIED_403
    )
    body = templates.render(err, signin_url="https://example.invalid/signin")
    assert "example-project" not in body["text"]
    assert "roles/" not in body["text"]
    # The reference code is a correlation token, not an explanation.
    assert "sts/sts_rejected_assertion" in body["text"]


def test_oauth_card_shape_matches_the_documented_teams_shape():
    card = templates.oauth_card(
        connection_name="google-workforce",
        signin_url="https://token.botframework.com/api/oauth/signin?signin=abc",
        token_exchange_uri="api://bot.example.com/11111111-1111-1111-1111-111111111111",
    )
    assert card["contentType"] == "application/vnd.microsoft.card.oauth"
    assert card["content"]["connectionName"] == "google-workforce"
    assert card["content"]["buttons"][0]["type"] == "signin"
    assert card["content"]["tokenExchangeResource"]["uri"].startswith("api://")


def test_missing_aad_object_id_refuses_and_offers_no_signin_loop():
    body = errors.missing_entra_object_id()
    assert "attachments" not in body  # a guest cannot sign in to a directory they are not in
    assert "missing_aad_object_id" in body["text"]
    assert "chat-only identifier" in body["text"]


def test_missing_aad_object_id_is_routed_to_its_own_template_by_render():
    body = templates.render(
        taxonomy.MissingEntraObjectId(channel_id="msteams", conversation_id="19:abc"),
        signin_url="https://example.invalid/signin",
    )
    # Even with a sign-in URL available, this variant suppresses the card.
    assert "attachments" not in body


# ==========================================================================
# Path 2: downstream denial -> template naming the resource
# ==========================================================================


def test_denial_names_the_refused_resource():
    body = errors.downstream_denial(
        resource="example-project.sales.orders", action="bigquery.tables.getData"
    )
    assert "example-project.sales.orders" in body["text"]
    assert "bigquery.tables.getData" in body["text"]
    # The template states the anti-pattern that was NOT taken.
    assert "service account" in body["text"]


def test_denial_without_a_resource_is_a_programming_error():
    with pytest.raises(ValueError):
        errors.downstream_denial(resource="")
    with pytest.raises(ValueError):
        taxonomy.DownstreamAuthorizationDenied(resource="   ")


def test_denial_quotes_a_named_role_only_when_the_upstream_text_named_one():
    with_role = errors.downstream_denial(
        resource="project example-project", named_role="roles/serviceusage.serviceUsageConsumer"
    )
    assert "roles/serviceusage.serviceUsageConsumer" in with_role["text"]

    without_role = errors.downstream_denial(resource="project example-project")
    assert "roles/" not in without_role["text"]


def test_transient_failure_is_not_worded_as_a_permission_problem():
    body = errors.transient_failure()
    assert "not a permissions problem" in body["text"]
    assert "denied" not in body["text"].lower()


# ==========================================================================
# The two 403s
# ==========================================================================


def test_verified_service_usage_consumer_403_is_a_missing_role_permission_error():
    """The load-bearing case. Verified live against project example-project."""
    err = classify_mod.classify(403, VERIFIED_403)

    assert isinstance(err, taxonomy.DownstreamAuthorizationDenied)
    assert err.confidently_classified is True
    assert err.resource == "project example-project"
    assert err.named_role == "roles/serviceusage.serviceUsageConsumer"
    # Not swallowed: the full text survives into the log fields.
    assert err.log_fields()["raw_message"] == VERIFIED_403


def test_bigquery_table_and_dataset_403s_also_classify_as_denials():
    table = classify_mod.classify(403, classify_mod.BIGQUERY_TABLE_DENIED_403)
    assert isinstance(table, taxonomy.DownstreamAuthorizationDenied)
    assert table.resource == "example-project:sales.orders"

    dataset = classify_mod.classify(403, classify_mod.BIGQUERY_DATASET_DENIED_403)
    assert isinstance(dataset, taxonomy.DownstreamAuthorizationDenied)
    assert "example-project:sales" in dataset.resource


def test_credential_type_403_is_an_identity_failure_not_a_missing_role():
    """The other 403. Same status code, completely different meaning.

    A 403 that rejects the credential or principal TYPE would be fatal to the
    design: no role grant fixes it. It must not be rendered as "ask for a
    role", and it must be loud.
    """
    err = classify_mod.classify(403, classify_mod.CREDENTIAL_TYPE_403_SYNTHETIC)

    assert isinstance(err, taxonomy.IdentityAcquisitionError)
    assert err.design_fatal is True
    assert err.reason_code == "credential_type_rejected"
    assert err.signin_will_help is False
    assert boundary._log_level_for(err) == logging.CRITICAL
    assert err.log_fields()["detail"] == classify_mod.CREDENTIAL_TYPE_403_SYNTHETIC


def test_missing_role_wins_over_an_incidental_mention_of_a_service_account():
    """Precedence: real role-grant messages sometimes mention a service account."""
    err = classify_mod.classify(403, classify_mod.MIXED_ROLE_AND_SERVICE_ACCOUNT_403_SYNTHETIC)
    assert isinstance(err, taxonomy.DownstreamAuthorizationDenied)
    assert err.resource == "project example-project"


def test_unrecognised_403_is_still_refused_but_flagged_for_a_human():
    err = classify_mod.classify(403, "Forbidden.", resource_hint="example-project.sales.orders")
    assert isinstance(err, taxonomy.DownstreamAuthorizationDenied)
    assert err.confidently_classified is False
    assert boundary._log_level_for(err) == logging.ERROR


def test_403_message_text_is_never_swallowed():
    for text in (
        VERIFIED_403,
        classify_mod.CREDENTIAL_TYPE_403_SYNTHETIC,
        "Forbidden.",
    ):
        err = classify_mod.classify(403, text)
        fields = err.log_fields()
        assert text in (fields.get("raw_message") or "") or text in (fields.get("detail") or "")


def test_404_is_not_rendered_as_a_denial():
    """We cannot tell 'absent' from 'hidden', and will not pretend to."""
    err = classify_mod.classify(404, "Not found: Table example-project:sales.orders")
    assert isinstance(err, taxonomy.UpstreamUnavailable)
    assert err.reason_code == "not_found"
    body = templates.render(err)
    assert "not a permissions problem" in body["text"]


@pytest.mark.parametrize("status", [500, 502, 503, 429])
def test_5xx_and_429_are_retryable_upstream_failures(status: int):
    err = classify_mod.classify(status, "backend exploded")
    assert isinstance(err, taxonomy.UpstreamUnavailable)
    assert err.retryable is True


def test_401_is_an_identity_failure_with_a_stage():
    err = classify_mod.classify(401, "AADSTS50076: consent required for the resource")
    assert isinstance(err, taxonomy.IdentityAcquisitionError)
    assert err.stage is taxonomy.IdentityStage.OBO


def test_resource_extraction_never_guesses():
    assert classify_mod.extract_resource("something went wrong") is None
    assert classify_mod.extract_named_role("something went wrong") is None


# ==========================================================================
# The asymmetry: what the model is told vs what is logged
# ==========================================================================


def test_model_is_told_only_that_access_was_denied_and_to_what():
    err = classify_mod.classify(403, VERIFIED_403)
    message = errors.model_facing(err)
    assert message == "Access denied to project example-project."


def test_raw_iam_error_never_reaches_the_model():
    err = classify_mod.classify(403, VERIFIED_403)
    message = errors.model_facing(err) or ""

    assert VERIFIED_403 not in message
    for fragment in (
        "Grant the caller",
        "roles/serviceusage.serviceUsageConsumer",
        "serviceusage.services.use",
        "console.developers.google.com",
        "Propagation of the new permission",
    ):
        assert fragment not in message
    # ... while the log keeps every one of them.
    assert err.log_fields()["raw_message"] == VERIFIED_403


def test_identity_failure_gives_the_model_nothing_at_all():
    err = taxonomy.IdentityAcquisitionError(stage="obo", detail=VERIFIED_403)
    assert errors.model_facing(err) is None


def test_leak_guard_fires_if_someone_widens_the_model_view(monkeypatch):
    """The regression this guard exists to catch: a 'helpful' append."""
    monkeypatch.setattr(
        boundary,
        "MODEL_DENIAL_TEMPLATE",
        "Access denied to {resource}. Upstream said: "
        "Grant the caller the roles/serviceusage.serviceUsageConsumer role, or a custom role",
    )
    err = classify_mod.classify(403, VERIFIED_403)
    with pytest.raises(boundary.ModelContextLeak):
        errors.model_facing(err)


def test_intercept_splits_the_three_audiences(caplog):
    class FakeForbidden(Exception):
        status_code = 403

    caplog.set_level(logging.WARNING)
    outcome = errors.intercept(
        FakeForbidden(VERIFIED_403),
        resource="example-project.sales.orders",
        action="bigquery.tables.getData",
        request_id="req-42",
    )

    # user: the template, naming the resource
    assert "project example-project" in outcome.user_activity["text"]
    # model: one sentence, no upstream text
    assert outcome.model_message == "Access denied to project example-project."
    assert "Grant the caller" not in (outcome.model_message or "")
    # log: everything
    assert outcome.log_fields["raw_message"] == VERIFIED_403
    assert outcome.log_fields["named_role"] == "roles/serviceusage.serviceUsageConsumer"
    # a denied tool does not by itself end the turn
    assert outcome.refused is False
    assert outcome.credential is None


async def test_guard_tool_call_returns_no_data_on_denial():
    class Denied(Exception):
        status_code = 403

    async def failing_tool():
        raise Denied(VERIFIED_403)

    result, outcome = await boundary.guard_tool_call(
        failing_tool, resource="example-project.sales.orders", action="bigquery.jobs.create"
    )
    assert result is None
    assert outcome is not None
    assert outcome.model_message == "Access denied to project example-project."


async def test_guard_tool_call_passes_success_straight_through():
    async def ok_tool():
        return {"rows": 3}

    result, outcome = await boundary.guard_tool_call(ok_tool, resource="example-project.sales.orders")
    assert result == {"rows": 3}
    assert outcome is None
