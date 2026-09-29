"""Read-heavy coordinator evidence stays useful, whole-addressed, and cheap."""

from __future__ import annotations

import json

from project_board.client import cli
from project_board.client.render import render_envelope


PROJECT_REF = "work:project:compact-coordinator-evidence"
IDENTITY_REF = "work:item:W393:compact-coordinator-reads-" + "i" * 96
EXACT_REF = IDENTITY_REF + "@2026-09-29T12:34:56Z-revision-17"
ASSIGNMENT_REF = "work:assignment:compact-coordinator-reads-" + "a" * 96
CONTROL_REF = "work:control:compact-coordinator-reads-" + "c" * 96
ENTRY_REF = "work:journal:2026-09-29T12:34:56Z-" + "j" * 96
REPOSITORY_REF = "repo:app-ecosystem/products/project-board/" + "r" * 96
CURSOR = "cursor." + "k" * 256
LONG_PROSE = "decision evidence " + ("large-body " * 2000) + "OMITTED_TAIL"


def _brief(result: dict) -> str:
    return render_envelope({"ok": True, "result": result})


def _assert_budget(text: str, *, lines: int, bytes_: int) -> None:
    assert len(text.splitlines()) <= lines
    assert len(text.encode("utf-8")) <= bytes_


def test_worker_context_keeps_coordinates_and_bounds_repeated_sections() -> None:
    team = [
        {
            "worker_alias": f"agent-{index}",
            "worker_name": f"codex-{index:02d}-" + "w" * 48,
            "role": "worker",
            "runtime_kind": "codex",
            "host_label": f"host-{index % 3}",
            "pool_status": "active",
            "presence": "online",
            "info_text": LONG_PROSE,
            "limit_state": {
                "kind": "ok",
                "windows": [
                    {
                        "name": "seven_day",
                        "used_percent": 42,
                        "resets_at": "2026-10-01T10:00:00Z",
                    }
                ],
            },
        }
        for index in range(14)
    ]
    repositories = [
        {
            "alias": f"repo-{index}",
            "url": f"git@example.test:team/repository-{index}.git",
            "role": "source",
            "branch": "main",
            "path": f"src/repository-{index}",
            "repository_ref": REPOSITORY_REF + str(index),
        }
        for index in range(14)
    ]
    result = {
        "project_ref": PROJECT_REF,
        "project_on_this_host": True,
        "project_card": "known",
        "project_goal": LONG_PROSE,
        "workspace": "/workspaces/codex-main",
        "workspace_source": "worker_record",
        "journal_state": "available",
        "journal_home_ref": REPOSITORY_REF + "/journal",
        "coordinator": {
            "state": "current",
            "acting": False,
            "revision": 8,
            "holder": {
                "worker_ref": "work:worker:" + "h" * 96,
                "worker_name": team[0]["worker_name"],
                "worker_alias": team[0]["worker_alias"],
                "attending": True,
                "private_history": LONG_PROSE,
            },
        },
        "team": team,
        "self": {
            "state": "received",
            "worker_ref": "work:worker:" + "s" * 96,
            "worker_name": team[0]["worker_name"],
            "owner": {"user_id": "user-1", "display_name": "Operator"},
            "runtime_account": {
                "account_id": "account-1",
                "email": "agent@example.test",
                "private_history": LONG_PROSE,
            },
        },
        "repositories": repositories,
        "repositories_revision": 5,
        "commit_identity": {
            "name": "Project Worker",
            "email": "worker@example.test",
            "source": "project",
            "commands": ["git config user.name 'Project Worker'"],
        },
        "project_files": [
            {
                "ref": REPOSITORY_REF + f"/facts-{index}.md",
                "local_path": f"/workspaces/codex-main/facts-{index}.md",
                "state": "available",
                "description": LONG_PROSE,
            }
            for index in range(14)
        ],
        "runtimes": [
            {
                "name": "maintainer",
                "host": "host-0",
                "kind": "shell",
                "profile_ref": REPOSITORY_REF + "/runtime-profile.md",
                "local_profile": "/workspaces/codex-main/runtime-profile.md",
                "actions": [],
            }
        ],
    }

    text = _brief(result)

    assert f"project_ref = {PROJECT_REF}" in text
    assert "project_on_this_host = True" in text
    assert f"coordinator.holder.worker_ref = {'work:worker:' + 'h' * 96}" in text
    assert "repositories[0].alias = repo-0" in text
    assert f"repositories[0].repository_ref = {REPOSITORY_REF}0" in text
    assert "team members: 8 of 14 shown in brief" in text
    assert "repositories: 8 of 14 shown in brief" in text
    assert "further project files: 8 of 14 shown in brief" in text
    assert "team usage:" in text
    assert "private_history" not in text
    assert "OMITTED_TAIL" not in text
    assert "--format json for every field" in text
    _assert_budget(text, lines=160, bytes_=24_000)


