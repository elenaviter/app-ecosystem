"""The item brief answers a current review-hold check without the full review."""

from datetime import datetime, timezone

import pytest

from project_board.client import render


NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
WORKER = "claude-code-" + "worker-identity-" * 30


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(render, "datetime", FrozenDatetime)


def item(**changes):
    return {
        "item_key": "W23", "title": "Review result", "status": "review",
        "revision": 7, "identity_ref": "work:plan:node:example",
        "item_ref": "work:plan:node:example:version-7", **changes,
    }


def brief(value):
    return render.render_envelope({
        "ok": True, "result": {"operation": "project.plan.item", "object": value},
    })


@pytest.mark.parametrize("review", [None, {}, {"hold": None}, {"hold": {}}])
def test_absent_hold_is_explicit(review):
    text = brief(item(review=review))
    assert "review.hold: none\n" in text
    assert "review.hold.reason" not in text


def test_hold_is_beside_status_and_revision_with_the_whole_worker_name():
    text = brief(item(review={"hold": {
        "waiting_on": WORKER, "due_at": "2026-10-16T12:00:00Z",
        "reason": "Wait for the independent check.", "set_at": "2026-10-14T12:00:00Z",
    }}))
    lines = text.splitlines()
    revision = next(i for i, line in enumerate(lines) if line.startswith("revision 7"))
    assert lines[revision + 1] == (
        f"review.hold: waiting_on {WORKER} · due_at 2026-10-16T12:00:00Z"
        " · set_at 2026-10-14T12:00:00Z"
    )
    assert "review.hold.reason: Wait for the independent check." in text
    assert "overdue" not in text


@pytest.mark.parametrize(("due_at", "overdue"), [
    ("2026-10-15T11:59:59Z", True),
    ("2026-10-15T13:59:59+02:00", True),
    ("2026-10-15T12:00:00Z", False),
    ("2026-10-15T12:00:01Z", False),
    ("2026-10-15T11:59:59", True),
    ("unknown-time", False),
    ("", False),
])
def test_overdue_is_a_read_time_utc_comparison_not_an_invented_state(due_at, overdue):
    text = brief(item(review={"hold": {
        "waiting_on": "operator", "due_at": due_at, "reason": "Check the result.",
    }}))
    assert (" (overdue)" in text) is overdue
    assert f"due_at {due_at or 'not reported'}" in text
    assert "waiting_on operator" in text


def test_long_unicode_reason_is_bounded_labelled_and_has_the_full_read_route():
    reason = "確認 🧪 " * 200 + "PRIVATE_TAIL"
    text = brief(item(review={"hold": {
        "waiting_on": WORKER, "due_at": "2026-10-16T12:00:00Z", "reason": reason,
    }}))
    prefix = "review.hold.reason preview (clipped): "
    line = next(line for line in text.splitlines() if line.startswith(prefix))
    assert len(line.removeprefix(prefix).encode("utf-8")) <= 320
    assert line.endswith("...")
    assert "PRIVATE_TAIL" not in text
    assert WORKER in text
    assert "pb worker item-read --project-ref <project-ref> --item-key W23" in text
    assert "--field review" in text


def test_stale_hold_does_not_hide_the_done_status_or_revision():
    text = brief(item(status="done", review={"hold": {
        "waiting_on": "operator", "due_at": "2026-10-15T11:00:00Z",
        "reason": "Historical waiting reason.",
    }}))
    assert "item: W23 · done · Review result" in text
    assert "revision 7" in text and " (overdue)" in text


def test_render_does_not_mutate_the_hold_or_full_review_read():
    import copy

    value = item(review={"hold": {
        "waiting_on": "operator", "due_at": "2026-10-15T11:00:00Z",
        "reason": "Full reason " * 100,
    }, "look_at": "The full independent checklist."})
    original = copy.deepcopy(value)
    full = {"ok": True, "result": {
        "schema": "problem-board.item-read.v1", **value,
        "fields": {"review": value["review"]},
    }}
    before = render.render_envelope(full)
    brief(value)
    assert value == original
    assert render.render_envelope(full) == before
    assert "Full reason " * 100 in before
