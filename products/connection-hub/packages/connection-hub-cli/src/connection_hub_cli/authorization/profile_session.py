"""OAuth authorization and refresh for governed MCP caller profiles."""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from filelock import AsyncFileLock, Timeout

from connection_hub.delegated_credentials.cards.identity import (
    CARD_KIND_AGENT,
    CARD_KIND_AUTOMATION,
    CARD_KIND_CONNECTOR,
)
from connection_hub.delegated_credentials.oauth.clients import (
    client_uses_full_card_catalog,
)
from connection_hub_cli.authorization.client import OAuthClient
from connection_hub_cli.authorization.device import DeviceAuthorizationFlow, Presenter
from connection_hub_cli.authorization.discovery import (
    McpOAuthEndpointDiscovery,
    OAuthDiscovery,
    OAuthDiscoveryResult,
)
from connection_hub_cli.authorization.flow import BrowserAuthorizationFlow
from connection_hub_cli.authorization.models import (
    OAuthClientRegistration,
    OAuthTokenSet,
)
from connection_hub_cli.errors import AuthorizationError, ProfileError
from connection_hub_cli.models import (
    CallerProfile,
    ProbeResult,
    ProfileOAuthMetadata,
    validate_access_id,
    validate_endpoint,
    validate_name,
)
from connection_hub_cli.state import ProfileStore


class OAuthProfileCredentialStore(Protocol):
    def put(self, credential_ref: str, token: OAuthTokenSet) -> None: ...

    def get(self, credential_ref: str) -> OAuthTokenSet | None: ...

    def remove(self, credential_ref: str) -> bool: ...


Probe = Callable[..., Awaitable[ProbeResult]]
_WHOLE_CARD_KINDS = frozenset({CARD_KIND_AGENT, CARD_KIND_AUTOMATION})


@dataclass(frozen=True, slots=True)
class OAuthProfileAuthorizationResult:
    profile: CallerProfile
    probe: ProbeResult


