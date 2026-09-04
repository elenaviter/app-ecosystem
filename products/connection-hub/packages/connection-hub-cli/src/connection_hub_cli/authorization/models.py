from __future__ import annotations

import ipaddress
import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from connection_hub_cli.errors import AuthorizationError

_TOKEN_AUTH_METHODS = frozenset({"none"})
_ACCESS_ID_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,256}$")


def _text(value: Any, *, maximum: int = 4096) -> str:
    candidate = str(value or "").strip()
    return candidate if len(candidate) <= maximum else ""


def _sequence_of_text(value: Any, *, maximum_items: int = 128) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > maximum_items:
        return ()
    values: list[str] = []
    for item in value:
        candidate = _text(item)
        if not candidate:
            return ()
        values.append(candidate)
    return tuple(values)


def _secret_token(value: Any, *, maximum: int = 65536) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if len(candidate) > maximum:
        return ""
    if any(not 0x21 <= ord(character) <= 0x7E for character in candidate):
        return ""
    return candidate


def _scope_value(value: Any, *, code: str, message: str) -> str:
    if value is None:
        candidate = ""
    elif isinstance(value, str):
        candidate = value.strip()
    else:
        raise AuthorizationError(code, message)
    if len(candidate) > 8192 or any(
        not 0x20 <= ord(character) <= 0x7E for character in candidate
    ):
        raise AuthorizationError(code, message)
    return candidate


def _is_loopback(hostname: str) -> bool:
    lowered = hostname.rstrip(".").lower()
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


def validate_web_url(
    value: Any,
    *,
    code: str,
    allow_query: bool = True,
    loopback_only: bool = False,
) -> str:
    raw = _text(value, maximum=8192)
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError:
        raise AuthorizationError(
            code,
            "The OAuth server published an invalid URL.",
        ) from None
    hostname = parsed.hostname or ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (not allow_query and parsed.query)
    ):
        raise AuthorizationError(code, "The OAuth server published an invalid URL.")
    if parsed.scheme.lower() == "http" and not _is_loopback(hostname):
        raise AuthorizationError(
            code,
            "OAuth requires HTTPS except when the server is on this device.",
        )
    if loopback_only and not _is_loopback(hostname):
        raise AuthorizationError(
            code,
            "The OAuth callback must resolve to this device.",
        )
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc,
            parsed.path or "/",
            parsed.query if allow_query else "",
            "",
        )
    )


