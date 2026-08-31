# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Host-neutral request-authenticator contracts."""

from connection_hub.authenticators.authority import (
    AuthRequestHints,
    AuthorityIdentity,
    SurfaceGuardRequirement,
    select_authenticator_candidates,
)
from connection_hub.authenticators.models import (
    AuthenticatedRequest,
    AuthenticatorRegistration,
    RequestEnvelope,
)
from connection_hub.authenticators.client import (
    DEFAULT_CONNECTION_HUB_BUNDLE_ID,
    REQUEST_AUTHENTICATE_OPERATION,
    ConnectionHubAuthenticatorsClient,
)

__all__ = [
    "AuthRequestHints",
    "AuthenticatedRequest",
    "AuthenticatorRegistration",
    "AuthorityIdentity",
    "ConnectionHubAuthenticatorsClient",
    "DEFAULT_CONNECTION_HUB_BUNDLE_ID",
    "REQUEST_AUTHENTICATE_OPERATION",
    "RequestEnvelope",
    "SurfaceGuardRequirement",
    "select_authenticator_candidates",
]
