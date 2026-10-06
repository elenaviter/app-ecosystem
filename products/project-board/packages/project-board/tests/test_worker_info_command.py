"""An agent publishes one line about itself on every card (W330).

`pb worker info write "<text>"` records the line locally; the relay carries it on
its next heartbeat, which every worker Card already holds, so no Card is
re-approved for it. The board answers with the stored line, and the command
then says the line is on the board. `clear` sends an empty line, which
removes it; a worker that never set one sends no key, so the board keeps what
it has. Teammates read the line in the team of `pb worker context`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from project_board.client import cli, relay
from project_board.contract.errors import DomainError
from test_attendance_materializes_project import PROJECT_REF, Board, _fresh_host
from procedure_reference import reference_text

LINE = "Operator: reviews only until 2026-09-30, do not route builds to me."


class InfoBoard(Board):
    """Stores worker_info like the service: absent leaves it, empty clears it."""

    info_text = ""

    async def action(self, *, object_ref: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        answer = await super().action(object_ref=object_ref, action=action, payload=payload)
        if action == "worker.heartbeat":
            info = (payload or {}).get("worker_info")
            if isinstance(info, dict):
                self.info_text = str(info.get("text") or "")
            answer["object"]["info_text"] = self.info_text
            answer["object"]["team"] = [{"worker_name": self.recipient, "role": "worker", "info_text": self.info_text}]
        return answer


def _heartbeats(board: Board) -> list[dict[str, Any]]:
    return [call["payload"] for call in board.calls if call["action"] == "worker.heartbeat"]


def _cli(identity, *arguments: str) -> dict[str, Any]:
    flags = ["--runtime-kind", identity.runtime_kind, "--runtime-session-id", identity.runtime_session_id]
    return cli._worker_command(cli.build_parser().parse_args(["worker", *arguments[:1], *flags, *arguments[1:]]))  # noqa: SLF001


def test_set_publish_show_clear_and_the_team_reads_it(tmp_path, monkeypatch):
    host, identity, field, config = _fresh_host(tmp_path)
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(host.path))
    board = InfoBoard(identity.worker_name)
    board.recipient = identity.worker_name
    adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=board)

    # Never set: the heartbeat carries no key, so the board keeps its value.
    asyncio.run(adapter.poll_attendances_once())
    assert all("worker_info" not in payload for payload in _heartbeats(board))
    assert _cli(identity, "info", "show")["rule"].startswith("Never set")

    recorded = _cli(identity, "info", "write", LINE)
    assert recorded["info_text"] == LINE
    assert recorded["on_board"] is False
    assert "within about two minutes" in recorded["rule"]

    asyncio.run(adapter.poll_attendances_once())
    assert _heartbeats(board)[-1]["worker_info"] == {"text": LINE}
    shown = _cli(identity, "info", "show")
    assert shown["on_board"] is True and shown["info_text"] == LINE

    # Teammates read it in pb worker context.
    context = _cli(identity, "context", "--project-ref", PROJECT_REF)
    assert [member["info_text"] for member in context["team"]] == [LINE]
    # And pb worker list shows the local record on this host.
    listed = cli._worker_command(cli.build_parser().parse_args(["worker", "list"]))  # noqa: SLF001
    assert [row["info"]["text"] for row in listed["workers"] if row.get("info")] == [LINE]

    cleared = _cli(identity, "info", "clear")
    assert cleared["info_text"] == "" and cleared["on_board"] is False
    asyncio.run(adapter.poll_attendances_once())
    assert _heartbeats(board)[-1]["worker_info"] == {"text": ""}
    assert board.info_text == ""
    # Bare info is show: it reads, never writes.
    assert _cli(identity, "info")["rule"] == "Cleared on the board."


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        (("info", "write", "first\nsecond"), "field_worker_info_multiline"),
        (("info", "write", "x" * 201), "field_worker_info_too_long"),
        (("info", "write", "tab\there"), "field_worker_info_control_character"),
        (("info", "write", "nul\x00here"), "field_worker_info_control_character"),
        (("info", "write", "   "), "field_worker_info_arguments"),
        (("info", "write"), "field_worker_info_arguments"),
        (("info", "clear", "text"), "field_worker_info_arguments"),
        (("info", "show", "text"), "field_worker_info_arguments"),
        # A bare line is refused: every change names its verb (operator, 2026-10-01).
        (("info", LINE), "field_worker_info_arguments"),
        (("info", "claude-code-a7b7935d"), "field_worker_info_arguments"),
    ],
)
def test_a_bad_line_is_refused_before_anything_is_recorded(tmp_path, monkeypatch, arguments, code):
    host, identity, field, config = _fresh_host(tmp_path)
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(host.path))
    with pytest.raises(DomainError) as raised:
        _cli(identity, *arguments)
    assert raised.value.code == code
    assert field.worker_info(identity.worker_name) == {}


def test_two_hundred_characters_is_accepted(tmp_path, monkeypatch):
    host, identity, field, config = _fresh_host(tmp_path)
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(host.path))
    assert _cli(identity, "info", "write", "y" * 200)["info_text"] == "y" * 200


def test_a_worker_on_no_project_publishes_it_on_the_discovery_heartbeat(tmp_path, monkeypatch):
    host, identity, field, config = _fresh_host(tmp_path)
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(host.path))
    board = InfoBoard(identity.worker_name, linked=False)
    board.recipient = identity.worker_name
    adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=board)
    asyncio.run(adapter.poll_attendances_once())

    _cli(identity, "info", "write", LINE)
    asyncio.run(adapter.poll_attendances_once())
    assert _heartbeats(board)[-1]["worker_info"] == {"text": LINE}
    assert _cli(identity, "info", "show")["on_board"] is True


def test_a_board_without_the_field_gets_one_forced_heartbeat_per_line_not_one_per_cycle(tmp_path, monkeypatch):
    """Review on #152: an old board never answers with info_text, so the line stays
    unacknowledged; it may force one heartbeat, then the relay's pacing holds."""

    def heartbeats_over_ten_cycles(*, linked: bool, line: str | None) -> int:
        host, identity, field, config = _fresh_host(tmp_path / f"{linked}-{line is not None}")
        monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(host.path))
        board = Board(identity.worker_name, linked=linked)  # never returns info_text
        adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=board)
        asyncio.run(adapter.poll_attendances_once())
        if line is not None:
            _cli(identity, "info", "write", line)
        before = len(_heartbeats(board))
        for _ in range(10):
            asyncio.run(adapter.poll_attendances_once())
        return len(_heartbeats(board)) - before

    for linked in (True, False):
        baseline = heartbeats_over_ten_cycles(linked=linked, line=None)
        with_line = heartbeats_over_ten_cycles(linked=linked, line=LINE)
        assert with_line <= baseline + 1, (linked, baseline, with_line)


