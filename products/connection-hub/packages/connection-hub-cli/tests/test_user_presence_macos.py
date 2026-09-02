from __future__ import annotations

import dataclasses
import logging
import platform

import pytest
from connection_hub_cli.user_presence import (
    ApprovalRequest,
    HttpOperationResult,
    MacOSUserPresenceBackend,
    UserPresenceError,
)
from connection_hub_cli.user_presence.native_macos import (
    SecretLease,
    SecurityFrameworkKeychain,
    _status_error,
)

_SECRET = b"protected-test-bearer-never-render"


def _request(
    *,
    body: bytes = b'{"ref":"main"}',
    operation: str = "host.reload",
) -> ApprovalRequest:
    return ApprovalRequest.bind(
        target_key="endpoint:https://host.example:tenant-a:project-a",
        caller_profile="human-admin",
        access_id="access-123",
        resource="kdcube.host",
        operation=operation,
        method="POST",
        target="https://host.example",
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


class _Transport:
    def __init__(self) -> None:
        self.saw_expected_credential = False
        self.calls = 0

    def send(
        self,
        request: ApprovalRequest,
        *,
        body: bytes,
        credential: memoryview,
        timeout_seconds: float,
    ) -> HttpOperationResult:
        self.calls += 1
        self.saw_expected_credential = bytes(credential) == _SECRET
        assert body == b'{"ref":"main"}'
        assert timeout_seconds == 15.0
        return HttpOperationResult(
            status_code=202,
            request_digest=request.request_digest,
            content_type="application/json",
            body=b'{"accepted":true}',
        )


def _backend(native: _NativeKeychain) -> MacOSUserPresenceBackend:
    return MacOSUserPresenceBackend(
        credential_ref="a" * 32,
        native=native,
        platform_name="Darwin",
    )


def test_enroll_passes_the_credential_only_to_native_storage() -> None:
    native = _NativeKeychain()

    result = _backend(native).enroll(f"Bearer {_SECRET.decode('ascii')}")

    assert result is None
    assert native.stored == _SECRET


def test_approve_returns_no_secret_and_wipes_the_lease() -> None:
    native = _NativeKeychain()

    result = _backend(native).approve(_request())

    assert result.approved is True
    assert result.signed_proof is None
    assert _SECRET.decode("ascii") not in repr(result)
    assert _SECRET.decode("ascii") not in str(result.to_safe_dict())
    assert _SECRET.decode("ascii") not in str(dataclasses.asdict(result))
    assert native.last_lease is not None
    with pytest.raises(UserPresenceError):
        native.last_lease.view()


def test_system_prompt_identifies_the_exact_operation() -> None:
    native = _NativeKeychain()

    _backend(native).approve(_request())

    assert "host.reload" in native.prompt
    assert "kdcube.host" in native.prompt
    assert "host.example" in native.prompt
    assert "human-admin" in native.prompt


def test_user_cancellation_is_structured_and_fails_closed() -> None:
    native = _NativeKeychain()
    native.read_error = UserPresenceError(
        "user_presence_cancelled", "The user cancelled system authentication."
    )

    with pytest.raises(UserPresenceError) as raised:
        _backend(native).approve(_request())

    assert raised.value.code == "user_presence_cancelled"


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
    backend = MacOSUserPresenceBackend(
        credential_ref="a" * 32,
        native=native,
        platform_name="Linux",
    )

    assert backend.available() is False
    with pytest.raises(UserPresenceError) as raised:
        backend.approve(_request())
    assert raised.value.code == "user_presence_unsupported_platform"
    assert native.read_count == 0


def test_body_mismatch_fails_before_native_credential_access() -> None:
    native = _NativeKeychain()
    transport = _Transport()

    with pytest.raises(UserPresenceError) as raised:
        _backend(native).execute_http(
            _request(), body=b'{"ref":"other"}', transport=transport
        )

    assert raised.value.code == "approval_digest_mismatch"
    assert native.read_count == 0
    assert transport.calls == 0


def test_exact_bound_http_request_executes_inside_the_backend() -> None:
    native = _NativeKeychain()
    transport = _Transport()

    result = _backend(native).execute_http(
        _request(),
        body=b'{"ref":"main"}',
        transport=transport,
        timeout_seconds=15.0,
    )

    assert result.status_code == 202
    assert result.body == b'{"accepted":true}'
    assert transport.saw_expected_credential is True
    assert native.last_lease is not None
    with pytest.raises(UserPresenceError):
        native.last_lease.view()


def test_result_for_another_digest_is_rejected() -> None:
    class WrongDigestTransport(_Transport):
        def send(self, request, **kwargs):
            del request, kwargs
            return HttpOperationResult(
                status_code=200,
                request_digest="0" * 64,
                content_type="application/json",
                body=b"{}",
            )

    with pytest.raises(UserPresenceError) as raised:
        _backend(_NativeKeychain()).execute_http(
            _request(), body=b'{"ref":"main"}', transport=WrongDigestTransport()
        )

    assert raised.value.code == "approval_digest_mismatch"


def test_response_containing_the_credential_is_blocked() -> None:
    class EchoTransport(_Transport):
        def send(self, request, **kwargs):
            del kwargs
            return HttpOperationResult(
                status_code=200,
                request_digest=request.request_digest,
                content_type="text/plain",
                body=_SECRET,
            )

    with pytest.raises(UserPresenceError) as raised:
        _backend(_NativeKeychain()).execute_http(
            _request(), body=b'{"ref":"main"}', transport=EchoTransport()
        )

    assert raised.value.code == "credential_exposure_blocked"
    assert _SECRET.decode("ascii") not in str(raised.value)


@pytest.mark.parametrize("failure_owner", ["native", "transport"])
def test_untrusted_failure_text_cannot_escape(
    failure_owner: str,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    native = _NativeKeychain()
    transport: _Transport = _Transport()
    if failure_owner == "native":
        native.read_error = RuntimeError(_SECRET.decode("ascii"))
    else:
        class FailingTransport(_Transport):
            def send(self, request, **kwargs):
                del request, kwargs
                raise RuntimeError(_SECRET.decode("ascii"))

        transport = FailingTransport()

    with caplog.at_level(logging.DEBUG), pytest.raises(UserPresenceError) as raised:
        _backend(native).execute_http(
            _request(), body=b'{"ref":"main"}', transport=transport
        )

    captured = capsys.readouterr()
    rendered = "\n".join(
        [
            captured.out,
            captured.err,
            caplog.text,
            str(raised.value),
            repr(raised.value),
            str(raised.value.to_dict()),
            repr(raised.value.__cause__),
        ]
    )
    assert _SECRET.decode("ascii") not in rendered
    assert raised.value.__cause__ is None


def test_secret_lease_repr_is_redacted_and_close_is_terminal() -> None:
    lease = SecretLease(bytearray(_SECRET))

    assert _SECRET.decode("ascii") not in repr(lease)
    assert str(lease) == "<redacted>"
    lease.close()
    with pytest.raises(UserPresenceError):
        lease.view()


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS framework check")
def test_security_framework_exposes_user_presence_access_control() -> None:
    native = SecurityFrameworkKeychain()

    assert native.available() is True
