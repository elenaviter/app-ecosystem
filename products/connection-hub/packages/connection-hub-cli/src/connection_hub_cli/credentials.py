from __future__ import annotations

import platform
from typing import NoReturn, Protocol

from app_foundation.secrets import (
    NativeSecretError,
    NativeSecretValueStore,
    accepted_native_backend,
)
from keyring.backend import KeyringBackend

from connection_hub_cli.errors import CredentialError

KEYRING_SERVICE = "tech.kdcube.connection-hub.delegated-caller"


class CredentialStore(Protocol):
    def put(self, credential_ref: str, bearer: str) -> None: ...

    def get(self, credential_ref: str) -> str | None: ...

    def remove(self, credential_ref: str) -> bool: ...

    def backend_name(self) -> str: ...

    def verify_ready(self) -> None: ...


class UnavailableCredentialStore:
    """Deferred native-store failure for commands that use client-owned OAuth."""

    def __init__(
        self,
        error: CredentialError,
        *,
        platform_name: str | None = None,
    ) -> None:
        self._code = error.code
        self._message = error.message
        self._exit_code = error.exit_code
        self.platform_name = platform_name or platform.system()
        self.store_name = {
            "Darwin": "macOS Keychain",
            "Windows": "Windows Credential Manager",
            "Linux": "Linux Secret Service",
        }.get(self.platform_name, "operating-system credential store")

    def put(self, _credential_ref: str, _bearer: str) -> None:
        self._raise()

    def get(self, _credential_ref: str) -> str | None:
        self._raise()

    def remove(self, _credential_ref: str) -> bool:
        self._raise()

    def backend_name(self) -> str:
        try:
            module, name = accepted_native_backend(self.platform_name)
        except NativeSecretError:
            return "unavailable"
        return f"{module}.{name} (required, unavailable)"

    def verify_ready(self) -> None:
        self._raise()

    def recovery_hint(self) -> str:
        if self.platform_name == "Darwin":
            return (
                "Run from the logged-in macOS desktop session and unlock the login "
                "Keychain before retrying."
            )
        if self.platform_name == "Windows":
            return (
                "Run from the intended interactive Windows desktop account and verify "
                "that Windows Credential Manager is available."
            )
        if self.platform_name == "Linux":
            return (
                "Run from a graphical Linux session with DBUS_SESSION_BUS_ADDRESS set, "
                "an available Secret Service provider, and an unlocked default collection."
            )
        return (
            "Use a supported desktop operating system or a client's native OAuth mode."
        )

    def _raise(self) -> NoReturn:
        raise CredentialError(
            self._code,
            self._message,
            exit_code=self._exit_code,
        ) from None


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


class NativeCredentialStore:
    """Connection Hub delegated-caller values in the selected native store."""

    def __init__(
        self,
        *,
        backend: KeyringBackend | None = None,
        platform_name: str | None = None,
        enforce_native_backend: bool = True,
    ) -> None:
        try:
            self._values = NativeSecretValueStore(
                service=KEYRING_SERVICE,
                backend=backend,
                platform_name=platform_name,
                enforce_native_backend=enforce_native_backend,
            )
        except NativeSecretError as exc:
            self._raise(exc)

    @property
    def platform_name(self) -> str:
        return self._values.platform_name

    @property
    def store_name(self) -> str:
        return self._values.store_name

    @property
    def native_backend(self) -> KeyringBackend:
        return self._values.native_backend

    def recovery_hint(self) -> str:
        return self._values.recovery_hint()

    def put(self, credential_ref: str, bearer: str) -> None:
        candidate = normalize_bearer(bearer)
        try:
            self._values.replace(credential_ref, candidate)
        except NativeSecretError as exc:
            self._raise(exc)

    def get(self, credential_ref: str) -> str | None:
        try:
            value = self._values.get(credential_ref)
        except NativeSecretError as exc:
            self._raise(exc)
        return normalize_bearer(value) if value is not None else None

    def remove(self, credential_ref: str) -> bool:
        try:
            return self._values.remove(credential_ref)
        except NativeSecretError as exc:
            self._raise(exc)

    def backend_name(self) -> str:
        return self._values.backend_name()

    def verify_ready(self) -> None:
        try:
            self._values.verify_ready()
        except NativeSecretError as exc:
            self._raise(exc)

    def _raise(self, exc: NativeSecretError) -> None:
        code = {
            "unsupported_native_secret_platform": "unsupported_credential_store",
            "insecure_native_secret_backend": "insecure_keyring_backend",
            "unavailable_native_secret_backend": "unavailable_keyring_backend",
            "native_secret_write_failed": "credential_store_write_failed",
            "native_secret_read_failed": "credential_store_read_failed",
            "native_secret_delete_failed": "credential_store_delete_failed",
            "native_secret_cleanup_failed": "credential_store_delete_failed",
            "native_secret_corrupt": "credential_store_read_failed",
            "native_secret_verification_failed": "credential_store_verification_failed",
            "native_secret_rollback_failed": "credential_rollback_failed",
            "native_secret_too_large": "invalid_credential",
            "native_secret_key_invalid": "invalid_credential_ref",
        }.get(exc.code, "credential_store_failed")
        raise CredentialError(code, exc.message) from None


class MacOSKeychainCredentialStore(NativeCredentialStore):
    """Compatibility name for callers that explicitly require macOS."""

    def __init__(
        self,
        *,
        backend: KeyringBackend | None = None,
        platform_name: str | None = None,
        enforce_native_backend: bool = True,
    ) -> None:
        if platform_name is not None and platform_name != "Darwin":
            raise CredentialError(
                "unsupported_credential_store",
                "This compatibility class requires macOS Keychain.",
            )
        super().__init__(
            backend=backend,
            platform_name="Darwin",
            enforce_native_backend=enforce_native_backend,
        )


__all__ = [
    "KEYRING_SERVICE",
    "CredentialStore",
    "MacOSKeychainCredentialStore",
    "NativeCredentialStore",
    "UnavailableCredentialStore",
    "normalize_bearer",
]