def test_journal_search_keeps_hit_coordinates_with_a_bounded_page() -> None:
    entries = [
        {
            "entry_ref": ENTRY_REF + str(index),
            "project_ref": PROJECT_REF,
            "work_ref": EXACT_REF + str(index),
            "repository_journal_ref": REPOSITORY_REF + f"/journal/{index}.md",
            "title": f"Coordinator evidence {index}",
            "status": "complete",
            "recorded_at": "2026-09-29T12:34:56Z",
            "worker_name": "codex-main",
            "summary": LONG_PROSE,
            "snippet": "matching words " + LONG_PROSE,
            "tags": ["coordination", "freshness"],
            "keywords": ["targeted read"],
            "see_also": [IDENTITY_REF + str(index)],
            "content": LONG_PROSE,
            "search_ranks": {"lexical": list(range(1000))},
        }
        for index in range(14)
    ]
    result = {
        "entries": entries,
        "index": {
            "state": "ready",
            "indexed_entries": 400,
            "issue_count": 0,
            "excluded_count": 2,
            "recorded_at": "2026-09-29T12:35:00Z",
            "diagnostics": LONG_PROSE,
        },
    }

    text = _brief(result)

    assert "journal search: returned 14 · brief 8" in text
    assert f"entry_ref: {ENTRY_REF}0" in text
    assert f"work_ref: {EXACT_REF}0" in text
    assert f"repository_journal_ref: {REPOSITORY_REF}/journal/0.md" in text
    assert "summary: decision evidence" in text
    assert "snippet: matching words decision evidence" in text
    assert "journal results: 8 of 14 shown in brief" in text
    assert ENTRY_REF + "13" not in text
    assert "content" not in text and "search_ranks" not in text
    assert "OMITTED_TAIL" not in text
    _assert_budget(text, lines=115, bytes_=18_000)


def test_source_status_keeps_release_and_commit_identity_without_diagnostics() -> None:
    release_id = "f" * 64
    commit = "d" * 40
    source = {
        "mode": "snapshot",
        "release_id": release_id,
        "commit": commit,
        "components": [{"name": "app_ecosystem", "commit": commit}],
    }
    relay = {
        "config": "/var/lib/problem-board/relay.json",
        "service_id": "problem-board-relay",
        "system": "Linux",
        "installed": True,
        "running": True,
        "source": source,
        "bootstrap_source": {"mode": "released", "version": "2026.09.29.1234"},
        "startup_record": {
            "pid": 1234,
            "started_at": "2026-09-29T12:34:56Z",
            "source": source,
        },
        "manager_status": LONG_PROSE,
        "relay_diagnostics": {"records": [LONG_PROSE] * 100},
        "log": {"state": "current", "tail": LONG_PROSE},
    }
    result = {
        "schema": "project-board.client-source-status.v2",
        "selected": source,
        "running_release": source,
        "released_bootstrap": {"mode": "released", "version": "2026.09.29.1234"},
        "selection_matches_bootstrap": True,
        "active_release": {
            "release_id": release_id,
            "path": "/opt/problem-board/releases/" + release_id,
            "source": source,
        },
        "environment": {
            "python": "/opt/problem-board/releases/current/venv/bin/python",
            "pb": "/opt/problem-board/releases/current/venv/bin/pb",
        },
        "launcher": {
            "installed": True,
            "current": True,
            "version": "2",
            "expected_version": "2",
            "path": "/usr/local/bin/pb",
            "expected_pb": "/opt/problem-board/releases/current/venv/bin/pb",
        },
        "relay": relay,
        "host_relays": [{**relay, "config": f"/targets/{index}/relay.json"} for index in range(12)],
    }

    text = _brief(result)

    assert f"release={release_id}" in text
    assert f"commit={commit}" in text
    assert f"active_release.release_id: {release_id}" in text
    assert "running: True" in text
    assert "host relays: 8 of 12 shown in brief" in text
    assert "relay_diagnostics" not in text
    assert "manager_status" not in text
    assert "OMITTED_TAIL" not in text
    _assert_budget(text, lines=130, bytes_=20_000)


