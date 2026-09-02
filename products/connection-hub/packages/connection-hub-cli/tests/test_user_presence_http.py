from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from connection_hub_cli.user_presence import ApprovalRequest
from connection_hub_cli.user_presence.operations import UrllibBoundHttpTransport


class _QuietHandler(BaseHTTPRequestHandler):
    authorization_seen = False
    redirect_target = ""

    def log_message(self, format: str, *args: object) -> None:
        del format, args


class _RedirectHandler(_QuietHandler):
    def do_GET(self) -> None:
        type(self).authorization_seen = bool(self.headers.get("Authorization"))
        self.send_response(302)
        self.send_header("Location", type(self).redirect_target)
        self.end_headers()


class _DestinationHandler(_QuietHandler):
    def do_GET(self) -> None:
        type(self).authorization_seen = bool(self.headers.get("Authorization"))
        self.send_response(200)
        self.end_headers()


def _serve(
    handler: type[BaseHTTPRequestHandler],
) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_bound_transport_does_not_follow_redirects_with_the_credential() -> None:
    destination, destination_thread = _serve(_DestinationHandler)
    redirect, redirect_thread = _serve(_RedirectHandler)
    _RedirectHandler.authorization_seen = False
    _DestinationHandler.authorization_seen = False
    _RedirectHandler.redirect_target = (
        f"http://127.0.0.1:{destination.server_port}/destination"
    )
    request = ApprovalRequest.bind(
        target_key="local-redirect-test",
        caller_profile="human-admin",
        access_id="access-123",
        resource="kdcube.host",
        operation="host.status",
        method="GET",
        target=f"http://127.0.0.1:{redirect.server_port}",
        path="/redirect",
        body=None,
        display_summary="Read the selected KDCube host status",
    )

    try:
        result = UrllibBoundHttpTransport().send(
            request,
            body=b"",
            credential=memoryview(bytearray(b"redirect-test-credential")),
            timeout_seconds=5.0,
        )
    finally:
        redirect.shutdown()
        destination.shutdown()
        redirect.server_close()
        destination.server_close()
        redirect_thread.join(timeout=2)
        destination_thread.join(timeout=2)

    assert result.status_code == 302
    assert _RedirectHandler.authorization_seen is True
    assert _DestinationHandler.authorization_seen is False
