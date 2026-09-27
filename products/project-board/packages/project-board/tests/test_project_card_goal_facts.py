"""The project card's goal and facts reach every agent, and a project may keep no journal (W370).

Operator ruling, 2026-09-27: the project card holds a goal and facts, and a
project may have no journal. The board sends ``goal``, ``facts`` and
``facts_revision`` in the project record that rides each heartbeat; the relay
keeps them on the host; ``pb worker context`` returns them as ``project_goal``,
``project_facts`` and ``project_facts_revision``, with ``project_card`` saying
whether the board sent them at all. A project with no journal reads
``journal_state: none``, not an error.
"""

from __future__ import annotations

import asyncio
from typing import Any

from project_board.client import relay
from test_commit_identity import IdentityBoard, _host
from test_workspace_report import _cli, _remote

PROJECT_ID = "demo-project-0a1b2c3d"
PROJECT_REF = "work:project:" + PROJECT_ID


class CardBoard(IdentityBoard):
    """A board whose project record carries the card's goal and facts, or none (an older board)."""

    def __init__(self, recipient, remote, *, card: dict[str, Any] | None = None, repositories=None) -> None:
        super().__init__(recipient, remote)
        self.card = card
        self.repositories = repositories

    async def action(self, *, object_ref: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        answer = await super().action(object_ref=object_ref, action=action, payload=payload)
        if action == "worker.heartbeat" and (payload or {}).get("project_ref"):
            project = answer["object"]["assignment_project"]
            if self.repositories is not None:
                project["repositories"] = self.repositories
            if self.card is not None:
                project.update(self.card)
        return answer


def _context(tmp_path, monkeypatch, **board: Any) -> tuple[dict[str, Any], Any, Any]:
    identity, field, config, workspace = _host(tmp_path, monkeypatch)
    remote = _remote(tmp_path / "remotes", "applications")
    card_board = CardBoard(identity.worker_name, remote, **board)
    adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=card_board)
    asyncio.run(adapter.poll_attendances_once())
    asyncio.run(adapter.poll_attendances_once())
    return _cli(identity, "context", "--project-ref", PROJECT_REF), (identity, adapter, card_board), field


GOAL = "Ship the quickstart so a stranger's machine works in one sitting."
FACTS = [
    {"label": "Release", "value": "2026.09.27.2142"},
    {"label": "Board", "value": "the team's deployment"},
]


def test_the_card_goal_and_facts_reach_the_context_and_follow_each_edit(tmp_path, monkeypatch):
    context, (identity, adapter, board), field = _context(
        tmp_path, monkeypatch, card={"goal": GOAL, "facts": FACTS, "facts_revision": 4}
    )
    assert context["project_card"] == "known"
    assert context["project_goal"] == GOAL
    assert context["project_facts"] == FACTS
    assert context["project_facts_revision"] == 4

    # An edit advances the revision; an older answer never replaces it. (The relay
    # hands every heartbeat's project row to this store call, scoped to its project.)
    def sync(row):
        if "facts_revision" in row:
            field.sync_project_card(PROJECT_ID, goal=row.get("goal"), facts=row.get("facts"), revision=row["facts_revision"])

    sync({"goal": GOAL, "facts": [*FACTS, {"label": "Host", "value": "two\nlines"}], "facts_revision": 5})
    again = _cli(identity, "context", "--project-ref", PROJECT_REF)
    assert again["project_facts_revision"] == 5
    assert again["project_facts"][-1] == {"label": "Host", "value": "two lines"}
    sync({"goal": "stale", "facts": [], "facts_revision": 3})
    assert _cli(identity, "context", "--project-ref", PROJECT_REF)["project_goal"] == GOAL
    assert field.read_project_card(PROJECT_ID)["facts_revision"] == 5


def test_the_board_bounds_hold_on_the_host(tmp_path, monkeypatch):
    many = [{"label": "L" * 200, "value": "v" * 900} for _ in range(70)]
    context, _, _ = _context(
        tmp_path, monkeypatch, card={"goal": "g" * 3000, "facts": many, "facts_revision": 1}
    )
    assert len(context["project_goal"]) == 2000
    assert len(context["project_facts"]) == 50
    assert len(context["project_facts"][0]["label"]) == 80 and len(context["project_facts"][0]["value"]) == 500


def test_a_board_that_predates_the_card_fields_reads_unknown(tmp_path, monkeypatch):
    context, _, _ = _context(tmp_path, monkeypatch, card=None)
    # The fixture's project row carries a goal, as boards did before W370: that alone is not the card.
    assert context["project_card"] == "unknown" and context["project_facts_revision"] == 0
    assert "project_goal" not in context and "project_facts" not in context


def test_an_empty_card_is_known_and_empty(tmp_path, monkeypatch):
    context, _, _ = _context(tmp_path, monkeypatch, card={"goal": "", "facts": [], "facts_revision": 1})
    assert context["project_card"] == "known" and context["project_goal"] == "" and context["project_facts"] == []


def test_a_project_with_no_journal_reads_none_not_an_error(tmp_path, monkeypatch):
    work_only = [{"alias": "ledger", "url": "/srv/operator/ledger", "role": "work", "branch": "main"}]
    context, _, _ = _context(
        tmp_path, monkeypatch, card={"goal": GOAL, "facts": FACTS, "facts_revision": 1}, repositories=work_only
    )
    assert context["journal_state"] == "none"
    assert "journal_error_code" not in context
    assert context["project_facts"] == FACTS


def test_a_declared_journal_that_is_not_here_is_still_unavailable(tmp_path, monkeypatch):
    # The default card lists a journal repository that is not cloned here.
    context, _, _ = _context(tmp_path, monkeypatch, card={"goal": GOAL, "facts": FACTS, "facts_revision": 1})
    assert context["journal_state"] == "unavailable" and context["journal_error_code"]


def test_the_skill_reads_the_card_first_and_a_project_without_a_journal_quietly():
    from project_board.client.procedures import source_package_path

    skill = " ".join((source_package_path() / "SKILL.md").read_text(encoding="utf-8").split())
    workspace = " ".join((source_package_path() / "references" / "project-workspace.md").read_text(encoding="utf-8").split())
    assert "`project_goal` and `project_facts` from `pb worker context` first" in skill
    for piece in (
        "## The project card: goal and facts",
        "**Read `project_goal` and `project_facts` first.**",
        "the card wins, and you tell the coordinator about the difference",
        "**With no journal** (`journal_state: none`)",
        "run no journal search or journal write",
        "(`plan.note.append`)",
        "**With `project_card: unknown`**",
    ):
        assert piece in workspace, piece
