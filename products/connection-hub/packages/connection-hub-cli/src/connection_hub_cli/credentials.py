from __future__ import annotations

import platform
import secrets
from typing import Protocol

import keyring
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

from connection_hub_cli.errors import CredentialError

KEYRING_SERVICE = "tech.kdcube.connection-hub.delegated-caller"
KEYRING_HEALTH_ACCOUNT = "health-check"


class CredentialStore(Protocol):
    def put(self, credential_ref: str, bearer: str) -> None: ...

    def get(self, credential_ref: str) -> str | None: ...

    def remove(self, credential_ref: str) -> bool: ...

    def backend_name(self) -> str: ...

    def verify_ready(self) -> None: ...


def normalize_bearer(value: str) -> str:
    candidate = str(value or "").strip()
    if candidate.lower() == "bearer":
        candidate = ""
    if candidate.lower().startswith("bearer "):
        candidate = candidate[7:].strip()
    if (
        not candidate
        or len(candidate) > 16384
        or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in candidate)
    ):
        raise CredentialError(
            "invalid_credential",
            "The delegated caller credential is empty or contains invalid characters.",
        )
    return candidate


class MacOSKeychainCredentialStore:
    def __init__(
        self,
        *,
        backend: KeyringBackend | None = None,
        platform_name: str | None = None,
        enforce_native_backend: bool = True,
    ) -> None:
        self._backend = backend or keyring.get_keyring()
        current_platform = platform_name or platform.system()
        if current_platform != "Darwin":
            raise CredentialError(
                "unsupported_credential_store",
                "This release verifies macOS Keychain only; Windows Credential Manager and Linux Secret Service remain pending.",
            )
        backend_module = type(self._backend).__module__
        if enforce_native_backend and backend_module != "keyring.backends.macOS":
            raise CredentialError(
                "insecure_keyring_backend",
                "A native macOS Keychain backend is required for delegated caller credentials.",
            )
        if getattr(self._backend, "priority", 0) <= 0:
            raise CredentialError(
                "unavailable_keyring_backend",
                "The macOS Keychain backend is unavailable.",
            )

    def put(self, credential_ref: str, bearer: str) -> None:
        candidate = normalize_bearer(bearer)
        try:
            self._backend.set_password(KEYRING_SERVICE, credential_ref, candidate)
        except Exception as exc:
            raise CredentialError(
                "credential_store_write_failed",
                "The delegated caller credential could not be stored in macOS Keychain.",
            ) from exc

    def get(self, credential_ref: str) -> str | None:
        try:
            value = self._backend.get_password(KEYRING_SERVICE, credential_ref)
        except Exception as exc:
            raise CredentialError(
                "credential_store_read_failed",
                "The delegated caller credential could not be read from macOS Keychain.",
            ) from exc
        return normalize_bearer(value) if value is not None else None

    def remove(self, credential_ref: str) -> bool:
        try:
            self._backend.delete_password(KEYRING_SERVICE, credential_ref)
        except PasswordDeleteError:
            return False
        except Exception as exc:
            raise CredentialError(
                "credential_store_delete_failed",
                "The delegated caller credential could not be removed from macOS Keychain.",
            ) from exc
        return True

    def backend_name(self) -> str:
        return f"{type(self._backend).__module__}.{type(self._backend).__qualname__}"

    def verify_ready(self) -> None:
        account = f"{KEYRING_HEALTH_ACCOUNT}-{secrets.token_hex(16)}"
        candidate = secrets.token_urlsafe(32)
        stored = False
        try:
            self.put(account, candidate)
            stored = True
            if self.get(account) != candidate:
                raise CredentialError(
                    "credential_store_verification_failed",
                    "macOS Keychain did not return the temporary verification credential.",
                )
        finally:
            if stored:
                self.remove(account)
