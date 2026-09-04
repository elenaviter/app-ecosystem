from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from connection_hub_cli.authorization.discovery import OAuthTransport
from connection_hub_cli.authorization.models import (
    AuthorizationServerMetadata,
    OAuthClientRegistration,
    OAuthTokenSet,
    validate_resource_identifier,
    validate_web_url,
)
from connection_hub_cli.authorization.pkce import PKCEParameters
from connection_hub_cli.errors import AuthorizationError

_RESERVED_AUTHORIZATION_PARAMETERS = frozenset(
    {
        "client_id",
        "code_challenge",
        "code_challenge_method",
        "redirect_uri",
        "resource",
        "response_type",
        "scope",
        "state",
    }
)


def _oauth_value(value: Any, *, maximum: int = 8192) -> str:
    candidate = str(value or "").strip()
    if (
        not candidate
        or len(candidate) > maximum
        or any(not 0x20 <= ord(character) <= 0x7E for character in candidate)
    ):
        raise AuthorizationError(
            "oauth_value_invalid",
            "An OAuth request value is invalid.",
        )
    return candidate


def _append_query(url: str, values: Mapping[str, str]) -> str:
    parsed = urlsplit(url)
    try:
        query = list(
            parse_qsl(
                parsed.query,
                keep_blank_values=True,
                max_num_fields=128,
            )
        )
    except ValueError:
        raise AuthorizationError(
            "oauth_authorization_endpoint_invalid",
            "The OAuth authorization endpoint has an invalid query.",
        ) from None
    if any(key in _RESERVED_AUTHORIZATION_PARAMETERS for key, _value in query):
        raise AuthorizationError(
            "oauth_authorization_endpoint_parameter_conflict",
            "The OAuth authorization endpoint contains a protected request parameter.",
        )
    query.extend(values.items())
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


