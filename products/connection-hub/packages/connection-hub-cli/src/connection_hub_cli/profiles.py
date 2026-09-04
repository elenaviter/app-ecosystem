from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from connection_hub_cli.credentials import CredentialStore, normalize_bearer
from connection_hub_cli.errors import CredentialError, ProfileError
from connection_hub_cli.models import CallerProfile, ManagedInstallation, ProbeResult
from connection_hub_cli.state import InstallationStore, ProfileStore

if TYPE_CHECKING:
    from connection_hub_cli.authorization.profile_session import (
        OAuthProfileSessionService,
    )

Probe = Callable[..., Awaitable[ProbeResult]]


@dataclass(frozen=True, slots=True)
class ProfileRemoval:
    profile: CallerProfile
    dangling_installations: int


class ProfileService:
    def __init__(
        self,
        *,
        profiles: ProfileStore,
        installations: InstallationStore,
        credentials: CredentialStore,
        probe: Probe,
        oauth_sessions: OAuthProfileSessionService | None = None,
    ) -> None:
        self.profiles = profiles
        self.installations = installations
        self.credentials = credentials
        self.probe = probe
        self.oauth_sessions = oauth_sessions

    async def add(
        self,
        *,
        name: str,
        endpoint: str,
        bearer: str,
        access_id: str | None = None,
    ) -> tuple[CallerProfile, ProbeResult]:
        if self.profiles.get(name) is not None:
            raise ProfileError(
                "profile_exists", f"Caller profile '{name}' already exists."
            )
        profile = CallerProfile.create(
            name=name, endpoint=endpoint, access_id=access_id
        )
        candidate = normalize_bearer(bearer)
        probe_result = await self.probe(endpoint=profile.endpoint, bearer=candidate)
        self.credentials.put(profile.credential_ref, candidate)
        try:
            self.profiles.add(profile)
        except Exception:
            try:
                self.credentials.remove(profile.credential_ref)
            except CredentialError:
                raise CredentialError(
                    "credential_cleanup_failed",
                    "Profile metadata could not be stored and its candidate native credential-store value could not be removed.",
                ) from None
            raise
        return profile, probe_result

    async def replace_credential(
        self,
        *,
        name: str,
        bearer: str,
    ) -> tuple[CallerProfile, ProbeResult]:
        profile = self.profiles.require(name)
        if profile.auth_type != "static_bearer":
            raise ProfileError(
                "profile_credential_managed_by_oauth",
                f"Caller profile '{name}' refreshes its credential through OAuth; authorize it again instead of replacing a bearer.",
            )
        candidate = normalize_bearer(bearer)
        probe_result = await self.probe(endpoint=profile.endpoint, bearer=candidate)
        previous = self.credentials.get(profile.credential_ref)
        self.credentials.put(profile.credential_ref, candidate)
        updated = profile.with_credential_replaced()
        try:
            self.profiles.update(updated)
        except Exception:
            try:
                if previous is None:
                    self.credentials.remove(profile.credential_ref)
                else:
                    self.credentials.put(profile.credential_ref, previous)
            except CredentialError:
                raise CredentialError(
                    "credential_rollback_failed",
                    "The profile metadata update failed and the previous native credential-store value could not be restored.",
                ) from None
            raise
        return updated, probe_result

    async def probe_profile(self, name: str) -> ProbeResult:
        profile = self.profiles.require(name)
        if profile.auth_type == "oauth":
            return await self._oauth_service().probe(profile.name)
        bearer = self.credentials.get(profile.credential_ref)
        if bearer is None:
            raise CredentialError(
                "credential_missing",
                f"Caller profile '{name}' has no credential in the operating-system credential store.",
            )
        return await self.probe(endpoint=profile.endpoint, bearer=bearer)

    def credential_present(self, profile: CallerProfile) -> bool:
        if profile.auth_type == "oauth":
            return self._oauth_service().credential_present(profile)
        return self.credentials.get(profile.credential_ref) is not None

    def remove(
        self,
        name: str,
        *,
        force: bool = False,
        server_card_revoked: bool = False,
        access_id: str | None = None,
    ) -> ProfileRemoval:
        profile = self.profiles.require(name)
        installed = self._installations_for_removal(profile, force=force)
        if profile.auth_type == "oauth":
            if not server_card_revoked:
                raise ProfileError(
                    "oauth_profile_server_revocation_required",
                    "Revoke this OAuth profile with 'profile disconnect'. If its local credential is unavailable, revoke the recorded access_id in Connection Hub, then repeat 'profile remove' with --server-card-revoked and --access-id.",
                )
            if not profile.access_id or access_id != profile.access_id:
                raise ProfileError(
                    "oauth_profile_access_id_confirmation_required",
                    "Local OAuth profile removal requires its exact recorded access_id.",
                )
            removed = self._oauth_service().retire_local(profile)
            return ProfileRemoval(
                profile=removed,
                dangling_installations=len(installed),
            )
        previous = self.credentials.get(profile.credential_ref)
        self.credentials.remove(profile.credential_ref)
        try:
            removed = self.profiles.remove(profile.name)
        except Exception:
            if previous is not None:
                try:
                    self.credentials.put(profile.credential_ref, previous)
                except CredentialError:
                    raise CredentialError(
                        "credential_rollback_failed",
                        "The local profile removal failed and its native credential-store value could not be restored.",
                    ) from None
            raise
        return ProfileRemoval(profile=removed, dangling_installations=len(installed))

    async def disconnect(self, name: str, *, force: bool = False) -> ProfileRemoval:
        profile = self.profiles.require(name)
        installed = self._installations_for_removal(profile, force=force)
        oauth = self._oauth_service()
        await oauth.revoke(profile)
        removed = oauth.retire_local(profile)
        return ProfileRemoval(
            profile=removed,
            dangling_installations=len(installed),
        )

    def _installations_for_removal(
        self,
        profile: CallerProfile,
        *,
        force: bool,
    ) -> list[ManagedInstallation]:
        installed = self.installations.for_profile(profile.name)
        if installed and not force:
            clients = ", ".join(sorted(item.client for item in installed))
            raise ProfileError(
                "profile_in_use",
                f"Caller profile '{profile.name}' is still installed for: {clients}. Remove those client entries first or use --force.",
            )
        return installed

    def _oauth_service(self) -> OAuthProfileSessionService:
        if self.oauth_sessions is None:
            raise ProfileError(
                "oauth_profiles_unavailable",
                "OAuth-backed caller profiles are unavailable in this process.",
            )
        return self.oauth_sessions
