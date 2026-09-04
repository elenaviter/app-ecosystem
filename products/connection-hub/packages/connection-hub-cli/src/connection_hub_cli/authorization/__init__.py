"""OAuth authorization primitives for Connection Hub CLI sessions."""

from connection_hub_cli.authorization.callback import (
    AuthorizationCallback,
    LoopbackCallbackServer,
)
from connection_hub_cli.authorization.client import OAuthClient
from connection_hub_cli.authorization.discovery import (
    HttpxOAuthTransport,
    McpOAuthDiscoveryResult,
    McpOAuthEndpointDiscovery,
    OAuthDiscovery,
    OAuthDiscoveryResult,
    OAuthTransport,
)
from connection_hub_cli.authorization.flow import (
    BrowserAuthorizationFlow,
    BrowserAuthorizationGrant,
    BrowserAuthorizationResult,
)
from connection_hub_cli.authorization.models import (
    AuthorizationServerMetadata,
    OAuthClientRegistration,
    OAuthTokenSet,
    ProtectedResourceMetadata,
)
from connection_hub_cli.authorization.pkce import PKCEParameters, generate_pkce
from connection_hub_cli.authorization.profile_session import (
    OAuthProfileAuthorizationResult,
    OAuthProfileCredentialStore,
    OAuthProfileSessionService,
)
from connection_hub_cli.authorization.session import (
    MacOSOAuthSessionCredentialStore,
    NativeOAuthProfileCredentialStore,
    NativeOAuthSessionCredentialStore,
    OAuthSessionRecord,
    OAuthSessionRepository,
    OAuthSessionStore,
    UnavailableOAuthCredentialStore,
    session_id_for_target,
)

__all__ = [
    "AuthorizationCallback",
    "AuthorizationServerMetadata",
    "BrowserAuthorizationFlow",
    "BrowserAuthorizationGrant",
    "BrowserAuthorizationResult",
    "HttpxOAuthTransport",
    "LoopbackCallbackServer",
    "MacOSOAuthSessionCredentialStore",
    "McpOAuthDiscoveryResult",
    "McpOAuthEndpointDiscovery",
    "NativeOAuthProfileCredentialStore",
    "NativeOAuthSessionCredentialStore",
    "OAuthClient",
    "OAuthClientRegistration",
    "OAuthDiscovery",
    "OAuthDiscoveryResult",
    "OAuthProfileAuthorizationResult",
    "OAuthProfileCredentialStore",
    "OAuthProfileSessionService",
    "OAuthSessionRecord",
    "OAuthSessionRepository",
    "OAuthSessionStore",
    "OAuthTokenSet",
    "OAuthTransport",
    "PKCEParameters",
    "ProtectedResourceMetadata",
    "UnavailableOAuthCredentialStore",
    "generate_pkce",
    "session_id_for_target",
]
