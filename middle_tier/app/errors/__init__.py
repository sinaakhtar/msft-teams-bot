"""ADR 004 error layer: two paths, two templates, no silent degradation.

    from app import errors

    errors.identity_failure(signin_url=...)      # path 1: refuse + sign-in card
    errors.downstream_denial(resource=...)       # path 2: name the resource
    errors.intercept(exc, resource=...)          # the tool boundary
    errors.classify_failure(403, message)        # the two-403s distinction

Layout::

    taxonomy.py   the typed hierarchy; separates what the template may see
                  from what the log must keep
    classify.py   status code AND message text -> taxonomy (two-403s)
    templates.py  the two user-facing templates + the sign-in card
    boundary.py   the tool-boundary interceptor; narrows the model's view

RELATIONSHIP TO THE OLD ``app/errors.py``
-----------------------------------------
This package supersedes the single-module ``app/errors.py`` that previously
sat beside it. Python resolves a package before a module of the same name, so
``import app.errors`` now lands here, and everything the old module exported is
re-exported below with the same names and the same call signatures - the
existing router and smoke tests keep working unchanged. That module is now
unreachable code and should be deleted; this package does not import it, so
deleting it is safe and changes nothing.
"""

from __future__ import annotations

from .boundary import (
    MODEL_DENIAL_TEMPLATE,
    MODEL_UPSTREAM_TEMPLATE,
    BoundaryOutcome,
    ModelContextLeak,
    acquire_credential_or_refuse,
    guard_tool_call,
    intercept,
    model_facing,
)
from .classify import SERVICE_USAGE_CONSUMER_403
from .classify import classify as classify_failure
from .classify import classify_exception, extract_named_role, extract_resource
from .taxonomy import (
    UNNAMED_RESOURCE,
    DownstreamAuthorizationDenied,
    IdentityAcquisitionError,
    IdentityStage,
    MiddleTierError,
    MissingEntraObjectId,
    UpstreamUnavailable,
    from_identity_error,
    from_port_error,
)
from .templates import (
    ADAPTIVE_CARD_CONTENT_TYPE,
    ADAPTIVE_CARD_SCHEMA,
    ADAPTIVE_CARD_VERSION,
    LOGIN_REQUEST_INVOKE_TYPE,
    OAUTH_CARD_CONTENT_TYPE,
    SIGNIN_CARD_CONTENT_TYPE,
    conversation_reset,
    downstream_denial,
    identity_failure,
    missing_entra_object_id,
    oauth_card,
    render,
    signin_card,
    transient_failure,
    unsupported_activity,
    welcome,
)

__all__ = [
    # taxonomy
    "MiddleTierError",
    "IdentityAcquisitionError",
    "IdentityStage",
    "MissingEntraObjectId",
    "DownstreamAuthorizationDenied",
    "UpstreamUnavailable",
    "UNNAMED_RESOURCE",
    "from_identity_error",
    "from_port_error",
    # classification
    "classify_failure",
    "classify_exception",
    "extract_resource",
    "extract_named_role",
    "SERVICE_USAGE_CONSUMER_403",
    # templates
    "ADAPTIVE_CARD_CONTENT_TYPE",
    "ADAPTIVE_CARD_SCHEMA",
    "ADAPTIVE_CARD_VERSION",
    "SIGNIN_CARD_CONTENT_TYPE",
    "OAUTH_CARD_CONTENT_TYPE",
    "LOGIN_REQUEST_INVOKE_TYPE",
    "identity_failure",
    "missing_entra_object_id",
    "downstream_denial",
    "transient_failure",
    "conversation_reset",
    "welcome",
    "unsupported_activity",
    "signin_card",
    "oauth_card",
    "render",
    # boundary
    "BoundaryOutcome",
    "ModelContextLeak",
    "MODEL_DENIAL_TEMPLATE",
    "MODEL_UPSTREAM_TEMPLATE",
    "intercept",
    "guard_tool_call",
    "acquire_credential_or_refuse",
    "model_facing",
]
