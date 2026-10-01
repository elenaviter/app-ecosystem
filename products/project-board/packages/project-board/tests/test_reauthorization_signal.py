"""An agent learns at once whether only the operator can repair its channel.

W457. A channel the relay parked after the server refused its credential
(``credential_refused``) makes every governed command fail with
``work_worker_reauthorization_required``, naming the exact command the operator
runs. A channel the relay is reconnecting after a transport, metadata or lock
failure says that this error does not call for re-authorization. Both are
decided before a command waits on the channel: a ``--work-ref`` send does not
spend its 90-second item check on a channel that cannot carry it.
"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest

from project_board.client import cli, first_run, host_config, relay_pacing as pacing
from project_board.contract.errors import DomainError

from relay_helpers import make_host as _host

PROJECT_REF = "work:project:demo-project"
WORK_REF = "work:plan:node:20261001T000000Z:w1:an-item"


def _pacing(config_path: Path) -> pacing.RelayPacing:
    return pacing.RelayPacing(Path(config_path).parent / pacing.PACING_FILENAME, rng=lambda: 1.0)


def _park(host, identity, *, reason: str, credential: bool) -> None:
    """What the relay does when a channel's observation is terminal."""

    _pacing(host.path).record_pending_refusal(
        identity.worker_name,
        fingerprint="fingerprint",
        permanent=True,
        reason=reason,
        credential=credential,
    )
    host_config.set_worker_channel_state(
        host.path, identity=identity, state="pending_authorization", expected_state="active"
    )


def _mark_reconnecting(host, identity) -> None:
    _pacing(host.path).record_failure(
        identity.worker_name, pacing.HANDSHAKE_TIMEOUT_REASON, handshake_timeout=True
    )


def _coordinate_args(host, identity) -> Namespace:
    return Namespace(
        command="coordinate",
        action="project.plan.item",
        contract=False,
        object_ref=PROJECT_REF,
        payload_json='{"item_key":"W1"}',
        payload_file="",
        runtime_kind=identity.runtime_kind,
        runtime_session_id=identity.runtime_session_id,
        config=str(host.path),
        route="relay",
        timeout_seconds=30.0,
    )


def _send_args(host, identity, **overrides: Any) -> Namespace:
    values = {
        "command": "worker",
        "worker_command": "send",
        "runtime_kind": identity.runtime_kind,
        "runtime_session_id": identity.runtime_session_id,
        "config": str(host.path),
        "project_ref": None,
        "recipient": "operator",
        "route": "auto",
        "kind": "blocked",
        "subject": "A note for the operator",
        "body": "one line",
        "body_file": None,
        "payload_file": None,
        "work_ref": None,
        "correlation_id": None,
        "reply_to": None,
        "idempotency_key": "w457-send-1",
        "attach": None,
    }
    values.update(overrides)
    return Namespace(**values)


def _report_args(host, identity) -> Namespace:
    return Namespace(
        command="worker",
        worker_command="report",
        runtime_kind=identity.runtime_kind,
        runtime_session_id=identity.runtime_session_id,
        config=str(host.path),
        project_ref=PROJECT_REF,
        assignment_ref="work:assignment:20261001T000000Z:assignment_1:a",
        ownership_version=1,
        state="working",
        summary="started",
        summary_file=None,
        result_ref=None,
        source_event_ref="work:mail:20261001T000000Z:mail_1:a",
        review_look_at=None,
        review_could_not_verify=None,
        reviewer=None,
        merged=None,
        deploy=None,
        nothing_to_deploy=False,
        scope="",
        wait_seconds=1.0,
    )


