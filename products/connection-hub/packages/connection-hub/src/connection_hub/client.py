# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Transport-neutral typed client for the Connection Hub connections contract."""

from __future__ import annotations

from typing import Any, Protocol

from connection_hub.contract import (
    AGENT_GRANT_CHECK,
    AGENT_GRANT_GET_TOKEN,
    CONNECTION_CATALOG,
    CONNECTION_DISCONNECT,
    CONNECTION_GET_TOKEN,
    CONNECTION_STATUS,
    OAUTH_START,
    CatalogEntry,
    ConnectionToken,
)


class ConnectionsTransport(Protocol):
    """A host transport that executes one named Connection Hub operation."""

    async def call(self, operation: str, payload: dict[str, Any]) -> Any: ...


class ConnectionsError(RuntimeError):
    """Raised when a Connection Hub operation returns an error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class ConnectionsClient:
    """Typed client over a host-supplied operation transport."""

    def __init__(self, transport: ConnectionsTransport) -> None:
        self._transport = transport

    async def catalog(self) -> list[CatalogEntry]:
        response = await self._call(CONNECTION_CATALOG)
        return [CatalogEntry.coerce(item) for item in (getattr(response, "items", None) or [])]

    async def status(self, provider: str) -> dict[str, Any]:
        response = await self._call(CONNECTION_STATUS, provider=provider)
        return dict(getattr(response, "object", None) or {})

    async def get_token(self, provider: str, account_id: str | None = None) -> ConnectionToken | None:
        payload: dict[str, Any] = {"provider": provider}
        if account_id:
            payload["account_id"] = account_id
        response = await self._call(CONNECTION_GET_TOKEN, **payload)
        attrs = dict(getattr(response, "attrs", None) or {})
        if not attrs.get("has_token"):
            return None
        result = dict(getattr(response, "object", None) or {})
        if not result.get("access_token"):
            return None
        return ConnectionToken.coerce(result)

    async def agent_grant_token(self, client_id: str, resource: str) -> ConnectionToken | None:
        response = await self._call(
            AGENT_GRANT_GET_TOKEN,
            client_id=client_id,
            resource=resource,
        )
        attrs = dict(getattr(response, "attrs", None) or {})
        if not attrs.get("has_token"):
            return None
        result = dict(getattr(response, "object", None) or {})
        if not result.get("access_token"):
            return None
        return ConnectionToken.coerce(result)

    async def resident_agent_grant_for_access_id(
        self,
        client_id: str,
        *,
        access_id: str,
    ) -> dict[str, Any] | None:
        """Resolve one exact resident-profile Card credential.

        This method is for trusted runtime adapters. The token remains a
        transport credential and the nested ``card`` is Connection Hub's
        non-secret public read model.
        """

        response = await self._call(
            AGENT_GRANT_GET_TOKEN,
            client_id=client_id,
            access_id=access_id,
        )
        attrs = dict(getattr(response, "attrs", None) or {})
        if not attrs.get("has_token"):
            return None
        result = dict(getattr(response, "object", None) or {})
        if not result.get("access_token") or not isinstance(result.get("card"), dict):
            return None
        return result

    async def agent_grant_check(
        self,
        client_id: str,
        namespace: str,
        operation: str,
        *,
        access_id: str = "",
        delegate_identity: str = "",
    ) -> dict[str, Any]:
        payload = {
            "client_id": client_id,
            "namespace": namespace,
            "operation": operation,
        }
        if access_id:
            payload["access_id"] = access_id
        if delegate_identity:
            payload["delegate_identity"] = delegate_identity
        response = await self._call(AGENT_GRANT_CHECK, **payload)
        return dict(getattr(response, "object", None) or {})

    async def disconnect(self, provider: str, account_id: str) -> dict[str, Any]:
        response = await self._call(
            CONNECTION_DISCONNECT,
            provider=provider,
            account_id=account_id,
        )
        return dict(getattr(response, "object", None) or {})

    async def start_oauth(
        self,
        provider: str,
        app_id: str | None = None,
        scopes: list[str] | None = None,
        return_hint: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"provider": provider}
        if app_id:
            payload["app_id"] = app_id
        if scopes:
            payload["scopes"] = list(scopes)
        if return_hint:
            payload["return_hint"] = return_hint
        response = await self._call(OAUTH_START, **payload)
        return dict(getattr(response, "object", None) or {})

    async def _call(self, operation: str, /, **payload: Any) -> Any:
        response = await self._transport.call(operation, dict(payload))
        if bool(getattr(response, "ok", False)):
            return response
        error = getattr(response, "error", None)
        code = str(getattr(error, "code", "") or "connections_error")
        message = str(getattr(error, "message", "") or "connections request failed")
        raise ConnectionsError(code, message)


__all__ = ["ConnectionsClient", "ConnectionsError", "ConnectionsTransport"]
