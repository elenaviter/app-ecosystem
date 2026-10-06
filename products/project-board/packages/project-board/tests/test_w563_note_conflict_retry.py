"""The brief error for a note revision conflict names its retry (W563, disposition 2)."""

from __future__ import annotations

from project_board.client.render import render_envelope


def test_a_note_revision_conflict_prints_the_same_text_retry_at_the_current_revision():
    # W563 disposition 2: the server names current_revision on a stale note
    # append; the brief error says how to retry it once, and that a
    # replacement edit reads the item again first.
    text = render_envelope({"ok": False, "error": {
        "code": "work_item_revision_conflict", "message": "The work item changed after it was read.",
        "details": {"expected_revision": 273, "current_revision": 275},
    }})

    assert "current_revision = 275" in text
    assert 'retry: plan.note.append only: the same text, unchanged, with "expected_revision": 275.' in text
    assert "A replacement edit (plan.item.update) reads the item again and decides first." in text
    plain = render_envelope({"ok": False, "error": {"code": "work_item_revision_conflict", "message": "changed"}})
    assert "retry:" not in plain, "no current revision, no retry line"