@pytest.fixture
def no_item_check(monkeypatch):
    """Fail the test if a command reaches the item check or the report queue."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the channel was not checked before the wait")

    monkeypatch.setattr(cli, "require_plan_item", refuse)
    monkeypatch.setattr(cli, "submit_assignment_report", refuse)


# -- the relay's record --------------------------------------------------------


def test_the_pending_refusal_reader_returns_what_the_relay_recorded(tmp_path):
    host, identity, _channel = _host(tmp_path)
    assert pacing.channel_pending_refusal(host.path, identity.worker_name) is None

    _park(host, identity, reason="oauth_token_request_failed", credential=True)

    refusal = pacing.channel_pending_refusal(host.path, identity.worker_name)
    assert refusal["credential"] is True
    assert refusal["permanent"] is True
    assert refusal["reason"] == "oauth_token_request_failed"
    assert refusal["refused_at"].endswith("Z")
    assert "fingerprint" not in refusal


# -- a refused credential: only the operator can act --------------------------


def _assert_reauthorization_required(error: DomainError, channel) -> None:
    assert error.code == "work_worker_reauthorization_required"
    details = error.details
    command = f"pb worker authorize {channel.profile} --device"
    assert details["credential_refused"] is True
    assert details["who_acts"] == "operator"
    assert details["required_action"] == command
    assert details["retryable"] is False
    assert details["reason"] == "oauth_token_request_failed"
    message = str(error)
    assert command in message
    assert "Retrying will not help" in message
    assert "operator" in message


def test_coordinate_names_the_operator_and_the_command_for_a_refused_credential(tmp_path):
    host, identity, channel = _host(tmp_path)
    _park(host, identity, reason="oauth_token_request_failed", credential=True)

    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(_coordinate_args(host, identity))

    _assert_reauthorization_required(refused.value, channel)


def test_unlinked_mail_from_a_refused_credential_is_refused_not_queued(tmp_path, no_item_check):
    host, identity, channel = _host(tmp_path)
    _park(host, identity, reason="oauth_token_request_failed", credential=True)

    with pytest.raises(DomainError) as refused:
        cli._worker_command(_send_args(host, identity))

    _assert_reauthorization_required(refused.value, channel)
    assert refused.value.details["delivered"] is False
    assert refused.value.details["idempotency_key"] == "w457-send-1"


def test_a_report_from_a_refused_credential_is_refused_before_it_waits(tmp_path, no_item_check):
    host, identity, channel = _host(tmp_path)
    _park(host, identity, reason="oauth_token_request_failed", credential=True)

    with pytest.raises(DomainError) as refused:
        cli._worker_command(_report_args(host, identity))

    _assert_reauthorization_required(refused.value, channel)


# -- no proof of a refused credential: never claimed --------------------------


def test_a_parked_channel_without_credential_proof_does_not_claim_a_refused_grant(tmp_path):
    host, identity, _channel = _host(tmp_path)
    _park(host, identity, reason="oauth_mcp_endpoint_unreachable", credential=False)

    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(_coordinate_args(host, identity))

    assert refused.value.code == "work_worker_channel_not_active"
    assert refused.value.details["credential_refused"] is False
    assert "does not prove the grant was refused" in str(refused.value)
    assert "pb worker inspect" in str(refused.value)


def test_a_never_authorized_profile_points_at_inspect_and_the_command(tmp_path):
    host, identity, channel = _host(tmp_path, authorized=False)

    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(_coordinate_args(host, identity))

    assert refused.value.code == "work_worker_channel_not_active"
    assert refused.value.details["required_action"] == (
        f"pb worker authorize {channel.profile} --device"
    )
    assert refused.value.details["credential_refused"] is False


def test_a_disabled_channel_names_the_operator_and_no_authorize_command(tmp_path):
    host, identity, _channel = _host(tmp_path)
    host_config.set_worker_channel_state(host.path, identity=identity, state="disabled")

    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(_coordinate_args(host, identity))

    assert refused.value.code == "work_worker_channel_not_active"
    assert refused.value.details["who_acts"] == "operator"
    assert "required_action" not in refused.value.details
    assert "disabled by the operator" in str(refused.value)


# -- a transient failure: re-authorization is not indicated -------------------


def test_reconnecting_says_it_does_not_call_for_reauthorization(tmp_path):
    host, identity, _channel = _host(tmp_path)
    _mark_reconnecting(host, identity)

    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(_coordinate_args(host, identity))

    assert refused.value.code == "work_coordinate_channel_reconnecting"
    assert refused.value.details["reauthorization_indicated"] is False
    assert "does not indicate that re-authorization is needed" in str(refused.value)


def test_a_linked_send_on_a_reconnecting_channel_fails_before_the_item_check(
    tmp_path, no_item_check
):
    host, identity, _channel = _host(tmp_path)
    _mark_reconnecting(host, identity)

    with pytest.raises(DomainError) as refused:
        cli._worker_command(
            _send_args(host, identity, project_ref=PROJECT_REF, work_ref=WORK_REF)
        )

    assert refused.value.code == "work_send_channel_reconnecting"
    assert refused.value.details["delivered"] is False
    assert refused.value.details["reauthorization_indicated"] is False


def test_a_linked_send_on_a_refused_credential_fails_before_the_item_check(
    tmp_path, no_item_check
):
    host, identity, channel = _host(tmp_path)
    _park(host, identity, reason="oauth_token_request_failed", credential=True)

    with pytest.raises(DomainError) as refused:
        cli._worker_command(
            _send_args(host, identity, project_ref=PROJECT_REF, work_ref=WORK_REF)
        )

    _assert_reauthorization_required(refused.value, channel)


# -- status and inspect name the same thing ------------------------------------


def test_status_names_the_operator_for_a_refused_credential():
    step = first_run._next_step(
        first_run.SESSION_NOT_ATTENDING,
        config="relay.json",
        relay={"installed": True, "running": True},
        session={
            "enrolled": True,
            "profile": "problem-board-codex-one",
            "channel_state": "pending_authorization",
            "authorization": "authorized",
            "refusal": {
                "credential": True,
                "reason": "oauth_token_request_failed",
                "refused_at": "2026-10-01T17:15:24Z",
            },
        },
    )

    assert step["step"] == "authorize_profile"
    assert step["command"] == "pb worker authorize problem-board-codex-one --device"
    assert "refused this agent's Card credential" in step["explain"]
    assert "Only the operator can fix it" in step["explain"]


def test_status_reconnecting_says_reauthorization_is_not_indicated():
    step = first_run._next_step(
        first_run.SESSION_RECONNECTING,
        config="relay.json",
        relay={"installed": True, "running": True},
        session={"connection": {"reason": "oauth_mcp_endpoint_unreachable", "attempts": 1}},
    )

    assert step["step"] == "wait_for_reconnect"
    assert "does not indicate that re-authorization is needed" in step["explain"]


# -- a pending channel the relay keeps retrying: still retryable ---------------


def _pending_retrying(host, identity, reason: str) -> None:
    """What the relay's pending loop records for a retryable failure."""

    state = _pacing(host.path)
    state.record_pending_refusal(
        identity.worker_name,
        fingerprint="fingerprint",
        permanent=False,
        reason=reason,
        credential=False,
    )
    state.record_failure(identity.worker_name, reason)
    host_config.set_worker_channel_state(
        host.path, identity=identity, state="pending_authorization", expected_state="active"
    )


