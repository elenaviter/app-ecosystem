from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from connection_hub_cli.user_presence import (
    BoundHttpOperation,
    MacOSUserPresenceBackend,
    UserPresenceError,
)
from connection_hub_cli.user_presence.native_macos import SecretLease

_SECRET = b"loopback-disposable-credential"


class _NativeKeychain:
    def __init__(self) -> None:
        self.read_error: Exception | None = None
        self.read_count = 0

    def available(self) -> bool:
        return True

    def store(self, account: str, secret: bytearray) -> None:
        del account, secret

    def read(self, account: str, *, prompt: str) -> SecretLease:
        del account, prompt
        self.read_count += 1
        if self.read_error is not None:
            raise self.read_error
        return SecretLease(bytearray(_SECRET))

    def delete(self, account: str, *, prompt: str) -> bool:
        del account, prompt
        return True


class _QuietHandler(BaseHTTPRequestHandler):
    authorization = ""
    path_seen = ""
    body_seen = b""
    request_count = 0
    redirect_target = ""

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _CaptureHandler(_QuietHandler):
    def do_POST(self) -> None:
        type(self).request_count += 1
        type(self).authorization = str(self.headers.get("Authorization") or "")
        type(self).path_seen = self.path
        length = int(self.headers.get("Content-Length") or "0")
        type(self).body_seen = self.rfile.read(length)
        response = b'{"accepted":true}'
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


class _EchoHandler(_QuietHandler):
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(_SECRET)))
        self.end_headers()
        self.wfile.write(_SECRET)


class _RedirectHandler(_QuietHandler):
    def do_GET(self) -> None:
        type(self).authorization = str(self.headers.get("Authorization") or "")
        self.send_response(302)
        self.send_header("Location", type(self).redirect_target)
        self.end_headers()


class _DestinationHandler(_QuietHandler):
    def do_GET(self) -> None:
        type(self).authorization = str(self.headers.get("Authorization") or "")
        type(self).request_count += 1
        self.send_response(200)
        self.end_headers()


def _serve(
    handler: type[BaseHTTPRequestHandler],
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _close(server: ThreadingHTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def _operation(
    *,
    target: str,
    method: str = "POST",
    path: str = "/api/manage/reload?wait=true",
    body: bytes = b'{"source":"git"}',
) -> BoundHttpOperation:
    return BoundHttpOperation.bind(
        target_key="loopback:tenant-a:project-a",
        tenant="tenant-a",
        project="project-a",
        caller_profile="human-admin",
        access_id="access-123",
        resource="kdcube.host",
        operation="host.reload",
        method=method,
        target=target,
        path=path,
        body=body,
        display_summary="Reload the selected KDCube host",
    )


def _backend(native: _NativeKeychain | None = None) -> MacOSUserPresenceBackend:
    return MacOSUserPresenceBackend._with_native_for_testing(
        credential_ref="loopback-test",
        native=native or _NativeKeychain(),
        platform_name="Darwin",
    )


def test_bound_execution_uses_exact_loopback_request() -> None:
    server, thread = _serve(_CaptureHandler)
    _CaptureHandler.authorization = ""
    _CaptureHandler.path_seen = ""
    _CaptureHandler.body_seen = b""
    _CaptureHandler.request_count = 0
    body = b'{"source":"git"}'
    operation = _operation(target=f"http://127.0.0.1:{server.server_port}")

    try:
        result = _backend().execute(operation, body=body, timeout_seconds=5.0)
    finally:
        _close(server, thread)

    assert result.status_code == 202
    assert result.body == b'{"accepted":true}'
    assert result.operation_digest == operation.operation_digest
    assert _CaptureHandler.request_count == 1
    assert _CaptureHandler.authorization == f"Bearer {_SECRET.decode('ascii')}"
    assert _CaptureHandler.path_seen == "/api/manage/reload?wait=true"
    assert _CaptureHandler.body_seen == body


def test_cancellation_causes_no_loopback_dispatch() -> None:
    server, thread = _serve(_CaptureHandler)
    _CaptureHandler.request_count = 0
    native = _NativeKeychain()
    native.read_error = UserPresenceError(
        "user_presence_cancelled",
        "The user cancelled system authentication.",
    )
    operation = _operation(target=f"http://127.0.0.1:{server.server_port}")

    try:
        with pytest.raises(UserPresenceError) as raised:
            _backend(native).execute(operation, body=b'{"source":"git"}')
    finally:
        _close(server, thread)

    assert raised.value.code == "user_presence_cancelled"
    assert native.read_count == 1
    assert _CaptureHandler.request_count == 0


def test_bound_execution_does_not_follow_redirects_with_credential() -> None:
    destination, destination_thread = _serve(_DestinationHandler)
    redirect, redirect_thread = _serve(_RedirectHandler)
    _RedirectHandler.authorization = ""
    _DestinationHandler.authorization = ""
    _DestinationHandler.request_count = 0
    _RedirectHandler.redirect_target = (
        f"http://127.0.0.1:{destination.server_port}/destination"
    )
    operation = _operation(
        target=f"http://127.0.0.1:{redirect.server_port}",
        method="GET",
        path="/redirect",
        body=b"",
    )

    try:
        result = _backend().execute(operation, body=b"", timeout_seconds=5.0)
    finally:
        _close(redirect, redirect_thread)
        _close(destination, destination_thread)

    assert result.status_code == 302
    assert _RedirectHandler.authorization == f"Bearer {_SECRET.decode('ascii')}"
    assert _DestinationHandler.authorization == ""
    assert _DestinationHandler.request_count == 0


def test_response_containing_exact_credential_is_blocked() -> None:
    server, thread = _serve(_EchoHandler)
    operation = _operation(target=f"http://127.0.0.1:{server.server_port}")

    try:
        with pytest.raises(UserPresenceError) as raised:
            _backend().execute(operation, body=b'{"source":"git"}')
    finally:
        _close(server, thread)

    assert raised.value.code == "credential_exposure_blocked"
    assert _SECRET.decode("ascii") not in str(raised.value)
