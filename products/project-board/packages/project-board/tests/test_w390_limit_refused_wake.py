"""A wake the provider refused for usage is pushed once more after that limit ends (W390).

Spark, 2026-09-30 (codex-app@spark1, read-only evidence): the five-hour window
read 100% at 02:01:31Z, resetting at 03:47:20Z. The relay pushed wake
f02fe0b7 at 02:06:02Z and its one retry at 02:07:11Z. Both native turns ended
within a second as usage refusals, so the W198 ceiling counted them and the
wake was exhausted at 02:09:21Z. The single wake-recover at 02:20:54Z went
into a session the relay was already holding until 03:47:20Z, and its turn
was refused too. After the reset the relay only deduplicated the exhausted
wake, and thirteen messages waited with nothing left to wake the session.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from project_board.client import relay
from project_board.client import store as store_module
from project_board.client import wake_recovery
from project_board.client.limit_state import codex_limit_state
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError
from relay_helpers import make_host, make_supervisor

SESSION = "01a0daac-91ae-7730-81dd-9ffc77207b92"
WAKE = "wake_f02fe0b76ae548f09fe5c674d423bbef"
REAL_FUTURE = store_module._future


def _rollout(root: Path, turns: list[tuple[str, str]]) -> None:
    day = root / "2026" / "09" / "30"
    day.mkdir(parents=True, exist_ok=True)
    codex = {
        "limit_id": "codex",
        "primary": {"used_percent": 100.0, "window_minutes": 300, "resets_at": 1790740040},
        "secondary": {"used_percent": 47.0, "window_minutes": 10080, "resets_at": 1791059251},
    }
    lines = [{"timestamp": "2026-09-30T02:01:31.000Z", "type": "event_msg",
              "payload": {"type": "token_count", "rate_limits": codex}}]
    for completed_at, error in turns:
        payload = {"type": "task_complete", "turn_id": completed_at}
        if error:
            payload["error"] = {"codex_error_info": error}
        lines.append({"timestamp": completed_at, "type": "event_msg", "payload": payload})
    (day / f"rollout-2026-09-25T22-25-37-{SESSION}.jsonl").write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


REFUSED = [
    ("2026-09-30T02:06:13.000Z", "usage_limit_exceeded"),
    ("2026-09-30T02:07:13.000Z", "usage_limit_exceeded"),
    ("2026-09-30T02:21:03.000Z", "usage_limit_exceeded"),
]


def test_a_refusal_whose_bucket_reset_names_the_passed_reset(tmp_path):
    _rollout(tmp_path, REFUSED)
    during = codex_limit_state(SESSION, sessions_root=tmp_path, now="2026-09-30T02:30:00Z")
    assert during["kind"] == "rate_limited" and during["resets_at"] == "2026-09-30T03:47:20Z"
    assert "cleared_at" not in during

    after = codex_limit_state(SESSION, sessions_root=tmp_path, now="2026-09-30T05:17:40Z")
    # The refusal still stands (no turn was served), and the reset that ended
    # its bucket is named, so the relay can push one wake.
    assert after["kind"] == "rate_limited" and after["reached"] == "usage_limit_exceeded"
    assert after["resets_at"] == ""
    assert after["cleared_at"] == "2026-09-30T03:47:20Z"
    assert after["refusal"] == "usage_limit_exceeded"


def test_a_served_turn_ends_the_refusal_and_its_cleared_reset(tmp_path):
    _rollout(tmp_path, REFUSED + [("2026-09-30T05:20:00.000Z", "")])
    served = codex_limit_state(SESSION, sessions_root=tmp_path, now="2026-09-30T05:21:00Z")
    # No refusal stands any more, so nothing asks the relay to re-arm a wake.
    assert served["kind"] != "rate_limited" and "refusal" not in served


def _stranded(tmp_path, monkeypatch):
    host, identity, channel = make_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.initialize(field_id="w390")
    field.register_worker(
        worker_name=identity.worker_name, worker_identity=identity.worker_identity,
        runtime_kind=identity.runtime_kind, runtime_session_id=identity.runtime_session_id,
        capabilities=[], authority_label="connection-hub:test-profile", control_plane_state="published",
    )
    field.listen_worker(identity.worker_name, check_interval_seconds=30)
    for number in range(3):
        field.send_mail("", sender="control-plane", recipient=identity.worker_name, kind="request",
                        subject=f"Pending {number}", body="Waiting.", idempotency_key=f"pending-{number}")
    monkeypatch.setattr(store_module, "_future", lambda seconds: "2020-01-01T00:00:00Z")
    for retry, submission in ((False, "sub-initial"), (True, "sub-retry")):
        field.prepare_worker_session_wake(identity.worker_name, message_refs=["work:mail:a"], wake_id=WAKE, retry=retry)
        field.record_worker_session_delivery(
            identity.worker_name, adapter="codex-queue", state="attached", event_kind="input.available",
            delivered=True, message_refs=["work:mail:a"], wake_id=WAKE, prepared=True, queued_submission_id=submission,
        )
        field.record_worker_session_queue_reconciliation(
            identity.worker_name, expected_wake_id=WAKE, result={"reconciled": True, "queued_submission_ids": []}
        )
    field.record_worker_session_queue_reconciliation(
        identity.worker_name, expected_wake_id=WAKE, result={"reconciled": True, "queued_submission_ids": []}
    )
    monkeypatch.setattr(store_module, "_future", REAL_FUTURE)
    subscription = field.worker_listener_session(identity.worker_name)["subscription"]
    assert subscription["outstanding_wake_id"] == WAKE and subscription.get("wake_retry_exhausted_since")
    return host, identity, channel, field


def _subscription(field, worker_name):
    return dict(field.worker_listener_session(worker_name)["subscription"])


def test_the_store_rearms_an_exhausted_wake_once_per_ended_limit(tmp_path, monkeypatch):
    _host, identity, _channel, field = _stranded(tmp_path, monkeypatch)
    name = identity.worker_name
    first = str(_subscription(field, name).get("wake_first_attempt_at") or "")

    assert not field.rearm_limit_consumed_wake(name, wake_id=WAKE, refused_at=first, cleared_at="2999-01-01T00:00:00Z"), \
        "a limit whose reset is still ahead re-arms nothing"
    assert not field.rearm_limit_consumed_wake(name, wake_id=WAKE, refused_at="2000-01-01T00:00:00Z",
                                               cleared_at="2026-09-30T03:47:20Z"), \
        "a refusal before the wake's first attempt does not explain the wake"
    assert not field.rearm_limit_consumed_wake(name, wake_id="wake_other", refused_at=first,
                                               cleared_at="2026-09-30T03:47:20Z")

    assert field.rearm_limit_consumed_wake(name, wake_id=WAKE, refused_at=first, cleared_at="2026-09-30T03:47:20Z")
    rearmed = _subscription(field, name)
    assert int(rearmed.get("wake_consumed_retries") or 0) == 0
    assert "wake_retry_exhausted_since" not in rearmed
    assert rearmed["wake_limit_rearmed_for"] == "2026-09-30T03:47:20Z"

    assert not field.rearm_limit_consumed_wake(name, wake_id=WAKE, refused_at=first, cleared_at="2026-09-30T03:47:20Z"), \
        "once per ended limit, never a loop"


def test_the_relay_pushes_the_exhausted_wake_once_after_the_limit_ends(tmp_path, monkeypatch):
    host, identity, channel, field = _stranded(tmp_path, monkeypatch)
    name = identity.worker_name
    first = str(_subscription(field, name).get("wake_first_attempt_at") or "")
    ended = {
        "kind": "rate_limited", "reached": "usage_limit_exceeded", "resets_at": "",
        "refusal": "usage_limit_exceeded", "observed_at": first, "cleared_at": "2026-09-30T03:47:20Z",
    }
    monkeypatch.setattr(relay, "session_with_limit_state", lambda listener, **_: {**listener, "limit_state": ended})
    supervisor = make_supervisor(host)
    pushes: list[dict] = []

    async def reconcile(*_args, **_kwargs):
        return None

    async def notify(_host, _channel, **kwargs):
        pushes.append(kwargs)
        return {"delivered": True}

    monkeypatch.setattr(supervisor, "_reconcile_session_queue", reconcile)
    monkeypatch.setattr(supervisor, "_notify_session", notify)

    asyncio.run(supervisor._notify_available_input(host, channel))
    assert len(pushes) == 1 and pushes[0]["wake_id"] == WAKE and pushes[0]["retried"] is True

    # The same ended limit does not re-arm it again: the next cycle only deduplicates.
    field.prepare_worker_session_wake(name, message_refs=["work:mail:a"], wake_id=WAKE, retry=True)
    field.record_worker_session_delivery(
        name, adapter="codex-queue", state="attached", event_kind="input.available",
        delivered=True, message_refs=["work:mail:a"], wake_id=WAKE, prepared=True, queued_submission_id="sub-rearm",
    )
    field.record_worker_session_queue_reconciliation(
        name, expected_wake_id=WAKE, result={"reconciled": True, "queued_submission_ids": []}
    )
    pushes.clear()
    asyncio.run(supervisor._notify_available_input(host, channel))
    assert _subscription(field, name)["wake_limit_rearmed_for"] == "2026-09-30T03:47:20Z"
    assert pushes == [], "the re-armed push is outstanding again: the same ended limit re-arms nothing"


def test_an_exhausted_wake_without_a_refusal_keeps_the_ceiling(tmp_path, monkeypatch):
    host, identity, channel, field = _stranded(tmp_path, monkeypatch)
    monkeypatch.setattr(relay, "session_with_limit_state",
                        lambda listener, **_: {**listener, "limit_state": {"kind": "ok", "resets_at": ""}})
    supervisor = make_supervisor(host)
    pushes: list[dict] = []

    async def reconcile(*_args, **_kwargs):
        return None

    async def notify(_host, _channel, **kwargs):
        pushes.append(kwargs)
        return {"delivered": True}

    monkeypatch.setattr(supervisor, "_reconcile_session_queue", reconcile)
    monkeypatch.setattr(supervisor, "_notify_session", notify)
    asyncio.run(supervisor._notify_available_input(host, channel))
    subscription = _subscription(field, identity.worker_name)
    assert subscription.get("wake_retry_exhausted_since"), "a session that ignored two wakes is not re-armed"
    assert "wake_limit_rearmed_for" not in subscription


def test_wake_recover_refuses_while_the_session_is_held_for_its_limit(tmp_path, monkeypatch):
    host, identity, _channel, field = _stranded(tmp_path, monkeypatch)
    monkeypatch.setattr(
        wake_recovery, "session_with_limit_state",
        lambda listener, **_: {**listener, "limit_state": {"kind": "rate_limited", "resets_at": "2999-01-01T00:00:00Z"}},
    )
    submitted: list[dict] = []
    with pytest.raises(DomainError) as refused:
        wake_recovery.recover_worker_wake(
            host, worker_name=identity.worker_name, wake_id=WAKE, requested_by="coordinator",
            notifier=lambda *args, **kwargs: submitted.append(kwargs) or {"delivered": True},
        )
    assert refused.value.code == "field_worker_wake_recovery_held"
    assert refused.value.details["held_until"] == "2999-01-01T00:00:00Z"
    assert submitted == [], "nothing reached the native queue"
    assert "wake_recovery" not in _subscription(field, identity.worker_name), "no recovery was reserved"