class OAuthClient:
    def __init__(self, *, transport: OAuthTransport) -> None:
        self._transport = transport

    async def register_native_client(
        self,
        *,
        metadata: AuthorizationServerMetadata,
        redirect_uri: str,
        client_name: str = "Connection Hub CLI",
        provisioned_client_id: str | None = None,
        client_metadata_url: str | None = None,
    ) -> OAuthClientRegistration:
        callback = validate_web_url(
            redirect_uri,
            code="oauth_callback_invalid",
            loopback_only=True,
        )
        if provisioned_client_id:
            return OAuthClientRegistration(
                client_id=_oauth_value(provisioned_client_id, maximum=4096),
                redirect_uris=(callback,),
                source="provisioned",
            )
        if client_metadata_url:
            from mcp.client.auth.oauth2 import is_valid_client_metadata_url

            metadata_url = validate_web_url(
                client_metadata_url,
                code="oauth_client_metadata_url_invalid",
            )
            if not is_valid_client_metadata_url(metadata_url):
                raise AuthorizationError(
                    "oauth_client_metadata_url_invalid",
                    "The OAuth client metadata document URL is invalid.",
                )
            if metadata.client_id_metadata_document_supported:
                return OAuthClientRegistration(
                    client_id=metadata_url,
                    redirect_uris=(callback,),
                    source="cimd",
                    client_metadata_url=metadata_url,
                )
        if not metadata.registration_endpoint:
            raise AuthorizationError(
                "oauth_provisioned_client_required",
                "This authorization server requires a provisioned client identifier.",
            )
        payload = {
            "client_name": _oauth_value(client_name, maximum=160),
            "redirect_uris": [callback],
            "application_type": "native",
            "token_endpoint_auth_method": "none",
            "grant_types": [
                "authorization_code",
                *(["refresh_token"] if metadata.supports_refresh else []),
            ],
            "response_types": ["code"],
        }
        registered = await self._transport.post_json(
            metadata.registration_endpoint,
            payload,
        )
        return OAuthClientRegistration.from_mapping(
            registered,
            expected_redirect_uri=callback,
        )

    def authorization_url(
        self,
        *,
        metadata: AuthorizationServerMetadata,
        client: OAuthClientRegistration,
        redirect_uri: str,
        resource: str,
        pkce: PKCEParameters,
        scope: str = "",
        extra_parameters: Mapping[str, str] | None = None,
    ) -> str:
        callback = validate_web_url(
            redirect_uri,
            code="oauth_callback_invalid",
            loopback_only=True,
        )
        if callback not in client.redirect_uris:
            raise AuthorizationError(
                "oauth_registered_callback_mismatch",
                "The OAuth callback is not registered for this client.",
            )
        values = {
            "response_type": "code",
            "client_id": client.client_id,
            "redirect_uri": callback,
            "state": _oauth_value(pkce.state),
            "code_challenge": _oauth_value(pkce.code_challenge),
            "code_challenge_method": "S256",
            "resource": validate_resource_identifier(resource),
        }
        normalized_scope = str(scope or "").strip()
        if normalized_scope:
            values["scope"] = _oauth_value(normalized_scope)
        for key, value in (extra_parameters or {}).items():
            parameter = _oauth_value(key, maximum=128)
            if parameter in _RESERVED_AUTHORIZATION_PARAMETERS:
                raise AuthorizationError(
                    "oauth_parameter_reserved",
                    "An OAuth extension attempted to replace a protected parameter.",
                )
            values[parameter] = _oauth_value(value)
        return _append_query(metadata.authorization_endpoint, values)

    async def exchange_code(
        self,
        *,
        metadata: AuthorizationServerMetadata,
        client: OAuthClientRegistration,
        redirect_uri: str,
        resource: str,
        code: str,
        code_verifier: str,
        scope: str = "",
        now: int | None = None,
    ) -> OAuthTokenSet:
        payload = {
            "grant_type": "authorization_code",
            "code": _oauth_value(code),
            "redirect_uri": validate_web_url(
                redirect_uri,
                code="oauth_callback_invalid",
                loopback_only=True,
            ),
            "client_id": client.client_id,
            "code_verifier": _oauth_value(code_verifier),
            "resource": validate_resource_identifier(resource),
        }
        response = await self._transport.post_form(metadata.token_endpoint, payload)
        return OAuthTokenSet.from_mapping(
            response,
            default_scope=scope,
            now=now,
        )

    async def refresh(
        self,
        *,
        metadata: AuthorizationServerMetadata,
        client: OAuthClientRegistration,
        resource: str,
        refresh_token: str,
        scope: str = "",
        now: int | None = None,
    ) -> OAuthTokenSet:
        if not metadata.supports_refresh:
            raise AuthorizationError(
                "oauth_refresh_unsupported",
                "The authorization server does not support session refresh.",
            )
        current_refresh = _oauth_value(refresh_token, maximum=65536)
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": current_refresh,
            "client_id": client.client_id,
            "resource": validate_resource_identifier(resource),
        }
        normalized_scope = str(scope or "").strip()
        if normalized_scope:
            payload["scope"] = _oauth_value(normalized_scope)
        response = await self._transport.post_form(metadata.token_endpoint, payload)
        return OAuthTokenSet.from_mapping(
            response,
            default_scope=normalized_scope,
            previous_refresh_token=current_refresh,
            now=now,
        )

    async def revoke(
        self,
        *,
        metadata: AuthorizationServerMetadata,
        client: OAuthClientRegistration,
        token: str,
        token_type_hint: str,
    ) -> None:
        if not metadata.revocation_endpoint:
            raise AuthorizationError(
                "oauth_revocation_unsupported",
                "The authorization server does not publish token revocation.",
            )
        if token_type_hint not in {"access_token", "refresh_token"}:
            raise AuthorizationError(
                "oauth_revocation_hint_invalid",
                "The OAuth revocation token type is invalid.",
            )
        await self._transport.post_form(
            metadata.revocation_endpoint,
            {
                "token": _oauth_value(token, maximum=65536),
                "token_type_hint": token_type_hint,
                "client_id": client.client_id,
            },
        )