class OAuthProfileSessionService:
    """Own OAuth profile tokens without returning refresh credentials to callers."""

    def __init__(
        self,
        *,
        profiles: ProfileStore,
        credentials: OAuthProfileCredentialStore,
        endpoint_discovery: McpOAuthEndpointDiscovery,
        discovery: OAuthDiscovery,
        authorization: BrowserAuthorizationFlow,
        device_authorization: DeviceAuthorizationFlow | None = None,
        oauth: OAuthClient,
        probe: Probe,
    ) -> None:
        self._profiles = profiles
        self._credentials = credentials
        self._endpoint_discovery = endpoint_discovery
        self._discovery = discovery
        self._authorization = authorization
        self._device_authorization = device_authorization
        self._oauth = oauth
        self._probe = probe
        self._transaction_lock = profiles.path.with_suffix(
            f"{profiles.path.suffix}.oauth.transaction.lock"
        )

    async def authorize(
        self,
        *,
        name: str,
        endpoint: str,
        scope: str = "",
        default_scope: str = "",
        client_name: str = "Connection Hub CLI",
        client_metadata: Mapping[str, Any] | None = None,
        provisioned_client_id: str | None = None,
        client_metadata_url: str | None = None,
        whole_card: bool | None = None,
        callback_port: int | None = None,
        device: bool = False,
        device_presenter: Presenter | None = None,
        timeout_seconds: float = 300.0,
        browser_opener=None,
    ) -> OAuthProfileAuthorizationResult:
        profile_name = validate_name(name)
        target = validate_endpoint(endpoint)
        async with self._authorization_slot(profile_name):
            self.verify_credential_store()
            located = await self._endpoint_discovery.discover(
                target, default_scope=default_scope
            )
            # An explicit scope is the caller overriding everything, including
            # the server's challenge. default_scope is the weaker statement
            # "these are the claims my operations need", which yields to a
            # challenge and only displaces the deployment's full advertised set.
            selected_scope = str(scope or located.scope).strip()
            discovered = OAuthDiscoveryResult(
                protected_resource=located.protected_resource,
                authorization_server=located.authorization_server,
            )
            use_whole_card = (
                client_uses_full_card_catalog(client_metadata)
                if whole_card is None
                else bool(whole_card)
            )
            authorization_resource = (
                "" if use_whole_card else located.protected_resource.resource
            )
            if device:
                if self._device_authorization is None or device_presenter is None:
                    raise AuthorizationError(
                        "oauth_device_authorization_unavailable",
                        "Device authorization is unavailable in this process.",
                    )
                grant = await self._device_authorization.authorize_discovered(
                    protected_resource_metadata_url=(
                        located.protected_resource_metadata_url
                    ),
                    discovered=discovered,
                    resource=authorization_resource,
                    scope=selected_scope,
                    client_name=client_name,
                    client_metadata=client_metadata,
                    provisioned_client_id=provisioned_client_id,
                    client_metadata_url=client_metadata_url,
                    presenter=device_presenter,
                    timeout_seconds=timeout_seconds,
                )
            else:
                grant = await self._authorization.authorize_discovered(
                    protected_resource_metadata_url=(
                        located.protected_resource_metadata_url
                    ),
                    discovered=discovered,
                    resource=authorization_resource,
                    scope=selected_scope,
                    client_name=client_name,
                    client_metadata=client_metadata,
                    provisioned_client_id=provisioned_client_id,
                    client_metadata_url=client_metadata_url,
                    callback_port=callback_port,
                    timeout_seconds=timeout_seconds,
                    browser_opener=browser_opener,
                )
            try:
                access_id = validate_access_id(grant.token.access_id)
            except ProfileError:
                access_id = None
            if access_id is None:
                await self._revoke_grant(grant)
                raise AuthorizationError(
                    "oauth_access_id_missing",
                    "The OAuth credential is not bound to a delegated caller card.",
                )
            try:
                metadata = self._metadata_from_grant(
                    grant,
                    scope=grant.token.scope or selected_scope,
                )
            except AuthorizationError:
                await self._revoke_grant(grant)
                raise
            profile = CallerProfile.create_oauth(
                name=profile_name,
                endpoint=target,
                access_id=access_id,
                oauth=metadata,
            )
            bound_token = self._token_for_profile(profile, grant.token)
            try:
                probe = await self._probe(
                    endpoint=profile.endpoint,
                    bearer=bound_token.access_token,
                )
                self._credentials.put(profile.credential_ref, bound_token)
                try:
                    self._profiles.add(profile)
                except Exception:
                    self._credentials.remove(profile.credential_ref)
                    raise
            except Exception:
                await self._revoke_grant(grant)
                raise
            return OAuthProfileAuthorizationResult(profile=profile, probe=probe)

    async def reconnect(
        self,
        profile_name: str,
        *,
        callback_port: int | None = None,
        device: bool = False,
        device_presenter: Presenter | None = None,
        timeout_seconds: float = 300.0,
        browser_opener=None,
    ) -> OAuthProfileAuthorizationResult:
        """Replace OAuth custody while preserving the recorded caller Card."""

        name = validate_name(profile_name)
        async with self._authorization_slot(name, require_existing=True):
            self.verify_credential_store()
            profile = self._require_oauth_profile(name)
            metadata = self._require_oauth(profile)
            client_id = str(metadata.client_id or "").strip()
            if not client_id:
                raise AuthorizationError(
                    "oauth_reconnect_client_id_missing",
                    "The OAuth profile needs its recorded client identifier before it can reconnect.",
                )

            located = await self._endpoint_discovery.discover(
                profile.endpoint,
                default_scope=metadata.scope,
            )
            use_whole_card = metadata.card_kind in _WHOLE_CARD_KINDS
            self._verify_reconnect_endpoint(
                profile,
                located,
                compare_resource=not use_whole_card,
            )
            discovered = OAuthDiscoveryResult(
                protected_resource=located.protected_resource,
                authorization_server=located.authorization_server,
            )
            authorization_resource = (
                "" if use_whole_card else located.protected_resource.resource
            )
            if device:
                if self._device_authorization is None or device_presenter is None:
                    raise AuthorizationError(
                        "oauth_device_authorization_unavailable",
                        "Device authorization is unavailable in this process.",
                    )
                grant = await self._device_authorization.authorize_discovered(
                    protected_resource_metadata_url=(
                        located.protected_resource_metadata_url
                    ),
                    discovered=discovered,
                    resource=authorization_resource,
                    scope=metadata.scope,
                    provisioned_client_id=client_id,
                    requested_access_id=profile.access_id,
                    presenter=device_presenter,
                    timeout_seconds=timeout_seconds,
                )
            else:
                grant = await self._authorization.authorize_discovered(
                    protected_resource_metadata_url=(
                        located.protected_resource_metadata_url
                    ),
                    discovered=discovered,
                    resource=authorization_resource,
                    scope=metadata.scope,
                    provisioned_client_id=client_id,
                    callback_port=callback_port,
                    timeout_seconds=timeout_seconds,
                    browser_opener=browser_opener,
                )
            if grant.registration.client_id != client_id:
                await self._revoke_grant(grant)
                raise AuthorizationError(
                    "oauth_reconnect_client_mismatch",
                    "The new OAuth grant used a different client identifier and was revoked.",
                )

            try:
                access_id = validate_access_id(grant.token.access_id)
            except ProfileError:
                access_id = None
            if access_id != profile.access_id:
                await self._revoke_grant(grant)
                error = AuthorizationError(
                    "oauth_reconnect_card_mismatch",
                    "The new OAuth grant belongs to a different caller Card and was revoked.",
                )
                error.details = {
                    "expected_access_id": profile.access_id,
                    "received_access_id": access_id,
                }
                raise error

            replacement = self._token_for_profile(
                profile,
                grant.token,
                require_explicit=True,
            )
            try:
                replacement_metadata = self._metadata_from_grant(
                    grant,
                    scope=grant.token.scope or metadata.scope,
                )
            except AuthorizationError:
                await self._revoke_grant(grant)
                raise
            try:
                committed = await self._commit_reconnected_token(
                    expected=profile,
                    replacement=replacement,
                    replacement_metadata=replacement_metadata,
                )
            except Exception as exc:
                raise self._matching_grant_failure(
                    exc,
                    credential_stored=False,
                ) from exc
            try:
                probe = await self._probe(
                    endpoint=profile.endpoint,
                    bearer=replacement.access_token,
                )
            except Exception as exc:
                raise self._matching_grant_failure(
                    exc,
                    credential_stored=True,
                ) from exc
            return OAuthProfileAuthorizationResult(profile=committed, probe=probe)

    async def access_token(self, profile_name: str) -> str:
        """The profile's current access token, refreshed first when it is expiring.

        The store lock is held only to read and to write. The refresh itself,
        a network round trip to the token endpoint, runs outside it under a
        lock of this profile's own, so one profile's refresh hanging on an
        absent server never makes another profile's read wait (2026-09-23:
        four channels opened together against a runtime still down, the
        first held the store lock through its hung refresh and the other
        three timed out on it). Two callers of the same profile still refresh
        once: the second finds the replacement when it re-reads.
        """

        profile, token = await self._read_token(profile_name)
        if not token.is_expiring(leeway_seconds=60):
            return token.access_token
        async with self._refresh_slot(profile_name):
            profile, token = await self._read_token(profile_name)
            if not token.is_expiring(leeway_seconds=60):
                return token.access_token
            replacement = await self._refresh(profile, token)
            return await self._commit_refreshed_token(profile, replacement)

    async def refresh_access_token(self, profile_name: str) -> str:
        """Mint a new access token now, whatever the local expiry says.

        ``access_token`` refreshes on this side's clock. A server that lost
        the session behind a locally current token (a store restart, an
        eviction) answers 401 to a bearer this clock still trusts, and the
        clock alone would re-present that bearer until its hour lapsed. The
        caller that met the refusal asks for the refresh here, once, with the
        refresh token that proves the card is still the card. A refused
        refresh is then the card's answer and not a stale session's.
        """

        async with self._refresh_slot(profile_name):
            profile, token = await self._read_token(profile_name)
            replacement = await self._refresh(profile, token)
            return await self._commit_refreshed_token(profile, replacement)

    async def _read_token(self, profile_name: str) -> tuple[CallerProfile, OAuthTokenSet]:
        """The profile record and its stored token, read under the store lock."""

        async with self._transaction(self._transaction_lock):
            profile = self._require_oauth_profile(profile_name)
            return profile, self._load_token(profile)

    async def _commit_refreshed_token(
        self,
        profile: CallerProfile,
        replacement: OAuthTokenSet,
    ) -> str:
        """Store a refreshed token under the store lock and return its access token.

        The profile is read again under the lock: a browser authorization or
        a reconnect that completed while the refresh was in flight has
        replaced the credential, and the newer one wins over a refresh of the
        old one.
        """

        async with self._transaction(self._transaction_lock):
            current = self._require_oauth_profile(profile.name)
            if (
                current.credential_ref != profile.credential_ref
                or current.access_id != profile.access_id
            ):
                return self._load_token(current).access_token
            previous = self._credentials.get(current.credential_ref)
            replacement = self._token_for_profile(current, replacement)
            self._replace_token(current, previous, replacement)
            return replacement.access_token

    @asynccontextmanager
    async def _transaction(self, lock_path: Path):
        """The store-wide lock, for a read or a write of profile state, never for I/O."""

        self._prepare_lock(lock_path)
        lock = AsyncFileLock(str(lock_path), timeout=10, mode=0o600)
        try:
            async with lock:
                self._secure_lock(lock_path)
                yield
        except Timeout:
            raise AuthorizationError(
                "oauth_profile_lock_timeout",
                "Timed out waiting for the OAuth profile lock.",
            ) from None

    @asynccontextmanager
    async def _refresh_slot(self, profile_name: str):
        """One refresh at a time per profile, so two callers refresh once."""

        lock_path = self._profiles.path.with_suffix(
            f"{self._profiles.path.suffix}.{profile_name}.oauth.refresh.lock"
        )
        self._prepare_lock(lock_path)
        lock = AsyncFileLock(str(lock_path), timeout=10, mode=0o600)
        try:
            async with lock:
                self._secure_lock(lock_path)
                yield
        except Timeout:
            raise AuthorizationError(
                "oauth_profile_lock_timeout",
                "Timed out waiting for this profile's OAuth refresh lock.",
            ) from None

    async def probe(self, profile_name: str) -> ProbeResult:
        profile = self._require_oauth_profile(profile_name)
        bearer = await self.access_token(profile.name)
        return await self._probe(endpoint=profile.endpoint, bearer=bearer)

    def credential_present(self, profile: CallerProfile) -> bool:
        self._require_oauth(profile)
        return self._credentials.get(profile.credential_ref) is not None

    def credential_status(self, profile: CallerProfile) -> dict[str, object]:
        self._require_oauth(profile)
        token = self._credentials.get(profile.credential_ref)
        if token is None:
            return {
                "credential": "missing",
                "expiry": "unknown",
                "expires_at": None,
                "refresh_ready": False,
            }
        self._token_for_profile(profile, token, require_explicit=True)
        now = int(time.time())
        if token.expires_at <= 0:
            expiry = "not_published"
        elif token.expires_at <= now:
            expiry = "expired"
        elif token.is_expiring(now=now, leeway_seconds=60):
            expiry = "expiring"
        else:
            expiry = "current"
        return {
            "credential": "present",
            "expiry": expiry,
            "expires_at": token.expires_at or None,
            "refresh_ready": bool(token.refresh_token),
        }

    async def revoke(self, profile: CallerProfile) -> None:
        metadata = self._require_oauth(profile)
        token = self._load_token(profile)
        server = await self._discover_server(profile)
        if (
            metadata.revocation_endpoint is None
            or server.revocation_endpoint != metadata.revocation_endpoint
        ):
            raise AuthorizationError(
                "oauth_profile_server_changed",
                "The OAuth revocation service changed; revoke this caller card in Connection Hub.",
            )
        await self._oauth.revoke(
            metadata=server,
            client=self._registration(profile),
            token=token.refresh_token or token.access_token,
            token_type_hint=(
                "refresh_token" if token.refresh_token else "access_token"
            ),
        )

    def remove_local(self, profile: CallerProfile) -> bool:
        self._require_oauth(profile)
        return self._credentials.remove(profile.credential_ref)

    def retire_local(self, profile: CallerProfile) -> CallerProfile:
        """Remove OAuth custody and metadata, restoring custody on state failure."""

        self._require_oauth(profile)
        previous = self._credentials.get(profile.credential_ref)
        if previous is not None:
            self._credentials.remove(profile.credential_ref)
        try:
            return self._profiles.remove(profile.name)
        except Exception:
            if previous is not None:
                try:
                    self._credentials.put(profile.credential_ref, previous)
                except Exception:  # noqa: BLE001 - rollback must contain any store failure
                    raise AuthorizationError(
                        "oauth_profile_store_rollback_failed",
                        "The OAuth profile removal failed and its previous credential could not be restored.",
                    ) from None
            raise

    def verify_credential_store(self) -> None:
        credential_ref = secrets.token_hex(16)
        token = OAuthTokenSet(
            access_token=secrets.token_urlsafe(32),
            access_id=f"credential-store-probe-{secrets.token_hex(8)}",
        )
        self._credentials.put(credential_ref, token)
        try:
            if self._credentials.get(credential_ref) != token:
                raise AuthorizationError(
                    "oauth_profile_store_probe_failed",
                    "The OAuth profile store did not return its disposable check value.",
                )
        finally:
            if not self._credentials.remove(credential_ref):
                raise AuthorizationError(
                    "oauth_profile_store_probe_cleanup_failed",
                    "The disposable OAuth profile value could not be removed.",
                )

    async def _refresh(
        self,
        profile: CallerProfile,
        token: OAuthTokenSet,
    ) -> OAuthTokenSet:
        metadata = self._require_oauth(profile)
        if not token.refresh_token:
            raise AuthorizationError(
                "oauth_profile_login_required",
                "The OAuth profile expired and requires browser authorization again.",
            )
        server = await self._discover_server(profile)
        return await self._oauth.refresh(
            metadata=server,
            client=self._registration(profile),
            resource=(
                None if metadata.card_kind in _WHOLE_CARD_KINDS else metadata.resource
            ),
            refresh_token=token.refresh_token,
            scope=metadata.scope,
        )

    async def _discover_server(self, profile: CallerProfile):
        metadata = self._require_oauth(profile)
        if metadata.protected_resource_metadata_url and metadata.resource:
            discovered = await self._discovery.discover(
                protected_resource_metadata_url=(
                    metadata.protected_resource_metadata_url
                ),
                expected_resource=metadata.resource,
            )
            server = discovered.authorization_server
        else:
            located = await self._endpoint_discovery.discover(
                profile.endpoint,
                default_scope=metadata.scope,
            )
            server = located.authorization_server
        if (
            server.issuer != metadata.issuer
            or server.token_endpoint != metadata.token_endpoint
        ):
            raise AuthorizationError(
                "oauth_profile_server_changed",
                "The OAuth server changed; browser authorization is required again.",
            )
        return server

    @staticmethod
    def _verify_reconnect_endpoint(
        profile: CallerProfile,
        located,
        *,
        compare_resource: bool,
    ) -> None:
        metadata = OAuthProfileSessionService._require_oauth(profile)
        server = located.authorization_server
        if (
            server.issuer != metadata.issuer
            or server.token_endpoint != metadata.token_endpoint
            or server.revocation_endpoint != metadata.revocation_endpoint
            or (
                compare_resource
                and (
                    located.protected_resource_metadata_url
                    != metadata.protected_resource_metadata_url
                    or located.protected_resource.resource != metadata.resource
                )
            )
        ):
            raise AuthorizationError(
                "oauth_profile_server_changed",
                "The OAuth endpoint identity changed; the recorded profile remains unchanged.",
            )

    async def _commit_reconnected_token(
        self,
        *,
        expected: CallerProfile,
        replacement: OAuthTokenSet,
        replacement_metadata: ProfileOAuthMetadata,
    ) -> CallerProfile:
        async with self._transaction(self._transaction_lock):
            current = self._require_oauth_profile(expected.name)
            self._require_same_reconnect_binding(expected, current)
            previous = self._credentials.get(current.credential_ref)
            self._replace_token(
                current,
                previous,
                replacement,
                oauth=replacement_metadata,
            )
            return self._require_oauth_profile(current.name)

    @staticmethod
    def _require_same_reconnect_binding(
        expected: CallerProfile,
        current: CallerProfile,
    ) -> None:
        expected_binding = (
            expected.name,
            expected.endpoint,
            expected.credential_ref,
            expected.access_id,
            expected.auth_type,
            expected.oauth,
            expected.created_at,
        )
        current_binding = (
            current.name,
            current.endpoint,
            current.credential_ref,
            current.access_id,
            current.auth_type,
            current.oauth,
            current.created_at,
        )
        if current_binding != expected_binding:
            raise AuthorizationError(
                "oauth_profile_changed_during_reconnect",
                "The OAuth profile changed during browser authorization; the new grant was not stored.",
            )

    @staticmethod
    def _matching_grant_failure(
        exc: Exception,
        *,
        credential_stored: bool,
    ) -> AuthorizationError:
        if isinstance(exc, AuthorizationError):
            code = exc.code
            message = exc.message.rstrip()
            details = dict(getattr(exc, "details", {}) or {})
        else:
            code = (
                "oauth_reconnect_probe_failed"
                if credential_stored
                else "oauth_reconnect_commit_failed"
            )
            message = (
                "The reconnected OAuth credential could not be checked."
                if credential_stored
                else "The reconnected OAuth credential could not be stored."
            )
            details = {}
        if credential_stored:
            guidance = (
                " The same-Card credential was stored, but its endpoint probe failed."
                " The caller Card remains active; retry the governed operation and do"
                " not replace the Card."
            )
        else:
            guidance = (
                " The matching caller Card was not revoked. Correct the local failure"
                " and retry reconnect; do not replace the Card."
            )
        error = AuthorizationError(code, message + guidance)
        details.update(
            {
                "card_preserved": True,
                "credential_stored": credential_stored,
            }
        )
        error.details = details
        return error

    @staticmethod
    def _metadata_from_grant(
        grant: Any,
        *,
        scope: str,
    ) -> ProfileOAuthMetadata:
        card_kind = str(grant.token.card_kind or "").strip()
        if not card_kind:
            raise AuthorizationError(
                "oauth_card_kind_missing",
                "The OAuth credential did not declare its delegated Card kind.",
            )
        if card_kind not in _WHOLE_CARD_KINDS | {CARD_KIND_CONNECTOR}:
            raise AuthorizationError(
                "oauth_profile_card_kind_invalid",
                "The OAuth credential declared an invalid delegated Card kind.",
            )
        whole_card = card_kind in _WHOLE_CARD_KINDS
        return ProfileOAuthMetadata(
            protected_resource_metadata_url=(
                None if whole_card else grant.protected_resource_metadata_url
            ),
            resource=(
                None if whole_card else grant.discovered.protected_resource.resource
            ),
            issuer=grant.discovered.authorization_server.issuer,
            token_endpoint=grant.discovered.authorization_server.token_endpoint,
            revocation_endpoint=(
                grant.discovered.authorization_server.revocation_endpoint
            ),
            client_id=grant.registration.client_id,
            client_source=grant.registration.source,
            client_metadata_url=grant.registration.client_metadata_url,
            scope=scope,
            card_kind=card_kind,
        )

    @staticmethod
    def _metadata_for_refreshed_token(
        metadata: ProfileOAuthMetadata,
        token: OAuthTokenSet,
    ) -> ProfileOAuthMetadata:
        card_kind = str(token.card_kind or "").strip()
        if not card_kind:
            raise AuthorizationError(
                "oauth_card_kind_missing",
                "The refreshed OAuth credential did not declare its delegated Card kind.",
            )
        if metadata.card_kind != card_kind:
            raise AuthorizationError(
                "oauth_profile_card_kind_mismatch",
                "The refreshed OAuth credential belongs to a different Card kind.",
            )
        if card_kind in _WHOLE_CARD_KINDS:
            return replace(
                metadata,
                protected_resource_metadata_url=None,
                resource=None,
                card_kind=card_kind,
            )
        if card_kind != CARD_KIND_CONNECTOR:
            raise AuthorizationError(
                "oauth_profile_card_kind_invalid",
                "The refreshed OAuth credential declared an invalid Card kind.",
            )
        if not metadata.protected_resource_metadata_url or not metadata.resource:
            raise AuthorizationError(
                "oauth_profile_resource_missing",
                "A connector OAuth profile requires its protected resource.",
            )
        return replace(metadata, card_kind=card_kind)

    def _load_token(self, profile: CallerProfile) -> OAuthTokenSet:
        token = self._credentials.get(profile.credential_ref)
        if token is None:
            raise AuthorizationError(
                "oauth_profile_credential_missing",
                "The OAuth profile credential is missing. Revoke its recorded access_id in Connection Hub before removing local profile state.",
            )
        return self._token_for_profile(profile, token, require_explicit=True)

    def _replace_token(
        self,
        profile: CallerProfile,
        previous: OAuthTokenSet | None,
        replacement: OAuthTokenSet,
        *,
        oauth: ProfileOAuthMetadata | None = None,
    ) -> None:
        current_oauth = self._require_oauth(profile)
        replacement_oauth = oauth or self._metadata_for_refreshed_token(
            current_oauth,
            replacement,
        )
        if (
            current_oauth.card_kind
            and replacement_oauth.card_kind != current_oauth.card_kind
        ):
            raise AuthorizationError(
                "oauth_profile_card_kind_mismatch",
                "The OAuth credential belongs to a different Card kind.",
            )
        self._credentials.put(profile.credential_ref, replacement)
        try:
            self._profiles.update(profile.with_oauth_replaced(replacement_oauth))
        except Exception:
            try:
                if previous is None:
                    self._credentials.remove(profile.credential_ref)
                else:
                    self._credentials.put(profile.credential_ref, previous)
            except Exception:  # noqa: BLE001 - rollback must contain any store failure
                raise AuthorizationError(
                    "oauth_profile_store_rollback_failed",
                    "The OAuth profile metadata update failed and its previous credential could not be restored.",
                ) from None
            raise

    @staticmethod
    def _token_for_profile(
        profile: CallerProfile,
        token: OAuthTokenSet,
        *,
        require_explicit: bool = False,
    ) -> OAuthTokenSet:
        token_access_id = validate_access_id(token.access_id)
        if token_access_id is None:
            if require_explicit:
                raise AuthorizationError(
                    "oauth_profile_access_id_mismatch",
                    "The stored OAuth credential does not match its caller card.",
                )
            return replace(token, access_id=profile.access_id)
        if token_access_id != profile.access_id:
            raise AuthorizationError(
                "oauth_profile_access_id_mismatch",
                "The OAuth credential does not match its caller card.",
            )
        profile_kind = str((profile.oauth.card_kind if profile.oauth else None) or "")
        token_kind = str(token.card_kind or "")
        if profile.auth_type == "oauth" and not token_kind:
            raise AuthorizationError(
                "oauth_card_kind_missing",
                "The OAuth credential does not declare its delegated Card kind.",
            )
        if profile_kind != token_kind:
            raise AuthorizationError(
                "oauth_profile_card_kind_mismatch",
                "The OAuth credential does not match its caller Card kind.",
            )
        return token

    @staticmethod
    def _registration(profile: CallerProfile) -> OAuthClientRegistration:
        metadata = OAuthProfileSessionService._require_oauth(profile)
        return OAuthClientRegistration(
            client_id=metadata.client_id,
            redirect_uris=(),
            source=metadata.client_source,
            client_metadata_url=metadata.client_metadata_url,
        )

    def _require_oauth_profile(self, profile_name: str) -> CallerProfile:
        return self._require_oauth_profile_record(
            self._profiles.require(validate_name(profile_name))
        )

    @staticmethod
    def _require_oauth_profile_record(profile: CallerProfile) -> CallerProfile:
        OAuthProfileSessionService._require_oauth(profile)
        return profile

    @staticmethod
    def _require_oauth(profile: CallerProfile) -> ProfileOAuthMetadata:
        if profile.auth_type != "oauth" or profile.oauth is None:
            raise ProfileError(
                "profile_not_oauth",
                f"Caller profile '{profile.name}' is not OAuth-backed.",
            )
        return profile.oauth

    async def _revoke_grant(self, grant) -> None:
        try:
            await self._oauth.revoke(
                metadata=grant.discovered.authorization_server,
                client=grant.registration,
                token=grant.token.refresh_token or grant.token.access_token,
                token_type_hint=(
                    "refresh_token" if grant.token.refresh_token else "access_token"
                ),
            )
        except Exception:  # noqa: BLE001
            access_id = validate_access_id(grant.token.access_id)
            suffix = f" Recorded access_id: {access_id}." if access_id else ""
            raise AuthorizationError(
                "oauth_profile_cleanup_failed",
                "The OAuth profile could not be stored or revoked; revoke its caller card in Connection Hub."
                + suffix,
            ) from None

    @asynccontextmanager
    async def _authorization_slot(
        self,
        profile_name: str,
        *,
        require_existing: bool = False,
    ):
        lock_path = self._profiles.path.with_suffix(
            f"{self._profiles.path.suffix}.{profile_name}.oauth.authorize.lock"
        )
        self._prepare_lock(lock_path)
        lock = AsyncFileLock(str(lock_path), timeout=10, mode=0o600)
        try:
            async with lock:
                self._secure_lock(lock_path)
                exists = self._profiles.get(profile_name) is not None
                if require_existing and not exists:
                    raise ProfileError(
                        "profile_not_found",
                        f"Caller profile '{profile_name}' does not exist.",
                    )
                if not require_existing and exists:
                    raise ProfileError(
                        "profile_exists",
                        f"Caller profile '{profile_name}' already exists.",
                    )
                yield
        except Timeout:
            raise AuthorizationError(
                "oauth_profile_authorization_in_progress",
                "Another browser authorization is active for this profile.",
            ) from None

    def _prepare_lock(self, lock_path: Path) -> None:
        self._profiles.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if lock_path.is_symlink():
            raise AuthorizationError(
                "oauth_profile_lock_symlink_rejected",
                "Connection Hub refuses to use a symbolic link as a profile lock.",
            )
        try:
            os.chmod(self._profiles.path.parent, 0o700)
        except OSError:
            raise AuthorizationError(
                "oauth_profile_directory_permissions",
                "Connection Hub cannot secure its OAuth profile directory.",
            ) from None

    @staticmethod
    def _secure_lock(lock_path: Path) -> None:
        try:
            os.chmod(lock_path, 0o600)
        except OSError:
            raise AuthorizationError(
                "oauth_profile_lock_failed",
                "Connection Hub cannot secure its OAuth profile lock.",
            ) from None


__all__ = [
    "OAuthProfileAuthorizationResult",
    "OAuthProfileCredentialStore",
    "OAuthProfileSessionService",
]
