from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from connection_hub.delegated_credentials.oauth.device import DEVICE_GRANT_TYPE
from connection_hub.caller.authorization.client import OAuthClient
from connection_hub.caller.authorization.discovery import OAuthDiscoveryResult
from connection_hub.caller.authorization.models import (
    OAuthClientRegistration,
    OAuthTokenSet,
    validate_web_url,
)
from connection_hub.caller.errors import AuthorizationError


Sleep = Callable[[float], Awaitable[None]]
Presenter = Callable[["DeviceAuthorizationPrompt"], None]


def _required_text(value: Any, *, code: str, maximum: int = 8192) -> str:
    candidate = str(value or "").strip()
    if (
        not candidate
        or len(candidate) > maximum
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in candidate)
    ):
        raise AuthorizationError(code, "The device authorization response is invalid.")
    return candidate


def _bounded_positive_int(
    value: Any,
    *,
    code: str,
    default: int | None = None,
    maximum: int = 86_400,
) -> int:
    if value is None and default is not None:
        return default
    if isinstance(value, bool):
        raise AuthorizationError(code, "The device authorization response is invalid.")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise AuthorizationError(
            code, "The device authorization response is invalid."
        ) from None
    if result < 1 or result > maximum:
        raise AuthorizationError(code, "The device authorization response is invalid.")
    return result


@dataclass(frozen=True, slots=True)
class DeviceAuthorizationPrompt:
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: int
    interval: int
    device_code: str = field(repr=False)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DeviceAuthorizationPrompt":
        if not isinstance(value, Mapping):
            raise AuthorizationError(
                "oauth_device_response_invalid",
                "The device authorization response is invalid.",
            )
        complete_raw = str(value.get("verification_uri_complete") or "").strip()
        return cls(
            device_code=_required_text(
                value.get("device_code"),
                code="oauth_device_code_missing",
                maximum=65536,
            ),
            user_code=_required_text(
                value.get("user_code"),
                code="oauth_user_code_missing",
                maximum=128,
            ),
            verification_uri=validate_web_url(
                value.get("verification_uri"),
                code="oauth_verification_uri_invalid",
            ),
            verification_uri_complete=(
                validate_web_url(
                    complete_raw,
                    code="oauth_verification_uri_invalid",
                )
                if complete_raw
                else None
            ),
            expires_in=_bounded_positive_int(
                value.get("expires_in"), code="oauth_device_expiry_invalid"
            ),
            interval=_bounded_positive_int(
                value.get("interval"),
                code="oauth_device_interval_invalid",
                default=5,
                maximum=300,
            ),
        )


@dataclass(frozen=True, slots=True)
class DeviceAuthorizationGrant:
    protected_resource_metadata_url: str
    discovered: OAuthDiscoveryResult
    registration: OAuthClientRegistration
    token: OAuthTokenSet


class DeviceAuthorizationFlow:
    def __init__(
        self,
        *,
        client: OAuthClient,
        sleep: Sleep = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._sleep = sleep
        self._monotonic = monotonic

    async def authorize_discovered(
        self,
        *,
        protected_resource_metadata_url: str,
        discovered: OAuthDiscoveryResult,
        resource: str,
        scope: str = "",
        client_name: str = "Connection Hub CLI",
        client_metadata: Mapping[str, Any] | None = None,
        provisioned_client_id: str | None = None,
        client_metadata_url: str | None = None,
        requested_access_id: str = "",
        expected_card_revision: int | None = None,
        presenter: Presenter,
        timeout_seconds: float | None = None,
    ) -> DeviceAuthorizationGrant:
        server = discovered.authorization_server
        if (
            not server.supports_device_authorization
            or not server.device_authorization_endpoint
        ):
            raise AuthorizationError(
                "oauth_device_authorization_unsupported",
                "The authorization server does not support device login.",
            )
        registration = await self._client.register_device_client(
            metadata=server,
            client_name=client_name,
            client_metadata=client_metadata,
            provisioned_client_id=provisioned_client_id,
            client_metadata_url=client_metadata_url,
        )
        prompt = await self._client.request_device_authorization(
            metadata=server,
            client=registration,
            resource=resource,
            scope=scope,
            requested_access_id=requested_access_id,
            expected_card_revision=expected_card_revision,
        )
        presenter(prompt)
        interval = prompt.interval
        deadline = self._monotonic() + prompt.expires_in
        if timeout_seconds is not None:
            deadline = min(deadline, self._monotonic() + max(1.0, timeout_seconds))
        while True:
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise AuthorizationError(
                    "oauth_device_expired",
                    "The device authorization request expired before approval.",
                )
            await self._sleep(min(float(interval), remaining))
            if self._monotonic() >= deadline:
                raise AuthorizationError(
                    "oauth_device_expired",
                    "The device authorization request expired before approval.",
                )
            try:
                token = await self._client.exchange_device_code(
                    metadata=server,
                    client=registration,
                    device_code=prompt.device_code,
                    scope=scope,
                )
            except AuthorizationError as exc:
                oauth_error = str(getattr(exc, "details", {}).get("oauth_error") or "")
                if oauth_error == "authorization_pending":
                    continue
                if oauth_error == "slow_down":
                    interval += 5
                    continue
                code = {
                    "access_denied": "oauth_device_access_denied",
                    "expired_token": "oauth_device_expired",
                    "device_code_replayed": "oauth_device_replayed",
                    "device_client_mismatch": "oauth_device_client_mismatch",
                    "device_card_mismatch": "oauth_device_card_mismatch",
                    "device_card_revision_conflict": "oauth_device_card_revision_conflict",
                    "device_authorization_restart_required": (
                        "oauth_device_authorization_restart_required"
                    ),
                }.get(oauth_error)
                if code:
                    raise AuthorizationError(
                        code,
                        (
                            "Token issuance failed after approval; restart device "
                            "authorization."
                            if oauth_error == "device_authorization_restart_required"
                            else "The device authorization request was refused."
                        ),
                    ) from None
                raise
            return DeviceAuthorizationGrant(
                protected_resource_metadata_url=protected_resource_metadata_url,
                discovered=discovered,
                registration=registration,
                token=token,
            )


__all__ = [
    "DEVICE_GRANT_TYPE",
    "DeviceAuthorizationFlow",
    "DeviceAuthorizationGrant",
    "DeviceAuthorizationPrompt",
]
