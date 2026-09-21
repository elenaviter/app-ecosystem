from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from connection_hub_cli.authorization.models import (
    AuthorizationServerMetadata,
    ProtectedResourceMetadata,
    authorization_server_metadata_urls,
    validate_resource_identifier,
    validate_web_url,
)
from connection_hub_cli.errors import AuthorizationError

MAX_OAUTH_RESPONSE_BYTES = 1024 * 1024
MAX_OAUTH_ERROR_REASON_CHARS = 512


def _request_label(failure_code: str) -> str:
    return {
        "oauth_metadata_request_failed": "OAuth metadata",
        "oauth_client_registration_failed": "OAuth client registration",
        "oauth_token_request_failed": "OAuth token",
    }.get(failure_code, "OAuth")


def _safe_request_url(endpoint: str, *, failure_code: str) -> str:
    if failure_code == "oauth_metadata_request_failed":
        return endpoint
    parsed = urlsplit(endpoint)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _bounded_reason(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:MAX_OAUTH_ERROR_REASON_CHARS]


def _metadata_response_reason(body: bytes, *, reason_phrase: str) -> str:
    try:
        payload = json.loads(body)
    except (UnicodeError, ValueError):
        payload = None
    if isinstance(payload, Mapping):
        for key in ("detail", "error", "error_description", "message"):
            reason = _bounded_reason(payload.get(key))
            if reason:
                return reason
    return _bounded_reason(reason_phrase)


# The registered token-endpoint error codes (RFC 6749 section 5.2, RFC 8707
# invalid_target, and the two section 4.1.2.1 codes servers also return here).
# Only a value from this set travels with a failure: the code is a fixed word,
# while a description or an unregistered value is server text that may echo
# what was submitted.
OAUTH_TOKEN_ERROR_CODES = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
        "invalid_target",
        "server_error",
        "temporarily_unavailable",
    }
)


def _token_error_code(body: bytes) -> str:
    """The registered ``error`` code of a refused token request, or "".

    It says why a grant was refused (``invalid_grant`` means the grant is
    gone and only a new authorization mints another, any other code is not
    the grant). On 2026-09-21 the relay reported only ``status=400`` and the
    operator could not tell whether re-authorizing was the remedy.
    """

    try:
        payload = json.loads(body)
    except (UnicodeError, ValueError):
        return ""
    if not isinstance(payload, Mapping):
        return ""
    code = payload.get("error")
    return code if isinstance(code, str) and code in OAUTH_TOKEN_ERROR_CODES else ""


def _request_error(
    *,
    failure_code: str,
    method: str,
    endpoint: str,
    status: int | None = None,
    server_reason: str = "",
    failure_kind: str = "",
    oauth_error: str = "",
) -> AuthorizationError:
    safe_url = _safe_request_url(endpoint, failure_code=failure_code)
    label = _request_label(failure_code)
    details: dict[str, Any] = {"method": method, "url": safe_url}
    if status is not None:
        details["status"] = int(status)
    if server_reason:
        details["server_reason"] = server_reason
    if oauth_error:
        details["oauth_error"] = oauth_error
    if failure_kind:
        details["failure_kind"] = failure_kind
    if status is None:
        message = f"{label} {method} {safe_url} could not be reached"
        if failure_kind:
            message += f" ({failure_kind})"
        message += "."
    else:
        message = f"{label} {method} {safe_url} returned HTTP {status}"
        if server_reason:
            message += f": {server_reason}"
        message += "."
    error = AuthorizationError(failure_code, message)
    error.status = int(status) if status is not None else None
    error.details = details
    return error


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
                if response.status_code not in expected_statuses:
                    server_reason = ""
                    oauth_error = ""
                    if failure_code == "oauth_metadata_request_failed":
                        server_reason = _metadata_response_reason(
                            bytes(body),
                            reason_phrase=str(response.reason_phrase or ""),
                        )
                    elif failure_code == "oauth_token_request_failed":
                        oauth_error = _token_error_code(bytes(body))
                        server_reason = oauth_error
                    raise _request_error(
                        failure_code=failure_code,
                        method=method,
                        endpoint=endpoint,
                        status=response.status_code,
                        server_reason=server_reason,
                        oauth_error=oauth_error,
                    )
        except AuthorizationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _request_error(
                failure_code=failure_code,
                method=method,
                endpoint=endpoint,
                failure_kind=type(exc).__name__,
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

    async def discover(
        self, endpoint: str, *, default_scope: str = ""
    ) -> McpOAuthDiscoveryResult:
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
        # Precedence, widest last. The server's own challenge is the most
        # specific statement of what this request needs, so it still wins. A
        # caller's declared needs come next: a client that knows which claims
        # its own operations require should ask for those rather than for
        # everything on offer. The advertised set remains the last resort,
        # because a deployment advertises the union of every app installed on
        # it, and asking for all of it hands the approving operator a consent
        # screen they cannot read and issues a standing capability far wider
        # than the caller's work.
        scope = (
            challenge_scope
            or str(default_scope or "").strip()
            or " ".join(resource.scopes_supported)
        )
        return McpOAuthDiscoveryResult(
            endpoint=target,
            protected_resource_metadata_url=metadata_url,
            protected_resource=discovered.protected_resource,
            authorization_server=discovered.authorization_server,
            scope=scope,
        )
