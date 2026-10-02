from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

from connection_hub.caller.authorization.models import (
    AuthorizationServerMetadata,
    ProtectedResourceMetadata,
    authorization_server_metadata_urls,
    validate_resource_identifier,
    validate_web_url,
)
from connection_hub.caller.authorization import request_records
from connection_hub.caller.authorization.client_pool import oauth_http_client
from connection_hub.caller.errors import AuthorizationError

MAX_OAUTH_RESPONSE_BYTES = 1024 * 1024
MAX_OAUTH_ERROR_REASON_CHARS = 512


# Every OAuth discovery, metadata and token request carries one random id
# that the relay logs with any failure, and that a proxy can log with the
# request, so a client failure can be matched with what the server side saw
# (W461, 2026-10-02: failing token requests left no proxy line, and nothing
# joined the two sides). It is random per request and carries no identity.
REQUEST_ID_HEADER = "X-Request-ID"


def _record_probe(
    request_id: str,
    started: float,
    status: int | None,
    outcome: str,
    phases: request_records.RequestPhases,
) -> None:
    """The MCP endpoint probe's request record; a 401 challenge is its expected answer."""

    request_records.record(
        request_id=request_id,
        kind="mcp_probe",
        method="POST",
        status=status,
        outcome=outcome,
        elapsed_seconds=time.monotonic() - started,
        phases=phases,
    )


def new_request_id() -> str:
    return secrets.token_hex(8)


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
        "authorization_pending",
        "slow_down",
        "access_denied",
        "expired_token",
        "device_code_replayed",
        "device_client_mismatch",
        "device_card_mismatch",
        "device_card_revision_conflict",
        # W414: an existing Card needs continuity proof from its machine.
        "card_continuity_required",
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


# The longest wait a server's Retry-After is taken at, so a wrong or hostile
# value cannot silence a client for days.
MAX_RETRY_AFTER_SECONDS = 86_400


def _retry_after_seconds(headers: Mapping[str, Any], body: bytes) -> int | None:
    """Seconds a refusing server asked the client to wait, if it said.

    Reads the ``Retry-After`` header, as delta-seconds or an HTTP date, and
    otherwise a ``retry_after`` number in a JSON body, which is how the
    KDCube gateway states its hourly window. On 2026-09-21 every gateway 429
    carried ``retry_after: 3600``, and a relay that could not see it kept
    retrying into a bucket it had already exceeded.
    """

    raw = str(headers.get("retry-after") or "").strip()
    seconds: float | None = None
    if raw:
        try:
            seconds = float(raw)
        except ValueError:
            try:
                moment = parsedate_to_datetime(raw)
                seconds = (moment - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, IndexError, OverflowError):
                seconds = None
    if seconds is None:
        try:
            payload = json.loads(body)
        except (UnicodeError, ValueError):
            payload = None
        if isinstance(payload, Mapping):
            value = payload.get("retry_after")
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                seconds = float(value)
    if seconds is None:
        return None
    return int(max(0, min(MAX_RETRY_AFTER_SECONDS, seconds)))