def _plan_search_item(index: int) -> dict:
    return {
        "item_key": f"W{393 + index}",
        "status": "working",
        "title": f"Compact coordinator read {index}",
        "identity_ref": IDENTITY_REF + str(index),
        "item_ref": EXACT_REF + str(index),
        "work_ref": EXACT_REF + str(index),
        "revision": 17 + index,
        "assignee": "codex-main",
        "derived_state": "ready",
        "updated_at": "2026-09-29T12:34:56Z",
        "search_rank": index + 1,
        "summary": LONG_PROSE,
        "search_text": LONG_PROSE,
        "source_content_hash": "b" * 64,
        "depends_on": [IDENTITY_REF + "dependency"],
    }


def test_plan_search_keeps_ranked_refs_cursor_and_a_bounded_page() -> None:
    result = {
        "operation": "project.plan.search",
        "object": {
            "schema": "problem-board.plan-index.v2",
            "project_ref": PROJECT_REF,
            "plan_revision": 71,
            "item_count": 900,
            "generation_token": "generation-" + "g" * 96,
            "query": "compact coordinator reads",
            "status": ["working"],
            "assignee": ["codex-main"],
            "lifecycle": ["active"],
            "snapshot_id": "plan-search-" + "s" * 96,
            "page": 1,
            "page_count": 4,
            "matched_count": 38,
            "spend": False,
            "items": [_plan_search_item(index) for index in range(14)],
            "next_cursor": CURSOR,
            "search_identity": {"large": LONG_PROSE},
        },
    }

    text = _brief(result)

    assert "plan search: matched 38 · returned 14 · brief 8" in text
    assert f"project_ref: {PROJECT_REF}" in text
    assert f"next_cursor: {CURSOR}" in text
    assert f"identity_ref: {IDENTITY_REF}0" in text
    assert f"item_ref: {EXACT_REF}0" in text
    assert "summary: decision evidence" in text
    assert "plan results: 8 of 14 shown in brief" in text
    assert IDENTITY_REF + "13" not in text
    assert "search_text" not in text and "source_content_hash" not in text
    assert "OMITTED_TAIL" not in text
    _assert_budget(text, lines=95, bytes_=18_000)


def test_plan_item_keeps_actionable_refs_and_bounds_the_complete_body() -> None:
    item = {
        "item_key": "W393",
        "status": "working",
        "title": "Compact coordinator reads",
        "identity_ref": IDENTITY_REF,
        "item_ref": EXACT_REF,
        "work_ref": EXACT_REF,
        "revision": 17,
        "assignee": "codex-main",
        "reviewer": "claude-review",
        "updated_at": "2026-09-29T12:34:56Z",
        "summary": LONG_PROSE,
        "description": LONG_PROSE,
        "acceptance": [f"acceptance {index} {LONG_PROSE}" for index in range(8)],
        "depends_on": [IDENTITY_REF + f"-dependency-{index}" for index in range(15)],
        "dependency_facts": [
            {"item_ref": IDENTITY_REF + f"-dependency-{index}", "state": "complete", "on_page": False}
            for index in range(15)
        ],
        "assignment": {
            "state": "working",
            "ownership_version": 4,
            "worker_name": "codex-main",
            "updated_at": "2026-09-29T12:34:56Z",
            "assignment_ref": ASSIGNMENT_REF,
            "identity_ref": IDENTITY_REF,
            "work_ref": EXACT_REF,
            "versioned_work_ref": EXACT_REF,
            "control_ref": CONTROL_REF,
        },
        "attachment_refs": [REPOSITORY_REF + f"/artifact-{index}.md" for index in range(15)],
        "notes": [LONG_PROSE] * 100,
    }

    text = _brief({"operation": "project.plan.item", "object": item})

    for ref in (IDENTITY_REF, EXACT_REF, ASSIGNMENT_REF, CONTROL_REF):
        assert ref in text
    assert "description preview: decision evidence" in text
    assert "acceptance lines: 5 of 8 shown in brief" in text
    assert "dependencies: 12 of 15 shown in brief" in text
    assert "dependency facts: 12 of 15 shown in brief" in text
    assert "attachments: 12 of 15 shown in brief" in text
    assert "notes" not in text and "OMITTED_TAIL" not in text
    _assert_budget(text, lines=75, bytes_=20_000)