TRANSIENT_REASONS = (
    "oauth_mcp_endpoint_unreachable",
    "oauth_token_request_failed",
    "oauth_profile_lock_timeout",
)


@pytest.mark.parametrize("reason", TRANSIENT_REASONS)
def test_a_pending_channel_retrying_a_transient_failure_stays_retryable(tmp_path, reason):
    host, identity, _channel = _host(tmp_path)
    _pending_retrying(host, identity, reason)

    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(_coordinate_args(host, identity))

    error = refused.value
    assert error.code == "work_coordinate_channel_reconnecting"
    assert error.details["retryable"] is True
    assert error.details["reauthorization_indicated"] is False
    assert error.details["channel_state"] == "pending_authorization"
    assert error.details["last_error"] == reason
    assert error.details["next_attempt_at"]
    assert "required_action" not in error.details
    assert "who_acts" not in error.details
    assert "does not indicate that re-authorization is needed" in str(error)


@pytest.mark.parametrize("reason", TRANSIENT_REASONS)
def test_a_linked_send_on_a_retrying_pending_channel_is_retryable_before_the_item_check(
    tmp_path, no_item_check, reason
):
    host, identity, _channel = _host(tmp_path)
    _pending_retrying(host, identity, reason)

    with pytest.raises(DomainError) as refused:
        cli._worker_command(
            _send_args(host, identity, project_ref=PROJECT_REF, work_ref=WORK_REF)
        )

    error = refused.value
    assert error.code == "work_send_channel_reconnecting"
    assert error.details["retryable"] is True
    assert error.details["delivered"] is False
    assert error.details["reauthorization_indicated"] is False
    assert "required_action" not in error.details


