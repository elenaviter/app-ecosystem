"""Open governed MCP connections from local Connection Hub caller profiles."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app_foundation.mcp import ConnectedRemoteTools, MessageHandler
from mcp import Client

from connection_hub_cli.credentials import CredentialStore
from connection_hub_cli.errors import CredentialError
from connection_hub_cli.remote_mcp import connect_remote_tools
from connection_hub_cli.state import ProfileStore


@asynccontextmanager
async def connect_profile_tools(
    *,
    profile_name: str,
    profiles: ProfileStore,
    credentials: CredentialStore,
    message_handler: MessageHandler | None = None,
) -> AsyncIterator[tuple[ConnectedRemoteTools, Client]]:
    """Resolve one local caller profile and open its governed MCP connection."""

    profile = profiles.require(profile_name)
    bearer = credentials.get(profile.credential_ref)
    if bearer is None:
        raise CredentialError(
            "credential_missing",
            f"Caller profile '{profile_name}' has no credential in the operating-system credential store.",
        )
    async with connect_remote_tools(
        endpoint=profile.endpoint,
        bearer=bearer,
        message_handler=message_handler,
    ) as connected:
        yield connected


__all__ = ["connect_profile_tools"]
