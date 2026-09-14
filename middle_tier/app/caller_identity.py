"""Caller identity extraction. ADR 003 lives here.

NAMING NOTE: this module was originally specified as ``app/identity.py``. It was
renamed because a concurrently-built component claimed ``app/identity/`` as a
package for the OBO/STS **identity broker** (the thing that implements
:class:`app.ports.IdentityBroker`). Python prefers a package over a
same-named module, so the two cannot coexist. The names describe two different
things and the collision was purely lexical:

  * ``app.caller_identity`` (here) - WHO the activity says the human is.
    Reads the request body. No network, no credentials.
  * ``app.identity`` (theirs)      - HOW that human gets a Google credential.
    Crosses the Microsoft -> Google trust boundary.

This module moved rather than theirs because it is one file against a
multi-module package, and because the type it returns is already
:class:`CallerIdentity`. See NOTES.md.

The Agent Runtime session key is ``entra:{tid}:{oid}``, built from the Entra
tenant id and the Entra object id of the human who typed the message.

WHY THIS FILE IS SHORT AND FUSSY
--------------------------------
Teams gives you two identifiers for the same person and they are not
interchangeable:

  * ``activity.from.id`` - the Teams MRI, a ``29:1a2b3c...`` opaque string.
    It is scoped to Teams. It is not an Entra identity. You cannot exchange it
    for a token, and no other system knows it.
  * ``activity.from.aadObjectId`` - the Entra object id (``oid``). This is the
    same principal Entra, Google Workspace federation and BigQuery all agree
    on.

If ``aadObjectId`` is missing and we quietly fall back to ``from.id``, we do
not get a degraded session - we get a SECOND, PERMANENT, UNFEDERATED IDENTITY
for a person who already has one. Their history splits in half, their token
exchange fails in a way that looks like an Entra problem, and the two halves
never reconcile because nothing maps MRI to oid after the fact.

So: no fallback. Missing ``aadObjectId`` raises
:class:`MissingEntraObjectId` and the turn is refused (ADR 004, fail closed).
``from.id`` is never read for identity purposes anywhere in this module; it is
carried only as an opaque correlation handle for logs and for addressing the
reply back to Teams.

This module does NOT perform the OBO / STS exchange. That is another
component's job; the seam it must satisfy is :class:`~app.ports.IdentityBroker`
in ``app.ports``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .auth.inbound import AuthenticatedCaller

#: Entra object ids and tenant ids are GUIDs. Validating the shape stops a
#: malformed or injected value from becoming part of a session key, and stops
#: a `:` in either field from forging a different composite key.
_GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

USER_KEY_PREFIX = "entra"


class IdentityError(Exception):
    """Base class for identity extraction failures. All are fail-closed."""


class MissingEntraObjectId(IdentityError):
    """`from.aadObjectId` was absent or empty on a validated activity.

    Raised INSTEAD OF falling back to the Teams MRI. See the module docstring.
    Callers must refuse the turn and emit the ADR 004 identity-failure message
    plus a sign-in card (see :mod:`app.errors`).

    In practice this happens for: anonymous/guest users in a meeting, some
    channel-scoped bot mentions where the tenant has restricted directory
    lookups, and any non-Teams channel. All of those are turns we cannot
    federate, so refusing is the correct outcome, not a bug to work around.
    """


class MissingTenantId(IdentityError):
    """The Entra tenant id could not be determined from the activity."""


class MalformedEntraIdentifier(IdentityError):
    """A tenant id or object id was present but was not a GUID."""


@dataclass(frozen=True)
class CallerIdentity:
    """The validated human behind an activity.

    :param tenant_id: Entra tenant GUID.
    :param object_id: Entra object id GUID (the ``oid`` claim's value).
    :param user_key: ``entra:{tid}:{oid}``. THE Agent Runtime session user id.
    :param teams_mri: ``from.id``. Opaque correlation handle ONLY. Never an
        identity, never a session key, never sent downstream as a subject.
    :param display_name: for rendering only.
    """

    tenant_id: str
    object_id: str
    user_key: str
    teams_mri: str | None = None
    display_name: str | None = None


def build_user_key(tenant_id: str, object_id: str) -> str:
    """Compose ``entra:{tid}:{oid}`` with both halves shape-checked.

    :raises MalformedEntraIdentifier: if either value is not a GUID.
    """
    if not _GUID_RE.match(tenant_id or ""):
        raise MalformedEntraIdentifier(f"tenant id is not a GUID: {tenant_id!r}")
    if not _GUID_RE.match(object_id or ""):
        raise MalformedEntraIdentifier(f"object id is not a GUID: {object_id!r}")
    # Lowercased so the same person never produces two session keys because
    # one channel happened to send uppercase hex.
    return f"{USER_KEY_PREFIX}:{tenant_id.lower()}:{object_id.lower()}"


def _tenant_id_from_activity(
    activity: Mapping[str, Any], *, expected_tenant_id: str | None
) -> str:
    """Read the tenant id, preferring the activity, falling back to config.

    Teams puts it at ``channelData.tenant.id``. Some activity shapes also carry
    ``conversation.tenantId``. If the activity carries one AND we are pinned to
    a configured tenant, they must agree - a mismatch means a cross-tenant
    activity reached a single-tenant bot, which is a refusal, not a merge.
    """
    channel_data = activity.get("channelData") or {}
    tenant = (channel_data.get("tenant") or {}) if isinstance(channel_data, Mapping) else {}
    from_activity = tenant.get("id") if isinstance(tenant, Mapping) else None

    if not from_activity:
        conversation = activity.get("conversation") or {}
        if isinstance(conversation, Mapping):
            from_activity = conversation.get("tenantId")

    if from_activity and expected_tenant_id:
        if str(from_activity).lower() != expected_tenant_id.lower():
            raise MissingTenantId(
                "activity tenant does not match the configured tenant; refusing "
                "to serve a cross-tenant turn"
            )

    resolved = from_activity or expected_tenant_id
    if not resolved:
        raise MissingTenantId(
            "no channelData.tenant.id on the activity and no configured tenant id"
        )
    return str(resolved)


def caller_from_activity(
    activity: Mapping[str, Any],
    *,
    caller: AuthenticatedCaller,
    expected_tenant_id: str | None = None,
) -> CallerIdentity:
    """Extract the caller identity from a CRYPTOGRAPHICALLY VALIDATED activity.

    :param activity: the parsed Bot Framework activity body.
    :param caller: proof from :mod:`app.auth.inbound` that the body came from a
        trusted channel. Required by type, not by convention - you cannot call
        this function on an unvalidated body without deliberately fabricating
        an :class:`AuthenticatedCaller`, which review will catch.
    :param expected_tenant_id: the tenant this deployment serves, if pinned.

    :raises MissingEntraObjectId: no ``from.aadObjectId``. FAIL CLOSED.
    :raises MissingTenantId: tenant unresolvable, or cross-tenant mismatch.
    :raises MalformedEntraIdentifier: identifiers present but not GUIDs.
    """
    if caller is None:  # pragma: no cover - defensive, type system covers it
        raise IdentityError("caller_from_activity requires a validated AuthenticatedCaller")

    sender = activity.get("from") or {}
    if not isinstance(sender, Mapping):
        raise MissingEntraObjectId("activity has no usable `from` object")

    object_id = sender.get("aadObjectId")

    if not object_id or not str(object_id).strip():
        # ---------------------------------------------------------------
        # THE FALLBACK THAT MUST NEVER EXIST
        #
        #   mri = sender.get("id")          # <-- NO
        #   return CallerIdentity(..., object_id=mri)
        #
        # `sender["id"]` is deliberately not read here. See module docstring.
        # ---------------------------------------------------------------
        raise MissingEntraObjectId(
            "activity `from.aadObjectId` is absent; refusing the turn rather "
            "than minting a second identity from the Teams MRI"
        )

    tenant_id = _tenant_id_from_activity(activity, expected_tenant_id=expected_tenant_id)
    user_key = build_user_key(str(tenant_id), str(object_id))

    return CallerIdentity(
        tenant_id=str(tenant_id).lower(),
        object_id=str(object_id).lower(),
        user_key=user_key,
        teams_mri=sender.get("id"),
        display_name=sender.get("name"),
    )


__all__ = [
    "CallerIdentity",
    "IdentityError",
    "MalformedEntraIdentifier",
    "MissingEntraObjectId",
    "MissingTenantId",
    "USER_KEY_PREFIX",
    "build_user_key",
    "caller_from_activity",
]
