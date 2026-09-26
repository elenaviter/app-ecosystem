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
        workspace=str(tmp_path / "workspace"),
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
    missing_path = str((tmp_path / "workspace").resolve() / "applications")
    assert gap["message"] == f"journal unavailable for work:project:project: applications not found at {missing_path}"


class RecordingField(SharedFieldStore):
    """The field store with its service-event outbox recorded, receipts included."""

    def __init__(self, root, events=None):
        super().__init__(root)
        self.events = [] if events is None else events

    def enqueue_service_event(self, project_id, **values):
        self.events.append({"project_id": project_id, **values})
        return {"queued": True}

    def service_event_receipt_exists(self, *, worker_name, idempotency_key):
        return any(event["idempotency_key"] == idempotency_key for event in self.events)


BINDING = {
    "journal_binding": {
        "project_ref": "work:project:project",
        "journal_home_ref": "repo:applications/docs/journal/projects/project",
        "project_artifact_ref": "",
        "revision": 1,
    }
}


def _host(tmp_path, checkout):
    """One host whose worker has not cloned the journal repository into its workspace yet (W343)."""

    host, _identity, channel = make_host(tmp_path)
    config = dataclasses.replace(
        relay.RelayConfig.from_host_channel(host, channel, project_id="project"),
        journal_workspace_root=tmp_path / "journal-workspace",
        workspace=str(checkout.parent),
        create_missing_journal_home=True,
    )
    SharedFieldStore(host.field_root).register_worker(
        worker_name=config.worker_name,
        runtime_kind=config.runtime_kind,
        capabilities=[],
        authority_label="authority:codex-api",
    )
    return host, config


def _clone(checkout):
    """The worker clones the journal repository into its workspace."""

    (checkout / ".git").mkdir(parents=True)
    (checkout / "docs" / "journal" / "projects" / "project").mkdir(parents=True)


def _relay(host, config, events=None):
    """A relay process: a fresh adapter over the host's field, as after a restart."""

    field = RecordingField(host.field_root, events)
    return relay.ProblemBoardHostRelayAdapter(config=config, field=field, client=object()), field


def _record(adapter):
    return adapter.field.journal_incident_record(adapter.config.worker_name, "work:project:project")


def test_the_worker_card_hears_once_that_the_journal_is_unavailable_and_once_that_it_is_back(tmp_path):
    checkout = tmp_path / "workspace" / "applications"
    adapter, field = _relay(*_host(tmp_path, checkout))

    adapter._reconcile_journal_binding(BINDING)
    adapter._reconcile_journal_binding(BINDING)

    [unavailable] = field.events
    assert unavailable["kind"] == "worker.journal"
    assert unavailable["summary"] == (
        f"journal unavailable for work:project:project: applications not found at {checkout.resolve()}"
    )
    assert unavailable["metadata"]["state"] == "unavailable"
    assert unavailable["metadata"]["path"] == str(checkout.resolve())

    _clone(checkout)
    restored = adapter._reconcile_journal_binding(BINDING)

    assert restored.get("state") != "unmapped"
    assert [event["metadata"]["state"] for event in field.events] == ["unavailable", "available"]
    assert field.events[1]["summary"] == "journal available again for work:project:project"
    assert field.events[1]["idempotency_key"] == unavailable["idempotency_key"] + ":restored"

    adapter._reconcile_journal_binding(BINDING)
    assert len(field.events) == 2


def test_a_relay_restarted_during_the_gap_does_not_report_it_again(tmp_path):
    checkout = tmp_path / "workspace" / "applications"
    host, config = _host(tmp_path, checkout)
    first, field = _relay(host, config)
    first._reconcile_journal_binding(BINDING)

    restarted, _field = _relay(host, config, field.events)
    restarted._reconcile_journal_binding(BINDING)
    restarted._reconcile_journal_binding(BINDING)

    assert [event["metadata"]["state"] for event in field.events] == ["unavailable"]


def test_a_journal_that_returns_across_a_restart_is_reported_back(tmp_path):
    checkout = tmp_path / "workspace" / "applications"
    host, config = _host(tmp_path, checkout)
    first, field = _relay(host, config)
    first._reconcile_journal_binding(BINDING)
    _clone(checkout)

    restarted, _field = _relay(host, config, field.events)
    restarted._reconcile_journal_binding(BINDING)
    restarted._reconcile_journal_binding(BINDING)

    unavailable, available = field.events
    assert available["metadata"]["state"] == "available"
    assert available["idempotency_key"] == unavailable["idempotency_key"] + ":restored"
    assert _record(restarted) == {}


def test_an_event_queued_before_its_phase_was_written_is_not_queued_again(tmp_path):
    checkout = tmp_path / "workspace" / "applications"
    host, config = _host(tmp_path, checkout)
    first, field = _relay(host, config)
    first._reconcile_journal_binding(BINDING)
    # The relay stopped after the outbox took the event and before the record said so.
    record = _record(first)
    first.field.write_journal_incident_record(
        first.config.worker_name, "work:project:project", {**record, "open_note": "pending"}
    )

    restarted, _field = _relay(host, config, field.events)
    restarted._reconcile_journal_binding(BINDING)

    assert len(field.events) == 1
    repaired = _record(restarted)
    assert repaired["open_note"] == "enqueued"


def test_a_close_that_failed_to_queue_is_finished_by_the_next_relay(tmp_path):
    checkout = tmp_path / "workspace" / "applications"
    host, config = _host(tmp_path, checkout)
    first, field = _relay(host, config)
    first._reconcile_journal_binding(BINDING)
    _clone(checkout)
    first._journal_notice_event = lambda **_values: False
    first._reconcile_journal_binding(BINDING)
    assert _record(first)["close_note"] == "pending"

    restarted, _field = _relay(host, config, field.events)
    restarted._reconcile_journal_binding(BINDING)
    restarted._reconcile_journal_binding(BINDING)

    assert [event["metadata"]["state"] for event in field.events] == ["unavailable", "available"]
