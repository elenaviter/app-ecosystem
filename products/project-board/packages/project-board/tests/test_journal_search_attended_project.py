"""An agent searches its project's journal with only a query (W305).

W305 acceptance: "In a worker session, one command with only a query searches
the project journal." `pb worker journal-search` required --project-ref, so an
agent about to act on a subject first had to find its project ref. A worker
attending exactly one project now searches that project's journal by default,
and a worker attending none or several is asked to name one, with the choices.
"""

from __future__ import annotations

import pytest

from project_board.client import cli, journals
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError
from relay_helpers import make_host

PROJECT = "work:project:demo-project-0a1b2c3d"
OTHER = "work:project:other-project"


def _search(monkeypatch, tmp_path, attended, *argv):
    host, identity, _channel = make_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.register_worker(
        worker_name=identity.worker_name,
        runtime_kind=identity.runtime_kind,
        capabilities=[],
        authority_label="authority:codex-api",
    )
    field.sync_worker_attendances(identity.worker_name, attended)
    searched = {}

    def search(self, query, **scope):
        searched.update(query=query, **scope)
        return []

    monkeypatch.setattr(journals.JournalWorkspace, "search", search)
    monkeypatch.setattr(journals.JournalWorkspace, "index_status", lambda self: {})
    args = cli.build_parser().parse_args(
        [
            "worker", "journal-search",
            "--config", str(host.path),
            "--runtime-kind", identity.runtime_kind,
            "--runtime-session-id", identity.runtime_session_id,
            "--query", "connect the agents on spark1",
            *argv,
        ]
    )
    return cli._worker_command(args), searched  # noqa: SLF001 - the command under test


def test_a_query_alone_searches_the_one_attended_project(monkeypatch, tmp_path):
    _result, searched = _search(monkeypatch, tmp_path, [PROJECT])

    assert searched["query"] == "connect the agents on spark1"
    assert searched["project_ref"] == PROJECT


def test_a_named_project_is_searched_as_named(monkeypatch, tmp_path):
    _result, searched = _search(monkeypatch, tmp_path, [PROJECT, OTHER], "--project-ref", OTHER)

    assert searched["project_ref"] == OTHER


@pytest.mark.parametrize(
    ("attended", "said"),
    [([], "attends no project"), ([PROJECT, OTHER], "attends several projects")],
)
def test_no_or_several_projects_are_refused_with_the_choices(monkeypatch, tmp_path, attended, said):
    with pytest.raises(DomainError) as refused:
        _search(monkeypatch, tmp_path, attended)

    assert refused.value.code == "field_project_ref_required"
    assert said in str(refused.value)
    assert refused.value.details["attended_project_refs"] == sorted(attended)