def _request_error(
    *,
    failure_code: str,
    method: str,
    endpoint: str,
    status: int | None = None,
    server_reason: str = "",
    failure_kind: str = "",
    oauth_error: str = "",
    retry_after_seconds: int | None = None,
    request_id: str = "",
) -> AuthorizationError:
    safe_url = _safe_request_url(endpoint, failure_code=failure_code)
    label = _request_label(failure_code)
    details: dict[str, Any] = {"method": method, "url": safe_url}
    if request_id:
        details["request_id"] = request_id
    if status is not None:
        details["status"] = int(status)
    if retry_after_seconds is not None:
        details["retry_after_seconds"] = int(retry_after_seconds)
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
    if request_id:
        message = f"{message[:-1]} (request_id {request_id})."
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

    async def _exchange(
        self,
        method: str,
        endpoint: str,
        *,
        request_id: str,
        json_payload: Mapping[str, Any] | None,
        form_payload: Mapping[str, str] | None,
        expected_statuses: set[int],
        failure_code: str,
        phases: request_records.RequestPhases,
    ) -> tuple[bytearray, int]:
        """One HTTP exchange: the response body and status, or the classified failure."""

        try:
            import httpx2

            async with (
                oauth_http_client(
                    transport=self._transport, timeout_seconds=self._timeout_seconds
                ) as client,
                client.stream(
                    method,
                    endpoint,
                    json=json_payload,
                    data=form_payload,
                    headers={"Accept": "application/json", REQUEST_ID_HEADER: request_id},
                    timeout=httpx2.Timeout(self._timeout_seconds),
                    extensions={"trace": phases.trace},
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
                        retry_after_seconds=_retry_after_seconds(
                            response.headers, bytes(body)
                        ),
                        request_id=request_id,
                    )
        except AuthorizationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _request_error(
                failure_code=failure_code,
                method=method,
                endpoint=endpoint,
                failure_kind=type(exc).__name__,
                request_id=request_id,
            ) from None
        return body, response.status_code

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
        request_id = new_request_id()
        started = time.monotonic()
        phases = request_records.RequestPhases(started)
        status: int | None = None
        outcome = "ok"
        try:
            body, status = await self._exchange(
                method,
                endpoint,
                request_id=request_id,
                json_payload=json_payload,
                form_payload=form_payload,
                expected_statuses=expected_statuses,
                failure_code=failure_code,
                phases=phases,
            )
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except AuthorizationError as exc:
            outcome = exc.code
            status = exc.details.get("status") if isinstance(exc.details, Mapping) else None
            raise
        finally:
            request_records.record(
                request_id=request_id,
                kind=request_records.kind_of(failure_code),
                method=method,
                status=status if isinstance(status, int) else None,
                outcome=outcome,
                elapsed_seconds=time.monotonic() - started,
                phases=phases,
            )
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


# Discovery results per process. Metadata changes rarely, and a relay that
# retried a refresh re-fetched both documents every time. Only successes are
# kept, so a failure is never served from here.
DISCOVERY_CACHE_SECONDS = 300.0
_DISCOVERY_CACHE: dict[tuple[str, str], tuple[float, "OAuthDiscoveryResult"]] = {}


def clear_discovery_cache() -> None:
    _DISCOVERY_CACHE.clear()


def _rate_limited(error: AuthorizationError) -> bool:
    return int(getattr(error, "status", 0) or 0) == 429


# A metadata candidate the server says is not there. Only these answers move
# discovery on to the next candidate as an absence.
METADATA_ABSENT_STATUSES = frozenset({404, 410})


def _metadata_absent(error: AuthorizationError) -> bool:
    return (
        error.code == "oauth_metadata_request_failed"
        and int(getattr(error, "status", 0) or 0) in METADATA_ABSENT_STATUSES
    )


def _metadata_transient(error: AuthorizationError) -> bool:
    """A metadata request that did not get the server's answer.

    No status (timeout, refused or dropped connection), a request timeout or a
    server error says nothing about whether the document exists (W461,
    2026-10-02: a timed-out candidate fell through to the root fallback's 404
    and was reported as metadata unavailable).
    """

    if error.code != "oauth_metadata_request_failed":
        return False
    status = getattr(error, "status", None)
    return status is None or int(status) == 408 or int(status) >= 500


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
        cache_key = (metadata_url, str(expected_resource or ""))
        cached = _DISCOVERY_CACHE.get(cache_key)
        if cached is not None and cached[0] > time.monotonic():
            return cached[1]
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
            except AuthorizationError as exc:
                # A rate limit is the host's answer for every candidate. Trying
                # the next one only spends more of the same bucket, and folding
                # it into "unavailable" would lose the Retry-After it carries.
                if _rate_limited(exc):
                    raise
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
        result = OAuthDiscoveryResult(
            protected_resource=resource,
            authorization_server=server,
        )
        _DISCOVERY_CACHE[cache_key] = (time.monotonic() + DISCOVERY_CACHE_SECONDS, result)
        return result


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
        request_id = new_request_id()
        probe_started = time.monotonic()
        probe_phases = request_records.RequestPhases(probe_started)
        try:
            import httpx2

            async with (
                oauth_http_client(
                    transport=self._http_transport, timeout_seconds=self._timeout_seconds
                ) as client,
                client.stream(
                    "POST",
                    target,
                    timeout=httpx2.Timeout(self._timeout_seconds),
                    extensions={"trace": probe_phases.trace},
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
                        REQUEST_ID_HEADER: request_id,
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
        except AuthorizationError as exc:
            _record_probe(request_id, probe_started, None, exc.code, probe_phases)
            raise
        except asyncio.CancelledError:
            _record_probe(request_id, probe_started, None, "cancelled", probe_phases)
            raise
        except Exception as exc:  # noqa: BLE001
            _record_probe(request_id, probe_started, None, "oauth_mcp_endpoint_unreachable", probe_phases)
            # The exception class says whether the endpoint timed out, refused
            # the connection or dropped it; its text may carry the URL and is
            # not kept.
            unreachable = AuthorizationError(
                "oauth_mcp_endpoint_unreachable",
                "The MCP endpoint could not be reached for OAuth discovery "
                f"({type(exc).__name__}, request_id {request_id}).",
            )
            unreachable.details = {
                "phase": "mcp_probe",
                "method": "POST",
                "url": _safe_request_url(target, failure_code=""),
                "failure_kind": type(exc).__name__,
                "request_id": request_id,
            }
            raise unreachable from None
        _record_probe(request_id, probe_started, response.status_code, "ok", probe_phases)
        if response.status_code != 401:
            raise AuthorizationError(
                "oauth_challenge_not_advertised",
                "The MCP endpoint did not advertise OAuth authorization.",
            )

        challenge_metadata = extract_resource_metadata_from_www_auth(response)
        challenge_scope = str(extract_scope_from_www_auth(response) or "").strip()
        metadata_url = ""
        resource: ProtectedResourceMetadata | None = None
        unanswered: AuthorizationError | None = None
        for candidate in build_protected_resource_metadata_discovery_urls(
            challenge_metadata,
            target,
        ):
            try:
                payload = await self._transport.get_json(candidate)
            except AuthorizationError as exc:
                if _metadata_absent(exc):
                    continue
                if _metadata_transient(exc):
                    # The next candidate may still answer; if none does, this
                    # failure is what discovery reports.
                    unanswered = unanswered or exc
                    continue
                # A rate limit, a refusal (401, 403, other 4xx) or a malformed
                # document is the answer: it is not an absence to skip past.
                raise
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
            if unanswered is not None:
                raise unanswered
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