def validate_resource_identifier(value: Any) -> str:
    raw = _text(value, maximum=8192)
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise AuthorizationError(
            "oauth_resource_invalid",
            "The protected resource identifier is invalid.",
        )
    try:
        parsed = urlsplit(raw)
        _ = parsed.port
    except ValueError:
        raise AuthorizationError(
            "oauth_resource_invalid",
            "The protected resource identifier is invalid.",
        ) from None
    scheme = parsed.scheme.lower()
    if not scheme or parsed.fragment:
        raise AuthorizationError(
            "oauth_resource_invalid",
            "The protected resource identifier is invalid.",
        )
    if scheme in {"http", "https"}:
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise AuthorizationError(
                "oauth_resource_invalid",
                "The protected resource identifier is invalid.",
            )
    elif scheme == "urn":
        if parsed.netloc or parsed.query or not parsed.path or " " in parsed.path:
            raise AuthorizationError(
                "oauth_resource_invalid",
                "The protected resource identifier is invalid.",
            )
    else:
        raise AuthorizationError(
            "oauth_resource_invalid",
            "The protected resource identifier uses an unsupported URI scheme.",
        )
    if scheme == "http" and not _is_loopback(parsed.hostname or ""):
        raise AuthorizationError(
            "oauth_resource_invalid",
            "The protected resource requires HTTPS unless it is on this device.",
        )
    if scheme == "urn":
        return f"urn:{parsed.path}"
    return urlunsplit((scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


def authorization_server_metadata_url(issuer: str) -> str:
    return authorization_server_metadata_urls(issuer)[0]


def authorization_server_metadata_urls(issuer: str) -> tuple[str, ...]:
    parsed = urlsplit(
        validate_web_url(
            issuer,
            code="oauth_authorization_server_invalid",
            allow_query=False,
        )
    )
    issuer_path = parsed.path.rstrip("/")
    appended_path = f"{issuer_path}/.well-known/oauth-authorization-server"
    appended = urlunsplit((parsed.scheme, parsed.netloc, appended_path, "", ""))
    if issuer_path:
        standard_path = f"/.well-known/oauth-authorization-server{issuer_path}"
    else:
        standard_path = "/.well-known/oauth-authorization-server"
    standard = urlunsplit((parsed.scheme, parsed.netloc, standard_path, "", ""))
    return (appended,) if appended == standard else (appended, standard)


@dataclass(frozen=True, slots=True)
class ProtectedResourceMetadata:
    resource: str
    authorization_server: str
    scopes_supported: tuple[str, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        expected_resource: str,
    ) -> ProtectedResourceMetadata:
        if not isinstance(value, Mapping):
            raise AuthorizationError(
                "oauth_resource_metadata_invalid",
                "The protected resource metadata is invalid.",
            )
        resource = validate_resource_identifier(value.get("resource"))
        expected = validate_resource_identifier(expected_resource)
        if resource != expected:
            raise AuthorizationError(
                "oauth_resource_mismatch",
                "The OAuth metadata belongs to a different protected resource.",
            )
        servers = _sequence_of_text(value.get("authorization_servers"))
        if not servers:
            raise AuthorizationError(
                "oauth_authorization_server_missing",
                "The protected resource did not publish an authorization server.",
            )
        if len(servers) != 1:
            raise AuthorizationError(
                "oauth_authorization_server_selection_required",
                "This release requires one authorization server for the selected resource.",
            )
        authorization_server = validate_web_url(
            servers[0],
            code="oauth_authorization_server_invalid",
            allow_query=False,
        ).rstrip("/")
        scopes = _sequence_of_text(value.get("scopes_supported"))
        return cls(
            resource=resource,
            authorization_server=authorization_server,
            scopes_supported=scopes,
        )


@dataclass(frozen=True, slots=True)
class AuthorizationServerMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    revocation_endpoint: str | None
    scopes_supported: tuple[str, ...]
    supports_refresh: bool
    authorization_response_issuer_required: bool
    client_id_metadata_document_supported: bool = False

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        expected_issuer: str,
    ) -> AuthorizationServerMetadata:
        if not isinstance(value, Mapping):
            raise AuthorizationError(
                "oauth_server_metadata_invalid",
                "The authorization server metadata is invalid.",
            )
        issuer = validate_web_url(
            value.get("issuer"),
            code="oauth_authorization_server_invalid",
            allow_query=False,
        ).rstrip("/")
        expected = validate_web_url(
            expected_issuer,
            code="oauth_authorization_server_invalid",
            allow_query=False,
        ).rstrip("/")
        if issuer != expected:
            raise AuthorizationError(
                "oauth_issuer_mismatch",
                "The OAuth metadata issuer does not match the advertised server.",
            )
        grants = _sequence_of_text(value.get("grant_types_supported"))
        responses = _sequence_of_text(value.get("response_types_supported"))
        challenge_methods = _sequence_of_text(
            value.get("code_challenge_methods_supported")
        )
        token_auth = _sequence_of_text(
            value.get("token_endpoint_auth_methods_supported")
        )
        if "authorization_code" not in grants or "code" not in responses:
            raise AuthorizationError(
                "oauth_authorization_code_unsupported",
                "The authorization server does not support authorization code login.",
            )
        if "S256" not in challenge_methods:
            raise AuthorizationError(
                "oauth_pkce_unsupported",
                "The authorization server does not support S256 PKCE.",
            )
        if not _TOKEN_AUTH_METHODS.intersection(token_auth):
            raise AuthorizationError(
                "oauth_public_client_unsupported",
                "The authorization server does not support a public native client.",
            )
        registration_raw = _text(value.get("registration_endpoint"), maximum=8192)
        revocation_raw = _text(value.get("revocation_endpoint"), maximum=8192)
        return cls(
            issuer=issuer,
            authorization_endpoint=validate_web_url(
                value.get("authorization_endpoint"),
                code="oauth_authorization_endpoint_invalid",
            ),
            token_endpoint=validate_web_url(
                value.get("token_endpoint"),
                code="oauth_token_endpoint_invalid",
            ),
            registration_endpoint=(
                validate_web_url(
                    registration_raw,
                    code="oauth_registration_endpoint_invalid",
                )
                if registration_raw
                else None
            ),
            revocation_endpoint=(
                validate_web_url(
                    revocation_raw,
                    code="oauth_revocation_endpoint_invalid",
                )
                if revocation_raw
                else None
            ),
            scopes_supported=_sequence_of_text(value.get("scopes_supported")),
            supports_refresh="refresh_token" in grants,
            authorization_response_issuer_required=bool(
                value.get("authorization_response_iss_parameter_supported")
            ),
            client_id_metadata_document_supported=bool(
                value.get("client_id_metadata_document_supported")
            ),
        )


@dataclass(frozen=True, slots=True)
class OAuthClientRegistration:
    client_id: str
    redirect_uris: tuple[str, ...]
    token_endpoint_auth_method: str = "none"
    source: str = "dcr"
    client_metadata_url: str | None = None

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        expected_redirect_uri: str,
    ) -> OAuthClientRegistration:
        if not isinstance(value, Mapping):
            raise AuthorizationError(
                "oauth_client_registration_invalid",
                "The OAuth client registration response is invalid.",
            )
        client_id = _text(value.get("client_id"), maximum=4096)
        redirects = _sequence_of_text(value.get("redirect_uris"))
        method = _text(value.get("token_endpoint_auth_method")) or "none"
        expected = validate_web_url(
            expected_redirect_uri,
            code="oauth_callback_invalid",
            loopback_only=True,
        )
        if not client_id or not redirects or method != "none":
            raise AuthorizationError(
                "oauth_client_registration_invalid",
                "The authorization server did not register a usable public client.",
            )
        normalized_redirects = tuple(
            validate_web_url(
                item,
                code="oauth_client_registration_invalid",
                loopback_only=True,
            )
            for item in redirects
        )
        if normalized_redirects and expected not in normalized_redirects:
            raise AuthorizationError(
                "oauth_registered_callback_mismatch",
                "The authorization server registered a different callback URL.",
            )
        return cls(
            client_id=client_id,
            redirect_uris=normalized_redirects,
            token_endpoint_auth_method=method,
            source="dcr",
        )


