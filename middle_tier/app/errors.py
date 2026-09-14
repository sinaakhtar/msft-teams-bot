"""ADR 004 user-facing templates: fail closed, say what happened, offer a path.

ADR 004 in full:

  * Identity failure -> an explicit message plus a sign-in card.
  * Downstream denial -> a templated message that NAMES the refused resource.
  * NEVER fall back to a service account.
  * NEVER hand a raw authorization error to the model to explain.

That last clause is the reason this module exists as data rather than as
prose generated downstream. If an authorization error reached the model, three
bad things follow: the model paraphrases a security boundary into something
reassuring and wrong, the raw error text (which can carry principals, resource
paths and sometimes tokens) is shipped to an inference endpoint, and the user
gets a different explanation every time for the same deterministic refusal.

So refusals are rendered here, from fixed templates, with no model in the loop.

Everything returned is a plain ``dict`` in Bot Framework Activity shape, ready
to be POSTed to the Bot Connector or returned from an invoke response. No SDK
types, so this module stays trivially testable and has no dependency on a
framework we might swap.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

ADAPTIVE_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.adaptive"
SIGNIN_CARD_CONTENT_TYPE = "application/vnd.microsoft.card.signin"
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
        activity["attachmentLayout"] = "list"
    return activity


# --------------------------------------------------------------------------
# Identity failure
# --------------------------------------------------------------------------


def signin_card(
    *,
    signin_url: str,
    title: str = "Sign in to continue",
    button_text: str = "Sign in",
) -> dict[str, Any]:
    """A Bot Framework sign-in card attachment.

    Uses the OAuth-style signin card rather than an Adaptive Card with a raw
    link, so Teams renders it through its own consent affordance instead of as
    an untrusted URL in message text.
    """
    return {
        "contentType": SIGNIN_CARD_CONTENT_TYPE,
        "content": {
            "text": title,
            "buttons": [
                {"type": "signin", "title": button_text, "value": signin_url}
            ],
        },
    }


def identity_failure(
    *,
    signin_url: str | None = None,
    reason_code: str = "identity_unavailable",
    support_contact: str | None = None,
) -> dict[str, Any]:
    """The ADR 004 identity-failure activity: message + sign-in card.

    Deliberately says *what* is missing and *what the user can do*, and nothing
    about internal error shape. `reason_code` is included as a short quoted
    token purely so a support ticket can be correlated with a log line; it is
    not an error message and carries no detail.

    :param signin_url: where to send the user to consent. If omitted (for
        example the sign-in flow itself is misconfigured) the card is dropped
        and the message alone is returned - never a broken button.
    """
    lines = [
        "**I could not confirm who you are, so I have not run anything.**",
        "",
        "This bot always acts as *you*, never as a shared service account, so a "
        "turn it cannot attribute to a signed-in identity is refused rather than "
        "served.",
    ]
    if signin_url:
        lines += ["", "Sign in below and send your message again."]
    else:
        lines += [
            "",
            "Sign-in is not available right now, which is a configuration problem "
            "on our side rather than anything you did.",
        ]
    if support_contact:
        lines += ["", f"If it keeps happening, contact {support_contact}."]
    lines += ["", f"_Reference: `{reason_code}`_"]

    attachments = [signin_card(signin_url=signin_url)] if signin_url else None
    return _message("\n".join(lines), attachments)


def missing_entra_object_id(
    *, signin_url: str | None = None, support_contact: str | None = None
) -> dict[str, Any]:
    """Specific identity failure: the activity had no `aadObjectId` (ADR 003).

    Split out from the generic case because the remedy is different. This is
    not "your token expired"; it is "Teams did not tell us your directory
    identity at all", which happens for guest/anonymous participants and for
    channels outside Teams. Telling those users to sign in again would send
    them round a loop that cannot terminate.
    """
    lines = [
        "**I could not confirm who you are, so I have not run anything.**",
        "",
        "Teams did not include your organisational directory ID with this "
        "message. That normally means you are joining as a guest or anonymous "
        "participant, or messaging from outside your organisation's tenant.",
        "",
        "Because every query runs under your own identity and permissions, "
        "there is no safe way for me to continue without it.",
        "",
        "Try messaging me in a one-to-one chat from your work account.",
    ]
    if support_contact:
        lines += ["", f"If you are on your work account and still see this, contact {support_contact}."]
    lines += ["", "_Reference: `missing_aad_object_id`_"]

    attachments = [signin_card(signin_url=signin_url)] if signin_url else None
    return _message("\n".join(lines), attachments)


# --------------------------------------------------------------------------
# Downstream denial
# --------------------------------------------------------------------------


def downstream_denial(
    *,
    resource: str,
    action: str | None = None,
    user_display: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """The ADR 004 denial activity. MUST name the refused resource.

    Naming the resource is the point. A bare "you don't have access" makes the
    user open a ticket that an admin cannot action; "you don't have access to
    `example-project.sales.orders`" makes it a thirty-second grant.

    This template is fixed text with the resource interpolated. No model, ever.

    :param resource: the thing that was refused. Required.
    :param action: optional verb/permission, e.g. ``bigquery.tables.getData``.
    :param request_id: correlation id for logs; shown so support can find it.
    """
    if not resource:
        raise ValueError(
            "downstream_denial requires a resource name; ADR 004 forbids an "
            "unattributed denial"
        )

    who = f"**{user_display}**" if user_display else "your account"
    what = f"`{action}` on `{resource}`" if action else f"`{resource}`"

    lines = [
        "**Access denied — I stopped here rather than working around it.**",
        "",
        f"The request needed {what}, and {who} is not permitted to use it.",
        "",
        "I did not retry under a service account. This bot only ever reads data "
        "with your own permissions, so a refusal for you is a refusal, full stop.",
        "",
        f"To fix it, ask whoever administers `{resource}` to grant you access, "
        "then send your message again.",
    ]
    if request_id:
        lines += ["", f"_Reference: `{request_id}`_"]

    return _message("\n".join(lines))


# --------------------------------------------------------------------------
# Other fixed templates
# --------------------------------------------------------------------------


def transient_failure(*, request_id: str | None = None) -> dict[str, Any]:
    """Backend was unavailable. Explicitly NOT a permission problem.

    Kept separate from :func:`downstream_denial` on purpose: telling a user
    they lack permission when the service was merely down sends them to an
    admin who will find nothing wrong and will not believe the next report.
    """
    lines = [
        "**Something on my side failed — this is not a permissions problem.**",
        "",
        "The agent backend did not respond. Your access is fine; please try "
        "again in a moment.",
    ]
    if request_id:
        lines += ["", f"_Reference: `{request_id}`_"]
    return _message("\n".join(lines))


def conversation_reset() -> dict[str, Any]:
    """Confirmation for the `/new` Conversation Reset command."""
    return _message(
        "**Started a new conversation.** Previous turns are no longer in "
        "context. Your access and identity are unchanged."
    )


def welcome(*, bot_name: str = "this assistant") -> dict[str, Any]:
    """`conversationUpdate` greeting. States the identity model up front."""
    return _message(
        "\n".join(
            [
                f"Hi — {bot_name} here.",
                "",
                "Ask me questions about your data in natural language. Everything "
                "I run executes **as you**, with your own permissions, so I can "
                "only ever show you what you could already query yourself.",
                "",
                "Send `/new` at any time to start a fresh conversation.",
            ]
        )
    )


def unsupported_activity(activity_type: str) -> dict[str, Any]:
    """Only used where a reply is genuinely expected; most unknown types are
    silently ignored by the router rather than answered."""
    return _message(
        f"I received a `{activity_type}` event that I do not handle. No action taken."
    )


__all__: Iterable[str] = (
    "ADAPTIVE_CARD_CONTENT_TYPE",
    "SIGNIN_CARD_CONTENT_TYPE",
    "conversation_reset",
    "downstream_denial",
    "identity_failure",
    "missing_entra_object_id",
    "signin_card",
    "transient_failure",
    "unsupported_activity",
    "welcome",
)
