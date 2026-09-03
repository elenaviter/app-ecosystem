from __future__ import annotations

import argparse
import re
import secrets
import sys
import threading
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

from connection_hub_cli.user_presence.contracts import BoundHttpOperation
from connection_hub_cli.user_presence.errors import UserPresenceError
from connection_hub_cli.user_presence.macos import MacOSUserPresenceBackend

_BODY = b'{"disposable":true}'
_CHANGED_BODY = b'{"disposable":true,"revision":2}'
_SAFE_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True, slots=True)
class _ObservedRequest:
    method: str
    path: str
    body: bytes
    credential_matched: bool


class _CaptureState:
    def __init__(self, *, expected_bearer: str) -> None:
        self._expected_authorization = f"Bearer {expected_bearer}"
        self._lock = threading.Lock()
        self._requests: list[_ObservedRequest] = []

    def record(self, *, method: str, path: str, body: bytes, authorization: str) -> None:
        observed = _ObservedRequest(
            method=method,
            path=path,
            body=body,
            credential_matched=authorization == self._expected_authorization,
        )
        with self._lock:
            self._requests.append(observed)

    def snapshot(self) -> tuple[_ObservedRequest, ...]:
        with self._lock:
            return tuple(self._requests)


class _CaptureServer(ThreadingHTTPServer):
    def __init__(self, *, expected_bearer: str) -> None:
        self.capture = _CaptureState(expected_bearer=expected_bearer)
        super().__init__(("127.0.0.1", 0), _CaptureHandler)


class _CaptureHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        if not 0 <= length <= 1024 * 1024:
            self.send_response(400)
            self.end_headers()
            return
        body = self.rfile.read(length)
        server = cast(_CaptureServer, self.server)
        server.capture.record(
            method=self.command,
            path=self.path,
            body=body,
            authorization=str(self.headers.get("Authorization") or ""),
        )
        response = b'{"accepted":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


class _LoopbackFixture:
    def __init__(self, *, expected_bearer: str) -> None:
        self._server = _CaptureServer(expected_bearer=expected_bearer)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _LoopbackFixture:  # noqa: PYI034 - Python 3.10
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def snapshot(self) -> tuple[_ObservedRequest, ...]:
        return self._server.capture.snapshot()


def _operation(
    *,
    origin: str,
    operation: str,
    body: bytes,
) -> BoundHttpOperation:
    return BoundHttpOperation.bind(
        target_key="interactive:disposable:local-tenant:local-project",
        tenant="local-tenant",
        project="local-project",
        caller_profile="interactive-human",
        access_id="interactive-access",
        resource="kdcube.host",
        operation=operation,
        method="POST",
        target=origin,
        path="/interactive-user-presence-check?mode=disposable",
        body=body,
        display_summary="Disposable local user-presence execution check",
    )


def _safe_error_code(error: UserPresenceError) -> str:
    return error.code if _SAFE_CODE_RE.fullmatch(str(error.code)) else "unknown_error"


def _safe_error_label(error: UserPresenceError) -> str:
    code = _safe_error_code(error)
    status = error.native_status
    if isinstance(status, int) and -(2**31) <= status < 2**31:
        return f"{code} (OSStatus {status})"
    return code


def _cancel_without_dispatch(
    *,
    backend: MacOSUserPresenceBackend,
    fixture: _LoopbackFixture,
    operation: BoundHttpOperation,
    body: bytes,
    instruction: str,
) -> bool:
    before = len(fixture.snapshot())
    input(instruction)
    try:
        backend.execute(operation, body=body, timeout_seconds=10.0)
    except UserPresenceError as exc:
        if exc.code != "user_presence_cancelled":
            print(f"FAIL: cancellation returned {_safe_error_label(exc)}.")
            return False
    else:
        print("FAIL: the cancelled operation was dispatched.")
        return False
    if len(fixture.snapshot()) != before:
        print("FAIL: cancellation changed the loopback dispatch count.")
        return False
    return True


def _exercise(
    *,
    backend: MacOSUserPresenceBackend,
    fixture: _LoopbackFixture,
) -> bool:
    initial = _operation(
        origin=fixture.origin,
        operation="host.status",
        body=_BODY,
    )
    if not _cancel_without_dispatch(
        backend=backend,
        fixture=fixture,
        operation=initial,
        body=_BODY,
        instruction="Press Enter, then CANCEL the first system prompt: ",
    ):
        return False

    input("Press Enter, then APPROVE the exact bound operation: ")
    try:
        result = backend.execute(initial, body=_BODY, timeout_seconds=10.0)
    except UserPresenceError as exc:
        print(f"FAIL: approved execution returned {_safe_error_label(exc)}.")
        return False
    observed = fixture.snapshot()
    if result.status_code != 200 or len(observed) != 1:
        print("FAIL: approval did not produce exactly one successful request.")
        return False
    received = observed[0]
    if (
        received.method != "POST"
        or received.path != "/interactive-user-presence-check?mode=disposable"
        or received.body != _BODY
        or not received.credential_matched
    ):
        print("FAIL: the loopback server received different bound request fields.")
        return False

    changed_operation = _operation(
        origin=fixture.origin,
        operation="host.reload",
        body=_BODY,
    )
    if not _cancel_without_dispatch(
        backend=backend,
        fixture=fixture,
        operation=changed_operation,
        body=_BODY,
        instruction="Press Enter, then CANCEL the changed-operation prompt: ",
    ):
        return False

    changed_body = _operation(
        origin=fixture.origin,
        operation="host.status",
        body=_CHANGED_BODY,
    )
    return _cancel_without_dispatch(
        backend=backend,
        fixture=fixture,
        operation=changed_body,
        body=_CHANGED_BODY,
        instruction="Press Enter, then CANCEL the changed-body prompt: ",
    )


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Interactively verify bound macOS Keychain user presence."
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="confirm that an interactive macOS authentication window is available",
    )
    args = parser.parse_args(argv)
    if not args.run:
        parser.error("pass --run only during a coordinated user-present window")

    disposable_bearer = secrets.token_urlsafe(48)
    backend = MacOSUserPresenceBackend(credential_ref=f"check-{uuid.uuid4().hex}")
    if not backend.available():
        print("FAIL: macOS user-presence support is unavailable.")
        return 1

    check_passed = False
    cleanup_passed = True
    enrolled = False
    try:
        with _LoopbackFixture(expected_bearer=disposable_bearer) as fixture:
            backend.enroll(disposable_bearer)
            enrolled = True
            check_passed = _exercise(backend=backend, fixture=fixture)
    except UserPresenceError as exc:
        print(f"FAIL: interactive check returned {_safe_error_label(exc)}.")
    except Exception:  # noqa: BLE001 - interactive failures render fixed text only
        print("FAIL: the interactive check could not complete.")
    finally:
        if enrolled:
            try:
                cleanup_passed = backend.remove()
            except UserPresenceError as exc:
                cleanup_passed = False
                print(f"FAIL: cleanup returned {_safe_error_label(exc)}.")
            except Exception:  # noqa: BLE001 - cleanup text is untrusted
                cleanup_passed = False
                print("FAIL: disposable Keychain item cleanup failed.")

    if check_passed and cleanup_passed:
        print("PASS: bound execution and disposable-item cleanup succeeded.")
        return 0
    if not cleanup_passed:
        print("FAIL: disposable Keychain item cleanup was not confirmed.")
    return 1


if __name__ == "__main__":
    sys.exit(run())