@dataclass(frozen=True, slots=True)
class OAuthTokenSet:
    access_token: str = field(repr=False)
    refresh_token: str = field(default="", repr=False)
    token_type: str = "Bearer"
    expires_at: int = 0
    scope: str = ""
    access_id: str | None = None

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        default_scope: str = "",
        previous_refresh_token: str = "",
        now: int | None = None,
    ) -> OAuthTokenSet:
        if not isinstance(value, Mapping):
            raise AuthorizationError(
                "oauth_token_response_invalid",
                "The authorization server returned an invalid token response.",
            )
        access_token = _secret_token(value.get("access_token"))
        raw_refresh_token = value.get("refresh_token")
        refresh_token = _secret_token(raw_refresh_token)
        previous_refresh = _secret_token(previous_refresh_token)
        token_type = _text(value.get("token_type")) or "Bearer"
        scope = _scope_value(
            value.get("scope"),
            code="oauth_token_response_invalid",
            message="The authorization server returned an invalid token response.",
        ) or _scope_value(
            default_scope,
            code="oauth_token_response_invalid",
            message="The authorization server returned an invalid token response.",
        )
        access_id = _text(value.get("access_id"), maximum=256) or None
        try:
            expires_in = int(value.get("expires_in") or 0)
        except (TypeError, ValueError):
            raise AuthorizationError(
                "oauth_token_response_invalid",
                "The authorization server returned an invalid token response.",
            ) from None
        if (
            not access_token
            or (raw_refresh_token not in (None, "") and not refresh_token)
            or (previous_refresh_token not in (None, "") and not previous_refresh)
            or token_type.lower() != "bearer"
            or expires_in < 0
            or (access_id is not None and not _ACCESS_ID_RE.fullmatch(access_id))
        ):
            raise AuthorizationError(
                "oauth_token_response_invalid",
                "The authorization server returned an invalid token response.",
            )
        issued_at = int(time.time()) if now is None else int(now)
        return cls(
            access_token=access_token,
            refresh_token=refresh_token or previous_refresh,
            token_type="Bearer",
            expires_at=issued_at + expires_in if expires_in else 0,
            scope=scope,
            access_id=access_id,
        )

    def is_expiring(self, *, now: int | None = None, leeway_seconds: int = 60) -> bool:
        if self.expires_at <= 0:
            return False
        moment = int(time.time()) if now is None else int(now)
        return self.expires_at <= moment + max(0, int(leeway_seconds))

    def to_secret_json(self) -> str:
        scope = _scope_value(
            self.scope,
            code="oauth_session_credential_invalid",
            message="The OAuth session credential is invalid.",
        )
        access_id = _text(self.access_id, maximum=256) or None
        if (
            _secret_token(self.access_token) != self.access_token
            or (
                self.refresh_token
                and _secret_token(self.refresh_token) != self.refresh_token
            )
            or not isinstance(self.token_type, str)
            or self.token_type.lower() != "bearer"
            or type(self.expires_at) is not int
            or self.expires_at < 0
            or scope != self.scope
            or access_id != self.access_id
            or (access_id is not None and not _ACCESS_ID_RE.fullmatch(access_id))
        ):
            raise AuthorizationError(
                "oauth_session_credential_invalid",
                "The OAuth session credential is invalid.",
            )
        return json.dumps(
            {
                "schema": "connection_hub_cli.oauth_token.v1",
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "token_type": self.token_type,
                "expires_at": self.expires_at,
                "scope": self.scope,
                "access_id": self.access_id,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    @classmethod
    def from_secret_json(cls, value: str) -> OAuthTokenSet:
        try:
            payload = json.loads(str(value or ""))
        except (TypeError, ValueError):
            raise AuthorizationError(
                "oauth_session_credential_invalid",
                "The stored OAuth session credential is invalid.",
            ) from None
        if not isinstance(payload, Mapping) or payload.get("schema") != (
            "connection_hub_cli.oauth_token.v1"
        ):
            raise AuthorizationError(
                "oauth_session_credential_invalid",
                "The stored OAuth session credential is invalid.",
            )
        access_token = _secret_token(payload.get("access_token"))
        raw_refresh_token = payload.get("refresh_token")
        refresh_token = _secret_token(raw_refresh_token)
        token_type = _text(payload.get("token_type")) or "Bearer"
        scope = _scope_value(
            payload.get("scope"),
            code="oauth_session_credential_invalid",
            message="The stored OAuth session credential is invalid.",
        )
        access_id = _text(payload.get("access_id"), maximum=256) or None
        try:
            expires_at = int(payload.get("expires_at") or 0)
        except (TypeError, ValueError):
            raise AuthorizationError(
                "oauth_session_credential_invalid",
                "The stored OAuth session credential is invalid.",
            ) from None
        if (
            not access_token
            or (raw_refresh_token not in (None, "") and not refresh_token)
            or token_type.lower() != "bearer"
            or expires_at < 0
            or (access_id is not None and not _ACCESS_ID_RE.fullmatch(access_id))
        ):
            raise AuthorizationError(
                "oauth_session_credential_invalid",
                "The stored OAuth session credential is invalid.",
            )
        return cls(
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="Bearer",
            expires_at=expires_at,
            scope=scope,
            access_id=access_id,
        )
