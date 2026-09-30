"""A Codex wake taken twice without a receive, with mail still pending (W405).

The Spark session on 2026-09-29: the initial wake was consumed at 18:42Z, its
one retry consumed at 18:46Z, no receive followed, and new mail kept
coalescing into the same wake until the coordinator recovered it by hand at
22:47Z. The automatic retry ceiling of W198 stays: these tests reproduce the
stranded state through the store's own methods, show it in pb worker list,
and pin the one explicit recovery and what resolves it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from project_board.client import store as store_module
from project_board.client.render import render_envelope
from project_board.client.store import SharedFieldStore
from project_board.client.wake_recovery import recover_worker_wake
from project_board.contract.errors import DomainError

SESSION = "01a0daac-91ae-7730-81dd-9ffc77207b92"
WORKER = f"codex-{SESSION}"
REAL_FUTURE = store_module._future
WAKE = "wake_2e5781298233429ea2001280c4d4b398"


def _field(tmp_path: Path) -> SharedFieldStore:
    field = SharedFieldStore(tmp_path / "shared-field")
    field.initialize(field_id="stranded-wake")
    field.register_worker(
        worker_name=WORKER,
        runtime_kind="codex",
        runtime_session_id=SESSION,
        capabilities=[],
        authority_label="authority:spark",
    )
    field.listen_worker(WORKER, check_interval_seconds=30)
    return field


def _consumed(field: SharedFieldStore, *, retry: bool, submission: str) -> None:
    """One native submission that the session took and never acknowledged."""

    field.prepare_worker_session_wake(WORKER, message_refs=["work:mail:a"], wake_id=WAKE, retry=retry)
    field.record_worker_session_delivery(
        WORKER,
        adapter="codex-queue",
        state="attached",
        event_kind="input.available",
        delivered=True,
        message_refs=["work:mail:a"],
        wake_id=WAKE,
        prepared=True,
        queued_submission_id=submission,
    )
    # The queue no longer lists it: the session took the turn.
    field.record_worker_session_queue_reconciliation(
        WORKER, expected_wake_id=WAKE, result={"reconciled": True, "queued_submission_ids": []}
    )


@pytest.fixture
def stranded(tmp_path, monkeypatch):
    # Every acknowledgement deadline is already past, as it was by 18:46Z.
    monkeypatch.setattr(store_module, "_future", lambda seconds: "2020-01-01T00:00:00Z")
    field = _field(tmp_path)
    for number in range(3):
        field.send_mail(
            "", sender="control-plane", recipient=WORKER, kind="request",
            subject=f"Pending {number}", body="Waiting behind the wake.", idempotency_key=f"pending-{number}",
        )
    _consumed(field, retry=False, submission="sub-initial")
    _consumed(field, retry=True, submission="sub-retry")
    field.record_worker_session_queue_reconciliation(
        WORKER, expected_wake_id=WAKE, result={"reconciled": True, "queued_submission_ids": []}
    )
    return field


def _brief(result: dict) -> str:
    return render_envelope({"ok": True, "result": result})


def _subscription(field: SharedFieldStore) -> dict:
    return dict((field.worker_listener_session(WORKER) or {}).get("subscription") or {})


def test_the_spark_sequence_strands_the_session_and_the_retry_ceiling_holds(stranded):
    subscription = _subscription(stranded)
    assert subscription["outstanding_wake_id"] == WAKE
    assert subscription["wake_delivery_state"] == "consumed"
    assert int(subscription["wake_consumed_retries"]) == 1
    assert subscription.get("wake_retry_exhausted_since")

    # New mail coalesces into the same wake, and no automatic path submits again.
    stranded.coalesce_worker_session_wake(WORKER, message_refs=["work:mail:b"], wake_id=WAKE)
    with pytest.raises(DomainError) as refused:
        stranded.prepare_worker_session_wake(WORKER, message_refs=["work:mail:b"], wake_id=WAKE, retry=True)
    assert refused.value.details["reason"] == "wake_consumed_retry_exhausted"
    assert "work:mail:b" in _subscription(stranded)["last_wake_message_refs"]


def test_pb_worker_list_names_the_stall_and_the_one_recovery(stranded):
    listed = {"workers": stranded.list_workers()}
    text = _brief(listed)
    assert f"NOTE: native delivery stalled: wake {WAKE} taken without a receive and its one retry used since" in text
    assert f"recover once: pb worker wake-recover --worker {WORKER} --wake-id {WAKE}" in text


def test_an_idle_codex_with_nothing_pending_prints_no_stall(tmp_path):
    field = _field(tmp_path)
    text = _brief({"workers": field.list_workers()})
    assert "native delivery stalled" not in text
    assert "wake-recover" not in text


class _Channel:
    def __init__(self, runtime_kind="codex"):
        self.worker_name = WORKER
        self.runtime_kind = runtime_kind
        self.runtime_session_id = SESSION


class _Config:
    def __init__(self, field_root, runtime_kind="codex"):
        self.field_root = field_root
        self.host_id = "spark1"
        self.workers = (_Channel(runtime_kind),)


def _notifier(result, calls):
    def notify(channel, **kwargs):
        calls.append(kwargs)
        return result
    return notify


def test_one_recovery_submits_the_existing_wake_and_a_second_is_refused(stranded):
    calls: list[dict] = []
    config = _Config(stranded.root)
    outcome = recover_worker_wake(
        config, worker_name=WORKER, wake_id=WAKE, requested_by="coordinator",
        notifier=_notifier({"adapter": "codex-queue", "delivered": True, "queued_submission_id": "01a0ef59"}, calls),
    )
    assert outcome["recovery"]["state"] == "submitted"
    assert outcome["recovery"]["submission_id"] == "01a0ef59"
    assert calls[0]["wake_id"] == WAKE, "the existing wake identity is kept"
    assert calls[0]["wake_provenance"]["attempt"] == "recovery"
    assert "not model handling" in outcome["next"]

    with pytest.raises(DomainError) as again:
        recover_worker_wake(
            config, worker_name=WORKER, wake_id=WAKE, requested_by="coordinator",
            notifier=_notifier({"delivered": True}, calls),
        )
    assert again.value.code == "field_worker_wake_recovery_exists"
    assert again.value.details["recovery"]["state"] == "submitted"
    assert len(calls) == 1, "a delayed acknowledgement never causes a second submission"

    text = _brief({"workers": stranded.list_workers()})
    assert "recovery: submitted" in text and "do not submit again" in text


def test_only_the_matching_receive_resolves_the_recovery(stranded):
    config = _Config(stranded.root)
    recover_worker_wake(
        config, worker_name=WORKER, wake_id=WAKE, requested_by="coordinator",
        notifier=_notifier({"delivered": True, "queued_submission_id": "01a0ef59"}, []),
    )
    # A receive for another wake id resolves nothing.
    stranded.check_in_worker_listener(WORKER, inbox_checked=True, wake_id="wake_other")
    assert _subscription(stranded)["wake_recovery"]["state"] == "submitted"

    stranded.check_in_worker_listener(WORKER, inbox_checked=True, wake_id=WAKE)
    subscription = _subscription(stranded)
    assert "outstanding_wake_id" not in subscription and "wake_recovery" not in subscription
    assert subscription["last_wake_recovery"]["resolved_by"] == "worker_receive"
    text = _brief({"workers": stranded.list_workers()})
    assert f"last recovery: wake {WAKE} resolved by the worker's receive" in text


def test_a_definite_failure_allows_one_more_recovery_and_an_unknown_one_does_not(stranded):
    config = _Config(stranded.root)
    failed = recover_worker_wake(
        config, worker_name=WORKER, wake_id=WAKE, requested_by="coordinator",
        notifier=_notifier({"delivered": False, "reason": "codex_command_not_found"}, []),
    )
    assert failed["recovery"]["state"] == "failed", "the queue process never started"
    unknown = recover_worker_wake(
        config, worker_name=WORKER, wake_id=WAKE, requested_by="coordinator",
        notifier=_notifier({"delivered": False, "state": "unreachable", "reason": "TimeoutExpired"}, []),
    )
    assert unknown["recovery"]["state"] == "outcome_unknown"
    with pytest.raises(DomainError) as held:
        recover_worker_wake(
            config, worker_name=WORKER, wake_id=WAKE, requested_by="coordinator",
            notifier=_notifier({"delivered": True}, []),
        )
    assert held.value.code == "field_worker_wake_recovery_exists"


def test_a_crash_during_the_queue_call_leaves_an_inspectable_reservation(stranded):
    config = _Config(stranded.root)

    def crash(channel, **kwargs):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        recover_worker_wake(config, worker_name=WORKER, wake_id=WAKE, requested_by="coordinator", notifier=crash)
    assert _subscription(stranded)["wake_recovery"]["state"] == "reserved"
    with pytest.raises(DomainError) as held:
        recover_worker_wake(
            config, worker_name=WORKER, wake_id=WAKE, requested_by="coordinator",
            notifier=_notifier({"delivered": True}, []),
        )
    assert held.value.code == "field_worker_wake_recovery_exists"


def test_recovery_is_refused_for_a_stale_wake_a_live_retry_or_a_claude_session(stranded, tmp_path):
    config = _Config(stranded.root)
    with pytest.raises(DomainError) as stale:
        recover_worker_wake(config, worker_name=WORKER, wake_id="wake_old", requested_by="c", notifier=_notifier({}, []))
    assert stale.value.code == "field_worker_wake_recovery_stale"

    fresh = SharedFieldStore(tmp_path / "fresh")
    fresh.initialize(field_id="fresh")
    fresh.register_worker(worker_name=WORKER, runtime_kind="codex", runtime_session_id=SESSION, capabilities=[], authority_label="a")
    fresh.listen_worker(WORKER, check_interval_seconds=30)
    fresh.prepare_worker_session_wake(WORKER, message_refs=["work:mail:a"], wake_id=WAKE)
    with pytest.raises(DomainError) as live:
        recover_worker_wake(_Config(fresh.root), worker_name=WORKER, wake_id=WAKE, requested_by="c", notifier=_notifier({}, []))
    assert live.value.code == "field_worker_wake_recovery_not_needed"

    with pytest.raises(DomainError) as claude:
        recover_worker_wake(_Config(stranded.root, "claude-code"), worker_name=WORKER, wake_id=WAKE, requested_by="c", notifier=_notifier({}, []))
    assert claude.value.code == "field_worker_wake_recovery_unsupported"


def test_the_stall_costs_at_most_two_lines_per_worker(stranded, tmp_path):
    idle = _brief({"workers": _field(tmp_path / "idle").list_workers()}).splitlines()
    stalled = _brief({"workers": stranded.list_workers()}).splitlines()
    assert 0 < len(stalled) - len(idle) <= 2


def test_the_cli_offers_wake_recover_with_the_exact_worker_and_wake():
    import contextlib
    import io

    from project_board.client import cli

    out = io.StringIO()
    with contextlib.redirect_stdout(out), pytest.raises(SystemExit):
        cli.main(["worker", "wake-recover", "--help"])
    text = " ".join(out.getvalue().split())
    assert "--worker WORKER" in text and "--wake-id WAKE_ID" in text
    assert "One recovery per wake; only the worker's own receive resolves it" in text


def test_the_coordinator_procedure_owns_detect_diagnose_recover_and_recheck():
    root = Path(__file__).resolve().parents[1] / "src" / "project_board" / "procedures" / "problem-board-worker" / "references"
    coordinator = " ".join((root / "coordinator.md").read_text(encoding="utf-8").split())
    delivery = " ".join((root / "delivery-and-recovery.md").read_text(encoding="utf-8").split())
    assert "## Recover a stalled Codex delivery" in (root / "coordinator.md").read_text(encoding="utf-8")
    assert "A running relay and an active Card prove transport, not that a model received its mail" in coordinator
    assert "`pb worker wake-recover --worker <stable name> --wake-id <id>`" in coordinator
    assert "A second call for the same wake is refused and shows the recorded attempt" in coordinator
    assert "`reserved` (a call interrupted before its outcome was recorded)" in coordinator
    assert "A recovery whose mail has since drained still shows, without a stall" in coordinator
    assert "Queue admission is not handling" in coordinator
    assert "resolved by the worker's receive" in coordinator
    assert "tell the operator in the project conversation (kind `blocked`)" in coordinator
    assert "Do not submit again" in coordinator
    assert "[coordinator](coordinator.md), Recover a stalled Codex delivery" in delivery


# W405 review findings (codex-app, 2026-09-29 23:44Z).


def test_an_exhausted_marker_with_nothing_pending_is_not_a_stall(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "_future", lambda seconds: "2020-01-01T00:00:00Z")
    field = _field(tmp_path)
    _consumed(field, retry=False, submission="sub-initial")
    _consumed(field, retry=True, submission="sub-retry")
    field.record_worker_session_queue_reconciliation(
        WORKER, expected_wake_id=WAKE, result={"reconciled": True, "queued_submission_ids": []}
    )
    # A fresh inbox check that did not name the stale wake; no mail waits.
    field.check_in_worker_listener(WORKER, inbox_checked=True)
    assert _subscription(field).get("wake_retry_exhausted_since")
    text = _brief({"workers": field.list_workers()})
    assert "native delivery stalled" not in text and "wake-recover" not in text
    with pytest.raises(DomainError) as refused:
        recover_worker_wake(_Config(field.root), worker_name=WORKER, wake_id=WAKE, requested_by="c", notifier=_notifier({"delivered": True}, []))
    assert refused.value.code == "field_worker_wake_recovery_not_needed"
    assert refused.value.details["pending_messages"] == 0


def test_a_queue_process_that_exits_nonzero_after_admission_is_not_retried(stranded, monkeypatch):
    import subprocess

    from project_board.client import session_delivery

    monkeypatch.setattr(session_delivery, "_codex_executable", lambda: Path("/bin/echo"))
    admitted = []

    def queue_then_fail(command, **kwargs):
        admitted.append(command)
        return subprocess.CompletedProcess(
            command, 1, stdout=f"Queued message sub-accepted-{len(admitted)} for thread {SESSION}.\n", stderr="late error"
        )

    monkeypatch.setattr(session_delivery.subprocess, "run", queue_then_fail)
    config = _Config(stranded.root)
    first = recover_worker_wake(config, worker_name=WORKER, wake_id=WAKE, requested_by="coordinator", notifier=session_delivery.notify_agent_session)
    assert first["recovery"]["state"] == "outcome_unknown", "a nonzero exit is not proof that nothing was queued"
    assert first["recovery"]["submission_id"] == "sub-accepted-1", "the admission it printed is kept"
    with pytest.raises(DomainError) as held:
        recover_worker_wake(config, worker_name=WORKER, wake_id=WAKE, requested_by="coordinator", notifier=session_delivery.notify_agent_session)
    assert held.value.code == "field_worker_wake_recovery_exists"
    assert len(admitted) == 1, "never a second native submission for the same wake"


def test_a_recovery_whose_mail_drained_stays_as_audit_without_a_stall(stranded, monkeypatch):
    # W405 review (codex-app, 2026-09-30 00:01Z): after a submitted recovery,
    # the worker pulled and settled its mail without acknowledging that wake.
    recover_worker_wake(
        _Config(stranded.root), worker_name=WORKER, wake_id=WAKE, requested_by="coordinator",
        notifier=_notifier({"delivered": True, "queued_submission_id": "01a0ef59"}, []),
    )
    monkeypatch.setattr(store_module, "_future", REAL_FUTURE)
    for row in stranded.pull_mail("", worker_name=WORKER, lease_owner="session", limit=10):
        stranded.settle_mail(
            "", worker_name=WORKER, message_ref=row["message_ref"],
            lease_id=row["lease"]["lease_id"], lease_owner="session", outcome="acknowledged",
        )
    stranded.check_in_worker_listener(WORKER, inbox_checked=True)
    assert stranded.pending_worker_mail_count_snapshot(WORKER) == 0
    assert _subscription(stranded)["wake_recovery"]["state"] == "submitted", "the fence stays"
    text = _brief({"workers": stranded.list_workers()})
    assert "native delivery stalled" not in text and "recover once" not in text
    assert f"recovery: submitted for wake {WAKE}" in text and "no mail pending" in text
    with pytest.raises(DomainError):
        recover_worker_wake(
            _Config(stranded.root), worker_name=WORKER, wake_id=WAKE, requested_by="coordinator",
            notifier=_notifier({"delivered": True}, []),
        )


def test_the_heartbeat_carries_the_recovery_of_the_outstanding_wake_only(stranded):
    from project_board.client.relay import _heartbeat_session_projection, session_with_wake_recovery

    session = stranded.worker_listener_session(WORKER)
    assert session_with_wake_recovery(session)["wake_recovery"] == {}, "no recovery yet"
    recover_worker_wake(
        _Config(stranded.root), worker_name=WORKER, wake_id=WAKE, requested_by="coordinator",
        notifier=_notifier({"delivered": True, "queued_submission_id": "01a0ef59"}, []),
    )
    row = session_with_wake_recovery(stranded.worker_listener_session(WORKER))
    projected = _heartbeat_session_projection(row)
    assert projected["wake_recovery"]["wake_id"] == WAKE
    assert projected["wake_recovery"]["state"] == "submitted"
    assert projected["wake_recovery"]["submission_id"] == "01a0ef59"
    assert set(projected["wake_recovery"]) == {"wake_id", "state", "requested_at", "recorded_at", "submission_id"}

    stranded.check_in_worker_listener(WORKER, inbox_checked=True, wake_id=WAKE)
    cleared = _heartbeat_session_projection(session_with_wake_recovery(stranded.worker_listener_session(WORKER)))
    assert cleared["wake_recovery"] == {}, "the matching receive clears it on the next heartbeat"
