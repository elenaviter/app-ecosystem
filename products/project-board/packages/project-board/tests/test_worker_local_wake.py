"""A durable mailbox write wakes exactly its Claude Code watch (W315)."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from project_board.client.local_wake import worker_wake_socket_path
from project_board.client.store import SharedFieldStore
from project_board.client.worker_watch import worker_watch_events


ALPHA = "claude-code-alpha"
BETA = "claude-code-beta"


def _field(tmp_path: Path) -> SharedFieldStore:
    field = SharedFieldStore(tmp_path / "shared-field")
    field.initialize(field_id="instant-wake")
    for worker in (ALPHA, BETA):
        field.register_worker(
            worker_name=worker,
            runtime_kind="claude-code",
            runtime_session_id=worker.removeprefix("claude-code-"),
            capabilities=[],
            authority_label=f"authority:{worker}",
        )
        field.listen_worker(worker, check_interval_seconds=300)
    return field


def _events(field: SharedFieldStore, worker: str):
    return worker_watch_events(
        field,
        worker_name=worker,
        check_interval_seconds=300,
        coalesce_seconds=1,
    )


def _send(field: SharedFieldStore, worker: str, key: str) -> dict:
    return field.send_mail(
        "",
        sender="control-plane",
        recipient=worker,
        kind="request",
        subject=f"Wake {worker}",
        body="Read this immediately.",
        idempotency_key=key,
    )


def _wait_for_listener(field: SharedFieldStore, worker: str) -> None:
    endpoint = worker_wake_socket_path(field.root, worker)
    deadline = time.monotonic() + 1
    while not endpoint.exists() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert endpoint.exists(), f"watch did not bind {endpoint}"


def test_mailbox_write_reaches_only_its_watch_in_about_one_second(tmp_path: Path):
    field = _field(tmp_path)
    alpha_events = _events(field, ALPHA)
    beta_events = _events(field, BETA)

    with ThreadPoolExecutor(max_workers=2) as pool:
        alpha = pool.submit(next, alpha_events)
        beta = pool.submit(next, beta_events)
        _wait_for_listener(field, ALPHA)
        _wait_for_listener(field, BETA)

        # A fresh store represents a restarted relay process using the same
        # durable field and proves that no in-process callback is involved.
        restarted_relay = SharedFieldStore(field.root)
        started = time.monotonic()
        sent = _send(restarted_relay, ALPHA, "wake-alpha")
        event = alpha.result(timeout=2)

        assert time.monotonic() - started < 1.5
        assert event["event"] == "problem_board.inbox_available"
        assert event["worker"] == ALPHA
        assert event["pending_count"] == 1
        assert sent["message_ref"] in field.pending_worker_mail_refs(ALPHA)
        time.sleep(0.05)
        assert not beta.done(), "mail for alpha woke beta's watch"

        _send(restarted_relay, BETA, "wake-beta")
        assert beta.result(timeout=2)["worker"] == BETA

    alpha_events.close()
    beta_events.close()


def test_mail_written_while_the_watch_is_down_is_seen_after_watch_restart(
    tmp_path: Path,
):
    field = _field(tmp_path)
    relay = SharedFieldStore(field.root)
    _send(relay, ALPHA, "wake-before-first-watch")

    first_watch = _events(field, ALPHA)
    assert next(first_watch)["pending_count"] == 1
    first_watch.close()

    # The closed watch intentionally leaves a stale socket name. A replacement
    # unlinks and binds it before probing the mail that arrived while down.
    assert worker_wake_socket_path(field.root, ALPHA).exists()
    _send(relay, ALPHA, "wake-between-watches")

    events = _events(SharedFieldStore(field.root), ALPHA)
    started = time.monotonic()
    event = next(events)
    try:
        assert time.monotonic() - started < 1.5
        assert event["event"] == "problem_board.inbox_available"
        assert event["worker"] == ALPHA
        assert event["pending_count"] == 2
    finally:
        events.close()
