"""W683: the brief rendering of a successful detach must not crash.

`pb worker detach` answers {"worker": <name string>, "session": {...}, "relay_channel": "..."}.
That shape matches the inspect renderer (session + worker), which read ``worker`` and
``channel`` as mappings and raised AttributeError on a string (reported 2026-10-08 for a
string ``channel``; on AE 37c88725 the string is ``worker``). The detach itself succeeded;
only the brief view failed. The renderer must keep the structured inspect view unchanged
and show the string forms as text.
"""

from __future__ import annotations

from project_board.client import render


def _brief(result) -> str:
    return render.render_envelope({"ok": True, "result": result})


def _detach_result():
    return {
        "worker": "claude-code-x",
        "session": {"state": "stopping", "inbox_check_state": "current", "subscription": {}},
        "relay_channel": "close_then_disable_on_reconciliation",
    }


def test_a_detach_result_renders_its_worker_and_session_without_crashing():
    text = _brief(_detach_result())
    assert "claude-code-x" in text
    assert "session: stopping" in text


def test_a_string_channel_renders_as_text_not_as_a_mapping():
    result = {**_detach_result(), "channel": "disabled"}
    text = _brief(result)
    assert "channel: disabled" in text and "claude-code-x" in text


def test_the_structured_inspect_view_is_unchanged():
    result = {
        "session": {"state": "listening", "inbox_check_state": "current",
                    "subscription": {"adapter": "codex-queue", "state": "subscribed", "wake_delivery_state": "queued"}},
        "channel": {"worker_name": "codex-a", "alias": "A", "state": "active"},
        "worker": {"worker_name": "codex-a", "authorization": {"state": "active"}},
        "authorization": {"state": "active"},
    }
    lines = render._render_inspect(result)  # noqa: SLF001
    assert lines[0] == "worker: codex-a · alias A"
    assert lines[1] == "channel: active · authorization active · worker authorization active"


def test_missing_channel_and_worker_still_render():
    lines = render._render_inspect({"session": {"state": "listening"}, "worker": {}})  # noqa: SLF001
    assert lines[0] == "worker: None · alias -"
