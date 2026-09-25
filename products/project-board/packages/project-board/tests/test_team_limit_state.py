"""The team roster keeps each teammate's usage limit (W26 line 7).

The board sends every linked agent's `limit_state` on the heartbeat's team
roster, so an agent reads a teammate's limit without asking it: an agent out
of tokens cannot answer mail. The host keeps it in team.json, which
`pb worker context` prints.
"""

from __future__ import annotations

from project_board.client.store import SharedFieldStore

PROJECT = "quickstart-works"


def test_team_json_keeps_a_teammates_limit_and_leaves_an_unreported_one_empty(tmp_path):
    store = SharedFieldStore(tmp_path / "field")
    store.initialize(field_id="team-limits")
    store.create_project(project_id=PROJECT, title="Quickstart works", goal="Onboard agents.", owner="operator")
    store.sync_project_team(
        PROJECT,
        [
            {
                "worker_name": "Claude-Code-Main", "role": "coordinator",
                "limit_state": {
                    "kind": "out_of_tokens", "reached": "primary", "resets_at": "2026-09-25T06:00:00Z",
                    "observed_at": "2026-09-25T01:00:00Z", "source": "claude-code-statusline",
                    "windows": [{"name": "primary", "used_percent": 100.0}],
                },
            },
            {"worker_name": "codex-main", "role": "worker"},
            {"worker_name": "codex-ui", "role": "worker", "limit_state": {"kind": ""}},
        ],
    )
    members = {row["worker_name"]: row for row in store.read_project_team(PROJECT)}
    assert members["claude-code-main"]["limit_state"] == {
        "kind": "out_of_tokens",
        "reached": "primary",
        "resets_at": "2026-09-25T06:00:00Z",
        "observed_at": "2026-09-25T01:00:00Z",
        "source": "claude-code-statusline",
    }
    assert members["codex-main"]["limit_state"] == {}
    assert members["codex-ui"]["limit_state"] == {}
