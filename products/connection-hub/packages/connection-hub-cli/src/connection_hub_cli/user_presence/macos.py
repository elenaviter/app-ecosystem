from __future__ import annotations

import hmac
import platform
import re

from connection_hub_cli.user_presence.backends import _safe_platform_name
from connection_hub_cli.user_presence.contracts import (
    ApprovalRequest,
    ApprovalResult,
    request_body_bytes,
)
from connection_hub_cli.user_presence.errors import UserPresenceError
from connection_hub_cli.user_presence.native_macos import (
    NativeMacOSKeychain,
    SecurityFrameworkKeychain,
)
from connection_hub_cli.user_presence.operations import (
    BoundHttpTransport,
    HttpOperationResult,
    UrllibBoundHttpTransport,
)

MACOS_USER_PRESENCE_MECHANISM = "macos-keychain-user-presence"
_CREDENTIAL_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MAX_CREDENTIAL_BYTES = 16384


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
        or len(encoded) > _MAX_CREDENTIAL_BYTES
        or any(byte <= 32 or byte == 127 for byte in encoded)
    ):
        raise UserPresenceError(
            "protected_credential_invalid", "The protected credential is invalid."
        )
    return bytearray(encoded)


def _validate_credential_view(value: memoryview) -> None:
    if (
        not value
        or len(value) > _MAX_CREDENTIAL_BYTES
        or any(byte <= 32 or byte == 127 or byte > 126 for byte in value)
    ):
        raise UserPresenceError(
            "protected_credential_invalid", "The protected credential is invalid."
        )


class MacOSUserPresenceBackend:
    """Uses a userPresence-protected Keychain item for one bound operation."""

    def __init__(
        self,
        *,
        credential_ref: str,
        native: NativeMacOSKeychain | None = None,
        platform_name: str | None = None,
    ) -> None:
        self.credential_ref = _validate_credential_ref(credential_ref)
        self.platform_name = _safe_platform_name(platform_name or platform.system())
        self._load_error: UserPresenceError | None = None
        self._native = native
        if self._native is None and self.platform_name == "Darwin":
            try:
                self._native = SecurityFrameworkKeychain()
            except UserPresenceError as exc:
                self._load_error = exc

    def available(self) -> bool:
        if self.platform_name != "Darwin" or self._native is None:
            return False
        try:
            return self._native.available()
        except Exception:  # noqa: BLE001 - availability probes must fail closed
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
        native = self._require_native()
        credential = _credential_buffer(bearer)
        try:
            try:
                native.store(self.credential_ref, credential)
            except UserPresenceError:
                raise
            except Exception:  # noqa: BLE001 - native failure text may hold a secret
                raise UserPresenceError(
                    "user_presence_security_error",
                    "The protected credential could not be stored.",
                ) from None
        finally:
            for index in range(len(credential)):
                credential[index] = 0

    def remove(self) -> bool:
        native = self._require_native()
        try:
            return native.delete(
                self.credential_ref,
                prompt="Remove the human-only KDCube management credential.",
            )
        except UserPresenceError:
            raise
        except Exception:  # noqa: BLE001 - native failure text may hold a secret
            raise UserPresenceError(
                "user_presence_security_error",
                "The protected credential could not be removed.",
            ) from None

    def approve(self, request: ApprovalRequest) -> ApprovalResult:
        """Authenticate item access and return non-authorizing approval metadata."""

        native = self._require_native()
        try:
            with native.read(
                self.credential_ref, prompt=request.system_prompt()
            ) as credential:
                _validate_credential_view(credential.view())
        except UserPresenceError:
            raise
        except Exception:  # noqa: BLE001 - native failure text may hold a secret
            raise UserPresenceError(
                "user_presence_security_error",
                "The protected credential could not be used.",
            ) from None
        return ApprovalResult(
            approved=True,
            mechanism=MACOS_USER_PRESENCE_MECHANISM,
            request_digest=request.request_digest,
        )

    def execute_http(
        self,
        request: ApprovalRequest,
        *,
        body: bytes | bytearray | memoryview | str | None = None,
        transport: BoundHttpTransport | None = None,
        timeout_seconds: float = 30.0,
    ) -> HttpOperationResult:
        body_value = request_body_bytes(body)
        if not request.matches_body(body_value):
            raise UserPresenceError(
                "approval_digest_mismatch",
                "The HTTP body does not match the user-presence request digest.",
            )
        native = self._require_native()
        sender = transport or UrllibBoundHttpTransport()
        try:
            with native.read(
                self.credential_ref, prompt=request.system_prompt()
            ) as credential:
                secret = credential.view()
                _validate_credential_view(secret)
                try:
                    result = sender.send(
                        request,
                        body=body_value,
                        credential=secret,
                        timeout_seconds=timeout_seconds,
                    )
                except UserPresenceError:
                    raise
                except Exception:  # noqa: BLE001 - transport failures are sanitized
                    raise UserPresenceError(
                        "bound_request_failed",
                        "The approved request could not be completed.",
                    ) from None
                if not hmac.compare_digest(
                    result.request_digest, request.request_digest
                ):
                    raise UserPresenceError(
                        "approval_digest_mismatch",
                        "The HTTP result does not match the approved request.",
                    )
                secret_bytes = bytes(secret)
                content_type = result.content_type.encode("utf-8", errors="ignore")
                if secret_bytes in result.body or secret_bytes in content_type:
                    raise UserPresenceError(
                        "credential_exposure_blocked",
                        "The approved request returned protected credential material.",
                    )
        except UserPresenceError:
            raise
        except Exception:  # noqa: BLE001 - native failure text may hold a secret
            raise UserPresenceError(
                "user_presence_security_error",
                "The protected credential could not be used.",
            ) from None
        return result
