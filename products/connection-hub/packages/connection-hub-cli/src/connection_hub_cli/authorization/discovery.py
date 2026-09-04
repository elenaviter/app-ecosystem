from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from connection_hub_cli.authorization.models import (
    AuthorizationServerMetadata,
    ProtectedResourceMetadata,
    authorization_server_metadata_urls,
    validate_resource_identifier,
    validate_web_url,
)
from connection_hub_cli.errors import AuthorizationError

MAX_OAUTH_RESPONSE_BYTES = 1024 * 1024


class OAuthTransport(Protocol):
    async def get_json(self, url: str) -> Mapping[str, Any]: ...

    async def post_json(
        self, url: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    async def post_form(
        self, url: str, payload: Mapping[str, str]
    ) -> Mapping[str, Any]: ...


class HttpxOAuthTransport:
    def __init__(self, *, transport: Any = None, timeout_seconds: float = 30.0) -> None:
        self._transport = transport
        self._timeout_seconds = max(1.0, min(float(timeout_seconds), 120.0))

    async def get_json(self, url: str) -> Mapping[str, Any]:
        return await self._request_json(
            "GET",
            url,
            expected_statuses={200},
            failure_code="oauth_metadata_request_failed",
        )

    async def post_json(
        self, url: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return await self._request_json(
            "POST",
            url,
            json_payload=payload,
            expected_statuses={200, 201},
            failure_code="oauth_client_registration_failed",
        )

    async def post_form(
        self, url: str, payload: Mapping[str, str]
    ) -> Mapping[str, Any]:
        return await self._request_json(
            "POST",
            url,
            form_payload=payload,
            expected_statuses={200, 201},
            failure_code="oauth_token_request_failed",
        )

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        json_payload: Mapping[str, Any] | None = None,
        form_payload: Mapping[str, str] | None = None,
        expected_statuses: set[int],
        failure_code: str,
    ) -> Mapping[str, Any]:
        endpoint = validate_web_url(url, code="oauth_endpoint_invalid")
        try:
            import httpx2

            async with (
                httpx2.AsyncClient(
                    timeout=httpx2.Timeout(self._timeout_seconds),
                    follow_redirects=False,
                    transport=self._transport,
                    trust_env=False,
                ) as client,
                client.stream(
                    method,
                    endpoint,
                    json=json_payload,
                    data=form_payload,
                    headers={"Accept": "application/json"},
                ) as response,
            ):
                if response.status_code not in expected_statuses:
                    raise AuthorizationError(
                        failure_code,
                        "The OAuth server rejected the request.",
                    )
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > MAX_OAUTH_RESPONSE_BYTES:
                            raise AuthorizationError(
                                "oauth_response_too_large",
                                "The OAuth server response is too large.",
                            )
                    except ValueError:
                        pass
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_OAUTH_RESPONSE_BYTES:
                        raise AuthorizationError(
                            "oauth_response_too_large",
                            "The OAuth server response is too large.",
                        )
        except AuthorizationError:
            raise
        except Exception:  # noqa: BLE001
            raise AuthorizationError(
                failure_code,
                "The OAuth server could not be reached.",
            ) from None
        try:
            value = json.loads(bytes(body))
        except (UnicodeError, ValueError):
            raise AuthorizationError(
                "oauth_response_invalid",
                "The OAuth server returned an invalid JSON response.",
            ) from None
        if not isinstance(value, Mapping):
            raise AuthorizationError(
                "oauth_response_invalid",
                "The OAuth server returned an invalid JSON response.",
            )
        return dict(value)


@dataclass(frozen=True, slots=True)
class OAuthDiscoveryResult:
    protected_resource: ProtectedResourceMetadata
    authorization_server: AuthorizationServerMetadata


@dataclass(frozen=True, slots=True)
class McpOAuthDiscoveryResult:
    endpoint: str
    protected_resource_metadata_url: str
    protected_resource: ProtectedResourceMetadata
    authorization_server: AuthorizationServerMetadata
    scope: str


class OAuthDiscovery:
    def __init__(self, *, transport: OAuthTransport) -> None:
        self._transport = transport

    async def discover(
        self,
        *,
        protected_resource_metadata_url: str,
        expected_resource: str,
    ) -> OAuthDiscoveryResult:
        metadata_url = validate_web_url(
            protected_resource_metadata_url,
            code="oauth_resource_metadata_url_invalid",
        )
        resource_payload = await self._transport.get_json(metadata_url)
        resource = ProtectedResourceMetadata.from_mapping(
            resource_payload,
            expected_resource=expected_resource,
        )
        server_payload: Mapping[str, Any] | None = None
        for candidate in authorization_server_metadata_urls(
            resource.authorization_server
        ):
            try:
                server_payload = await self._transport.get_json(candidate)
                break
            except AuthorizationError:
                continue
        if server_payload is None:
            raise AuthorizationError(
                "oauth_server_metadata_unavailable",
                "The authorization server metadata is unavailable.",
            ) from None
        server = AuthorizationServerMetadata.from_mapping(
            server_payload,
            expected_issuer=resource.authorization_server,
        )
        return OAuthDiscoveryResult(
            protected_resource=resource,
            authorization_server=server,
        )


class McpOAuthEndpointDiscovery:
    """Discover OAuth metadata starting from one Streamable HTTP endpoint."""

    def __init__(
        self,
        *,
        transport: OAuthTransport,
        http_transport: Any = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._transport = transport
        self._oauth = OAuthDiscovery(transport=transport)
        self._http_transport = http_transport
        self._timeout_seconds = max(1.0, min(float(timeout_seconds), 120.0))

    async def discover(self, endpoint: str) -> McpOAuthDiscoveryResult:
        from mcp.client.auth.oauth2 import (
            build_protected_resource_metadata_discovery_urls,
            check_resource_allowed,
            extract_resource_metadata_from_www_auth,
            extract_scope_from_www_auth,
            resource_url_from_server_url,
        )
        from mcp.types import LATEST_PROTOCOL_VERSION

        target = validate_web_url(endpoint, code="oauth_mcp_endpoint_invalid")
        try:
            import httpx2

            async with (
                httpx2.AsyncClient(
                    timeout=httpx2.Timeout(self._timeout_seconds),
                    follow_redirects=False,
                    transport=self._http_transport,
                    trust_env=False,
                ) as client,
                client.stream(
                    "POST",
                    target,
                    json={
                        "jsonrpc": "2.0",
                        "id": "connection-hub-oauth-discovery",
                        "method": "initialize",
                        "params": {
                            "protocolVersion": LATEST_PROTOCOL_VERSION,
                            "capabilities": {},
                            "clientInfo": {
                                "name": "connection-hub-cli",
                                "version": "1",
                            },
                        },
                    },
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                        "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION,
                    },
                ) as response,
            ):
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        if int(content_length) > MAX_OAUTH_RESPONSE_BYTES:
                            raise AuthorizationError(
                                "oauth_response_too_large",
                                "The MCP endpoint response is too large.",
                            )
                    except ValueError:
                        pass
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_OAUTH_RESPONSE_BYTES:
                        raise AuthorizationError(
                            "oauth_response_too_large",
                            "The MCP endpoint response is too large.",
                        )
        except AuthorizationError:
            raise
        except Exception:  # noqa: BLE001
            raise AuthorizationError(
                "oauth_mcp_endpoint_unreachable",
                "The MCP endpoint could not be reached for OAuth discovery.",
            ) from None
        if response.status_code != 401:
            raise AuthorizationError(
                "oauth_challenge_not_advertised",
                "The MCP endpoint did not advertise OAuth authorization.",
            )

        challenge_metadata = extract_resource_metadata_from_www_auth(response)
        challenge_scope = str(extract_scope_from_www_auth(response) or "").strip()
        metadata_url = ""
        resource: ProtectedResourceMetadata | None = None
        for candidate in build_protected_resource_metadata_discovery_urls(
            challenge_metadata,
            target,
        ):
            try:
                payload = await self._transport.get_json(candidate)
            except AuthorizationError:
                continue
            published_resource = validate_resource_identifier(payload.get("resource"))
            requested_resource = resource_url_from_server_url(target)
            if not check_resource_allowed(
                requested_resource=requested_resource,
                configured_resource=published_resource,
            ):
                raise AuthorizationError(
                    "oauth_resource_mismatch",
                    "The OAuth metadata belongs to a different protected resource.",
                )
            resource = ProtectedResourceMetadata.from_mapping(
                payload,
                expected_resource=published_resource,
            )
            metadata_url = validate_web_url(
                candidate,
                code="oauth_resource_metadata_url_invalid",
            )
            break
        if resource is None:
            raise AuthorizationError(
                "oauth_resource_metadata_unavailable",
                "The MCP protected-resource metadata is unavailable.",
            )
        discovered = await self._oauth.discover(
            protected_resource_metadata_url=metadata_url,
            expected_resource=resource.resource,
        )
        scope = challenge_scope or " ".join(resource.scopes_supported)
        return McpOAuthDiscoveryResult(
            endpoint=target,
            protected_resource_metadata_url=metadata_url,
            protected_resource=discovered.protected_resource,
            authorization_server=discovered.authorization_server,
            scope=scope,
        )
