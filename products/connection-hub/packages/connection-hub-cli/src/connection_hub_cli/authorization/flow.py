from __future__ import annotations

import asyncio
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial

from connection_hub_cli.authorization.callback import LoopbackCallbackServer
from connection_hub_cli.authorization.client import OAuthClient
from connection_hub_cli.authorization.discovery import OAuthDiscovery
from connection_hub_cli.authorization.pkce import generate_pkce
from connection_hub_cli.authorization.session import (
    OAuthSessionRecord,
    OAuthSessionRepository,
)
from connection_hub_cli.errors import AuthorizationError

BrowserOpener = Callable[[str], bool]
CallbackFactory = Callable[..., LoopbackCallbackServer]


@dataclass(frozen=True, slots=True)
class BrowserAuthorizationResult:
    session: OAuthSessionRecord


class BrowserAuthorizationFlow:
    def __init__(
        self,
        *,
        discovery: OAuthDiscovery,
        client: OAuthClient,
        sessions: OAuthSessionRepository,
        browser_opener: BrowserOpener = webbrowser.open,
        callback_factory: CallbackFactory = LoopbackCallbackServer,
    ) -> None:
        self._discovery = discovery
        self._client = client
        self._sessions = sessions
        self._browser_opener = browser_opener
        self._callback_factory = callback_factory

    async def authorize_and_store(
        self,
        *,
        target_key: str,
        protected_resource_metadata_url: str,
        resource: str,
        scope: str = "",
        provisioned_client_id: str | None = None,
        authorization_parameters: Mapping[str, str] | None = None,
        timeout_seconds: float = 300.0,
        browser_opener: BrowserOpener | None = None,
    ) -> BrowserAuthorizationResult:
        async with self._sessions.authorization_slot(target_key):
            return await self._authorize_and_store(
                target_key=target_key,
                protected_resource_metadata_url=protected_resource_metadata_url,
                resource=resource,
                scope=scope,
                provisioned_client_id=provisioned_client_id,
                authorization_parameters=authorization_parameters,
                timeout_seconds=timeout_seconds,
                browser_opener=browser_opener,
            )

    async def _authorize_and_store(
        self,
        *,
        target_key: str,
        protected_resource_metadata_url: str,
        resource: str,
        scope: str = "",
        provisioned_client_id: str | None = None,
        authorization_parameters: Mapping[str, str] | None = None,
        timeout_seconds: float = 300.0,
        browser_opener: BrowserOpener | None = None,
    ) -> BrowserAuthorizationResult:
        self._sessions.verify_credential_store()
        discovered = await self._discovery.discover(
            protected_resource_metadata_url=protected_resource_metadata_url,
            expected_resource=resource,
        )
        server = discovered.authorization_server
        if not server.revocation_endpoint:
            raise AuthorizationError(
                "oauth_revocation_unsupported",
                "This KDCube authorization server does not publish token revocation.",
            )
        pkce = generate_pkce()
        callback = self._callback_factory(
            expected_state=pkce.state,
            expected_issuer=server.issuer,
            issuer_required=server.authorization_response_issuer_required,
        )
        try:
            registration = await self._client.register_native_client(
                metadata=server,
                redirect_uri=callback.redirect_uri,
                provisioned_client_id=provisioned_client_id,
            )
            authorization_url = self._client.authorization_url(
                metadata=server,
                client=registration,
                redirect_uri=callback.redirect_uri,
                resource=discovered.protected_resource.resource,
                pkce=pkce,
                scope=scope,
                extra_parameters=authorization_parameters,
            )
            try:
                opener = (
                    browser_opener
                    if browser_opener is not None
                    else self._browser_opener
                )
                opened = bool(opener(authorization_url))
            except Exception:  # noqa: BLE001
                raise AuthorizationError(
                    "oauth_browser_open_failed",
                    "Connection Hub CLI could not open the authorization page.",
                ) from None
            if not opened:
                raise AuthorizationError(
                    "oauth_browser_open_failed",
                    "The browser did not accept the authorization page.",
                )
            callback_result = await asyncio.to_thread(
                partial(callback.wait, timeout_seconds=timeout_seconds)
            )
            token = await self._client.exchange_code(
                metadata=server,
                client=registration,
                redirect_uri=callback.redirect_uri,
                resource=discovered.protected_resource.resource,
                code=callback_result.code,
                code_verifier=pkce.code_verifier,
                scope=scope,
            )
            try:
                session = OAuthSessionRecord.create(
                    target_key=target_key,
                    resource_metadata_url=protected_resource_metadata_url,
                    resource=discovered.protected_resource.resource,
                    issuer=server.issuer,
                    token_endpoint=server.token_endpoint,
                    revocation_endpoint=server.revocation_endpoint,
                    client_id=registration.client_id,
                    scope=scope,
                    token=token,
                )
                self._sessions.create(session, token)
            except Exception:
                try:
                    await self._client.revoke(
                        metadata=server,
                        client=registration,
                        token=token.refresh_token or token.access_token,
                        token_type_hint=(
                            "refresh_token" if token.refresh_token else "access_token"
                        ),
                    )
                except Exception:  # noqa: BLE001
                    raise AuthorizationError(
                        "oauth_session_cleanup_failed",
                        "The local OAuth session could not be stored or revoked; revoke the new caller card in Connection Hub.",
                    ) from None
                raise
            return BrowserAuthorizationResult(session=session)
        finally:
            callback.close()
