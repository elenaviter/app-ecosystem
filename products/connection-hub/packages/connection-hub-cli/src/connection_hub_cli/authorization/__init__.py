"""OAuth authorization primitives for Connection Hub CLI sessions."""

from connection_hub_cli.authorization.callback import (
    AuthorizationCallback,
    LoopbackCallbackServer,
)
from connection_hub_cli.authorization.client import OAuthClient
from connection_hub_cli.authorization.discovery import (
    HttpxOAuthTransport,
    OAuthDiscovery,
    OAuthDiscoveryResult,
    OAuthTransport,
)
from connection_hub_cli.authorization.flow import (
    BrowserAuthorizationFlow,
    BrowserAuthorizationResult,
)
from connection_hub_cli.authorization.models import (
    AuthorizationServerMetadata,
    OAuthClientRegistration,
    OAuthTokenSet,
    ProtectedResourceMetadata,
)
from connection_hub_cli.authorization.pkce import PKCEParameters, generate_pkce
from connection_hub_cli.authorization.session import (
    MacOSOAuthSessionCredentialStore,
    OAuthSessionRecord,
    OAuthSessionRepository,
    OAuthSessionStore,
    session_id_for_target,
)

__all__ = [
    "AuthorizationCallback",
    "AuthorizationServerMetadata",
    "BrowserAuthorizationFlow",
    "BrowserAuthorizationResult",
    "HttpxOAuthTransport",
    "LoopbackCallbackServer",
    "MacOSOAuthSessionCredentialStore",
    "OAuthClient",
    "OAuthClientRegistration",
    "OAuthDiscovery",
    "OAuthDiscoveryResult",
    "OAuthSessionRecord",
    "OAuthSessionRepository",
    "OAuthSessionStore",
    "OAuthTokenSet",
    "OAuthTransport",
    "PKCEParameters",
    "ProtectedResourceMetadata",
    "generate_pkce",
    "session_id_for_target",
]
