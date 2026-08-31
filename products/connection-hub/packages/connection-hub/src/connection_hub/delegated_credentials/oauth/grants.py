# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Delegated-client session minting for Connection Hub external credentials."""
from __future__ import annotations

from typing import Any, Callable, Iterable, List, Mapping

# Short-lived access token; the refresh token (long-lived, rotating) keeps a
# daily-or-seldom routine working without re-consent.
ACCESS_TOKEN_TTL_SECONDS = 3600

DELEGATED_CLIENT_ROLE = "kdcube:role:delegated-client"


SessionAuthorityFactory = Callable[..., Any]


def oauth_tenant_project(
    source: Any | None = None,
    *,
    config: Any | None = None,
) -> tuple[str, str]:
    from connection_hub.delegated_credentials.oauth.config import (
        oauth_delegated_config,
    )

    cfg = config or oauth_delegated_config(source)
    return cfg.tenant, cfg.project


def integration_subject(grantor_subject: str, *, client_id: str = "") -> str:
    """Dedicated external-client identity tied to the consenting platform subject."""
    client = str(client_id or "delegated_client").strip() or "delegated_client"
    client = "_".join(client.split())
    return f"integration:{client}:{grantor_subject}"


def _delegated_roles_for_scopes(scopes: Iterable[str]) -> list[str]:
    del scopes
    return [DELEGATED_CLIENT_ROLE]


def _delegated_permissions_for_scopes(scopes: Iterable[str]) -> list[str]:
    return sorted({str(item).strip() for item in (scopes or []) if str(item).strip()})


async def mint_delegated_client_access_token(
    sub: str,
    scopes: List[str],
    *,
    authority=None,
    authority_factory: SessionAuthorityFactory | None = None,
    config: Any | None = None,
    client_id: str = "",
    operations: List[str] | None = None,
    credential: Mapping[str, Any] | None = None,
    ttl_seconds: int = ACCESS_TOKEN_TTL_SECONDS,
) -> dict:
    """Mint a least-privilege external-client session for a delegated connection.

    ``sub`` is the consenting user's subject; the token is issued to the derived
    integration identity, never to ``sub`` itself.
    """
    if authority is None:
        if authority_factory is None:
            raise RuntimeError(
                "mint_delegated_client_access_token requires a session-authority "
                "instance or factory"
            )
        tenant, project = oauth_tenant_project(source=None, config=config)
        authority = authority_factory(tenant=tenant, project=project)

    isub = integration_subject(sub, client_id=client_id)
    operation_list = list(operations or [])
    grant = await authority.login_or_register(
        sub=isub,
        username=f"delegated-client:{client_id or 'unknown'}",
        name="Delegated client connection",
        roles=_delegated_roles_for_scopes(scopes),
        permissions=_delegated_permissions_for_scopes(scopes),
        provider="integration",
        provider_subject=sub,
        metadata={
            "credential": dict(credential or {}),
            "delegated_client": {
                "client_id": str(client_id or "").strip(),
                "scopes": list(scopes or []),
                "operations": operation_list,
            },
        },
        ttl_seconds=ttl_seconds,
    )
    return {"access_token": grant.token, "expires_in": ttl_seconds, "session_id": getattr(grant, "session_id", "")}


__all__ = [
    "ACCESS_TOKEN_TTL_SECONDS",
    "DELEGATED_CLIENT_ROLE",
    "SessionAuthorityFactory",
    "integration_subject",
    "mint_delegated_client_access_token",
    "oauth_tenant_project",
]
