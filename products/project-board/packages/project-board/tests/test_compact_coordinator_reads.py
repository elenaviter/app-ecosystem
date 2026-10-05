"""Read-heavy coordinator evidence stays useful, whole-addressed, and cheap."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any

from project_board.client import cli, relay
from project_board.client.render import render_envelope
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError
from test_attendance_materializes_project import _fresh_host


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
                        # A reset still ahead: a fixed date that has passed adds a
                        # "reset passed" line per member and breaks the bound
                        # from that moment on (it did on 2026-10-01 10:00Z).
                        "resets_at": "2099-01-01T00:00:00Z",
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
    # W393: every teammate is accounted for, never a silent first eight.
    assert "team: 14 · shown 14" in text
    for index in range(14):
        assert f"--- team member {index + 1} of 14: agent-{index} (codex-{index:02d}-" in text
    assert "repositories: 8 of 14 shown in brief" in text
    assert "further project files: 8 of 14 shown in brief" in text
    assert "team usage:" in text
    assert "private_history" not in text
    assert "OMITTED_TAIL" not in text
    assert "--format json for every field" in text
    # Every teammate is shown, so the budget grows per member and stays fixed
    # for everything else: 14 members with long info lines measure 192 lines
    # and 26,998 bytes.
    _assert_budget(text, lines=60 + 10 * 14, bytes_=8_000 + 1_500 * 14)


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


def test_source_status_distinguishes_released_sources_with_the_same_version() -> None:
    version = "2026.09.29.1234"
    running_release_id = "a" * 64
    startup_release_id = "b" * 64
    result = {
        "schema": "project-board.client-source-status.v2",
        "running_release": {
            "mode": "released",
            "version": version,
            "release_id": running_release_id,
        },
        "selection_matches_bootstrap": True,
        "relay": {
            "startup_record": {
                "pid": 1234,
                "started_at": "2026-09-29T12:34:56Z",
                "source": {
                    "mode": "released",
                    "version": version,
                    "release_id": startup_release_id,
                },
            }
        },
    }

    text = _brief(result)

    assert (
        f"running pb: source=released version={version} release={running_release_id}"
        in text
    )
    assert (
        f"startup source: source=released version={version} release={startup_release_id}"
        in text
    )


def test_worker_context_marks_old_usage_as_a_provenanced_report() -> None:
    observed_at = "2026-09-25T01:00:00Z"
    result = {
        "project_ref": PROJECT_REF,
        "workspace": "/workspaces/codex-main",
        "repositories": [],
        "team": [
            {
                "worker_name": "claude-code-old-sample",
                "worker_alias": "claude-old@host-one",
                "host_label": "host-one",
                "limit_state": {
                    "kind": "ok",
                    "source": "claude-code-statusline",
                    "observed_at": observed_at,
                    "windows": [
                        {
                            "name": "seven_day",
                            "used_percent": 45,
                            "window_minutes": 10080,
                            "resets_at": "2026-09-26T10:00:00Z",
                        }
                    ],
                },
            }
        ],
    }

    text = _brief(result)

    assert (
        "  claude-old@host-one (claude-code-old-sample) on host-one: "
        "last reported usage ok · week 45% resets 09-26 10:00Z "
        f"· source claude-code-statusline · observed {observed_at}"
        in text
    )


def test_compact_worker_and_team_rows_show_relay_reported_runtime_identity(
    tmp_path,
) -> None:
    model_observed = "2026-09-29T12:30:00Z"
    account_observed = "2026-09-29T12:31:00Z"
    runtime_model = {
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
        "source": "codex-rollout",
        "observed_at": model_observed,
    }
    runtime_account = {
        "provider": "openai",
        "account_id": "account-1",
        "email": "agent@example.test",
        "source": "host-auth",
        "observed_at": account_observed,
    }
    worker_text = _brief(
        {
            "workers": [
                {
                    "worker_alias": "codex-main",
                    "worker_name": "codex-01",
                    "runtime_kind": "codex",
                    "pool_status": "active",
                    "reachability": {
                        "state": "listening",
                        "session_state": "attached",
                        "reachable": True,
                    },
                    "runtime_model": runtime_model,
                    "board_record": {
                        "runtime_account": {
                            key: value
                            for key, value in runtime_account.items()
                            if key not in {"source", "observed_at"}
                        },
                        "runtime_account_source": "host-auth",
                        "runtime_account_observed_at": account_observed,
                    },
                }
            ]
        }
    )

    assert (
        "runtime: model gpt-5.6-sol · reasoning effort xhigh · state reported "
        f"· source codex-rollout · observed {model_observed}"
        in worker_text
    )
    assert (
        "provider account: provider openai · account_id account-1 · "
        "email agent@example.test · state reported · source host-auth "
        f"· observed {account_observed}"
        in worker_text
    )
    _assert_budget(worker_text, lines=15, bytes_=4_000)

    store = SharedFieldStore(tmp_path / "field")
    store.initialize(field_id="compact-runtime-evidence")
    store.create_project(
        project_id="compact-runtime-evidence",
        title="Compact runtime evidence",
        goal="Keep relay reports visible.",
        owner="operator",
    )
    store.sync_project_team(
        "compact-runtime-evidence",
        [
            {
                "worker_alias": "codex-main",
                "worker_name": "codex-01",
                "runtime_kind": "codex",
                "presence": "online",
                "runtime_model": runtime_model,
                "runtime_account": runtime_account,
            }
        ],
    )
    [stored_member] = store.read_project_team("compact-runtime-evidence")
    assert stored_member["runtime_model"] == runtime_model
    assert stored_member["runtime_account"] == runtime_account

    context_text = _brief(
        {
            "project_ref": PROJECT_REF,
            "workspace": "/workspaces/codex-main",
            "repositories": [],
            "team": [stored_member],
        }
    )
    assert f"source codex-rollout · observed {model_observed}" in context_text
    assert f"source host-auth · observed {account_observed}" in context_text
    _assert_budget(context_text, lines=25, bytes_=5_000)


def test_team_runtime_identity_reaches_context_from_real_heartbeat_evidence(
    tmp_path, monkeypatch
) -> None:
    """The server-shaped team projection is derived from what the relay sends."""

    project_id = PROJECT_REF.rsplit(":", 1)[-1]
    host, identity, field, base_config = _fresh_host(tmp_path)
    field.create_project(
        project_id=project_id,
        title="Compact coordinator evidence",
        goal="Keep current team runtime evidence visible.",
        owner="operator",
    )
    field.sync_worker_attendances(identity.worker_name, [PROJECT_REF])
    model_observed = "2026-09-29T12:30:00Z"
    field.record_runtime_model(
        identity.worker_name,
        {
            "model": "gpt-5.6-sol",
            "effort": "xhigh",
            "source": "codex-rollout",
            "observed_at": model_observed,
        },
    )
    account_observations: list[Any] = [
        {
            "account_id": "account-1",
            "email": "agent@example.test",
            "organization": "org-1",
        },
        DomainError(
            "work_runtime_account_unavailable",
            "The coding runtime account file could not be read.",
        ),
        DomainError(
            "work_runtime_account_missing",
            "The coding runtime did not report a signed-in account.",
        ),
    ]

    async def account_reader() -> dict[str, str]:
        observation = account_observations.pop(0)
        if isinstance(observation, Exception):
            raise observation
        return observation

    class ProducerContractBoard:
        """The server contract, driven only by fields the real relay publishes."""

        def __init__(self) -> None:
            self.heartbeats: list[dict[str, Any]] = []
            self.runtime_model: dict[str, Any] = {}
            self.runtime_account: dict[str, Any] = {}

        def _consume(self, payload: dict[str, Any]) -> None:
            sessions = payload.get("agent_sessions")
            if isinstance(sessions, list):
                session = next(
                    (row for row in sessions if isinstance(row, dict)), {}
                )
                model = session.get("runtime_model")
                self.runtime_model = (
                    {**model, "state": "reported"}
                    if isinstance(model, dict) and model
                    else {}
                )
            evidence = payload.get("runtime_account_evidence")
            if not isinstance(evidence, dict):
                return  # A legacy/unrelated omission means unchanged.
            state = str(evidence.get("state") or "")
            if state == "reported":
                account = payload.get("runtime_account")
                assert isinstance(account, dict) and account.get("account_id")
                self.runtime_account = {
                    **account,
                    "source": evidence["source"],
                    "observed_at": evidence["observed_at"],
                    "state": "reported",
                }
            elif state == "stale":
                if self.runtime_account:
                    self.runtime_account = {**self.runtime_account, "state": "stale"}
            elif state == "missing":
                self.runtime_account = {}

        async def action(
            self,
            *,
            object_ref: str,
            action: str,
            payload: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            if action == "worker.heartbeat":
                heartbeat = dict(payload or {})
                self.heartbeats.append(heartbeat)
                self._consume(heartbeat)
                return {
                    "ok": True,
                    "object": {
                        "team": [
                            {
                                "worker_alias": "codex-main",
                                "worker_name": identity.worker_name,
                                "role": "coordinator",
                                "runtime_kind": "codex",
                                "host_label": "host-one",
                                "pool_status": "active",
                                "presence": "online",
                                "runtime_model": self.runtime_model,
                                "runtime_account": self.runtime_account,
                            }
                        ]
                    },
                }
            if action == "control.pull":
                return {"ok": True, "object": {"lease_id": "", "items": []}}
            return {"ok": True, "object": {"ref": object_ref}}

    board = ProducerContractBoard()
    adapter = relay.ProblemBoardHostRelayAdapter(
        config=dataclasses.replace(base_config, project_id=project_id),
        field=field,
        client=board,
        runtime_account_reader=account_reader,
    )
    monkeypatch.setenv("PROBLEM_BOARD_CONFIG", str(host.path))

    def poll_and_render() -> str:
        asyncio.run(
            adapter._poll_project_once(  # noqa: SLF001 - heartbeat boundary
                agent_sessions=adapter._listener_sessions(),  # noqa: SLF001
                force_heartbeat=True,
            )
        )
        context = cli._worker_command(  # noqa: SLF001 - CLI result under test
            cli.build_parser().parse_args(
                [
                    "worker",
                    "context",
                    "--runtime-kind",
                    identity.runtime_kind,
                    "--runtime-session-id",
                    identity.runtime_session_id,
                    "--project-ref",
                    PROJECT_REF,
                ]
            )
        )
        return _brief(context)

    reported_text = poll_and_render()
    first = board.heartbeats[0]
    first_evidence = first["runtime_account_evidence"]
    assert first["project_ref"] == PROJECT_REF
    assert first["agent_sessions"][0]["runtime_model"]["model"] == "gpt-5.6-sol"
    assert first["agent_sessions"][0]["runtime_model"]["effort"] == "xhigh"
    assert first["runtime_account"]["account_id"] == "account-1"
    assert first_evidence["state"] == "reported"
    assert first_evidence["source"] == "host-report"
    assert first_evidence["observed_at"]
    assert (
        "model gpt-5.6-sol · reasoning effort xhigh · state reported "
        f"· source codex-rollout · observed {model_observed}"
    ) in reported_text
    assert (
        "account_id account-1 · email agent@example.test · organization org-1 "
        "· state reported · source host-report "
        f"· observed {first_evidence['observed_at']}"
    ) in reported_text

    stale_text = poll_and_render()
    second = board.heartbeats[1]
    assert "agent_sessions" not in second  # unchanged, not missing
    assert "runtime_account" not in second
    assert second["runtime_account_evidence"]["state"] == "stale"
    assert "model gpt-5.6-sol · reasoning effort xhigh · state reported" in stale_text
    assert (
        "account_id account-1 · email agent@example.test · organization org-1 "
        "· state stale · source host-report "
        f"· observed {first_evidence['observed_at']}"
    ) in stale_text

    missing_text = poll_and_render()
    third = board.heartbeats[2]
    assert "agent_sessions" not in third
    assert "runtime_account" not in third
    assert third["runtime_account_evidence"]["state"] == "missing"
    [stored_member] = field.read_project_team(project_id)
    assert stored_member["runtime_model"]["model"] == "gpt-5.6-sol"
    assert stored_member["runtime_account"] == {}
    assert "model gpt-5.6-sol · reasoning effort xhigh · state reported" in missing_text
    assert (
        "provider account: missing · state missing · source not reported "
        "· observed not reported"
    ) in missing_text


def test_compact_worker_and_team_rows_name_missing_and_stale_runtime_identity() -> None:
    missing = _brief(
        {
            "workers": [
                {
                    "worker_alias": "new-worker",
                    "worker_name": "codex-new",
                    "runtime_kind": "codex",
                    "pool_status": "active",
                    "reachability": {
                        "state": "listening",
                        "session_state": "attached",
                        "reachable": True,
                    },
                }
            ]
        }
    )
    assert (
        "runtime: model missing · reasoning effort missing · state missing "
        "· source not reported · observed not reported"
        in missing
    )
    assert (
        "provider account: missing · state missing · source not reported "
        "· observed not reported"
        in missing
    )
    unprovenanced = _brief(
        {
            "workers": [
                {
                    "worker_alias": "unprovenanced-worker",
                    "worker_name": "codex-unprovenanced",
                    "runtime_kind": "codex",
                    "pool_status": "active",
                    "runtime_account": {"account_id": "account-without-provenance"},
                }
            ]
        }
    )
    assert (
        "provider account: account_id account-without-provenance · state reported "
        "· source not reported · observed not reported"
        in unprovenanced
    )

    stale = _brief(
        {
            "project_ref": PROJECT_REF,
            "workspace": "/workspaces/codex-main",
            "repositories": [],
            "team": [
                {
                    "worker_alias": "stale-worker",
                    "worker_name": "codex-stale",
                    "runtime_kind": "codex",
                    "presence": "stale",
                    "runtime_model": {
                        "model": "gpt-5.6-sol",
                        "effort": "high",
                        "source": "codex-rollout",
                        "observed_at": "2026-09-25T01:00:00Z",
                    },
                    "runtime_account": {
                        "account_id": "account-old",
                        "source": "host-auth",
                        "observed_at": "2026-09-25T01:01:00Z",
                    },
                }
            ],
        }
    )
    assert "reasoning effort high · state stale · source codex-rollout" in stale
    assert "account_id account-old · state stale · source host-auth" in stale
    _assert_budget(missing + unprovenanced + stale, lines=55, bytes_=9_000)


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
    # W563: a count, three files and the commands that list and read the rest,
    # not one line per file.
    assert "attachments: 15 · first 3 shown · list: pb worker item-attachment-list" in text
    assert text.count(REPOSITORY_REF) == 3
    assert "read one: pb worker item-attachment-read" in text
    assert "notes" not in text and "OMITTED_TAIL" not in text
    _assert_budget(text, lines=75, bytes_=20_000)


def test_plan_item_prints_each_ref_once_and_drops_the_title_from_the_summary() -> None:
    item = {
        "item_key": "W563",
        "status": "working",
        "title": "Stop context churn",
        "identity_ref": IDENTITY_REF,
        "item_ref": EXACT_REF,
        "revision": 8,
        "summary": "Stop context churn Operator request: bounded reads.",
        "note_count": 100,
        "attachment_count": 30,
        "attachments": [
            {"filename": f"report-{index}.md", "file_ref": f"pbfile:owner/items/file-{index}", "downloadable": True}
            for index in range(30)
        ],
        "assignment": {
            "state": "assigned",
            "ownership_version": 1,
            "worker_name": "claude-e-main",
            "assignment_ref": ASSIGNMENT_REF,
            "identity_ref": IDENTITY_REF,
            "work_ref": EXACT_REF + "-older",
            "versioned_work_ref": EXACT_REF + "-older",
        },
    }

    text = _brief({"operation": "project.plan.item", "object": item})

    assert text.splitlines().count(f"identity_ref: {IDENTITY_REF}") == 1
    assert "assignment.identity_ref" not in text
    assert f"assignment.assignment_ref: {ASSIGNMENT_REF}" in text
    # The version the ownership was issued at stays in the JSON only.
    assert EXACT_REF + "-older" not in text
    assert "summary: Operator request: bounded reads." in text
    assert "notes: 100 · read: pb coordinate plan.notes.list" in text
    assert "attachments: 30 · first 3 shown" in text
    assert "pbfile:owner/items/file-3" not in text
    assert "downloadable" not in text


def test_item_attachment_list_pages_names_and_refs_without_links(monkeypatch) -> None:
    item = {
        "attachments": [
            {"filename": f"f{index}.txt", "file_ref": f"pbfile:o/i/{index}", "download_url": "https://signed.example/x"}
            for index in range(25)
        ]
    }
    monkeypatch.setattr(cli, "_worker_item", lambda args: item)
    args = type("Args", (), {"project_ref": PROJECT_REF, "item_key": "W1", "offset": 20, "limit": 20})()

    page = cli._worker_item_attachment_list(args)

    assert page["attachment_count"] == 25 and page["returned"] == 5
    assert page["next_offset"] is None
    assert page["attachments"][0] == {"filename": "f20.txt", "file_ref": "pbfile:o/i/20"}
    assert "signed.example" not in json.dumps(page)
    args.offset, args.limit = 0, 101
    try:
        cli._worker_item_attachment_list(args)
    except DomainError as error:
        assert error.code == "work_item_attachment_page_invalid"
    else:
        raise AssertionError("an oversized page was accepted")


def test_plan_item_shows_the_latest_actionable_review_return_reason() -> None:
    item = {
        "item_key": "W393",
        "status": "working",
        "title": "Compact coordinator reads",
        "identity_ref": IDENTITY_REF,
        "item_ref": EXACT_REF,
        "revision": 18,
        "review_history": [
            {
                "decision": "return",
                "timestamp": "2026-09-29T12:00:00Z",
                "reason": "Older return reason.",
            },
            {
                "decision": "return",
                "operation": "review.return",
                "timestamp": "2026-09-29T14:18:36Z",
                "actor": {"label": "codex-review"},
                "reason": "Rebase the overlapping server change, then add runtime identity. "
                + LONG_PROSE,
            },
        ],
    }

    text = _brief({"operation": "project.plan.item", "object": item})

    assert (
        "latest review return: decision return · at 2026-09-29T14:18:36Z "
        "· by codex-review"
        in text
    )
    assert (
        "latest review return reason: Rebase the overlapping server change, "
        "then add runtime identity. decision evidence"
        in text
    )
    assert "Older return reason" not in text
    assert "OMITTED_TAIL" not in text
    _assert_budget(text, lines=20, bytes_=5_000)


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


def _plan_index_item(index: int) -> dict[str, Any]:
    return {
        "item_key": f"W{500 + index}",
        "status": "working",
        "derived_state": "working",
        "title": f"Index item {index} " + "t" * 40,
        "identity_ref": f"{IDENTITY_REF}{index}",
        "item_ref": f"{EXACT_REF}{index}",
        "assignee": "codex-main",
        "acting_assignee": "codex-main",
        "reviewer": "",
        "revision": 40 + index,
        "updated_at": "2026-10-05T12:00:00Z",
        "note_count": 86,
        "attachment_count": 10,
        "depends_on": [],
        "keywords": ["channel reconnect", "Data Bus", "governed dispatch"],
        "tags": ["priority-0", "relay"],
        "search_content_hash": "h" * 64,
        "source_content_hash": "s" * 64,
        "embedding_model_id": "",
        "embedding_present": False,
        "version_slug": "v" * 64,
        "available_transitions": [
            {"label": "Cancel", "operation": "work.cancel", "requires_reason": True},
            {"label": "Release assignment", "operation": "assignment.return", "requires_reason": True},
        ],
    }


def test_plan_index_is_two_lines_per_item_and_keeps_paging() -> None:
    items = [_plan_index_item(index) for index in range(7)]
    result = {
        "operation": "project.plan.index",
        "object": {
            "schema": "problem-board.plan-index.v2",
            "project_ref": PROJECT_REF,
            "plan_revision": 7872,
            "item_count": 555,
            "matched_count": 10,
            "count": 7,
            "page": 1,
            "page_count": 2,
            "generation_token": "generation-" + "g" * 96,
            "next_cursor": CURSOR,
            "state_counts": [{"state": "todo", "count": 154}, {"state": "working", "count": 10}],
            "items": items,
        },
    }

    text = _brief(result)

    assert "plan index: matched 10 · returned 7 · page 1 of 2 · plan revision 7872" in text
    assert f"next_cursor: {CURSOR}" in text
    assert "state counts: todo 154 · working 10" in text
    for index in range(7):
        line = next(line for line in text.splitlines() if line.startswith(f"--- W{500 + index} "))
        assert "working" in line and "assignee codex-main" in line and "notes 86" in line
        assert f"identity_ref: {IDENTITY_REF}{index}" in text
    for bulk in ("search_content_hash", "source_content_hash", "available_transitions", "embedding", "keywords", "version_slug"):
        assert bulk not in text
    # Two lines per item plus a fixed header; the flat form was 17 KB for a
    # real seven-item page (W563 baseline B1).
    _assert_budget(text, lines=7 * 2 + 8, bytes_=3_200)


def test_workspace_sweep_brief_counts_paths_and_prints_removals_whole() -> None:
    trees = [
        {
            "path": f"/ws/wt/w{index}-app",
            "kind": "implementation",
            "item": f"W{index}",
            "branch": f"work/w{index}",
            "head": "",
            "action": "keep",
            "size_bytes": 1000,
            "dirty": [],
            "untracked": [],
            "ignored": [f"build/cache-{n}/" for n in range(300)],
            "unpushed_commits": 0,
            "keep": ["ignored files outside regenerable folders (evidence?): build/cache-0/", "job not ended"],
        }
        for index in range(55)
    ]
    trees.append({"path": "/ws/rv/w9-app-abc", "kind": "review", "item": "W9", "head": "abc123", "action": "remove",
                  "size_bytes": 10, "dirty": [], "untracked": [], "ignored": [], "keep": []})
    result = {
        "worker": "claude-code-x", "workspace": "/ws", "trees": trees,
        "would_remove": ["/ws/rv/w9-app-abc"], "total_bytes": 55010,
        "scratch_runs": [], "loose": [],
    }

    text = _brief(result)

    assert "workspace sweep: /ws · trees 56 · would remove 1" in text
    assert "would_remove: /ws/rv/w9-app-abc" in text
    assert "--- keep · implementation · item W0 · work/w0 · size 1000 · ignored 300 · /ws/wt/w0-app" in text
    assert "(+1 more)" in text and "build/cache-299/" not in text
    # 56 trees: 60 rows are shown whole, so nothing is omitted here.
    _assert_budget(text, lines=130, bytes_=16_000)


def test_a_clipped_scope_is_readable_whole_without_download_links(monkeypatch) -> None:
    # W563, Root 16:50Z (W459 checkpoint): a reviewer stopped because the brief
    # item clipped the current scope, and the JSON read was off limits because
    # the item's attachments could carry download links. The brief view names
    # the safe full read, and that read prints the scope whole with no link.
    scope = "CURRENT ROOT SCOPE: " + " ".join(f"step-{index} do the exact thing" for index in range(400)) + " SCOPE_TAIL"
    item = {
        "item_key": "W459",
        "status": "review",
        "title": "Review scope",
        "identity_ref": IDENTITY_REF,
        "item_ref": EXACT_REF,
        "revision": 41,
        "updated_at": "2026-10-05T16:40:00Z",
        "assignee": "codex-app",
        "reviewer": "claude-e-app",
        "description": scope,
        "acceptance": [f"line {index}" for index in range(7)],
        "review": {"look_at": "REVIEW_STEPS " + "x" * 900 + " REVIEW_TAIL", "could_not_verify": "None"},
        "assignment": {"assignment_ref": ASSIGNMENT_REF, "ownership_version": 3, "state": "working",
                       "worker_name": "codex-app"},
        "note_count": 96,
        "attachments": [
            {"filename": f"report-{index}.md", "file_ref": f"pbfile:o/i/{index}", "mime": "text/markdown",
             "download_url": f"https://signed.example/{index}?token=secret", "download_path": "/api/x/download",
             "downloadable": True}
            for index in range(26)
        ],
        "review_history": [{"decision": "return", "reason": "HISTORY_BODY"}],
    }

    brief = _brief({"operation": "project.plan.item", "object": item})
    assert "SCOPE_TAIL" not in brief
    assert (
        "clipped above: description, acceptance, review · read whole: pb worker item-read "
        "--project-ref <project-ref> --item-key W459 --field description --field acceptance --field review"
    ) in brief

    monkeypatch.setattr(cli, "_worker_item", lambda args: item)
    args = type("Args", (), {"project_ref": PROJECT_REF, "item_key": "W459", "field": []})()
    result = cli._worker_item_read(args)
    full = render_envelope({"ok": True, "result": result})

    assert "SCOPE_TAIL" in full and "REVIEW_TAIL" in full and "[7] line 6" in full
    assert "item: W459 · review · revision 41" in full
    assert f"item_ref: {EXACT_REF}" in full
    assert f"assignment.assignment_ref: {ASSIGNMENT_REF}" in full and "ownership 3" in full
    assert "attachments: 26" in full and "attachment: report-25.md · pbfile:o/i/25" in full
    serialized = json.dumps(result)
    for leaked in ("signed.example", "token=secret", "download", "HISTORY_BODY"):
        assert leaked not in serialized and leaked not in full, leaked
    assert "notes: 96 (not read here" in full

    args.field = ["description"]
    narrow = cli._worker_item_read(args)
    assert list(narrow["fields"]) == ["description"]


def test_worker_context_names_one_identity_command_instead_of_one_per_clone() -> None:
    commands = [f"git -C /ws/repo-{index} config user.{key} value" for index in range(3) for key in ("name", "email")]
    result = {
        "project_ref": PROJECT_REF,
        "workspace": "/ws",
        "team": [],
        "repositories": [],
        "commit_identity": {"name": "agent@host", "email": "agent@example.test", "source": "project", "commands": commands},
    }

    text = _brief(result)

    assert "  name = agent@host" in text and "  email = agent@example.test" in text
    assert (
        f"  set in every clone (6 git config commands): pb worker workspace-report --project-ref {PROJECT_REF} --set-identity"
    ) in text
    assert "git -C /ws/repo-0" not in text
