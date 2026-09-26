# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""One construction of the caller layer's stores and services (W322).

Problem Board's ``pb`` and the ``connection-hub`` command line both start from
``build_caller_services``; the command line adds its own client, host and
management services on top.
"""

from __future__ import annotations

from dataclasses import dataclass

from connection_hub.caller.authorization import (
    BrowserAuthorizationFlow,
    DeviceAuthorizationFlow,
    HttpxOAuthTransport,
    McpOAuthEndpointDiscovery,
    NativeOAuthProfileCredentialStore,
    NativeOAuthSessionCredentialStore,
    OAuthClient,
    OAuthDiscovery,
    OAuthSessionRepository,
    OAuthSessionStore,
    UnavailableOAuthCredentialStore,
)
from connection_hub.caller.authorization.profile_session import OAuthProfileSessionService
from connection_hub.caller.credentials import (
    CredentialStore,
    NativeCredentialStore,
    UnavailableCredentialStore,
)
from connection_hub.caller.errors import CredentialError
from connection_hub.caller.paths import StatePaths
from connection_hub.caller.profiles import ProfileService
from connection_hub.caller.remote_mcp import probe_remote_tools
from connection_hub.caller.state import InstallationStore, ProfileStore


@dataclass
class CallerServices:
    profiles: ProfileStore
    installations: InstallationStore
    credentials: CredentialStore
    oauth_sessions: OAuthSessionStore
    oauth_repository: OAuthSessionRepository
    oauth_discovery: OAuthDiscovery
    oauth_client: OAuthClient
    authorization_flow: BrowserAuthorizationFlow
    oauth_profile_sessions: OAuthProfileSessionService
    profile_service: ProfileService


def build_caller_services(*, paths: StatePaths | None = None) -> CallerServices:
    selected_paths = paths or StatePaths.default()
    profiles = ProfileStore(selected_paths.profiles)
    installations = InstallationStore(selected_paths.installations)
    oauth_sessions = OAuthSessionStore(selected_paths.oauth_sessions)
    try:
        native_credentials = NativeCredentialStore()
    except CredentialError as exc:
        credentials: CredentialStore = UnavailableCredentialStore(exc)
        oauth_credentials = UnavailableOAuthCredentialStore(exc, error_prefix="oauth_session")
        oauth_profile_credentials = UnavailableOAuthCredentialStore(
            exc,
            error_prefix="oauth_profile",
        )
    else:
        credentials = native_credentials
        oauth_credentials = NativeOAuthSessionCredentialStore(
            backend=native_credentials.native_backend,
            platform_name=native_credentials.platform_name,
        )
        oauth_profile_credentials = NativeOAuthProfileCredentialStore(
            backend=native_credentials.native_backend,
            platform_name=native_credentials.platform_name,
        )
    oauth_transport = HttpxOAuthTransport()
    oauth_discovery = OAuthDiscovery(transport=oauth_transport)
    oauth_client = OAuthClient(transport=oauth_transport)
    oauth_repository = OAuthSessionRepository(sessions=oauth_sessions, credentials=oauth_credentials)
    authorization_flow = BrowserAuthorizationFlow(
        discovery=oauth_discovery,
        client=oauth_client,
        sessions=oauth_repository,
    )
    oauth_profile_sessions = OAuthProfileSessionService(
        profiles=profiles,
        credentials=oauth_profile_credentials,
        endpoint_discovery=McpOAuthEndpointDiscovery(transport=oauth_transport),
        discovery=oauth_discovery,
        authorization=authorization_flow,
        device_authorization=DeviceAuthorizationFlow(client=oauth_client),
        oauth=oauth_client,
        probe=probe_remote_tools,
    )
    profile_service = ProfileService(
        profiles=profiles,
        installations=installations,
        credentials=credentials,
        probe=probe_remote_tools,
        oauth_sessions=oauth_profile_sessions,
    )
    return CallerServices(
        profiles=profiles,
        installations=installations,
        credentials=credentials,
        oauth_sessions=oauth_sessions,
        oauth_repository=oauth_repository,
        oauth_discovery=oauth_discovery,
        oauth_client=oauth_client,
        authorization_flow=authorization_flow,
        oauth_profile_sessions=oauth_profile_sessions,
        profile_service=profile_service,
    )


__all__ = ["CallerServices", "build_caller_services"]
