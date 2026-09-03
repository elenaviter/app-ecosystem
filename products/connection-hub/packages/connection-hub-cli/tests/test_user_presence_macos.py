from __future__ import annotations

import inspect
import logging
import math
import platform

import pytest
from connection_hub_cli.user_presence import (
    BoundHttpOperation,
    HttpOperationResult,
    MacOSUserPresenceBackend,
    UserPresenceError,
    interactive_macos_check,
)
from connection_hub_cli.user_presence import macos as macos_module
from connection_hub_cli.user_presence.native_macos import (
    MAX_PROTECTED_CREDENTIAL_BYTES,
    SecretLease,
    SecurityFrameworkKeychain,
    _copy_protected_data,
    _status_error,
)

_SECRET = b"protected-test-bearer-never-render"


def _operation(
    *,
    body: bytes = b'{"ref":"main"}',
    operation: str = "host.reload",
) -> BoundHttpOperation:
    return BoundHttpOperation.bind(
        target_key="endpoint:https://host.example:tenant-a:project-a",
        tenant="tenant-a",
        project="project-a",
        caller_profile="human-admin",
        access_id="access-123",
        resource="kdcube.host",
        operation=operation,
        method="POST",
        target="https://host.example:8443",
        path="/api/manage/reload",
        body=body,
        display_summary="Reload the selected KDCube host",
    )


class _NativeKeychain:
    def __init__(self) -> None:
        self.available_value = True
        self.read_error: Exception | None = None
        self.store_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.stored: bytes | None = None
        self.stored_buffer: bytearray | None = None
        self.last_lease: SecretLease | None = None
        self.prompt = ""
        self.read_count = 0

    def available(self) -> bool:
        return self.available_value

    def store(self, account: str, secret: bytearray) -> None:
        del account
        if self.store_error is not None:
            raise self.store_error
        self.stored = bytes(secret)
        self.stored_buffer = secret

    def read(self, account: str, *, prompt: str) -> SecretLease:
        del account
        self.read_count += 1
        self.prompt = prompt
        if self.read_error is not None:
            raise self.read_error
        self.last_lease = SecretLease(bytearray(_SECRET))
        return self.last_lease

    def delete(self, account: str, *, prompt: str) -> bool:
        del account, prompt
        if self.delete_error is not None:
            raise self.delete_error
        self.stored = None
        return True


def _backend(native: _NativeKeychain) -> MacOSUserPresenceBackend:
    return MacOSUserPresenceBackend._with_native_for_testing(
        credential_ref="a" * 32,
        native=native,
        platform_name="Darwin",
    )


