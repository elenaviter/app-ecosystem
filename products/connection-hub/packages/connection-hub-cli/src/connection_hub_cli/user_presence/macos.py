from __future__ import annotations

import hmac
import platform
import re

from connection_hub_cli.user_presence.backends import _safe_platform_name
from connection_hub_cli.user_presence.contracts import (
    BoundHttpOperation,
    request_body_bytes,
)
from connection_hub_cli.user_presence.errors import UserPresenceError
from connection_hub_cli.user_presence.native_macos import (
    MAX_PROTECTED_CREDENTIAL_BYTES,
    NativeMacOSKeychain,
    SecurityFrameworkKeychain,
)
from connection_hub_cli.user_presence.operations import (
    HttpOperationResult,
    _BoundRequestFailure,
    _BoundResponseTooLarge,
    _execute_bound_http,
    _validate_timeout_seconds,
)

_CREDENTIAL_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")

_NATIVE_ERROR_MESSAGES = {
    "user_presence_cancelled": "The user cancelled system authentication.",
    "user_presence_authentication_failed": "System authentication failed.",
    "user_presence_item_missing": (
        "The user-presence-protected credential does not exist."
    ),
    "user_presence_item_exists": (
        "A user-presence-protected credential already exists for this reference."
    ),
    "user_presence_interaction_unavailable": (
        "System authentication cannot be displayed in this session."
    ),
    "user_presence_unavailable": "The macOS Keychain service is unavailable.",
    "user_presence_security_error": (
        "The macOS Security framework could not use the protected credential."
    ),
    "protected_credential_invalid": "The protected credential is invalid.",
}

_EXECUTION_ERROR_MESSAGES = {
    "protected_credential_invalid": "The protected credential is invalid.",
    "bound_response_too_large": (
        "The bound HTTP response exceeded the fixed size limit."
    ),
    "bound_request_failed": "The bound HTTP operation could not be completed.",
    "bound_operation_mismatch": (
        "The HTTP result does not match the bound operation."
    ),
    "credential_exposure_blocked": (
        "The bound HTTP response contained protected credential material."
    ),
}


def _validate_credential_ref(value: str) -> str:
    if not isinstance(value, str) or not _CREDENTIAL_REF_RE.fullmatch(value):
        raise UserPresenceError(
            "invalid_credential_ref",
            "The protected credential reference is invalid.",
        )
    return value


def _credential_buffer(value: str) -> bytearray:
    if not isinstance(value, str):
        raise UserPresenceError(
            "protected_credential_invalid", "The protected credential is invalid."
        )
    candidate = value.strip()
    if candidate.lower().startswith("bearer "):
        candidate = candidate[7:].strip()
    try:
        encoded = candidate.encode("ascii")
    except UnicodeEncodeError:
        raise UserPresenceError(
            "protected_credential_invalid", "The protected credential is invalid."
        ) from None
    if (
        not encoded
        or len(encoded) > MAX_PROTECTED_CREDENTIAL_BYTES
        or any(byte <= 32 or byte == 127 for byte in encoded)
    ):
        raise UserPresenceError(
            "protected_credential_invalid", "The protected credential is invalid."
        )
    return bytearray(encoded)


def _validate_credential_view(value: memoryview) -> None:
    if (
        not value
        or len(value) > MAX_PROTECTED_CREDENTIAL_BYTES
        or any(byte <= 32 or byte == 127 or byte > 126 for byte in value)
    ):
        raise UserPresenceError(
            "protected_credential_invalid", "The protected credential is invalid."
        )


def _sanitized_native_error(error: Exception) -> UserPresenceError:
    if isinstance(error, UserPresenceError) and error.code in _NATIVE_ERROR_MESSAGES:
        native_status = (
            error.native_status if isinstance(error.native_status, int) else None
        )
        return UserPresenceError(
            error.code,
            _NATIVE_ERROR_MESSAGES[error.code],
            native_status=native_status,
        )
    return UserPresenceError(
        "user_presence_security_error",
        "The macOS Security framework could not use the protected credential.",
    )


def _sanitized_execution_error(error: UserPresenceError) -> UserPresenceError:
    code = error.code if error.code in _EXECUTION_ERROR_MESSAGES else "bound_request_failed"
    return UserPresenceError(code, _EXECUTION_ERROR_MESSAGES[code])


def _prepare_execution(
    operation: BoundHttpOperation,
    *,
    body: bytes | bytearray | memoryview | str | None,
    timeout_seconds: float,
) -> tuple[bytes, float]:
    if not isinstance(operation, BoundHttpOperation):
        raise UserPresenceError(
            "invalid_bound_operation",
            "A validated bound HTTP operation is required.",
        )
    operation.validate_integrity()
    body_value = request_body_bytes(body)
    if not operation.matches_body(body_value):
        raise UserPresenceError(
            "bound_operation_mismatch",
            "The HTTP body does not match the bound operation digest.",
        )
    timeout_value = _validate_timeout_seconds(timeout_seconds)
    return body_value, timeout_value


