from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from connection_hub_cli.credentials import CredentialStore, normalize_bearer
from connection_hub_cli.errors import CredentialError, ProfileError
from connection_hub_cli.models import CallerProfile, ProbeResult
from connection_hub_cli.state import InstallationStore, ProfileStore

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
    ) -> None:
        self.profiles = profiles
        self.installations = installations
        self.credentials = credentials
        self.probe = probe

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
                pass
            raise
        return profile, probe_result

    async def replace_credential(
        self,
        *,
        name: str,
        bearer: str,
    ) -> tuple[CallerProfile, ProbeResult]:
        profile = self.profiles.require(name)
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
            except CredentialError as rollback_exc:
                raise CredentialError(
                    "credential_rollback_failed",
                    "The profile metadata update failed and the previous Keychain credential could not be restored.",
                ) from rollback_exc
            raise
        return updated, probe_result

    async def probe_profile(self, name: str) -> ProbeResult:
        profile = self.profiles.require(name)
        bearer = self.credentials.get(profile.credential_ref)
        if bearer is None:
            raise CredentialError(
                "credential_missing",
                f"Caller profile '{name}' has no credential in macOS Keychain.",
            )
        return await self.probe(endpoint=profile.endpoint, bearer=bearer)

    def credential_present(self, profile: CallerProfile) -> bool:
        return self.credentials.get(profile.credential_ref) is not None

    def remove(self, name: str, *, force: bool = False) -> ProfileRemoval:
        profile = self.profiles.require(name)
        installed = self.installations.for_profile(profile.name)
        if installed and not force:
            clients = ", ".join(sorted(item.client for item in installed))
            raise ProfileError(
                "profile_in_use",
                f"Caller profile '{name}' is still installed for: {clients}. Remove those client entries first or use --force.",
            )
        previous = self.credentials.get(profile.credential_ref)
        self.credentials.remove(profile.credential_ref)
        try:
            removed = self.profiles.remove(profile.name)
        except Exception:
            if previous is not None:
                try:
                    self.credentials.put(profile.credential_ref, previous)
                except CredentialError as rollback_exc:
                    raise CredentialError(
                        "credential_rollback_failed",
                        "The local profile removal failed and its Keychain credential could not be restored.",
                    ) from rollback_exc
            raise
        return ProfileRemoval(profile=removed, dangling_installations=len(installed))
