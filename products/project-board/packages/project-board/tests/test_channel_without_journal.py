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
    missing_path = str((tmp_path / "never-cloned").resolve())
    assert gap["message"] == f"journal unavailable for work:project:project: applications not found at {missing_path}"


class RecordingField(SharedFieldStore):
    def __init__(self, root):
        super().__init__(root)
        self.events = []

    def enqueue_service_event(self, project_id, **values):
        self.events.append({"project_id": project_id, **values})
        return {"queued": True}


BINDING = {
    "journal_binding": {
        "project_ref": "work:project:project",
        "journal_home_ref": "repo:applications/docs/journal/projects/project",
        "project_artifact_ref": "",
        "revision": 1,
    }
}


def test_the_worker_card_hears_once_that_the_journal_is_unavailable_and_once_that_it_is_back(tmp_path):
    host, _identity, channel = make_host(tmp_path)
    checkout = tmp_path / "later-cloned"
    config = dataclasses.replace(
        relay.RelayConfig.from_host_channel(host, channel, project_id="project"),
        journal_workspace_root=tmp_path / "journal-workspace",
        source_repositories={"applications": str(checkout)},
        create_missing_journal_home=True,
    )
    field = RecordingField(host.field_root)
    adapter = relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=object())

    adapter._reconcile_journal_binding(BINDING)
    adapter._reconcile_journal_binding(BINDING)

    [unavailable] = field.events
    assert unavailable["kind"] == "worker.journal"
    assert unavailable["summary"] == (
        f"journal unavailable for work:project:project: applications not found at {checkout.resolve()}"
    )
    assert unavailable["metadata"]["state"] == "unavailable"
    assert unavailable["metadata"]["path"] == str(checkout.resolve())

    (checkout / "docs" / "journal" / "projects" / "project").mkdir(parents=True)
    restored = adapter._reconcile_journal_binding(BINDING)

    assert restored.get("state") != "unmapped"
    assert [event["metadata"]["state"] for event in field.events] == ["unavailable", "available"]
    assert field.events[1]["summary"] == "journal available again for work:project:project"
    assert field.events[1]["idempotency_key"] == unavailable["idempotency_key"] + ":restored"

    adapter._reconcile_journal_binding(BINDING)
    assert len(field.events) == 2