class MacOSUserPresenceBackend:
    """In-process prototype for one user-presence-gated bound HTTP operation."""

    def __init__(self, *, credential_ref: str) -> None:
        self._initialize(
            credential_ref=credential_ref,
            native=None,
            platform_name=platform.system(),
        )

    @classmethod
    def _with_native_for_testing(
        cls,
        *,
        credential_ref: str,
        native: NativeMacOSKeychain | None,
        platform_name: str,
    ) -> MacOSUserPresenceBackend:
        instance = cls.__new__(cls)
        instance._initialize(
            credential_ref=credential_ref,
            native=native,
            platform_name=platform_name,
        )
        return instance

    def _initialize(
        self,
        *,
        credential_ref: str,
        native: NativeMacOSKeychain | None,
        platform_name: str,
    ) -> None:
        self.credential_ref = _validate_credential_ref(credential_ref)
        self.platform_name = _safe_platform_name(platform_name)
        self._load_error: UserPresenceError | None = None
        self._native = native
        if self._native is None and self.platform_name == "Darwin":
            try:
                self._native = SecurityFrameworkKeychain()
            except UserPresenceError as exc:
                self._load_error = _sanitized_native_error(exc)

    def available(self) -> bool:
        if self.platform_name != "Darwin" or self._native is None:
            return False
        try:
            return self._native.available()
        except Exception:  # noqa: BLE001 - availability probes fail closed
            return False

    def _require_native(self) -> NativeMacOSKeychain:
        if self.platform_name != "Darwin":
            raise UserPresenceError(
                "user_presence_unsupported_platform",
                f"macOS user presence is unavailable on {self.platform_name}.",
            )
        if self._native is None:
            if self._load_error is not None:
                raise UserPresenceError(
                    self._load_error.code,
                    self._load_error.message,
                    native_status=self._load_error.native_status,
                )
            raise UserPresenceError(
                "user_presence_unavailable",
                "The macOS Security framework user-presence API is unavailable.",
            )
        return self._native

    def enroll(self, bearer: str) -> None:
        credential = _credential_buffer(bearer)
        native = self._require_native()
        try:
            try:
                native.store(self.credential_ref, credential)
            except Exception as exc:  # noqa: BLE001 - native text is untrusted
                raise _sanitized_native_error(exc) from None
        finally:
            for index in range(len(credential)):
                credential[index] = 0

    def remove(self) -> bool:
        native = self._require_native()
        try:
            return native.delete(
                self.credential_ref,
                prompt="Remove the KDCube user-presence prototype credential.",
            )
        except Exception as exc:  # noqa: BLE001 - native text is untrusted
            raise _sanitized_native_error(exc) from None

    def execute(
        self,
        operation: BoundHttpOperation,
        *,
        body: bytes | bytearray | memoryview | str | None = None,
        timeout_seconds: float = 30.0,
    ) -> HttpOperationResult:
        body_value, timeout_value = _prepare_execution(
            operation,
            body=body,
            timeout_seconds=timeout_seconds,
        )
        native = self._require_native()
        try:
            lease = native.read(
                self.credential_ref,
                prompt=operation.system_prompt(),
            )
        except Exception as exc:  # noqa: BLE001 - native text is untrusted
            raise _sanitized_native_error(exc) from None

        try:
            with lease:
                secret = lease.view()
                _validate_credential_view(secret)
                try:
                    result = _execute_bound_http(
                        operation,
                        body=body_value,
                        credential=secret,
                        timeout_seconds=timeout_value,
                    )
                    if not isinstance(result, HttpOperationResult):
                        raise _BoundRequestFailure
                except _BoundResponseTooLarge:
                    raise UserPresenceError(
                        "bound_response_too_large",
                        "The bound HTTP response exceeded the fixed size limit.",
                    ) from None
                except Exception:  # noqa: BLE001 - all transport text is discarded
                    raise UserPresenceError(
                        "bound_request_failed",
                        "The bound HTTP operation could not be completed.",
                    ) from None

                if not hmac.compare_digest(
                    result.operation_digest,
                    operation.operation_digest,
                ):
                    raise UserPresenceError(
                        "bound_operation_mismatch",
                        "The HTTP result does not match the bound operation.",
                    )
                secret_bytes = bytes(secret)
                content_type = result.content_type.encode("utf-8", errors="ignore")
                if secret_bytes in result.body or secret_bytes in content_type:
                    raise UserPresenceError(
                        "credential_exposure_blocked",
                        "The bound HTTP response contained protected credential material.",
                    )
        except UserPresenceError as exc:
            raise _sanitized_execution_error(exc) from None
        except Exception as exc:  # noqa: BLE001 - lease errors are untrusted
            raise _sanitized_native_error(exc) from None
        return result