def test_the_procedure_says_what_the_line_is_for_and_when_to_read_a_teammates():
    """Operator, 2026-10-01: the line carries what others need to plan around
    this agent, kept current or cleared, and a teammate's line is read before
    starting contact with it. Every PB agent in any project follows this."""

    from pathlib import Path

    import project_board

    reference = Path(project_board.__file__).resolve().parent / "procedures" / "problem-board-worker" / "references" / "collaboration.md"
    text = " ".join(reference_text(reference).split())
    assert "The info line says what the team needs to plan around you, and nothing else" in text
    assert "Findings, checkpoint results and analysis go to mail, item notes and reports, not to this line." in text
    assert "clear it when nothing on it would help anyone plan" in text
    assert "A restriction and your status share this one line, the restriction first, and a rewrite keeps the restriction" in text
    assert "**Read a teammate's line before you start contact with it.**" in text
    assert "A line that says paused, restricted or do not use means you do not wake it" in text


def test_the_procedure_names_the_verbs():
    """Operator, 2026-10-01: an agent passed a teammate's name to `pb worker info`
    to look it up and overwrote its own line. Reading and writing are separate verbs."""

    from pathlib import Path

    import project_board

    root = Path(project_board.__file__).resolve().parent / "procedures" / "problem-board-worker"
    collaboration = reference_text(root / "references" / "collaboration.md")
    coordinator = reference_text(root / "references" / "coordinator.md")
    for verb in ("pb worker info show", 'pb worker info write "', "pb worker info clear"):
        assert verb in collaboration
    for text in (collaboration, coordinator, (root / "SKILL.md").read_text(encoding="utf-8")):
        assert 'pb worker info "' not in text and "pb worker info --clear" not in text


def test_the_worker_rereads_its_assignments_periodically():
    """Operator, 2026-10-01: an agent periodically checks what it is assigned
    against what it remembers, because an assignment notice is sent once."""

    from pathlib import Path

    import project_board

    root = Path(project_board.__file__).resolve().parent / "procedures" / "problem-board-worker"
    skill = " ".join((root / "SKILL.md").read_text(encoding="utf-8").split())
    collaboration = " ".join(reference_text(root / "references" / "collaboration.md").split())
    # Operator correction, 2026-10-01: periodic, never per step, wake or guard prompt.
    # Ownership 6: once per native wake batch, plus about every 30 minutes; never per command, message or guard.
    assert "Read it again once per native wake batch and, while you work, about every 30 minutes at the next safe boundary, never per command, per leased message or per guard prompt" in skill
    assert "Read them again once per native wake batch (the addressed mail one wake delivers, received together)" in collaboration
    assert "once about 30 minutes of active work have passed since the last full read, at the next safe boundary" in collaboration
    assert "a command, a leased message within a batch, a guard prompt or a work boundary is not a reason for a full read" in collaboration
    assert "[brief-output](../brief-output.md)), which the brief view does not show" in collaboration
    assert "An addressed change to one assignment or its ownership, or a doubt about one item, reads only that item, including the assignment's own task" in collaboration
    for text in (skill, collaboration):
        assert "at every work boundary and on each guard prompt or wake" not in text
        assert "at every work boundary and at least on each guard prompt or wake" not in text
    assert "an assignment's notice is sent once, so the board is the record" in collaboration


def test_the_wake_lifecycle_scenarios_name_rules_that_exist():
    """W455 ownership 6: four scenarios for an independent forward test (loaded
    unchanged wake, fresh session, changed revision, coordinator wait). Each
    names the rule that governs it, and that rule must be in the procedure."""

    import json
    from pathlib import Path

    import project_board

    root = Path(project_board.__file__).resolve().parent / "procedures" / "problem-board-worker"
    fixture = Path(__file__).resolve().parent / "fixtures" / "w455_wake_lifecycle_scenarios.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    ids = {scenario["id"] for scenario in data["scenarios"]}
    assert ids == {"loaded-unchanged-wake", "fresh-session", "changed-revision", "active-assignment-coordinator-wait"}
    for scenario in data["scenarios"]:
        assert scenario["expected"] and scenario["forbidden"], scenario["id"]
        for name, quote in scenario["governing"]:
            text = " ".join(reference_text(root / name).split())
            assert quote in text, (scenario["id"], name, quote)
    skill = " ".join((root / "SKILL.md").read_text(encoding="utf-8").split())
    # The lifecycle sits at the entrypoint, before step 1 of Start Or Resume.
    assert skill.index("**Which session this is.**") < skill.index("1. Read the repository instructions of the folder you are in.")
    assert "replay no enrollment, startup read or full skill load" in skill
    assert "nothing here overrides it" in skill

