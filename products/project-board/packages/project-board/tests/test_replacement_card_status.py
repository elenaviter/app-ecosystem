"""A server-side Card binding refusal is not an instruction to authorize again."""
from __future__ import annotations

import asyncio

import pytest

from project_board.client import first_run
from project_board.client import cli
from project_board.client.cli import _channel_not_usable_error
from project_board.client.relay_pacing import (
    PACING_FILENAME, RelayPacing, channel_pending_refusal, connection_hub_failure_code,
)
from project_board.client.render import render_envelope
from project_board.contract.errors import DomainError
from relay_helpers import make_host, make_supervisor

CAUSE = "card_transactions_direct_write_refused"


def test_pending_turn_carries_only_the_hub_code_to_status(tmp_path, monkeypatch):
    async def scenario():
        host, identity, channel = make_host(tmp_path, authorized=False)
        supervisor = make_supervisor(host)
        error = DomainError("work_project_control_connection_hub_refused", "private response text",
                            status=409, details={"connection_hub_error": CAUSE,
                                                "reason": "private details must not persist"})
        await supervisor._finish_pending_turn(host, channel, "test-fingerprint", error)
        refusal = channel_pending_refusal(host.path, identity.worker_name)
        assert refusal["connection_hub_error"] == CAUSE
        assert refusal["credential"] is False
        assert "private" not in (tmp_path / PACING_FILENAME).read_text()
        session = {"enrolled": True, "channel_state": "pending_authorization",
                   "authorization": first_run.AUTHORIZED, "refusal": refusal}
        next_step = first_run._next_step(first_run.SESSION_NOT_ATTENDING,
                                       config=str(host.path), relay={}, session=session)
        assert next_step["step"] == "resolve_channel_refusal"
        assert next_step["command"] == ""
        assert CAUSE in next_step["explain"]
        text = render_envelope({"ok": True, "result": {
            "schema": first_run.SCHEMA, "state": first_run.SESSION_NOT_ATTENDING,
            "machine": {}, "session": session, "next": next_step,
        }})
        assert f"Connection Hub cause: {CAUSE}" in text
        assert "refusal: work_project_control_connection_hub_refused" in text
        assert "pb worker authorize" not in text
        monkeypatch.setattr(cli, "inspect_profile_metadata", lambda *args: {"state": first_run.AUTHORIZED})
        send_error = _channel_not_usable_error(host.path, channel, sending=True)
        assert send_error.details["connection_hub_error"] == CAUSE
        assert "authorize" not in send_error.details.get("required_action", "")

    asyncio.run(scenario())


@pytest.mark.parametrize("cause", ["", CAUSE])
def test_missing_profile_keeps_authorize_even_with_an_old_noncredential_refusal(tmp_path, monkeypatch, cause):
    host, _, channel = make_host(tmp_path, authorized=False)
    pacing = RelayPacing(tmp_path / PACING_FILENAME)
    pacing.record_pending_refusal(channel.worker_name, fingerprint="old", permanent=True,
                                 reason="handshake_timeout", connection_hub_error=cause)
    refusal = channel_pending_refusal(host.path, channel.worker_name)
    session = {"enrolled": True, "profile": channel.profile, "channel_state": "pending_authorization",
               "authorization": "profile_metadata_absent", "refusal": refusal}
    step = first_run._next_step(first_run.SESSION_NOT_ATTENDING, config=str(host.path), relay={}, session=session)
    assert step["step"] == "authorize_profile"
    monkeypatch.setattr(cli, "inspect_profile_metadata", lambda *args: {"state": "profile_metadata_absent"})
    assert "authorize" in _channel_not_usable_error(host.path, channel).details["required_action"]


@pytest.mark.parametrize("value", ["secret message", "https://example.test/token", "x" * 129, {}, None])
def test_hub_cause_must_be_a_bounded_code(tmp_path, value):
    error = DomainError("outer", "ignored", details={"connection_hub_error": value})
    assert connection_hub_failure_code(error) == ""
    pacing = RelayPacing(tmp_path / PACING_FILENAME)
    pacing.record_pending_refusal("test-worker", fingerprint="test", permanent=True,
                                 reason="outer", connection_hub_error=value)
    assert channel_pending_refusal(tmp_path / "relay.json", "test-worker")["connection_hub_error"] == ""


def test_actual_credential_refusal_still_requires_authorization():
    session = {"enrolled": True, "channel_state": "pending_authorization", "profile": "test-profile",
               "refusal": {"credential": True, "permanent": True, "reason": "delegated_access_not_active"}}
    step = first_run._next_step(first_run.SESSION_NOT_ATTENDING, config="test", relay={}, session=session)
    assert step["step"] == "authorize_profile"
    assert step["command"] == "pb worker authorize test-profile --device"
