"""Activity routing.

The router runs AFTER :mod:`app.auth.inbound` has proven the activity came
from a trusted channel, and AFTER :mod:`app.caller_identity` has produced a
:class:`~app.caller_identity.CallerIdentity`. It decides *which* handler runs. It does
not decide anything about content: no prompting, no model calls, no rewriting
of user text beyond stripping a leading slash command.

Activity types handled:

  * ``message``            - normal user turn, or a ``/new`` reset command.
  * ``conversationUpdate`` - welcome when the bot itself is added.
  * ``invoke``             - Teams SSO token exchange lands here. STUBBED.
  * anything else          - ignored safely, logged at DEBUG, HTTP 200.

"Ignored safely" is the important one. Teams sends a long tail of event types
(``typing``, ``installationUpdate``, ``messageReaction``, ``endOfConversation``,
Teams-specific ``event`` activities) and new ones appear without notice. A
router that 500s on an unrecognised type turns a harmless new Teams feature
into an outage and, worse, makes Azure Bot Service retry it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from . import errors
from .auth.inbound import AuthenticatedCaller
from .caller_identity import (
    CallerIdentity,
    MissingEntraObjectId,
    caller_from_activity,
)
from .logging_utils import log_event
from .ports import (
    AgentRuntimeClient,
    AuthorizationDenied,
    IdentityBroker,
    IdentityUnavailable,
    PortError,
    SessionManager,
    StreamingRendererFactory,
    TransientBackendError,
)

logger = logging.getLogger(__name__)

#: The Conversation Reset command. Matched case-insensitively, after Teams
#: strips the `<at>Bot</at>` mention, allowing `/new`, `/New`, `/reset`.
RESET_COMMANDS = frozenset({"/new", "/reset", "/clear"})

#: Teams SSO token exchange invoke name.
INVOKE_SIGNIN_TOKEN_EXCHANGE = "signin/tokenExchange"
INVOKE_SIGNIN_VERIFY_STATE = "signin/verifyState"


@dataclass
class RouteResult:
    """What the HTTP layer should do with the turn.

    :param status: HTTP status for the POST /api/messages response.
    :param body: JSON body, or None for an empty 200/202.
    :param handled: whether a handler actually ran (False = safely ignored).
    """

    status: int = 200
    body: Mapping[str, Any] | None = None
    handled: bool = True


@dataclass
class Dependencies:
    """The seams the router calls into. All optional at this stage.

    Every one of these is owned by a different worker (see :mod:`app.ports`).
    Where an implementation is not yet wired in, the router degrades to a
    clearly-labelled no-op rather than pretending. It never fabricates a reply
    that looks like an agent answer.
    """

    identity_broker: IdentityBroker | None = None
    sessions: SessionManager | None = None
    runtime: AgentRuntimeClient | None = None
    #: A FACTORY, not a renderer. See ``app.ports.StreamingRendererFactory``:
    #: a renderer is single-turn state and this object is process-scoped.
    renderer: StreamingRendererFactory | None = None
    signin_url: str | None = None
    support_contact: str | None = None
    bot_name: str = "the data assistant"


def _text_of(activity: Mapping[str, Any]) -> str:
    """User text with the bot @mention removed.

    Teams prefixes channel messages with the mention, so a user typing
    ``@Bot /new`` produces ``"<at>Bot</at> /new"``. Strip mention entities by
    the offsets Teams gives us rather than by regex on the markup.
    """
    text = activity.get("text") or ""
    entities = activity.get("entities") or []
    mentions = [
        e.get("text")
        for e in entities
        if isinstance(e, Mapping)
        and e.get("type") == "mention"
        and isinstance(e.get("text"), str)
    ]
    for mention_markup in mentions:
        text = text.replace(mention_markup, "")
    return text.strip()


def is_reset_command(text: str) -> bool:
    return text.strip().lower() in RESET_COMMANDS


async def route_activity(
    activity: Mapping[str, Any],
    *,
    caller: AuthenticatedCaller,
    deps: Dependencies,
    expected_tenant_id: str | None = None,
) -> RouteResult:
    """Dispatch one validated activity.

    :param caller: proof of channel authenticity. Required.
    :param deps: the seams. Missing implementations degrade visibly.

    Never raises for an unrecognised activity. Does raise for a programming
    error, which the HTTP layer converts to a 500.
    """
    activity_type = activity.get("type")

    if activity_type == "conversationUpdate":
        return await _handle_conversation_update(activity, deps=deps)

    if activity_type == "invoke":
        return await _handle_invoke(activity, caller=caller, deps=deps)

    if activity_type == "message":
        return await _handle_message(
            activity, caller=caller, deps=deps, expected_tenant_id=expected_tenant_id
        )

    log_event(
        logger,
        logging.DEBUG,
        "ignoring unhandled activity type",
        activity_type=activity_type,
        channel_id=activity.get("channelId"),
    )
    return RouteResult(status=200, body=None, handled=False)


# --------------------------------------------------------------------------
# conversationUpdate
# --------------------------------------------------------------------------


async def _handle_conversation_update(
    activity: Mapping[str, Any], *, deps: Dependencies
) -> RouteResult:
    """Welcome, but only when the BOT is the account being added.

    ``membersAdded`` fires for every human joining a channel too. Greeting each
    of them individually is how a bot gets muted.
    """
    members_added = activity.get("membersAdded") or []
    recipient_id = (activity.get("recipient") or {}).get("id")

    bot_was_added = any(
        isinstance(m, Mapping) and m.get("id") == recipient_id for m in members_added
    )
    if not bot_was_added:
        return RouteResult(status=200, body=None, handled=False)

    log_event(
        logger,
        logging.INFO,
        "bot added to conversation; sending welcome",
        conversation_id=(activity.get("conversation") or {}).get("id"),
    )
    return RouteResult(status=200, body=errors.welcome(bot_name=deps.bot_name))


# --------------------------------------------------------------------------
# invoke
# --------------------------------------------------------------------------


async def _handle_invoke(
    activity: Mapping[str, Any],
    *,
    caller: AuthenticatedCaller,
    deps: Dependencies,
) -> RouteResult:
    """Teams `invoke` activities.

    TODO(OWNER: OBO / STS exchange component - the implementer of
    ``app.ports.IdentityBroker``): implement ``signin/tokenExchange``.

    This is the Teams SSO single-sign-on flow. Teams posts an ``invoke`` whose
    ``value.token`` is an Entra token for OUR bot's app registration, obtained
    silently without prompting the user. That token is the input to
    ``IdentityBroker.get_google_access_token``.

    Required behaviour when implemented, in order:

      1. Read ``value.token`` and ``value.id`` (the exchange id).
      2. Deduplicate on ``value.id``: Teams sends the SAME exchange to every
         active instance of the bot, and without dedup two instances race to
         redeem one assertion and one of them gets a replay error.
      3. Call ``IdentityBroker.get_google_access_token(user_key, token)``.
      4. On success return HTTP 200 with an empty ``InvokeResponse`` body.
      5. On failure return HTTP 412 with body
         ``{"id": <exchange id>, "connectionName": ..., "failureDetail": ...}``.
         412 specifically: it is the code Teams treats as "consent needed" and
         which makes it fall back to the visible sign-in card. Returning 500
         here makes Teams show a generic error and never retry.

    Until that lands, we return 501 with no body. Deliberately NOT 200: a 200
    tells Teams the exchange succeeded, and the user then waits for a reply
    that will never come. A visible failure is the honest answer.
    """
    invoke_name = activity.get("name")

    if invoke_name in {INVOKE_SIGNIN_TOKEN_EXCHANGE, INVOKE_SIGNIN_VERIFY_STATE}:
        log_event(
            logger,
            logging.WARNING,
            "Teams SSO invoke received but the IdentityBroker is not implemented; "
            "returning 501",
            invoke_name=invoke_name,
            broker_wired=deps.identity_broker is not None,
        )
        # 501 is honest. See the docstring for why not 200.
        return RouteResult(status=501, body=None)

    log_event(
        logger, logging.DEBUG, "ignoring unhandled invoke", invoke_name=invoke_name
    )
    return RouteResult(status=200, body=None, handled=False)


# --------------------------------------------------------------------------
# message
# --------------------------------------------------------------------------


async def _handle_message(
    activity: Mapping[str, Any],
    *,
    caller: AuthenticatedCaller,
    deps: Dependencies,
    expected_tenant_id: str | None,
) -> RouteResult:
    """A user turn.

    Identity is resolved FIRST, before anything else happens, because ADR 003
    says a turn we cannot attribute is refused. Resolving it late would mean
    doing work (session creation, runtime warm-up) on behalf of a caller we
    have not identified.
    """
    try:
        identity: CallerIdentity = caller_from_activity(
            activity, caller=caller, expected_tenant_id=expected_tenant_id
        )
    except MissingEntraObjectId as exc:
        # ADR 003 + ADR 004: refuse the turn. No fallback to `from.id`.
        log_event(
            logger,
            logging.WARNING,
            "refusing turn: activity carried no aadObjectId",
            channel_id=activity.get("channelId"),
            conversation_id=(activity.get("conversation") or {}).get("id"),
            detail=str(exc),
        )
        return RouteResult(
            status=200,
            body=errors.missing_entra_object_id(
                signin_url=deps.signin_url, support_contact=deps.support_contact
            ),
        )
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "refusing turn: identity could not be established",
            error_type=type(exc).__name__,
            detail=str(exc),
        )
        return RouteResult(
            status=200,
            body=errors.identity_failure(
                signin_url=deps.signin_url,
                reason_code="identity_unresolvable",
                support_contact=deps.support_contact,
            ),
        )

    text = _text_of(activity)

    if is_reset_command(text):
        return await _handle_reset(activity, identity=identity, deps=deps)

    return await _handle_agent_turn(activity, identity=identity, text=text, deps=deps)


async def _acquire_user_token(
    activity: Mapping[str, Any],
    *,
    identity: CallerIdentity,
    deps: Dependencies,
) -> tuple[str | None, RouteResult | None]:
    """Tool Identity (ADR 002). Returns ``(token, None)`` or ``(None, refusal)``.

    Extracted because both a normal turn and ``/new`` need it: the Agent
    Runtime sessions subresource is called AS THE USER, so resetting a
    conversation needs the same credential as asking a question. Doing it in
    one place also means there is exactly one function to audit for the rule
    that a failure here never falls back to ambient credentials.
    """
    assert deps.identity_broker is not None  # narrowed by the caller

    sso_token = _sso_token_from_activity(activity)
    if not sso_token:
        # Nothing to exchange. Prompt sign-in; the invoke flow will supply it.
        return None, RouteResult(
            status=200,
            body=errors.identity_failure(
                signin_url=deps.signin_url,
                reason_code="no_sso_token",
                support_contact=deps.support_contact,
            ),
        )

    try:
        token = await deps.identity_broker.get_google_access_token(
            identity.user_key, sso_token
        )
    except IdentityUnavailable:
        return None, RouteResult(
            status=200,
            body=errors.identity_failure(
                signin_url=deps.signin_url,
                reason_code="token_exchange_failed",
                support_contact=deps.support_contact,
            ),
        )
    except AuthorizationDenied as exc:
        return None, RouteResult(
            status=200,
            body=errors.downstream_denial(
                resource=exc.resource, user_display=identity.display_name
            ),
        )
    except TransientBackendError:
        return None, RouteResult(status=200, body=errors.transient_failure())

    return token, None


async def _handle_reset(
    activity: Mapping[str, Any], *, identity: CallerIdentity, deps: Dependencies
) -> RouteResult:
    """`/new` Conversation Reset. ADR 005: session lifecycle is ours."""
    if deps.sessions is None or deps.identity_broker is None:
        log_event(
            logger,
            logging.WARNING,
            "/new received but no SessionManager is wired; nothing was reset",
            user_key=identity.user_key,
        )
        return RouteResult(
            status=200,
            body=errors.transient_failure(request_id="session-manager-not-wired"),
        )

    user_token, refusal = await _acquire_user_token(
        activity, identity=identity, deps=deps
    )
    if refusal is not None:
        return refusal
    assert user_token is not None

    try:
        session = await deps.sessions.reset(
            identity.user_key,
            conversation_id=_conversation_id(activity),
            access_token=user_token,
        )
    except AuthorizationDenied as exc:
        return RouteResult(
            status=200,
            body=errors.downstream_denial(
                resource=exc.resource, user_display=identity.display_name
            ),
        )
    except TransientBackendError:
        log_event(
            logger,
            logging.ERROR,
            "session reset failed transiently",
            user_key=identity.user_key,
        )
        return RouteResult(status=200, body=errors.transient_failure())
    except PortError as exc:
        # Anything else the session layer refuses with -- an unsupported group
        # conversation, a malformed user key. Not retryable and not a
        # permissions problem, but a 500 here would make Azure Bot Service
        # retry a turn that will fail identically every time.
        log_event(
            logger,
            logging.ERROR,
            "session reset refused",
            user_key=identity.user_key,
            error_type=type(exc).__name__,
            detail=str(exc),
        )
        return RouteResult(
            status=200, body=errors.transient_failure(request_id="session-refused")
        )

    log_event(
        logger,
        logging.INFO,
        "conversation reset",
        user_key=identity.user_key,
        session=session.session_id,
    )
    return RouteResult(status=200, body=errors.conversation_reset())


async def _handle_agent_turn(
    activity: Mapping[str, Any],
    *,
    identity: CallerIdentity,
    text: str,
    deps: Dependencies,
) -> RouteResult:
    """Relay a user turn to the agent runtime.

    This is a relay, not a decision point. The only branching here is on
    failure taxonomy (ADR 004), never on content.
    """
    missing = [
        name
        for name, dep in (
            ("IdentityBroker", deps.identity_broker),
            ("SessionManager", deps.sessions),
            ("AgentRuntimeClient", deps.runtime),
        )
        if dep is None
    ]
    if missing:
        # Honest degradation: say nothing that looks like an agent answer.
        log_event(
            logger,
            logging.WARNING,
            "agent turn received but required components are not wired",
            missing=missing,
            user_key=identity.user_key,
        )
        return RouteResult(
            status=200,
            body=errors.transient_failure(request_id="components-not-wired"),
        )

    assert deps.identity_broker and deps.sessions and deps.runtime  # narrowing

    # --- Tool Identity (ADR 002). No token, no turn. -----------------------
    user_token, refusal = await _acquire_user_token(
        activity, identity=identity, deps=deps
    )
    if refusal is not None:
        return refusal
    assert user_token is not None

    # --- session (ADR 005) -------------------------------------------------
    try:
        session = await deps.sessions.get_or_create(
            identity.user_key,
            conversation_id=_conversation_id(activity),
            access_token=user_token,
        )
    except AuthorizationDenied as exc:
        return RouteResult(
            status=200,
            body=errors.downstream_denial(
                resource=exc.resource, user_display=identity.display_name
            ),
        )
    except TransientBackendError:
        return RouteResult(status=200, body=errors.transient_failure())
    except PortError as exc:
        log_event(
            logger,
            logging.ERROR,
            "session resolution refused",
            user_key=identity.user_key,
            error_type=type(exc).__name__,
            detail=str(exc),
        )
        return RouteResult(
            status=200, body=errors.transient_failure(request_id="session-refused")
        )

    # --- invoke and stream (ADR 001) ---------------------------------------
    conversation_ref = _conversation_reference(activity)
    stream_error: Exception | None = None

    # One renderer per turn, not one per process: `begin`/`push`/`finish`
    # describe a single Teams bubble with a single sequence counter, so a
    # shared instance would interleave concurrent users' answers.
    renderer = None
    if deps.renderer is not None:
        try:
            renderer = deps.renderer.for_turn(
                conversation_ref,
                request_id=activity.get("id"),
                user_display=identity.display_name,
            )
        except Exception as exc:
            # We cannot post back to this conversation at all -- most likely an
            # unrecognised serviceUrl. Say so through the response body, which
            # is the one channel that does not depend on the connector.
            log_event(
                logger,
                logging.ERROR,
                "cannot build a renderer for this conversation",
                user_key=identity.user_key,
                error_type=type(exc).__name__,
                detail=str(exc),
            )
            return RouteResult(
                status=200,
                body=errors.transient_failure(request_id="renderer-unavailable"),
            )

    if renderer is not None:
        await renderer.begin(conversation_ref)

    try:
        async for event in deps.runtime.stream_query(
            session=session,
            message=text,
            user_access_token=user_token,
            request_id=activity.get("id"),
        ):
            if renderer is not None:
                await renderer.push(event)
    except AuthorizationDenied as exc:
        stream_error = exc
        if renderer is not None:
            await renderer.finish(error=exc)
        return RouteResult(
            status=200,
            body=errors.downstream_denial(
                resource=exc.resource,
                user_display=identity.display_name,
                request_id=activity.get("id"),
            ),
        )
    except IdentityUnavailable as exc:
        stream_error = exc
        # The user token was rejected. Drop the cache so the next turn
        # re-exchanges rather than replaying a revoked credential.
        await deps.identity_broker.invalidate(identity.user_key)
        if renderer is not None:
            await renderer.finish(error=exc)
        return RouteResult(
            status=200,
            body=errors.identity_failure(
                signin_url=deps.signin_url,
                reason_code="token_rejected_downstream",
                support_contact=deps.support_contact,
            ),
        )
    except TransientBackendError as exc:
        stream_error = exc
        if renderer is not None:
            await renderer.finish(error=exc)
        return RouteResult(status=200, body=errors.transient_failure())
    except PortError as exc:
        # An unclassified seam failure. Still not a 500: the turn is lost
        # either way, and a non-2xx only adds an Azure retry of the same
        # doomed activity.
        stream_error = exc
        log_event(
            logger,
            logging.ERROR,
            "agent turn failed with an unclassified seam error",
            user_key=identity.user_key,
            error_type=type(exc).__name__,
            detail=str(exc),
        )
        if renderer is not None:
            await renderer.finish(error=exc)
        return RouteResult(status=200, body=errors.transient_failure())
    finally:
        if renderer is not None and stream_error is None:
            await renderer.finish()

    return RouteResult(status=200, body=None)


def _sso_token_from_activity(activity: Mapping[str, Any]) -> str | None:
    """Pull a Teams SSO token if the turn carries one.

    TODO(OWNER: OBO / STS exchange component): in the real flow the token
    arrives on the ``invoke`` activity, not on the ``message``, and is held in
    per-user state between the two. This helper exists so the seam is visible;
    it currently only reads an explicitly-attached value and returns None
    otherwise, which routes the user to the sign-in card. It does NOT invent a
    token and it does NOT fall back to ambient credentials.
    """
    value = activity.get("value")
    if isinstance(value, Mapping):
        token = value.get("token")
        if isinstance(token, str) and token:
            return token
    return None


def _conversation_id(activity: Mapping[str, Any]) -> str:
    """The Teams conversation id.

    ADR 005 scopes one Agent Runtime session per (user, conversation), so this
    is half the session mapping key. It is NOT an identity: `conversation.id`
    says where the conversation is happening, never who is asking. The `who`
    comes from `from.aadObjectId` and nowhere else (ADR 003).
    """
    conversation = activity.get("conversation")
    if isinstance(conversation, Mapping):
        value = conversation.get("id")
        if isinstance(value, str) and value:
            return value
    return ""


def _conversation_reference(activity: Mapping[str, Any]) -> dict[str, Any]:
    """Minimal reply address for the renderer."""
    return {
        "serviceUrl": activity.get("serviceUrl"),
        "channelId": activity.get("channelId"),
        "conversation": activity.get("conversation"),
        "recipient": activity.get("from"),
        "bot": activity.get("recipient"),
        "activityId": activity.get("id"),
    }


__all__ = [
    "Dependencies",
    "RESET_COMMANDS",
    "RouteResult",
    "is_reset_command",
    "route_activity",
]
