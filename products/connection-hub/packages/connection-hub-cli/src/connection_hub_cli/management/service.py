from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from kdcube_cli.management.client import ManagementClient
from kdcube_cli.management.models import (
    ConsentRecovery,
    ManagementDenial,
    ManagementRequest,
    ManagementResult,
)

from connection_hub_cli.authorization.client import OAuthClient
from connection_hub_cli.authorization.discovery import OAuthDiscovery
from connection_hub_cli.authorization.models import (
    AuthorizationServerMetadata,
    OAuthClientRegistration,
    OAuthTokenSet,
)
from connection_hub_cli.authorization.session import (
    OAuthSessionRecord,
    OAuthSessionRepository,
    session_id_for_target,
)
from connection_hub_cli.errors import AuthorizationError

ConsentHandler = Callable[[ConsentRecovery], Awaitable[None]]


class AuthorizedManagementService:
    def __init__(
        self,
        *,
        sessions: OAuthSessionRepository,
        discovery: OAuthDiscovery,
        oauth: OAuthClient,
        management: ManagementClient,
    ) -> None:
        self._sessions = sessions
        self._discovery = discovery
        self._oauth = oauth
        self._management = management

    async def execute(
        self,
        request: ManagementRequest,
    ) -> ManagementResult | ManagementDenial:
        expected_session_id = session_id_for_target(request.target_key)
        _record, token = await self._sessions.refresh_if_expiring(
            expected_session_id,
            refresher=self._refresh,
        )
        return await self._management.execute(
            request,
            bearer=token.access_token,
        )

    async def execute_with_consent(
        self,
        request: ManagementRequest,
        *,
        consent_handler: ConsentHandler,
    ) -> ManagementResult | ManagementDenial:
        result = await self.execute(request)
        if not isinstance(result, ManagementDenial) or result.recovery is None:
            return result
        if result.recovery.expires_at <= int(time.time()):
            raise AuthorizationError(
                "management_recovery_expired",
                "The management approval request has expired.",
            )
        await consent_handler(result.recovery)
        return await self.execute(request)

    async def disconnect(self, target_key: str) -> OAuthSessionRecord:
        session_id = session_id_for_target(target_key)
        record, token = self._sessions.load(session_id)
        server = await self._discover_server(record)
        if (
            record.revocation_endpoint is None
            or server.revocation_endpoint != record.revocation_endpoint
        ):
            raise AuthorizationError(
                "oauth_session_server_changed",
                "The OAuth revocation service changed; revoke this card in Connection Hub.",
            )
        revoke_token = token.refresh_token or token.access_token
        token_type_hint = "refresh_token" if token.refresh_token else "access_token"
        await self._oauth.revoke(
            metadata=server,
            client=OAuthClientRegistration(
                client_id=record.client_id,
                redirect_uris=(),
            ),
            token=revoke_token,
            token_type_hint=token_type_hint,
        )
        return self._sessions.remove(session_id)

    async def _refresh(
        self,
        record: OAuthSessionRecord,
        token: OAuthTokenSet,
    ) -> OAuthTokenSet:
        if not token.refresh_token:
            raise AuthorizationError(
                "oauth_session_login_required",
                "The OAuth session expired and requires browser login.",
            )
        server = await self._discover_server(record)
        return await self._oauth.refresh(
            metadata=server,
            client=OAuthClientRegistration(
                client_id=record.client_id,
                redirect_uris=(),
            ),
            resource=record.resource,
            refresh_token=token.refresh_token,
            scope=record.scope,
        )

    async def _discover_server(
        self,
        record: OAuthSessionRecord,
    ) -> AuthorizationServerMetadata:
        discovered = await self._discovery.discover(
            protected_resource_metadata_url=record.resource_metadata_url,
            expected_resource=record.resource,
        )
        server = discovered.authorization_server
        if (
            server.issuer != record.issuer
            or server.token_endpoint != record.token_endpoint
        ):
            raise AuthorizationError(
                "oauth_session_server_changed",
                "The OAuth server changed; browser login is required again.",
            )
        return server