def _assignment(index: int) -> dict:
    identity = IDENTITY_REF + str(index)
    exact = EXACT_REF + str(index)
    return {
        "item_key": f"W{393 + index}",
        "item_title": f"Compact coordinator read {index}",
        "item_status": "working",
        "state": "working",
        "ownership_version": index + 1,
        "assignment_ref": ASSIGNMENT_REF + str(index),
        "identity_ref": identity,
        "work_ref": exact,
        "versioned_work_ref": exact,
        "control_ref": CONTROL_REF + str(index),
        "worker_name": "codex-main",
        "assigned_at": "2026-09-29T12:00:00Z",
        "updated_at": "2026-09-29T12:34:56Z",
        "task": {"instructions": LONG_PROSE, "private_material": LONG_PROSE},
        "sources": [
            {
                "repository_ref": REPOSITORY_REF + str(index),
                "base_commit": f"{index:040x}",
                "branch": f"work/w{393 + index}",
            }
        ],
        "reports": [LONG_PROSE] * 100,
    }


def test_assignment_list_keeps_ownership_refs_and_a_bounded_page() -> None:
    result = {
        "operation": "assignment.list",
        "object": {
            "schema": "problem-board.assignment-list.v1",
            "project_ref": PROJECT_REF,
            "worker_ref": "work:worker:" + "w" * 96,
            "worker_name": "codex-main",
            "criteria": {
                "refs": [ASSIGNMENT_REF],
                "status": ["working"],
                "query": "compact reads",
                "match": "all",
            },
            "item_count": 42,
            "matched_count": 14,
            "items": [_assignment(index) for index in range(14)],
            "generation_token": "assignment-generation-" + "g" * 96,
            "next_cursor": CURSOR,
            "updated_at": "2026-09-29T12:34:56Z",
        },
    }

    text = _brief(result)

    assert "assignments: matched 14 · returned 14 · brief 8 · total current 42" in text
    assert f"next_cursor: {CURSOR}" in text
    assert f"assignment_ref: {ASSIGNMENT_REF}0" in text
    assert f"identity_ref: {IDENTITY_REF}0" in text
    assert f"work_ref: {EXACT_REF}0" in text
    assert f"source[0].repository_ref: {REPOSITORY_REF}0" in text
    assert "task preview: decision evidence" in text
    assert "assignments: 8 of 14 shown in brief" in text
    assert ASSIGNMENT_REF + "13" not in text
    assert "reports" not in text and "private_material" not in text
    assert "OMITTED_TAIL" not in text
    _assert_budget(text, lines=145, bytes_=24_000)


def test_json_format_is_the_explicit_full_detail_path(monkeypatch, capsys) -> None:
    full = {
        "operation": "project.plan.item",
        "object": {
            "item_key": "W393",
            "identity_ref": IDENTITY_REF,
            "description": LONG_PROSE,
            "full_only_history": [LONG_PROSE, LONG_PROSE],
        },
    }
    monkeypatch.setattr(cli, "_coordinate_command", lambda _args: full)

    exit_code = cli.main(
        [
            "coordinate",
            "project.plan.item",
            "--object-ref",
            PROJECT_REF,
            "--format",
            "json",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0 and captured.err == ""
    assert json.loads(captured.out) == {"ok": True, "result": full}
    assert "OMITTED_TAIL" in captured.out
