"""W393 remaining brief-output budget regressions."""

from project_board.client.render import render_envelope


LONG = "diagnostic history " * 4000 + "HIDDEN_TAIL"
REF = "work:note:" + "n" * 128
CURSOR = "cursor." + "c" * 320


def brief(result):
    return render_envelope({"ok": True, "result": result})


def test_relay_service_status_keeps_attention_and_bounds_histories():
    channels = [
        {
            "worker_alias": f"worker-{i}@spark1",
            "worker_name": f"worker-{i}-" + "w" * 60,
            "channel_state": "active",
            "relay_diagnostic": {
                "state": "degraded" if i == 13 else "ready",
                "code": "connection_lost" if i == 13 else "",
                "started_at": "2026-09-29T20:00:00Z" if i == 13 else "",
                "recent": [
                    {"code": "old_error", "message": LONG, "started_at": "2026-09-28T01:00:00Z"}
                    for _ in range(20)
                ],
            },
        }
        for i in range(18)
    ]
    result = {
        "schema": "problem-board.relay-service.v1",
        "service_id": "relay.service",
        "system": "Linux",
        "installed": True,
        "running": True,
        "definition": "/opt/problem-board/relay.service",
        "config": "/etc/problem-board/relay.json",
        "source": {"mode": "snapshot", "release_id": "r" * 64, "commit": "a" * 40},
        "bootstrap_source": {"mode": "snapshot", "release_id": "r" * 64, "commit": "a" * 40},
        "startup_record": {"pid": 1234, "started_at": "2026-09-29T20:00:00Z"},
        "manager_status": LONG,
        "relay_diagnostics": {
            "transport": {"degraded": True, "code": "relay_offline", "recent": [LONG] * 100},
            "channels": channels,
        },
    }
    text = brief(result)
    assert "running: True" in text
    assert "relay_offline" in text
    assert "worker-13@spark1" in text
    assert "connection_lost" in text
    assert "channels: 8 of 18 shown in brief" in text
    assert "worker-17@spark1" not in text
    assert "manager_status" not in text and "HIDDEN_TAIL" not in text
    assert len(text.splitlines()) <= 45
    assert len(text.encode()) <= 8_000


def test_notes_keep_page_coordinates_and_every_ref_without_repeating_item():
    notes = [
        {
            "note_ref": REF + str(i),
            "ordinal": i,
            "author": "worker",
            "created_at": "2026-09-29T21:00:00Z",
            "text": LONG,
        }
        for i in range(100)
    ]
    item_ref = "work:plan:node:w393:" + "i" * 100
    text = brief({
        "operation": "plan.notes.list",
        "object": {
            "items": notes,
            "total": 340,
            "next_cursor": CURSOR,
            "identity_ref": item_ref,
            "work_ref": item_ref + ":revision-30",
            "item": {
                "item_key": "W393", "title": "Bounded notes", "status": "working",
                "identity_ref": item_ref, "item_ref": item_ref + ":revision-30",
                "revision": 30, "description": LONG, "notes": notes,
            },
        },
    })
    assert f"next_cursor: {CURSOR}" in text
    assert f"note 100: {REF}99" in text
    assert "note previews: 8 of 100 shown in brief" in text
    assert "preview: diagnostic history" in text
    assert "description" not in text and "HIDDEN_TAIL" not in text
    assert len(text.splitlines()) <= 125
    assert len(text.encode()) <= 35_000


def test_inspect_keeps_actionable_wake_attention_bounded():
    wake = "wake_" + "w" * 128
    refs = ["work:mail:" + "m" * 100 + str(i) for i in range(100)]
    text = brief({
        "session": {
            "state": "working", "inbox_check_state": "current",
            "last_message_refs": refs, "last_control_refs": refs,
            "subscription": {
                "adapter": "codex-queue", "state": "attached",
                "wake_delivery_state": "retry_exhausted",
                "outstanding_wake_id": wake, "wake_attempts": 3,
                "queue_reconciliation_required": True, "last_error": LONG,
            },
        },
        "channel": {"worker_name": "codex-app", "alias": "codex-app@spark1", "state": "reconnecting",
                    "connection": {"state": "reconnecting", "next_attempt_at": "2026-09-29T20:10:00Z",
                                   "last_error_code": "connection_lost", "last_error_summary": LONG}},
        "worker": {"authorization": {"state": "active"}, "relay_diagnostic": {
            "state": "degraded", "code": "handler_error", "message": LONG,
            "recent": [{"code": "old_error", "message": LONG}] * 20,
        }, "reachability": {
            "wake_state": "retry_exhausted", "wake_retry_exhausted_since": "2026-09-29T20:00:00Z",
        }},
        "authorization": {"state": "active"},
        "mail": {"active_leases": {"total": 2}, "quarantine_count": 1},
    })
    assert f"outstanding_wake_id: {wake}" in text
    assert "wake attention: queue reconciliation required" in text
    assert "wake attention state: retry_exhausted" in text
    assert "next attempt 2026-09-29T20:10:00Z" in text
    assert "connection_lost" in text and "handler_error" in text
    assert "active mail leases: 2" in text
    assert "last message refs: 12 of 100 shown in brief" in text
    assert "HIDDEN_TAIL" not in text
    assert len(text.splitlines()) <= 55
    assert len(text.encode()) <= 8_000
