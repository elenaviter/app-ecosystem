"""pb status --format brief is a verdict for a recovery decision (W563).

Root, 2026-10-06 04:14-04:18 UTC (W563 note_e2512d98): during a relay
reconnect the brief status printed 138 lines, about 2,854 tokens, mostly
source subtree hashes printed twice, for a decision that needs the channel
state, the error, the next retry and the authorization.
"""

from __future__ import annotations

from project_board.client.render import render_envelope

SUBTREES = {f"packages/p{index}": "f" * 40 for index in range(4)}


def _status(**session_extra):
    source = {"commit": "a" * 40, "release_id": "b" * 64, "subtrees": SUBTREES,
              "components": [{"name": "app_ecosystem", "commit": "a" * 40, "subtrees": SUBTREES}]}
    return {
        "schema": "problem-board.first-run-status.v1",
        "state": "session_reconnecting",
        "machine": {
            "default_config": "/h/config.json",
            "targets": [{"target_id": "dev", "host_id": "dev-main", "workers": 23}],
            "relay": {"installed": True, "running": True, "source": source, "startup_source": source},
            "client": {"pinned": True, "source": dict(source, commit="c" * 40)},
            "prerequisites": {
                "ok": True, "missing": [],
                "checked": [
                    {"name": "python", "found": True, "why": "w" * 200},
                    {"name": "keyring", "found": False, "fix": "Unlock the login keyring, then run pb status again."},
                ],
            },
            "default_target": {"target_id": "dev", "host_id": "dev-main"},
        },
        "session": {
            "worker_name": "codex-root", "alias": "codex-main", "channel_state": "reconnecting",
            "authorization": "authorized", "control_plane_state": "published",
            "attended_project_refs": ["work:project:one"],
            **session_extra,
        },
        "next": {"step": "wait_for_relay_retry", "command": "", "explain": "The relay retries on its own."},
    }


def test_a_reconnecting_session_reads_as_a_short_verdict():
    status = _status(connection={
        "state": "reconnecting", "attempts": 3, "reason": "work_relay_transport_unavailable",
        "schedule": "backoff", "next_attempt_at": "2026-10-06T04:16:07Z", "attempt_in_progress": True,
    })

    text = render_envelope({"ok": True, "result": status})

    assert "status: session_reconnecting · next step wait_for_relay_retry" in text
    assert "channel reconnecting · authorization authorized" in text
    assert ("connection: reconnecting · error work_relay_transport_unavailable · attempts 3 · "
            "schedule backoff · next attempt 2026-10-06T04:16:07Z · an attempt is running now") in text
    assert "client: commit cccccccccccc · pinned True · DIFFERS from the relay" in text
    assert "keyring NOT usable, fix: Unlock the login keyring, then run pb status again." in text
    assert "subtrees" not in text and "f" * 40 not in text, "hashes stay in the JSON"
    lines = text.splitlines()
    assert len(lines) <= 15, f"{len(lines)} lines"
    assert len(text.encode("utf-8")) < 1_500


def test_a_parked_channel_names_its_refusal():
    status = _status(channel_state="pending_authorization",
                     refusal={"code": "delegated_card_not_active", "permanent": True, "credential": True})

    text = render_envelope({"ok": True, "result": status})

    assert "refusal: delegated_card_not_active · permanent True · credential refused True" in text
