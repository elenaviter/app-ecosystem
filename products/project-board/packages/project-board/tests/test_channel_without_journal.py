"""A worker's channel opens without any journal checkout on the machine (W304 D13).

On spark1, 2026-09-24, an agent authorized end to end and its relay then refused
to open the channel: `A mapped LOCAL repository checkout does not exist`. The
host's source map named a checkout no step had created, and the relay built its
whole repository map before any channel, refusing all of it for one missing
entry. A channel never depends on a journal: a missing checkout is reported for
the project whose journal needs it, and controls keep flowing.
"""

from __future__ import annotations

import dataclasses

import pytest

from project_board.client import relay
from project_board.client.journals import RepositoryMap
from project_board.contract.errors import DomainError
from project_board.client.store import SharedFieldStore
from relay_helpers import make_host


def test_the_relay_map_records_a_missing_checkout_instead_of_refusing(tmp_path):
    present = tmp_path / "app-ecosystem"
    (present / "docs").mkdir(parents=True)

    mapped = RepositoryMap.from_mapping(
        {"app-ecosystem": str(present), "applications": str(tmp_path / "applications")},
        require_existing=False,
    )

    assert set(mapped.roots) == {"app-ecosystem"}
    assert set(mapped.missing) == {"applications"}
    with pytest.raises(DomainError) as refused:
        mapped.resolve("repo:applications/docs/journal")
    assert refused.value.code == "journal_repository_root_missing"
    assert mapped.resolve("repo:app-ecosystem/docs")[1] == present / "docs"


def test_configuration_still_refuses_a_missing_checkout(tmp_path):
    with pytest.raises(DomainError) as refused:
        RepositoryMap.from_mapping({"applications": str(tmp_path / "typo")})

    assert refused.value.code == "journal_repository_root_missing"


def test_a_channel_opens_and_the_missing_journal_is_reported_for_its_project(tmp_path):
    host, _identity, channel = make_host(tmp_path)
    config = dataclasses.replace(
        relay.RelayConfig.from_host_channel(host, channel, project_id="project"),
        journal_workspace_root=tmp_path / "journal-workspace",
        source_repositories={"applications": str(tmp_path / "never-cloned")},
    )

    adapter = relay.ProblemBoardHostRelayAdapter(
        config=config,
        field=SharedFieldStore(host.field_root),
        client=object(),
    )
    gap = adapter._reconcile_journal_binding(
        {
            "journal_binding": {
                "project_ref": "work:project:project",
                "journal_home_ref": "repo:applications/docs/journal/projects/project",
                "project_artifact_ref": "",
                "revision": 1,
            }
        }
    )

    assert gap["state"] == "unmapped"
    assert gap["error_code"] == "journal_repository_root_missing"
    assert gap["repository"] == "applications"
