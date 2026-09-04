"""Strict native credential-store selection and bounded secret values."""

from __future__ import annotations

import base64
import hashlib
import json
import platform
import secrets
from dataclasses import dataclass

import keyring
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

_BACKENDS = {
    "Darwin": ("keyring.backends.macOS", "Keyring", "macOS Keychain"),
    "Windows": (
        "keyring.backends.Windows",
        "WinVaultKeyring",
        "Windows Credential Manager",
    ),
    "Linux": (
        "keyring.backends.SecretService",
        "Keyring",
        "Linux Secret Service",
    ),
}
_MANIFEST_PREFIX = "app-foundation.native-secret.v1:"
_MANIFEST_FORMAT = "app_foundation.native_secret.v1"
_WINDOWS_CHUNK_BYTES = 768
MAX_NATIVE_SECRET_VALUE_BYTES = 288 * 1024
_MAX_CHUNKS = 512


class NativeSecretError(RuntimeError):
    """A stable, secret-safe failure from native credential custody."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True, slots=True)
class _Manifest:
    generation: str
    utf8_bytes: int
    chunks: int
    sha256: str

    def render(self) -> str:
        payload = {
            "format": _MANIFEST_FORMAT,
            "generation": self.generation,
            "utf8_bytes": self.utf8_bytes,
            "chunks": self.chunks,
            "sha256": self.sha256,
        }
        return _MANIFEST_PREFIX + json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def parse(cls, value: str) -> _Manifest | None:
        if not value.startswith(_MANIFEST_PREFIX):
            return None
        try:
            payload = json.loads(value[len(_MANIFEST_PREFIX) :])
            if not isinstance(payload, dict) or set(payload) != {
                "format",
                "generation",
                "utf8_bytes",
                "chunks",
                "sha256",
            }:
                raise ValueError
            generation = payload["generation"]
            digest = payload["sha256"]
            byte_count = payload["utf8_bytes"]
            chunk_count = payload["chunks"]
            if (
                payload["format"] != _MANIFEST_FORMAT
                or not isinstance(generation, str)
                or len(generation) != 32
                or any(ch not in "0123456789abcdef" for ch in generation)
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(ch not in "0123456789abcdef" for ch in digest)
                or type(byte_count) is not int
                or byte_count < 0
                or byte_count > MAX_NATIVE_SECRET_VALUE_BYTES
                or type(chunk_count) is not int
                or chunk_count < 1
                or chunk_count > _MAX_CHUNKS
            ):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError):
            raise NativeSecretError(
                "native_secret_corrupt",
                "The native credential-store record is invalid.",
            ) from None
        return cls(
            generation=generation,
            utf8_bytes=byte_count,
            chunks=chunk_count,
            sha256=digest,
        )


def accepted_native_backend(platform_name: str) -> tuple[str, str]:
    """Return the exact reviewed keyring backend class for a platform."""

    accepted = _BACKENDS.get(platform_name)
    if accepted is None:
        raise NativeSecretError(
            "unsupported_native_secret_platform",
            "This operating system has no supported native credential store.",
        )
    return accepted[0], accepted[1]


class NativeSecretValueStore:
    """Store bounded text values in the current user's native credential store."""

    def __init__(
        self,
        *,
        service: str,
        backend: KeyringBackend | None = None,
        platform_name: str | None = None,
        enforce_native_backend: bool = True,
        max_value_bytes: int = MAX_NATIVE_SECRET_VALUE_BYTES,
    ) -> None:
        self._service = self._validate_key(service, field="service")
        self._platform = platform_name or platform.system()
        accepted_module, accepted_class = accepted_native_backend(self._platform)
        try:
            self._backend = backend or keyring.get_keyring()
        except Exception:  # noqa: BLE001
            raise NativeSecretError(
                "unavailable_native_secret_backend",
                self._unavailable_message(),
            ) from None
        backend_type = type(self._backend)
        if enforce_native_backend and (
            backend_type.__module__,
            backend_type.__qualname__,
        ) != (accepted_module, accepted_class):
            raise NativeSecretError(
                "insecure_native_secret_backend",
                f"{self.store_name} is required for native secret custody on this platform.",
            )
        try:
            priority = float(self._backend.priority)
        except Exception:  # noqa: BLE001
            priority = 0
        if priority <= 0:
            raise NativeSecretError(
                "unavailable_native_secret_backend",
                self._unavailable_message(),
            )
        if max_value_bytes < 1 or max_value_bytes > MAX_NATIVE_SECRET_VALUE_BYTES:
            raise ValueError(
                f"max_value_bytes must be between 1 and {MAX_NATIVE_SECRET_VALUE_BYTES}"
            )
        self._max_value_bytes = max_value_bytes

    @property
    def platform_name(self) -> str:
        return self._platform

    @property
    def native_backend(self) -> KeyringBackend:
        """Return the selected adapter for composing isolated service namespaces."""

        return self._backend

    @property
    def store_name(self) -> str:
        return _BACKENDS[self._platform][2]

    def backend_name(self) -> str:
        backend_type = type(self._backend)
        return f"{backend_type.__module__}.{backend_type.__qualname__}"

    def recovery_hint(self) -> str:
        if self._platform == "Darwin":
            return (
                "Run from the logged-in macOS desktop session and unlock the login "
                "Keychain before retrying."
            )
        if self._platform == "Windows":
            return (
                "Run from the intended interactive Windows desktop account and verify "
                "that Windows Credential Manager is available."
            )
        return (
            "Run from a graphical Linux session with DBUS_SESSION_BUS_ADDRESS set, "
            "an available Secret Service provider, and an unlocked default collection."
        )

    def replace(self, account: str, value: str) -> None:
        key = self._validate_key(account, field="account")
        encoded = self._validate_value(value)
        if self._platform != "Windows":
            self._set(key, value)
            return
        self._replace_windows(key, encoded)

    def get(self, account: str) -> str | None:
        key = self._validate_key(account, field="account")
        raw = self._get(key)
        if raw is None:
            return None
        manifest = _Manifest.parse(raw)
        if manifest is None:
            return self._validated_loaded_value(raw)
        return self._read_generation(key, manifest)

    def remove(self, account: str) -> bool:
        key = self._validate_key(account, field="account")
        if self._platform != "Windows":
            return self._delete(key)
        raw = self._get(key)
        if raw is None:
            return False
        manifest = _Manifest.parse(raw)
        if manifest is not None and not self._delete_generation(key, manifest):
            raise NativeSecretError(
                "native_secret_cleanup_failed",
                "The native credential-store value was only partially removed.",
            )
        if not self._delete(key):
            raise NativeSecretError(
                "native_secret_cleanup_failed",
                "The native credential-store value was only partially removed.",
            )
        return True

    def verify_ready(self) -> None:
        account = f"health-check-{secrets.token_hex(16)}"
        candidate = secrets.token_urlsafe(32)
        stored = False
        try:
            self.replace(account, candidate)
            stored = True
            if self.get(account) != candidate:
                raise NativeSecretError(
                    "native_secret_verification_failed",
                    "The native credential store did not return its disposable check value.",
                )
        finally:
            if stored and not self.remove(account):
                raise NativeSecretError(
                    "native_secret_cleanup_failed",
                    "The disposable native credential-store value was not removed.",
                )

    def _replace_windows(self, account: str, encoded: bytes) -> None:
        previous_raw = self._get(account)
        previous_manifest = (
            _Manifest.parse(previous_raw) if previous_raw is not None else None
        )
        generation = secrets.token_hex(16)
        chunks = [
            encoded[offset : offset + _WINDOWS_CHUNK_BYTES]
            for offset in range(0, len(encoded), _WINDOWS_CHUNK_BYTES)
        ] or [b""]
        if len(chunks) > _MAX_CHUNKS:
            raise NativeSecretError(
                "native_secret_too_large",
                "The secret value exceeds the supported native-store size.",
            )
        manifest = _Manifest(
            generation=generation,
            utf8_bytes=len(encoded),
            chunks=len(chunks),
            sha256=hashlib.sha256(encoded).hexdigest(),
        )
        written = 0
        committed = False
        try:
            for index, chunk in enumerate(chunks):
                rendered = base64.urlsafe_b64encode(chunk).decode("ascii")
                self._set(self._chunk_key(account, generation, index), rendered)
                written += 1
            if self._read_generation(account, manifest).encode("utf-8") != encoded:
                raise NativeSecretError(
                    "native_secret_verification_failed",
                    "The native credential store did not verify a candidate value.",
                )
            self._set(account, manifest.render())
            committed = True
            if self._read_generation(account, manifest).encode("utf-8") != encoded:
                raise NativeSecretError(
                    "native_secret_verification_failed",
                    "The native credential store did not verify the committed value.",
                )
        except Exception:
            if committed:
                try:
                    if previous_raw is None:
                        if not self._delete(account):
                            raise NativeSecretError(
                                "native_secret_rollback_failed",
                                "The native credential-store manifest could not be rolled back.",
                            )
                    else:
                        self._set(account, previous_raw)
                except NativeSecretError:
                    raise NativeSecretError(
                        "native_secret_rollback_failed",
                        "The native credential-store manifest could not be rolled back.",
                    ) from None
            self._cleanup_candidate(account, generation, written)
            raise
        if previous_manifest is not None and not self._delete_generation(
            account, previous_manifest
        ):
            raise NativeSecretError(
                "native_secret_cleanup_failed",
                "The new native credential-store value is active, but the previous generation could not be fully removed.",
            )

    def _cleanup_candidate(self, account: str, generation: str, count: int) -> None:
        failed = False
        for index in range(count):
            try:
                self._delete(self._chunk_key(account, generation, index))
            except NativeSecretError:
                failed = True
        if failed:
            raise NativeSecretError(
                "native_secret_cleanup_failed",
                "An incomplete native credential-store candidate could not be fully removed.",
            ) from None

    def _read_generation(self, account: str, manifest: _Manifest) -> str:
        assembled = bytearray()
        for index in range(manifest.chunks):
            chunk = self._get(self._chunk_key(account, manifest.generation, index))
            if chunk is None:
                raise NativeSecretError(
                    "native_secret_corrupt",
                    "The native credential-store value is incomplete.",
                )
            try:
                decoded = base64.b64decode(chunk, altchars=b"-_", validate=True)
            except (ValueError, TypeError):
                raise NativeSecretError(
                    "native_secret_corrupt",
                    "The native credential-store value is invalid.",
                ) from None
            assembled.extend(decoded)
            if len(assembled) > self._max_value_bytes:
                raise NativeSecretError(
                    "native_secret_corrupt",
                    "The native credential-store value exceeds its declared bound.",
                )
        value = bytes(assembled)
        if (
            len(value) != manifest.utf8_bytes
            or hashlib.sha256(value).hexdigest() != manifest.sha256
        ):
            raise NativeSecretError(
                "native_secret_corrupt",
                "The native credential-store value failed integrity verification.",
            )
        try:
            decoded_value = value.decode("utf-8")
        except UnicodeDecodeError:
            raise NativeSecretError(
                "native_secret_corrupt",
                "The native credential-store value has invalid text encoding.",
            ) from None
        return self._validated_loaded_value(decoded_value)

    def _delete_generation(self, account: str, manifest: _Manifest) -> bool:
        complete = True
        for index in range(manifest.chunks):
            try:
                self._delete(self._chunk_key(account, manifest.generation, index))
            except NativeSecretError:
                complete = False
        return complete

    def _set(self, account: str, value: str) -> None:
        try:
            self._backend.set_password(self._service, account, value)
        except Exception:  # noqa: BLE001
            raise NativeSecretError(
                "native_secret_write_failed",
                f"The value could not be stored in {self.store_name}.",
            ) from None

    def _get(self, account: str) -> str | None:
        try:
            value = self._backend.get_password(self._service, account)
        except Exception:  # noqa: BLE001
            raise NativeSecretError(
                "native_secret_read_failed",
                f"The value could not be read from {self.store_name}.",
            ) from None
        if value is not None and not isinstance(value, str):
            raise NativeSecretError(
                "native_secret_corrupt",
                "The native credential-store value has an invalid representation.",
            )
        return value

    def _delete(self, account: str) -> bool:
        try:
            self._backend.delete_password(self._service, account)
        except PasswordDeleteError:
            return False
        except Exception:  # noqa: BLE001
            raise NativeSecretError(
                "native_secret_delete_failed",
                f"The value could not be removed from {self.store_name}.",
            ) from None
        return True

    def _validated_loaded_value(self, value: str) -> str:
        encoded = value.encode("utf-8")
        if len(encoded) > self._max_value_bytes:
            raise NativeSecretError(
                "native_secret_corrupt",
                "The native credential-store value exceeds its supported bound.",
            )
        return value

    def _validate_value(self, value: str) -> bytes:
        if not isinstance(value, str):
            raise TypeError("native secret values must be text")
        encoded = value.encode("utf-8")
        if len(encoded) > self._max_value_bytes:
            raise NativeSecretError(
                "native_secret_too_large",
                "The secret value exceeds the supported native-store size.",
            )
        return encoded

    @staticmethod
    def _validate_key(value: str, *, field: str) -> str:
        candidate = str(value or "").strip()
        if (
            not candidate
            or len(candidate) > 512
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in candidate)
        ):
            raise NativeSecretError(
                "native_secret_key_invalid",
                f"The native credential-store {field} is invalid.",
            )
        return candidate

    @staticmethod
    def _chunk_key(account: str, generation: str, index: int) -> str:
        account_digest = hashlib.sha256(account.encode("utf-8")).hexdigest()
        return f"native-v1.{account_digest}.{generation}.{index:04x}"

    def _unavailable_message(self) -> str:
        store_name = _BACKENDS[self._platform][2]
        if self._platform == "Linux":
            return (
                f"{store_name} is unavailable. A graphical session D-Bus, Secret "
                "Service provider, and unlocked default collection are required."
            )
        return f"{store_name} is unavailable for this operating-system user."


__all__ = [
    "MAX_NATIVE_SECRET_VALUE_BYTES",
    "NativeSecretError",
    "NativeSecretValueStore",
    "accepted_native_backend",
]