# -- producer to consumer: the relay's own cycle, then the agent's next command --


def _refusing_supervisor(host, error: BaseException):
    """A relay whose channel open fails the way Connection Hub refuses it."""

    from contextlib import asynccontextmanager

    from project_board.client import relay

    @asynccontextmanager
    async def connector(_host, _channel, *, replacement_epoch):
        raise error
        yield  # pragma: no cover - the open fails before a session exists

    return relay.ProblemBoardRelaySupervisor(config_path=host.path, connector=connector)


def _invalid_grant() -> DomainError:
    return DomainError(
        "oauth_token_request_failed",
        "OAuth token POST returned HTTP 400: invalid_grant.",
        status=400,
        details={"oauth_error": "invalid_grant"},
    )


def _unreachable() -> DomainError:
    return DomainError(
        "oauth_mcp_endpoint_unreachable",
        "The MCP endpoint could not be reached for OAuth discovery.",
        status=503,
    )


def _cycle(supervisor) -> None:
    """One relay cycle; a cycle where every channel failed ends the way the
    relay's own loop expects, with a retryable error it catches."""

    import asyncio

    from service_foundation.host_relay.contracts import HostRelayRetryableError

    try:
        asyncio.run(supervisor.poll_once())
    except HostRelayRetryableError:
        pass


def test_a_refused_grant_seen_by_the_relay_reaches_the_next_command(tmp_path):
    host, identity, channel = _host(tmp_path)

    _cycle(_refusing_supervisor(host, _invalid_grant()))

    parked = host_config.HostRelayConfig.load(host.path).worker(identity)
    assert parked.state == "pending_authorization"
    refusal = pacing.channel_pending_refusal(host.path, identity.worker_name)
    assert refusal["credential"] is True and refusal["permanent"] is True
    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(_coordinate_args(host, identity))
    _assert_reauthorization_required(refused.value, channel)


def test_an_unreachable_endpoint_seen_by_the_relay_stays_retryable(tmp_path):
    host, identity, _channel = _host(tmp_path)

    _cycle(_refusing_supervisor(host, _unreachable()))

    assert host_config.HostRelayConfig.load(host.path).worker(identity).state == "active"
    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(_coordinate_args(host, identity))
    assert refused.value.code == "work_coordinate_channel_reconnecting"
    assert refused.value.details["reauthorization_indicated"] is False
    assert "required_action" not in refused.value.details


def test_a_parked_channel_retried_into_an_unreachable_endpoint_stays_retryable(tmp_path):
    host, identity, _channel = _host(tmp_path)
    _cycle(_refusing_supervisor(host, _invalid_grant()))
    # The pending loop retries the parked channel later; this time the
    # endpoint cannot be reached, which says nothing about the grant.
    state = _pacing(host.path)
    state._state["channels"].pop(identity.worker_name, None)
    state._state["pending"][identity.worker_name]["fingerprint"] = "changed"
    state._save()

    _cycle(_refusing_supervisor(host, _unreachable()))

    refusal = pacing.channel_pending_refusal(host.path, identity.worker_name)
    assert refusal["permanent"] is False and refusal["credential"] is False
    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(_coordinate_args(host, identity))
    assert refused.value.code == "work_coordinate_channel_reconnecting"
    assert refused.value.details["channel_state"] == "pending_authorization"
    assert refused.value.details["retryable"] is True
