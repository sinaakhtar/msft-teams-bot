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
from .sso import SsoState

logger = logging.getLogger(__name__)

#: Delivers one activity to one conversation over the Bot Connector.
#: ``send(activity, conversation_ref) -> Any``. Injected so the router stays
#: testable without a network, and so there is exactly one place to audit for
#: "does a reply actually leave the process".
ReplySender = Callable[[Mapping[str, Any], Mapping[str, Any]], Awaitable[Any]]

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
    :param reply: an activity to DELIVER to the conversation over the Bot
        Connector. This is how a user actually sees anything.
    :param body: JSON body of the HTTP response itself. Only ``invoke``
        activities have one; see the warning below.
    :param handled: whether a handler actually ran (False = safely ignored).

    ``reply`` vs ``body``, and why they are not the same field
    ---------------------------------------------------------
    They were the same field until a live Teams test showed the bot replying
    to nobody: every template was rendered, returned with a 200, and silently
    discarded.

    The Bot Framework does not read the HTTP response body of a `message` or
    `conversationUpdate` POST. It reads the status code and nothing else. A
    reply is only seen by a human if the bot makes a SECOND, outbound request
    to ``{serviceUrl}/v3/conversations/{conversation.id}/activities``, which is
    what ``app.streaming.connector`` does.

    The one exception is ``invoke``, whose response body IS the protocol
    payload (an ``InvokeResponse``, or the 412 body Teams reads to decide
    whether to fall back to a visible sign-in card). That is what ``body`` is
    for, and it must NOT be posted to the connector.

    So: user-visible prose goes in ``reply``. Protocol payloads go in ``body``.
    Setting ``body`` on a message turn is the original bug and will be
    invisible to the user.
    """

    status: int = 200
    reply: Mapping[str, Any] | None = None
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
    #: Delivers a single non-streamed activity to a conversation over the Bot
    #: Connector: ``send(activity, conversation_ref)``. Every template reply
    #: in this module goes through here. Without it the router still returns
    #: the right thing and the user still sees nothing, which is precisely
    #: the failure this seam exists to make impossible to reintroduce.
    reply_sender: ReplySender | None = None
    #: Holds the Teams assertion between the ``invoke`` that carries it and
    #: the ``message`` that needs it. See :mod:`app.sso`.
    sso_state: SsoState | None = None
    #: Name of the Azure Bot OAuth connection. Empty disables silent SSO and
    #: falls back to the ADR 004 refusal, which is honest but never yields a
    #: token: Teams only starts the exchange when an OAuthCard names a
    #: connection.
    oauth_connection_name: str = ""
    #: The App ID URI Teams mints the assertion against. Must match the
    #: manifest's ``webApplicationInfo.resource`` and the connection's Token
    #: Exchange URL.
    token_exchange_uri: str = ""
    signin_url: str | None = None
    support_contact: str | None = None
    bot_name: str = "the data assistant"

    @property
    def sso_enabled(self) -> bool:
        """Whether the silent exchange can even be attempted."""
        return bool(
            self.oauth_connection_name and self.token_exchange_uri and self.sso_state
        )


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
        result = await _handle_conversation_update(activity, deps=deps)

    elif activity_type == "invoke":
        # Not delivered through the connector: an invoke's reply is its HTTP
        # response body, by protocol. See RouteResult. Anything the exchange
        # needs to SAY to the user (a replayed answer) it delivers itself.
        return await _handle_invoke(
            activity,
            caller=caller,
            deps=deps,
            expected_tenant_id=expected_tenant_id,
        )

    elif activity_type == "message":
        result = await _handle_message(
            activity, caller=caller, deps=deps, expected_tenant_id=expected_tenant_id
        )

    else:
        log_event(
            logger,
            logging.DEBUG,
            "ignoring unhandled activity type",
            activity_type=activity_type,
            channel_id=activity.get("channelId"),
        )
        return RouteResult(status=200, body=None, handled=False)

    await _deliver_reply(result, activity, deps=deps)
    return result


def _reply_unless_rendered(
    template: Mapping[str, Any], *, renderer: Any | None
) -> Mapping[str, Any] | None:
    """Drop the router's template when the renderer has already spoken.

    ``renderer.finish(error=...)`` terminates the Teams bubble with its own
    ADR 004 message, over the same connector this router would use. Returning
    the template as well posts a second activity, so the user reads the same
    refusal twice.

    This was latent for as long as replies were dropped on the floor: with one
    of the two messages going nowhere, nobody could see the duplicate. It is
    real the moment delivery works, so it is fixed in the same change.
    """
    return None if renderer is not None else template


async def _deliver_reply(
    result: RouteResult, activity: Mapping[str, Any], *, deps: Dependencies
) -> None:
    """Post ``result.reply`` to the conversation. The turn's only real output.

    Failure here is logged loudly and does NOT change the status code. The
    activity was handled; retrying it via a 5xx would re-run the whole turn
    (including a fresh agent call) to fix what is a transport problem on the
    reply leg. But it must never be silent: a swallowed error here is
    indistinguishable, from the user's seat, from the bot ignoring them.
    """
    if result.reply is None:
        return

    if deps.reply_sender is None:
        log_event(
            logger,
            logging.ERROR,
            "reply produced but no reply_sender is wired; the user will see nothing",
            activity_type=activity.get("type"),
            conversation_id=(activity.get("conversation") or {}).get("id"),
        )
        return

    try:
        await deps.reply_sender(result.reply, _conversation_reference(activity))
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "failed to deliver reply to Teams",
            activity_type=activity.get("type"),
            conversation_id=(activity.get("conversation") or {}).get("id"),
            error_type=type(exc).__name__,
            detail=str(exc),
        )


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
    return RouteResult(status=200, reply=errors.welcome(bot_name=deps.bot_name))


# --------------------------------------------------------------------------
# invoke
# --------------------------------------------------------------------------


async def _handle_invoke(
    activity: Mapping[str, Any],
    *,
    caller: AuthenticatedCaller,
    deps: Dependencies,
    expected_tenant_id: str | None = None,
) -> RouteResult:
    """Teams `invoke` activities, chiefly the SSO token exchange.

    Teams posts an ``invoke`` whose ``value.token`` is an Entra token for OUR
    bot's app registration, minted silently for the signed-in user against the
    App ID URI in :attr:`Dependencies.token_exchange_uri`. That token is the
    user assertion :class:`app.ports.IdentityBroker` needs.

    Status codes are protocol, not preference:

      * **200, empty body** on success. Teams treats anything else as a failed
        exchange.
      * **412** on failure, with ``{"id", "connectionName", "failureDetail"}``.
        412 is specifically the code Teams reads as "consent needed", which
        makes it fall back to the visible sign-in card. A 500 here makes Teams
        show a generic error and never retry, and a 200 on a failed exchange
        strands the user waiting for a reply that cannot come.

    Note this handler does NOT forward the assertion to the Bot Framework
    Token Service. That service exists to redeem a token on behalf of bots
    that want the connection's own scopes. This bot runs its own OBO chain
    (ADR 002) and needs the raw Teams assertion, so the connection exists only
    to make Teams perform the silent exchange in the first place.
    """
    invoke_name = activity.get("name")

    if invoke_name == INVOKE_SIGNIN_TOKEN_EXCHANGE:
        return await _handle_token_exchange(
            activity,
            caller=caller,
            deps=deps,
            expected_tenant_id=expected_tenant_id,
        )

    if invoke_name == INVOKE_SIGNIN_VERIFY_STATE:
        # The visible-card fallback completing. There is no assertion on this
        # activity, so there is nothing to redeem; the user's next message
        # will re-enter the silent path. 200 so Teams closes the sign-in UI.
        log_event(
            logger,
            logging.INFO,
            "signin/verifyState received; deferring to the next turn",
            conversation_id=(activity.get("conversation") or {}).get("id"),
        )
        return RouteResult(status=200, body=None)

    log_event(
        logger, logging.DEBUG, "ignoring unhandled invoke", invoke_name=invoke_name
    )
    return RouteResult(status=200, body=None, handled=False)


def _exchange_failure(
    *, exchange_id: str, connection_name: str, detail: str
) -> RouteResult:
    """The 412 body Teams reads to decide whether to show the sign-in card.

    ``failureDetail`` is operator-facing and reaches Microsoft's client, so it
    names the stage that failed and never the credential, the claims, or the
    upstream error text.
    """
    return RouteResult(
        status=412,
        body={
            "id": exchange_id,
            "connectionName": connection_name,
            "failureDetail": detail,
        },
    )


async def _handle_token_exchange(
    activity: Mapping[str, Any],
    *,
    caller: AuthenticatedCaller,
    deps: Dependencies,
    expected_tenant_id: str | None,
) -> RouteResult:
    """Redeem one ``signin/tokenExchange``, then replay what the user asked."""
    value = activity.get("value")
    value = value if isinstance(value, Mapping) else {}
    exchange_id = str(value.get("id") or "")
    connection_name = str(value.get("connectionName") or deps.oauth_connection_name)
    assertion = value.get("token")

    if not isinstance(assertion, str) or not assertion:
        log_event(
            logger,
            logging.WARNING,
            "token exchange carried no assertion",
            exchange_id=exchange_id,
        )
        return _exchange_failure(
            exchange_id=exchange_id,
            connection_name=connection_name,
            detail="no token on the exchange request",
        )

    if deps.identity_broker is None or deps.sso_state is None:
        log_event(
            logger,
            logging.ERROR,
            "token exchange received but SSO is not wired",
            broker_wired=deps.identity_broker is not None,
            state_wired=deps.sso_state is not None,
        )
        return _exchange_failure(
            exchange_id=exchange_id,
            connection_name=connection_name,
            detail="the bot is not configured for single sign-on",
        )

    # ADR 003: attribute the exchange before redeeming it. An assertion we
    # cannot key to a user is an assertion we cannot safely store.
    try:
        identity = caller_from_activity(
            activity, caller=caller, expected_tenant_id=expected_tenant_id
        )
    except Exception as exc:
        log_event(
            logger,
            logging.WARNING,
            "refusing token exchange: identity could not be established",
            error_type=type(exc).__name__,
            detail=str(exc),
        )
        return _exchange_failure(
            exchange_id=exchange_id,
            connection_name=connection_name,
            detail="the signed-in identity could not be established",
        )

    # Teams fans the same exchange out to every active client and instance.
    # An assertion is single-use, so the loser of that race would otherwise
    # report a replay error that reads exactly like a broken configuration.
    if not deps.sso_state.claim_exchange(exchange_id):
        log_event(
            logger,
            logging.INFO,
            "duplicate token exchange ignored",
            exchange_id=exchange_id,
            user_key=identity.user_key,
        )
        # 200, not 412: the exchange IS being handled, just not by this call.
        return RouteResult(status=200, body=None)

    try:
        await deps.identity_broker.get_google_access_token(
            identity.user_key, assertion
        )
    except AuthorizationDenied as exc:
        log_event(
            logger,
            logging.WARNING,
            "token exchange refused downstream",
            user_key=identity.user_key,
            resource=exc.resource,
        )
        return _exchange_failure(
            exchange_id=exchange_id,
            connection_name=connection_name,
            detail="the signed-in user is not authorized for this resource",
        )
    except IdentityUnavailable as exc:
        # The usual cause is missing consent, which is exactly what 412 tells
        # Teams to resolve by showing the card.
        log_event(
            logger,
            logging.WARNING,
            "token exchange failed; asking Teams for visible consent",
            user_key=identity.user_key,
            error_type=type(exc).__name__,
            detail=str(exc),
        )
        return _exchange_failure(
            exchange_id=exchange_id,
            connection_name=connection_name,
            detail="consent is required before this app can act as you",
        )
    except (TransientBackendError, PortError) as exc:
        log_event(
            logger,
            logging.ERROR,
            "token exchange failed transiently",
            user_key=identity.user_key,
            error_type=type(exc).__name__,
            detail=str(exc),
        )
        return _exchange_failure(
            exchange_id=exchange_id,
            connection_name=connection_name,
            detail="the identity service is temporarily unavailable",
        )

    # The assertion is good. Hold it so the message turn can use it without a
    # second round trip through Teams.
    deps.sso_state.remember_assertion(identity.user_key, assertion)
    log_event(
        logger,
        logging.INFO,
        "token exchange succeeded",
        user_key=identity.user_key,
        exchange_id=exchange_id,
    )

    await _replay_parked_turn(
        caller=caller, deps=deps, identity=identity, expected_tenant_id=expected_tenant_id
    )

    # 200 with an empty InvokeResponse body. Teams requires the empty body.
    return RouteResult(status=200, body=None)


async def _replay_parked_turn(
    *,
    caller: AuthenticatedCaller,
    deps: Dependencies,
    identity: CallerIdentity,
    expected_tenant_id: str | None,
) -> None:
    """Answer the question the user asked before they were signed in.

    Without this the first turn of every session is silently eaten: the user
    types a question, Teams performs a sign-in they never see, and nothing
    answers. They have no way to tell that from the bot ignoring them.

    Runs inside the invoke request on purpose. The alternative, a background
    task, is worse here: Cloud Run throttles CPU outside a request, so a
    detached turn can stall indefinitely with nothing to surface it. The cost
    is that this request now carries a full agent turn, so it is subject to
    the same reply-window problem as any other turn (see runbook 11).

    Never raises. A failure to replay must not turn a SUCCESSFUL exchange into
    a 412, which would send Teams back round the sign-in loop having already
    redeemed the assertion.
    """
    if deps.sso_state is None:
        return

    parked = deps.sso_state.take_parked_turn(identity.user_key)
    if parked is None:
        return

    log_event(
        logger,
        logging.INFO,
        "replaying the turn parked before sign-in",
        user_key=identity.user_key,
    )

    try:
        result = await _handle_agent_turn(
            parked,
            identity=identity,
            text=_text_of(parked),
            deps=deps,
        )
        # Delivered against the PARKED activity: its conversation is where the
        # user is waiting, and the invoke may not carry the same reference.
        await _deliver_reply(result, parked, deps=deps)
    except Exception as exc:
        log_event(
            logger,
            logging.ERROR,
            "failed to replay the parked turn after sign-in",
            user_key=identity.user_key,
            error_type=type(exc).__name__,
            detail=str(exc),
        )


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
            reply=errors.missing_entra_object_id(
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
            reply=errors.identity_failure(
                signin_url=deps.signin_url,
                reason_code="identity_unresolvable",
                support_contact=deps.support_contact,
            ),
        )

    text = _text_of(activity)

    if is_reset_command(text):
        return await _handle_reset(activity, identity=identity, deps=deps)

    return await _handle_agent_turn(activity, identity=identity, text=text, deps=deps)


def _prompt_for_sign_in(
    activity: Mapping[str, Any],
    *,
    identity: CallerIdentity,
    deps: Dependencies,
) -> RouteResult:
    """No assertion for this user yet. Start the silent exchange.

    Parks the turn first. Teams does not resend the user's message after a
    sign-in, so whatever they typed only survives if we keep it: see
    :meth:`app.sso.SsoState.park_turn`.

    When SSO is not configured this degrades to the ADR 004 refusal, which is
    honest but terminal, because without an OAuthCard naming a connection
    Teams never starts an exchange and no token will ever arrive.
    """
    if not deps.sso_enabled:
        log_event(
            logger,
            logging.WARNING,
            "no user assertion and SSO is not configured; refusing the turn",
            user_key=identity.user_key,
            connection_configured=bool(deps.oauth_connection_name),
            uri_configured=bool(deps.token_exchange_uri),
            state_wired=deps.sso_state is not None,
        )
        return RouteResult(
            status=200,
            reply=errors.identity_failure(
                signin_url=deps.signin_url,
                reason_code="no_sso_token",
                support_contact=deps.support_contact,
            ),
        )

    assert deps.sso_state is not None  # narrowed by sso_enabled
    deps.sso_state.park_turn(identity.user_key, activity)

    log_event(
        logger,
        logging.INFO,
        "no user assertion yet; parking the turn and prompting for the exchange",
        user_key=identity.user_key,
        connection_name=deps.oauth_connection_name,
    )
    return RouteResult(
        status=200,
        reply=errors.sso_prompt(
            connection_name=deps.oauth_connection_name,
            token_exchange_uri=deps.token_exchange_uri,
        ),
    )


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

    sso_token = _sso_token_from_activity(
        activity, user_key=identity.user_key, state=deps.sso_state
    )
    if not sso_token:
        return None, _prompt_for_sign_in(activity, identity=identity, deps=deps)

    try:
        token = await deps.identity_broker.get_google_access_token(
            identity.user_key, sso_token
        )
    except IdentityUnavailable:
        # The stored assertion was rejected. Drop it, or every subsequent turn
        # replays the same dead token and the user is stuck in a refusal loop
        # with no way to trigger a fresh exchange.
        if deps.sso_state is not None:
            deps.sso_state.forget_assertion(identity.user_key)
        return None, RouteResult(
            status=200,
            reply=errors.identity_failure(
                signin_url=deps.signin_url,
                reason_code="token_exchange_failed",
                support_contact=deps.support_contact,
            ),
        )
    except AuthorizationDenied as exc:
        return None, RouteResult(
            status=200,
            reply=errors.downstream_denial(
                resource=exc.resource, user_display=identity.display_name
            ),
        )
    except TransientBackendError:
        return None, RouteResult(status=200, reply=errors.transient_failure())

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
            reply=errors.transient_failure(request_id="session-manager-not-wired"),
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
            reply=errors.downstream_denial(
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
        return RouteResult(status=200, reply=errors.transient_failure())
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
            status=200, reply=errors.transient_failure(request_id="session-refused")
        )

    log_event(
        logger,
        logging.INFO,
        "conversation reset",
        user_key=identity.user_key,
        session=session.session_id,
    )
    return RouteResult(status=200, reply=errors.conversation_reset())


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
            reply=errors.transient_failure(request_id="components-not-wired"),
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
            reply=errors.downstream_denial(
                resource=exc.resource, user_display=identity.display_name
            ),
        )
    except TransientBackendError:
        return RouteResult(status=200, reply=errors.transient_failure())
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
            status=200, reply=errors.transient_failure(request_id="session-refused")
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
            # Most likely an unrecognised serviceUrl. The old comment here
            # said to answer "through the response body, which is the one
            # channel that does not depend on the connector" -- that channel
            # does not exist; the Bot Framework discards the body. So this is
            # a best-effort reply over the same connector that just failed to
            # produce a renderer, and it will probably fail too. The ERROR log
            # above it is the part that is actually load-bearing.
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
                reply=errors.transient_failure(request_id="renderer-unavailable"),
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
            reply=_reply_unless_rendered(
                errors.downstream_denial(
                    resource=exc.resource,
                    user_display=identity.display_name,
                    request_id=activity.get("id"),
                ),
                renderer=renderer,
            ),
        )
    except IdentityUnavailable as exc:
        stream_error = exc
        # The user token was rejected. Drop the cache so the next turn
        # re-exchanges rather than replaying a revoked credential. The stored
        # Teams assertion goes too: it is the input that produced the rejected
        # credential, so keeping it just reproduces the same rejection.
        await deps.identity_broker.invalidate(identity.user_key)
        if deps.sso_state is not None:
            deps.sso_state.forget_assertion(identity.user_key)
        if renderer is not None:
            await renderer.finish(error=exc)
        return RouteResult(
            status=200,
            reply=_reply_unless_rendered(
                errors.identity_failure(
                    signin_url=deps.signin_url,
                    reason_code="token_rejected_downstream",
                    support_contact=deps.support_contact,
                ),
                renderer=renderer,
            ),
        )
    except TransientBackendError as exc:
        stream_error = exc
        if renderer is not None:
            await renderer.finish(error=exc)
        return RouteResult(
            status=200,
            reply=_reply_unless_rendered(
                errors.transient_failure(), renderer=renderer
            ),
        )
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
        return RouteResult(
            status=200,
            reply=_reply_unless_rendered(
                errors.transient_failure(), renderer=renderer
            ),
        )
    finally:
        if renderer is not None and stream_error is None:
            await renderer.finish()

    return RouteResult(status=200, body=None)


def _sso_token_from_activity(
    activity: Mapping[str, Any],
    *,
    user_key: str | None = None,
    state: SsoState | None = None,
) -> str | None:
    """Find the user's Teams assertion for this turn, or None.

    Two places, in order:

      1. Attached to the activity itself. Rare in practice; a normal Teams
         ``message`` does not carry one.
      2. The per-user store, put there by the ``signin/tokenExchange`` invoke.
         This is the real path, and its absence was why every turn refused.

    Returns None rather than inventing anything. It does NOT fall back to
    ambient credentials: per ADR 002 there is no service-account path here,
    and a turn with no user assertion is a turn that does not run.
    """
    value = activity.get("value")
    if isinstance(value, Mapping):
        token = value.get("token")
        if isinstance(token, str) and token:
            return token

    if state is not None and user_key:
        return state.assertion_for(user_key)

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
