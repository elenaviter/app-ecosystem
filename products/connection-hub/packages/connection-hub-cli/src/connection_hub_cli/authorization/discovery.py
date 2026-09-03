from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from connection_hub_cli.authorization.models import (
    AuthorizationServerMetadata,
    ProtectedResourceMetadata,
    authorization_server_metadata_urls,
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