def _render_failure(
    error: UserPresenceError,
    *,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> str:
    captured = capsys.readouterr()
    return "\n".join(
        [
            captured.out,
            captured.err,
            caplog.text,
            str(error),
            repr(error),
            str(error.to_dict()),
            repr(error.__cause__),
        ]
    )


def test_enroll_passes_secret_to_native_then_wipes_owned_buffer() -> None:
    native = _NativeKeychain()

    result = _backend(native).enroll(f"Bearer {_SECRET.decode('ascii')}")

    assert result is None
    assert native.stored == _SECRET
    assert native.stored_buffer is not None
    assert bytes(native.stored_buffer) == b"\x00" * len(_SECRET)


def test_system_prompt_identifies_operation_deployment_and_origin_first() -> None:
    native = _NativeKeychain()
    native.read_error = UserPresenceError(
        "user_presence_cancelled",
        "untrusted text",
    )

    with pytest.raises(UserPresenceError):
        _backend(native).execute(_operation(), body=b'{"ref":"main"}')

    assert native.prompt.startswith(
        "host.reload for tenant-a/project-a at https://host.example:8443;"
    )
    assert "Reload the selected" not in native.prompt


def test_user_cancellation_is_structured_sanitized_and_fails_closed() -> None:
    native = _NativeKeychain()
    marker = _SECRET.decode("ascii")
    native.read_error = UserPresenceError("user_presence_cancelled", marker)

    with pytest.raises(UserPresenceError) as raised:
        _backend(native).execute(_operation(), body=b'{"ref":"main"}')

    assert raised.value.code == "user_presence_cancelled"
    assert str(raised.value) == "The user cancelled system authentication."
    assert marker not in str(raised.value.to_dict())
    assert raised.value.__cause__ is None


def test_untrusted_native_failure_text_cannot_escape(
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    native = _NativeKeychain()
    marker = _SECRET.decode("ascii")
    native.read_error = RuntimeError(marker)

    with caplog.at_level(logging.DEBUG), pytest.raises(UserPresenceError) as raised:
        _backend(native).execute(_operation(), body=b'{"ref":"main"}')

    assert raised.value.code == "user_presence_security_error"
    assert marker not in _render_failure(raised.value, capsys=capsys, caplog=caplog)
    assert raised.value.__cause__ is None


def test_unknown_native_status_is_preserved_without_native_text() -> None:
    native = _NativeKeychain()
    marker = _SECRET.decode("ascii")
    native.read_error = UserPresenceError(
        "user_presence_security_error",
        marker,
        native_status=-34018,
    )

    with pytest.raises(UserPresenceError) as raised:
        _backend(native).execute(_operation(), body=b'{"ref":"main"}')

    assert raised.value.code == "user_presence_security_error"
    assert raised.value.native_status == -34018
    assert marker not in str(raised.value)
    assert marker not in str(raised.value.to_dict())
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    "status,code",
    [
        (-128, "user_presence_cancelled"),
        (-25291, "user_presence_unavailable"),
        (-25293, "user_presence_authentication_failed"),
        (-25299, "user_presence_item_exists"),
        (-25300, "user_presence_item_missing"),
        (-25308, "user_presence_interaction_unavailable"),
        (-25315, "user_presence_interaction_unavailable"),
        (-99999, "user_presence_security_error"),
    ],
)
def test_native_security_statuses_have_distinct_safe_codes(
    status: int, code: str
) -> None:
    error = _status_error(status, action="read")

    assert error.code == code
    assert error.native_status == status
    assert _SECRET.decode("ascii") not in str(error)


def test_unavailable_platform_fails_without_native_access() -> None:
    native = _NativeKeychain()
    backend = MacOSUserPresenceBackend._with_native_for_testing(
        credential_ref="a" * 32,
        native=native,
        platform_name="Linux",
    )

    assert backend.available() is False
    with pytest.raises(UserPresenceError) as raised:
        backend.execute(_operation(), body=b'{"ref":"main"}')
    assert raised.value.code == "user_presence_unsupported_platform"
    assert native.read_count == 0


def test_body_mismatch_fails_before_native_credential_access() -> None:
    native = _NativeKeychain()

    with pytest.raises(UserPresenceError) as raised:
        _backend(native).execute(_operation(), body=b'{"ref":"other"}')

    assert raised.value.code == "bound_operation_mismatch"
    assert native.read_count == 0


@pytest.mark.parametrize(
    "timeout",
    [0.0, 121.0, math.nan, math.inf, -math.inf, True, "ten"],
)
def test_invalid_timeout_fails_before_native_credential_access(
    timeout: object,
) -> None:
    native = _NativeKeychain()

    with pytest.raises(UserPresenceError) as raised:
        _backend(native).execute(
            _operation(),
            body=b'{"ref":"main"}',
            timeout_seconds=timeout,  # type: ignore[arg-type]
        )

    assert raised.value.code == "invalid_request_timeout"
    assert native.read_count == 0


def test_mutated_operation_fails_integrity_before_native_access() -> None:
    native = _NativeKeychain()
    operation = _operation()
    object.__setattr__(operation, "target", "http://127.0.0.1:1")

    with pytest.raises(UserPresenceError) as raised:
        _backend(native).execute(operation, body=b'{"ref":"main"}')

    assert raised.value.code == "invalid_bound_operation"
    assert native.read_count == 0


@pytest.mark.parametrize("kind", ["normal", "structured"])
def test_transport_failure_text_is_always_sanitized(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = _SECRET.decode("ascii")
    native = _NativeKeychain()

    def fail(*args, **kwargs):
        del args, kwargs
        if kind == "structured":
            raise UserPresenceError("attacker_selected_code", marker)
        raise RuntimeError(marker)

    monkeypatch.setattr(macos_module, "_execute_bound_http", fail)
    with caplog.at_level(logging.DEBUG), pytest.raises(UserPresenceError) as raised:
        _backend(native).execute(_operation(), body=b'{"ref":"main"}')

    assert raised.value.code == "bound_request_failed"
    assert marker not in _render_failure(raised.value, capsys=capsys, caplog=caplog)
    assert raised.value.__cause__ is None
    assert native.read_count == 1
    assert native.last_lease is not None
    with pytest.raises(UserPresenceError):
        native.last_lease.view()


def test_internal_result_for_another_digest_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def wrong_digest(*args, **kwargs):
        del args, kwargs
        return HttpOperationResult(
            status_code=200,
            operation_digest="0" * 64,
            content_type="application/json",
            body=b"{}",
        )

    monkeypatch.setattr(macos_module, "_execute_bound_http", wrong_digest)
    with pytest.raises(UserPresenceError) as raised:
        _backend(_NativeKeychain()).execute(
            _operation(),
            body=b'{"ref":"main"}',
        )

    assert raised.value.code == "bound_operation_mismatch"


def test_public_execution_signature_has_no_transport_callback() -> None:
    parameters = inspect.signature(MacOSUserPresenceBackend.execute).parameters
    constructor = inspect.signature(MacOSUserPresenceBackend).parameters

    assert "transport" not in parameters
    assert "response_limit" not in parameters
    assert list(parameters) == ["self", "operation", "body", "timeout_seconds"]
    assert list(constructor) == ["credential_ref"]


def test_native_oversize_is_rejected_before_pointer_access() -> None:
    class OversizeCoreFoundation:
        pointer_calls = 0

        def CFDataGetLength(self, reference: int) -> int:
            del reference
            return MAX_PROTECTED_CREDENTIAL_BYTES + 1

        def CFDataGetBytePtr(self, reference: int):
            del reference
            self.pointer_calls += 1
            raise AssertionError("pointer must not be requested")

    core = OversizeCoreFoundation()

    with pytest.raises(UserPresenceError) as raised:
        _copy_protected_data(core, 1)  # type: ignore[arg-type]

    assert raised.value.code == "protected_credential_invalid"
    assert core.pointer_calls == 0


def test_secret_lease_repr_is_redacted_and_close_is_terminal() -> None:
    lease = SecretLease(bytearray(_SECRET))

    assert _SECRET.decode("ascii") not in repr(lease)
    assert str(lease) == "<redacted>"
    lease.close()
    with pytest.raises(UserPresenceError):
        lease.view()


def test_interactive_diagnostic_renders_only_bounded_native_status() -> None:
    error = UserPresenceError(
        "user_presence_security_error",
        "untrusted native detail",
        native_status=-34018,
    )
    assert (
        interactive_macos_check._safe_error_label(error)
        == "user_presence_security_error (OSStatus -34018)"
    )

    unbounded = UserPresenceError(
        "not safe to render",
        _SECRET.decode("ascii"),
        native_status=2**40,
    )
    assert interactive_macos_check._safe_error_label(unbounded) == "unknown_error"


def test_interactive_cleanup_failure_forces_nonzero_without_printing_secret(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    marker = _SECRET.decode("ascii")

    class Backend:
        def __init__(self, *, credential_ref: str) -> None:
            del credential_ref

        def available(self) -> bool:
            return True

        def enroll(self, bearer: str) -> None:
            assert bearer == marker

        def remove(self) -> bool:
            return False

    class Fixture:
        def __init__(self, *, expected_bearer: str) -> None:
            assert expected_bearer == marker

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            del exc_type, exc, traceback

    monkeypatch.setattr(
        interactive_macos_check.secrets,
        "token_urlsafe",
        lambda size: marker,
    )
    monkeypatch.setattr(
        interactive_macos_check,
        "MacOSUserPresenceBackend",
        Backend,
    )
    monkeypatch.setattr(interactive_macos_check, "_LoopbackFixture", Fixture)
    monkeypatch.setattr(
        interactive_macos_check,
        "_exercise",
        lambda **kwargs: True,
    )

    assert interactive_macos_check.run(["--run"]) == 1
    captured = capsys.readouterr()
    assert marker not in captured.out
    assert marker not in captured.err


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS framework check")
def test_security_framework_exposes_user_presence_access_control() -> None:
    native = SecurityFrameworkKeychain()

    assert native.available() is True
