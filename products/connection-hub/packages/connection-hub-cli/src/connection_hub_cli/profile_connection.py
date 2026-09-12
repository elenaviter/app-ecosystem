"""Open governed MCP connections from local Connection Hub caller profiles."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app_foundation.mcp import ConnectedRemoteTools, MessageHandler
from mcp import Client

from connection_hub_cli.authorization.profile_session import (
    OAuthProfileSessionService,
)
from connection_hub_cli.credentials import CredentialStore
from connection_hub_cli.errors import CredentialError
from connection_hub_cli.remote_mcp import connect_remote_tools
from connection_hub_cli.state import ProfileStore


async def resolve_profile_bearer(
    *,
    profile_name: str,
    profiles: ProfileStore,
    credentials: CredentialStore,
    oauth_sessions: OAuthProfileSessionService | None = None,
) -> str:
    """Return the current bearer a caller profile presents.

    Separate from opening a connection, because a profile's credential is not
    only an MCP concern. A caller that presents its card to another transport
    needs exactly this value and nothing else, and resolving it here keeps one
    answer to "what does this profile present" rather than two that can drift.

    An OAuth-backed profile refreshes; a static one is read from the operating
    system's credential store. Either way the secret stays in native custody
    until the moment it is used.
    """

    profile = profiles.require(profile_name)
    if getattr(profile, "auth_type", "static_bearer") == "oauth":
        if oauth_sessions is None:
            raise CredentialError(
                "oauth_profile_session_unavailable",
                "This process cannot refresh the selected OAuth-backed caller profile.",
            )
        bearer = await oauth_sessions.access_token(profile.name)
    else:
        bearer = credentials.get(profile.credential_ref)
    if bearer is None:
        raise CredentialError(
            "credential_missing",
            f"Caller profile '{profile_name}' has no credential in the operating-system credential store.",
        )
    return bearer


@asynccontextmanager
async def connect_profile_tools(
    *,
    profile_name: str,
    profiles: ProfileStore,
    credentials: CredentialStore,
    oauth_sessions: OAuthProfileSessionService | None = None,
    message_handler: MessageHandler | None = None,
) -> AsyncIterator[tuple[ConnectedRemoteTools, Client]]:
    """Resolve one local caller profile and open its governed MCP connection."""

    profile = profiles.require(profile_name)
    bearer = await resolve_profile_bearer(
        profile_name=profile_name,
        profiles=profiles,
        credentials=credentials,
        oauth_sessions=oauth_sessions,
    )
    async with connect_remote_tools(
        endpoint=profile.endpoint,
        bearer=bearer,
        message_handler=message_handler,
    ) as connected:
        yield connected


__all__ = ["connect_profile_tools", "resolve_profile_bearer"]
