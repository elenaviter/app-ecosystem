from __future__ import annotations

import threading
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit

import pytest

from connection_hub_cli.authorization.callback import LoopbackCallbackServer
from connection_hub_cli.errors import AuthorizationError


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def test_callback_rejects_wrong_state_without_consuming_the_listener() -> None:
    server = LoopbackCallbackServer(
        expected_state="expected-state",
        expected_issuer="https://auth.example.test",
        issuer_required=True,
    )
    try:
        callback_url = urlsplit(server.redirect_uri)
        assert callback_url.hostname == "127.0.0.1"
        assert callback_url.port is not None
        assert callback_url.path == "/callback"

        wrong = urlencode(
            {
                "state": "wrong-state",
                "code": "attacker-code",
                "iss": "https://auth.example.test",
            }
        )
        status, body = _get(f"{server.redirect_uri}?{wrong}")
        assert status == 400
        assert "attacker-code" not in body

        correct = urlencode(
            {
                "state": "expected-state",
                "code": "real-code",
                "iss": "https://auth.example.test",
            }
        )
        status, body = _get(f"{server.redirect_uri}?{correct}")
        callback = server.wait(timeout_seconds=1)
        assert status == 200
        assert "Return to the terminal to confirm setup." in body
        assert "Authorization complete" not in body
        assert "real-code" not in body
        assert callback.code == "real-code"
        assert callback.issuer == "https://auth.example.test"
    finally:
        server.close()


def test_callback_requires_advertised_issuer_and_rejects_replay() -> None:
    server = LoopbackCallbackServer(
        expected_state="expected-state",
        expected_issuer="https://auth.example.test",
        issuer_required=True,
    )
    try:
        missing_issuer = urlencode(
            {"state": "expected-state", "code": "missing-issuer-code"}
        )
        status, _body = _get(f"{server.redirect_uri}?{missing_issuer}")
        assert status == 400

        accepted = urlencode(
            {
                "state": "expected-state",
                "code": "accepted-code",
                "iss": "https://auth.example.test",
            }
        )
        assert _get(f"{server.redirect_uri}?{accepted}")[0] == 200
        assert _get(f"{server.redirect_uri}?{accepted}")[0] == 409
        assert server.wait(timeout_seconds=1).code == "accepted-code"
    finally:
        server.close()


def test_callback_turns_provider_denial_into_a_safe_error() -> None:
    marker = "provider-description-secret"
    server = LoopbackCallbackServer(
        expected_state="expected-state",
        expected_issuer="https://auth.example.test",
        issuer_required=False,
    )
    try:
        query = urlencode(
            {
                "state": "expected-state",
                "error": "access_denied",
                "error_description": marker,
            }
        )
        status, body = _get(f"{server.redirect_uri}?{query}")
        assert status == 400
        assert marker not in body
        with pytest.raises(AuthorizationError) as raised:
            server.wait(timeout_seconds=1)
        assert raised.value.code == "oauth_authorization_denied"
        assert marker not in str(raised.value)
    finally:
        server.close()


def test_closing_callback_wakes_a_pending_waiter() -> None:
    server = LoopbackCallbackServer(
        expected_state="expected-state",
        expected_issuer="https://auth.example.test",
        issuer_required=False,
    )
    errors: list[AuthorizationError] = []

    def wait() -> None:
        try:
            server.wait(timeout_seconds=30)
        except AuthorizationError as exc:
            errors.append(exc)

    waiter = threading.Thread(target=wait)
    waiter.start()
    server.close()
    waiter.join(timeout=1)

    assert waiter.is_alive() is False
    assert len(errors) == 1
    assert errors[0].code == "oauth_callback_closed"
