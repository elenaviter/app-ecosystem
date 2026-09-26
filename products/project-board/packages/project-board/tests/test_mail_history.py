from __future__ import annotations

import json
from pathlib import Path

from project_board.client.mail_history import MailHistoryStore


def test_terminal_mail_is_partitioned_by_project_agent_and_hour(tmp_path: Path):
    history = MailHistoryStore(tmp_path / "field")
    row = {
        "message_id": "mail_one",
        "recipient": "Codex-UI",
        "created_at": "2026-09-25T01:15:00Z",
        "settled_at": "2026-09-25T01:20:00Z",
    }

    path = history.write(
        project_id="project-one",
        family="mail-processed",
        agent="Codex-UI",
        record_id="mail_one",
        row=row,
    )

    assert path == (
        tmp_path / "field" / "projects" / "project-one" / "mail-processed"
        / "codex-ui" / "2026" / "09" / "25" / "01"
        / "20260925T011500.000000Z_mail_one.json"
    )
    assert history.read(
        project_id="project-one",
        family="mail-processed",
        agent="codex-ui",
        record_id="mail_one",
    ) == row


def test_mail_history_reads_an_exact_legacy_key_until_migration(tmp_path: Path):
    history = MailHistoryStore(tmp_path / "field")
    legacy = (
        tmp_path / "field" / "projects" / "project-one" / "idempotency" / "mail" / "key.json"
    )
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({"request_hash": "abc"}))

    assert history.read(
        project_id="project-one",
        family="mail-idempotency",
        agent="codex-ui",
        record_id="key",
    ) == {"request_hash": "abc"}
