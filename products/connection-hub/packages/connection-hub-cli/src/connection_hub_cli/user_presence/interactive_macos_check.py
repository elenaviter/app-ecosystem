from __future__ import annotations

import argparse
import secrets
import sys
import uuid

from connection_hub_cli.user_presence.contracts import ApprovalRequest
from connection_hub_cli.user_presence.errors import UserPresenceError
from connection_hub_cli.user_presence.macos import MacOSUserPresenceBackend


def _request(*, operation: str) -> ApprovalRequest:
    return ApprovalRequest.bind(
        target_key="interactive:disposable:tenant:project",
        caller_profile="interactive-human",
        access_id="interactive-access",
        resource="kdcube.host",
        operation=operation,
        method="POST",
        target="https://localhost",
        path="/interactive-user-presence-check",
        body=b'{"disposable":true}',
        display_summary="Run the disposable KDCube user-presence check",
    )


def run() -> int:
    parser = argparse.ArgumentParser(
        description="Interactively verify macOS Keychain userPresence enforcement."
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="confirm that an interactive macOS authentication window is available",
    )
    args = parser.parse_args()
    if not args.run:
        parser.error("pass --run only during a coordinated user-present window")

    backend = MacOSUserPresenceBackend(credential_ref=f"check-{uuid.uuid4().hex}")
    if not backend.available():
        print("FAIL: macOS user-presence support is unavailable.")
        return 1

    enrolled = False
    try:
        backend.enroll(secrets.token_urlsafe(48))
        enrolled = True
        request = _request(operation="host.status")

        input("Press Enter, then CANCEL the system authentication prompt: ")
        try:
            backend.approve(request)
        except UserPresenceError as exc:
            if exc.code != "user_presence_cancelled":
                print(f"FAIL: cancellation returned {exc.code}.")
                return 1
        else:
            print("FAIL: the cancellation check was approved.")
            return 1

        input("Press Enter, then APPROVE the system authentication prompt: ")
        result = backend.approve(request)
        if not result.authorizes(request):
            print("FAIL: approval was not bound to the requested digest.")
            return 1
        if result.authorizes(_request(operation="host.reload")):
            print("FAIL: approval authorized a different operation.")
            return 1
        if result.signed_proof is not None:
            print("FAIL: the bearer-backed adapter unexpectedly returned proof bytes.")
            return 1
        print(
            "PASS: cancellation denied use and approval matched only the exact "
            "request."
        )
        return 0
    finally:
        if enrolled:
            try:
                backend.remove()
            except UserPresenceError as exc:
                print(f"FAIL: disposable Keychain item cleanup returned {exc.code}.")


if __name__ == "__main__":
    sys.exit(run())
