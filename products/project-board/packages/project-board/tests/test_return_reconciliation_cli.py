"""Admitted two-channel CLI return flow, with only the remote read stubbed."""
from __future__ import annotations

from copy import deepcopy

import pytest

from project_board.client import cli
from project_board.client.return_reconciliation import ReturnReconciliation
from project_board.client.store import SharedFieldStore
from project_board.contract.worker_identity import WorkerSessionIdentity
from relay_helpers import two_channel_host

PROJECT = "work:project:project-one"


@pytest.mark.parametrize("coordinator_also_returning", [False, True])
def test_cli_census_request_coordinator_reply_and_explicit_confirmation(
    tmp_path, monkeypatch, coordinator_also_returning,
):
    host, requester, coordinator = two_channel_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.initialize(field_id="return-cli")
    field.create_project(project_id="project-one", title="Return flow", goal="Current authority", owner="operator")
    for channel in (requester, coordinator):
        field.register_worker(
            worker_name=channel.worker_name, worker_identity=channel.worker_identity,
            runtime_kind=channel.runtime_kind, runtime_session_id=channel.runtime_session_id,
            capabilities=[], authority_label="connection-hub:fixture-card",
            control_plane_state="published", attended_project_refs=[PROJECT],
        )
        field.listen_worker(channel.worker_name)
    current = {"state": "held", "holder": {"worker_name": coordinator.worker_name, "attending": True},
               "revision": 7, "since": "2026-11-08T08:00:00Z"}
    field.sync_project_coordinator("project-one", current)

    # The fixture admits two synthetic native identities into the real host
    # configuration. It uses no ambient agent session or credential store.
    monkeypatch.setattr(cli, "_identity", lambda args: WorkerSessionIdentity.create(args.runtime_kind, args.runtime_session_id))
    reads = []
    def governed_read(args):
        assert args.action == "project.coordinator.get"
        assert args.object_ref == PROJECT
        assert args.route == "relay"
        reads.append(args.runtime_session_id)
        return {"operation": args.action, "object": deepcopy(current)}
    monkeypatch.setattr(cli, "_coordinate_command", governed_read)

    parser = cli.build_parser()
    def run(channel, *arguments):
        args = parser.parse_args([
            "worker", *arguments, "--config", str(host.path),
            "--runtime-kind", channel.runtime_kind, "--runtime-session-id", channel.runtime_session_id,
        ])
        return cli._worker_command(args)

    def old_request(channel, key):
        field.send_mail("project-one", sender="control-plane", recipient=channel.worker_name,
                        kind="request", subject="Old task", body="Ask current authority before resuming.",
                        idempotency_key=key)
        ReturnReconciliation(field, channel.worker_name).record_cause("quota_hold", {"source": "fixture", "hold": key})

    old_request(requester, "requester-old-task")
    if coordinator_also_returning:
        old_request(coordinator, "coordinator-old-task")
    received = run(requester, "receive")
    [signal] = [s for s in received["signals"] if s["kind"] == "worker.return.reconciliation_required"]
    old = received["items"][0]["message"]
    census = run(requester, "inbox", "--limit", "1")
    assert census["headers"][0]["message_ref"] == old["message_ref"]
    preview = run(requester, "inbox", "--message-ref", old["message_ref"])
    assert preview["inspection_only"] is True

    request = run(requester, "send", "--project-ref", PROJECT,
                  "--recipient", coordinator.worker_name, "--kind", "request",
                  "--correlation-id", signal["correlation_id"], "--subject", "Return census",
                  "--body", "One old request, original date preserved; current assignment checked. Which work remains?",
                  "--idempotency-key", "return-census-one")
    coordinator_input = run(coordinator, "receive")
    assert request["message_ref"] in {i["message"]["message_ref"] for i in coordinator_input["items"]}

    # Regression: the draft applied the requester's request_context to this
    # coordinator reply, refusing it with field_return_episode_mismatch.
    reply = run(coordinator, "send", "--project-ref", PROJECT,
                "--recipient", requester.worker_name, "--kind", "reply",
                "--correlation-id", signal["correlation_id"], "--reply-to", request["message_ref"],
                "--subject", "Current resume list", "--body", "The current resume list is empty. The old request is superseded; record its obsolete disposition.",
                "--idempotency-key", "return-decision-one")
    selected = run(requester, "receive", "--project-ref", PROJECT,
                   "--correlation-id", signal["correlation_id"], "--sender", coordinator.worker_name)
    [decision] = [i["message"] for i in selected["items"] if i["message"]["message_ref"] == reply["message_ref"]]
    assert decision["reply_to"] == request["message_ref"]
    run(requester, "settle", "--project-ref", PROJECT, "--message-ref", decision["message_ref"],
        "--lease-id", decision["lease"]["lease_id"], "--outcome", "acknowledged")
    assert ReturnReconciliation(field, requester.worker_name).signals()
    run(requester, "settle", "--project-ref", PROJECT, "--message-ref", old["message_ref"],
        "--lease-id", old["lease"]["lease_id"], "--outcome", "obsolete",
        "--summary", "Current coordinator decision records an empty resume list; old request superseded.")
    confirmed = run(requester, "return-confirm", "--project-ref", PROJECT,
                    "--episode-id", signal["episode_id"], "--decision-message-ref", reply["message_ref"])
    assert confirmed["state"] == "confirmed"
    assert ReturnReconciliation(field, requester.worker_name).signals() == []
    assert reads == [requester.runtime_session_id, requester.runtime_session_id]
    replay = run(requester, "return-confirm", "--project-ref", PROJECT,
                 "--episode-id", signal["episode_id"], "--decision-message-ref", reply["message_ref"])
    assert replay["replayed"] is True
    assert replay["confirmed_at"] == confirmed["confirmed_at"]
    if coordinator_also_returning:
        assert ReturnReconciliation(field, coordinator.worker_name).signals()
