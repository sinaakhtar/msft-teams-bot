"""The two ADR 004 templates, plus the fixed messages around them.

ADR 004 gives the failure surface exactly two user-facing shapes:

  (a) **Identity failure** -> an explicit message naming this as an identity
      problem, PLUS a sign-in card.
  (b) **Downstream denial** -> a templated message that NAMES the refused
      resource, and nothing else.

Everything here is fixed text with values interpolated. No model is in the
loop, ever. If an authorization error were paraphrased by a language model,
three bad things follow: the model turns a security boundary into something
reassuring and wrong, the raw error text (which carries principals, resource
paths and occasionally tokens) is shipped to an inference endpoint, and the
same deterministic refusal is explained differently every time.

TONE
----
Plain, specific, non-apologetic. ADR 004 accepts that these read as more
abrupt than model prose. That is the trade: a refusal the user can act on beats
a fluent one they cannot.

SHAPE
-----
Every function returns a plain ``dict`` in Bot Framework Activity shape, ready
to POST to the Bot Connector or return from an invoke response. No SDK types,
so this module stays trivially testable and survives a framework swap.

SIGN-IN CARD REFERENCES (checked 2026-09-07)
--------------------------------------------
* Bot Framework card schema, sign-in card (``contentType``
  ``application/vnd.microsoft.card.signin``; fields ``text`` and ``buttons``,
  each button a ``cardAction`` of type ``signin`` whose ``value`` is the
  sign-in URL):
  https://github.com/microsoft/botframework-sdk/blob/main/specs/botframework-activity/botframework-cards.md
* Teams card reference (which Bot Framework cards Teams renders, sign-in card
  included):
  https://learn.microsoft.com/en-us/microsoftteams/platform/task-modules-and-cards/cards/cards-reference
* Teams bot authentication with OAuthCard
  (``application/vnd.microsoft.card.oauth``, ``connectionName``,
  ``tokenExchangeResource`` for SSO):
  https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/authentication/add-authentication
* Teams SSO / token exchange overview:
  https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/authentication/bot-sso-overview
* Invoke response for a sign-in prompt from an Adaptive Card action
  (``statusCode`` 401, ``type`` ``application/vnd.microsoft.activity.loginRequest``):
  https://learn.microsoft.com/en-us/microsoftteams/platform/task-modules-and-cards/cards/universal-actions-for-adaptive-cards/authentication-flow-in-universal-action-for-adaptive-cards
"""

from __future__ import annotations

from typing import Any, Iterable

from .taxonomy import (
    DownstreamAuthorizationDenied,
    IdentityAcquisitionError,
    MiddleTierError,
    MissingEntraObjectId,
    UpstreamUnavailable,
)

ADAPTIVE_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"
SIGNIN_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.signin"
OAUTH_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.oauth"
LOGIN_REQUEST_INVOKE_TYPE = "application/vnd.microsoft.activity.loginRequest"
ADAPTIVE_CARD_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"
ADAPTIVE_CARD_VERSION = "1.5"


