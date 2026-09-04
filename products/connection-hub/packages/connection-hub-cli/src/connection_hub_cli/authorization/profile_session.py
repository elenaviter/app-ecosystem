"""OAuth authorization and refresh for governed MCP caller profiles."""

from __future__ import annotations

import os
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from filelock import AsyncFileLock, Timeout

from connection_hub_cli.authorization.client import OAuthClient
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
        oauth: OAuthClient,
        probe: Probe,
    ) -> None:
        self._profiles = profiles
        self._credentials = credentials
        self._endpoint_discovery = endpoint_discovery
        self._discovery = discovery
        self._authorization = authorization
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
        provisioned_client_id: str | None = None,
        client_metadata_url: str | None = None,
        callback_port: int | None = None,
        timeout_seconds: float = 300.0,
        browser_opener=None,
    ) -> OAuthProfileAuthorizationResult:
        profile_name = validate_name(name)
        target = validate_endpoint(endpoint)
        async with self._authorization_slot(profile_name):
            self.verify_credential_store()
            located = await self._endpoint_discovery.discover(target)
            selected_scope = str(scope or located.scope).strip()
            discovered = OAuthDiscoveryResult(
                protected_resource=located.protected_resource,
                authorization_server=located.authorization_server,
            )
            grant = await self._authorization.authorize_discovered(
                protected_resource_metadata_url=(
                    located.protected_resource_metadata_url
                ),
                discovered=discovered,
                scope=selected_scope,
                provisioned_client_id=provisioned_client_id,
                client_metadata_url=client_metadata_url,
                callback_port=callback_port,
                timeout_seconds=timeout_seconds,
                browser_opener=browser_opener,
            )
            access_id = validate_access_id(grant.token.access_id)
            if access_id is None:
                await self._revoke_grant(grant)
                raise AuthorizationError(
                    "oauth_access_id_missing",
                    "The OAuth credential is not bound to a delegated caller card.",
                )
            metadata = ProfileOAuthMetadata(
                protected_resource_metadata_url=(grant.protected_resource_metadata_url),
                resource=grant.discovered.protected_resource.resource,
                issuer=grant.discovered.authorization_server.issuer,
                token_endpoint=(grant.discovered.authorization_server.token_endpoint),
                revocation_endpoint=(
                    grant.discovered.authorization_server.revocation_endpoint
                ),
                client_id=grant.registration.client_id,
                client_source=grant.registration.source,
                client_metadata_url=grant.registration.client_metadata_url,
                scope=grant.token.scope or selected_scope,
            )
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

    async def access_token(self, profile_name: str) -> str:
        self._prepare_lock(self._transaction_lock)
        lock = AsyncFileLock(str(self._transaction_lock), timeout=10, mode=0o600)
        try:
            async with lock:
                self._secure_lock(self._transaction_lock)
                profile = self._require_oauth_profile(profile_name)
                token = self._load_token(profile)
                if token.is_expiring(leeway_seconds=60):
                    replacement = await self._refresh(profile, token)
                    replacement = self._token_for_profile(profile, replacement)
                    self._replace_token(profile, token, replacement)
                    token = replacement
                return token.access_token
        except Timeout:
            raise AuthorizationError(
                "oauth_profile_lock_timeout",
                "Timed out waiting for the OAuth profile lock.",
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
            resource=metadata.resource,
            refresh_token=token.refresh_token,
            scope=metadata.scope,
        )

    async def _discover_server(self, profile: CallerProfile):
        metadata = self._require_oauth(profile)
        discovered = await self._discovery.discover(
            protected_resource_metadata_url=(metadata.protected_resource_metadata_url),
            expected_resource=metadata.resource,
        )
        server = discovered.authorization_server
        if (
            server.issuer != metadata.issuer
            or server.token_endpoint != metadata.token_endpoint
        ):
            raise AuthorizationError(
                "oauth_profile_server_changed",
                "The OAuth server changed; browser authorization is required again.",
            )
        return server

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
        previous: OAuthTokenSet,
        replacement: OAuthTokenSet,
    ) -> None:
        self._credentials.put(profile.credential_ref, replacement)
        try:
            self._profiles.update(profile.with_credential_replaced())
        except Exception:
            try:
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
    async def _authorization_slot(self, profile_name: str):
        lock_path = self._profiles.path.with_suffix(
            f"{self._profiles.path.suffix}.{profile_name}.oauth.authorize.lock"
        )
        self._prepare_lock(lock_path)
        lock = AsyncFileLock(str(lock_path), timeout=10, mode=0o600)
        try:
            async with lock:
                self._secure_lock(lock_path)
                if self._profiles.get(profile_name) is not None:
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