def _message(text: str, attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    activity: dict[str, Any] = {
        "type": "message",
        "textFormat": "markdown",
        "text": text,
    }
    if attachments:
        activity["attachments"] = attachments
    return activity


# ==========================================================================
# Cards
# ==========================================================================


def signin_card(
    *,
    signin_url: str,
    title: str = "Sign in to continue",
    button_text: str = "Sign in",
) -> dict[str, Any]:
    """A Bot Framework sign-in card attachment.

    Uses the sign-in card rather than an Adaptive Card with a raw hyperlink so
    Teams renders it through its own consent affordance instead of as an
    untrusted URL in message text.

    Shape per the Bot Framework card spec: ``contentType``
    ``application/vnd.microsoft.card.signin``, ``content.text``, and
    ``content.buttons[]`` where each button is a ``cardAction`` with
    ``type: "signin"`` and ``value`` set to the sign-in URL.
    """
    return {
        "contentType": SIGNIN_CARD_CONTENT_TYPE,
        "content": {
            "text": title,
            "buttons": [{"type": "signin", "title": button_text, "value": signin_url}],
        },
    }


def oauth_card(
    *,
    connection_name: str,
    text: str = "Sign in to continue",
    button_text: str = "Sign in",
    signin_url: str | None = None,
    token_exchange_uri: str | None = None,
    token_exchange_id: str | None = None,
    provider_id: str | None = None,
) -> dict[str, Any]:
    """An OAuthCard attachment, for deployments using the Bot Framework token service.

    Preferred over :func:`signin_card` when an Azure Bot OAuth connection
    exists, because Teams can then satisfy it silently via SSO token exchange
    instead of showing a button: the client sends ``signin/tokenExchange`` and
    the user never sees a prompt. That matters here because ADR 004 expects
    Google credential expiry (~3600s) mid-conversation to be a NORMAL event,
    not an exceptional one, and a visible sign-in button every hour would
    train users to click through consent prompts without reading them.

    ``tokenExchangeResource`` is only emitted when a URI is supplied; an empty
    one makes Teams fall back to the button, which is the safe degradation.

    :param connection_name: the OAuth connection configured on the Azure Bot
        resource. Must match exactly or the token service refuses the card.
    """
    content: dict[str, Any] = {
        "text": text,
        "connectionName": connection_name,
        "buttons": [{"type": "signin", "title": button_text, "value": signin_url}],
    }
    if token_exchange_uri:
        content["tokenExchangeResource"] = {
            "id": token_exchange_id or token_exchange_uri,
            "uri": token_exchange_uri,
            "providerId": provider_id,
        }
    return {"contentType": OAUTH_CARD_CONTENT_TYPE, "content": content}


# ==========================================================================
# Template (a): identity failure
# ==========================================================================


def identity_failure(
    *,
    signin_url: str | None = None,
    reason_code: str = "identity_unavailable",
    support_contact: str | None = None,
    stage: str | None = None,
    connection_name: str | None = None,
    token_exchange_uri: str | None = None,
) -> dict[str, Any]:
    """ADR 004 template (a): refuse the turn, name it as an identity problem, offer sign-in.

    Says *what* is missing and *what the user can do*, and nothing about
    internal error shape. ``reason_code`` (and ``stage``, when known) appear
    only as a short quoted token so a support ticket can be correlated with a
    log line. They are references, not explanations, and carry no upstream
    detail.

    What this function will never do is return a partial answer. There is no
    argument that produces a degraded-but-served turn, because the fallback
    that would produce one - running the query under a service account - is
    recorded in ADR 004 as explicitly rejected.

    :param signin_url: where to send the user to consent. If omitted (the
        sign-in flow itself is misconfigured, or the failure is one signing in
        cannot fix) the card is dropped and the message alone is returned.
        Never a broken button.
    :param connection_name: if set, an OAuthCard is emitted instead of a
        sign-in card so Teams can complete the exchange silently.
    """
    reference = reason_code if not stage else f"{stage}/{reason_code}"

    lines = [
        "**I could not confirm who you are, so I stopped.**",
        "",
        "This is an identity problem, not a permissions problem. I run every "
        "query as you, with your own access, so a turn I cannot attribute to a "
        "signed-in identity is refused rather than served under some other "
        "account.",
    ]

    attachments: list[dict[str, Any]] = []
    if signin_url and connection_name:
        attachments.append(
            oauth_card(
                connection_name=connection_name,
                signin_url=signin_url,
                token_exchange_uri=token_exchange_uri,
            )
        )
        lines += ["", "Sign in below and send the message again."]
    elif signin_url:
        attachments.append(signin_card(signin_url=signin_url))
        lines += ["", "Sign in below and send the message again."]
    else:
        lines += [
            "",
            "Sign-in is not available right now, so there is nothing for you to "
            "retry. Report the reference below.",
        ]

    if support_contact:
        lines += ["", f"If it keeps happening, contact {support_contact}."]
    lines += ["", f"Reference: `{reference}`"]

    return _message("\n".join(lines), attachments or None)


def missing_entra_object_id(
    *, signin_url: str | None = None, support_contact: str | None = None
) -> dict[str, Any]:
    """Identity failure, ADR 003 variant: the activity carried no ``aadObjectId``.

    Split from the generic case because the remedy is different. This is not
    "your token expired", it is "Teams did not tell us your directory identity
    at all", which happens for guest and anonymous participants and outside
    Teams. Telling those users to sign in again sends them round a loop that
    cannot terminate, so the sign-in card is suppressed by default.

    The turn is refused rather than served under ``from.id``, the Teams MRI.
    The MRI is always present, which is what makes it tempting; using it would
    create a second, unfederated identity for a person who already has an
    Entra object id, and nothing would ever reconcile the two.
    """
    lines = [
        "**I could not confirm who you are, so I stopped.**",
        "",
        "This conversation did not include your Microsoft Entra directory "
        "identity. That happens for guest and anonymous participants. I will "
        "not fall back to your chat-only identifier, because that would create "
        "a second identity for you that your data access does not follow.",
        "",
        "Try again from a Teams account in this organisation.",
    ]
    if support_contact:
        lines += ["", f"If you believe this is wrong, contact {support_contact}."]
    lines += ["", "Reference: `missing_aad_object_id`"]

    attachments = [signin_card(signin_url=signin_url)] if signin_url else None
    return _message("\n".join(lines), attachments)


# ==========================================================================
# Template (b): downstream authorization denial
# ==========================================================================


def downstream_denial(
    *,
    resource: str,
    action: str | None = None,
    named_role: str | None = None,
    user_display: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """ADR 004 template (b): the denial message. MUST name the refused resource.

    Naming the resource is the entire point. A bare "you don't have access"
    makes the user open a ticket an admin cannot action; "you don't have access
    to `example-project.sales.orders`" makes it a thirty-second grant.

    Fixed text with values interpolated. No model, ever. In exchange the user
    does not get a suggested workaround ("try a table you can see") - ADR 004
    accepts that cost explicitly, because the model cannot tell a missing role
    from a non-existent table from a typo, and a confident wrong explanation is
    more expensive than a missing suggestion.

    :param resource: what was refused. Required and non-empty.
    :param action: optional permission or verb, e.g. ``bigquery.tables.getData``.
    :param named_role: a role name lifted verbatim from the upstream error.
        Interpolated only if present; never inferred here.
    :param request_id: correlation id, shown so support can find the log line.
    :raises ValueError: empty resource. An unattributed denial is a
        programming error, not a message.
    """
    if not resource or not resource.strip():
        raise ValueError(
            "downstream_denial requires a resource name: ADR 004 template (b) "
            "exists to name the refused resource."
        )

    who = user_display or "You"
    lines = [
        "**Access denied. I stopped here rather than working around it.**",
        "",
        f"{who} do not have access to `{resource.strip()}`"
        + (f" (`{action}`)." if action else "."),
        "",
        "I did not retry this under a service account or any other identity. "
        "Everything I run executes as you, so this is the answer your own "
        "access gives.",
        "",
        "To fix it, ask whoever administers that resource to grant you access"
        + (f", naming `{named_role}`." if named_role else "."),
    ]
    if request_id:
        lines += ["", f"Reference: `{request_id}`"]

    return _message("\n".join(lines))


# ==========================================================================
# Neither path
# ==========================================================================


def transient_failure(*, request_id: str | None = None) -> dict[str, Any]:
    """The backend failed. Explicitly NOT worded as a permission problem.

    Kept separate from :func:`downstream_denial` on purpose: telling users they
    lack permission when the service was merely down sends them to an admin who
    finds nothing wrong and will not believe the next report.
    """
    lines = [
        "**Something on my side failed. This is not a permissions problem.**",
        "",
        "Send the message again. If it keeps failing, the backend is down "
        "rather than refusing you.",
    ]
    if request_id:
        lines += ["", f"Reference: `{request_id}`"]
    return _message("\n".join(lines))


def conversation_reset() -> dict[str, Any]:
    """Confirmation for the ``/new`` Conversation Reset command."""
    return _message(
        "**Started a new conversation.** Previous turns are no longer in "
        "context. Your access and identity are unchanged."
    )


def welcome(*, bot_name: str = "this assistant") -> dict[str, Any]:
    """``conversationUpdate`` greeting. States the identity model up front."""
    return _message(
        "\n".join(
            [
                f"Hi — {bot_name} here.",
                "",
                "Ask me questions about your data in natural language. "
                "Everything I run executes **as you**, with your own "
                "permissions, so I can only ever show you what you could "
                "already query yourself.",
                "",
                "Send `/new` at any time to start a fresh conversation.",
            ]
        )
    )


def unsupported_activity(activity_type: str) -> dict[str, Any]:
    """Only for activity types where a reply is genuinely expected."""
    return _message(
        f"I received a `{activity_type}` event that I do not handle. No action taken."
    )


# ==========================================================================
# Taxonomy -> template dispatch
# ==========================================================================


def render(
    error: MiddleTierError,
    *,
    signin_url: str | None = None,
    support_contact: str | None = None,
    connection_name: str | None = None,
    token_exchange_uri: str | None = None,
    user_display: str | None = None,
) -> dict[str, Any]:
    """Render any taxonomy error into its ADR 004 activity.

    Two paths, two templates. The dispatch is exhaustive over the taxonomy and
    falls through to the transient template - not the denial template - for
    anything unknown, because inventing a permission problem is worse than
    admitting a backend one.
    """
    if isinstance(error, MissingEntraObjectId):
        # signin_will_help is False for this case, so the card is suppressed
        # unless a caller explicitly overrides it.
        return missing_entra_object_id(signin_url=None, support_contact=support_contact)

    if isinstance(error, IdentityAcquisitionError):
        return identity_failure(
            signin_url=signin_url if error.signin_will_help else None,
            reason_code=error.reason_code,
            stage=error.stage.value,
            support_contact=support_contact,
            connection_name=connection_name if error.signin_will_help else None,
            token_exchange_uri=token_exchange_uri,
        )

    if isinstance(error, DownstreamAuthorizationDenied):
        fields = error.template_fields()
        return downstream_denial(
            resource=fields["resource"],
            action=fields["action"],
            named_role=fields["named_role"],
            request_id=fields["request_id"],
            user_display=user_display,
        )

    if isinstance(error, UpstreamUnavailable):
        return transient_failure(request_id=error.request_id)

    return transient_failure()


__all__: Iterable[str] = (
    "ADAPTIVE_CARD_CONTENT_TYPE",
    "ADAPTIVE_CARD_SCHEMA",
    "ADAPTIVE_CARD_VERSION",
    "LOGIN_REQUEST_INVOKE_TYPE",
    "OAUTH_CARD_CONTENT_TYPE",
    "SIGNIN_CARD_CONTENT_TYPE",
    "conversation_reset",
    "downstream_denial",
    "identity_failure",
    "missing_entra_object_id",
    "oauth_card",
    "render",
    "signin_card",
    "transient_failure",
    "unsupported_activity",
    "welcome",
)
