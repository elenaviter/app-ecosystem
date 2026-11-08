"""A census and decision cross independent host fields through routed controls."""
from copy import deepcopy

import pytest

from project_board.client import cli, host_config
from project_board.client.io import content_hash
from project_board.client.return_reconciliation import PREFIX, ReturnReconciliation
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError
from project_board.contract.worker_identity import WorkerSessionIdentity

PROJECT = "work:project:project-one"


def _host(path, host_id, session_id):
    host = host_config.initialize_host_config(
        target_id="target", endpoint="https://runtime.example/mcp",
        tenant="tenant", platform_project="project", host_id=host_id,
        allowed_roots=[str(path)], source_repositories={},
        config_path=path / "relay.json", state_root=path / "state",
    )
    identity = WorkerSessionIdentity.create("codex", session_id)
    channel = host_config.enroll_worker_channel(
        host.path, identity=identity, profile=f"problem-board-{host_id}", authorized=True,
    )
    host = host_config.HostRelayConfig.load(host.path)
    field = SharedFieldStore(host.field_root)
    field.initialize(field_id=host_id)
    field.create_project(project_id="project-one", title="Return", goal="Current work", owner="operator")
    field.register_worker(
        worker_name=channel.worker_name, worker_identity=channel.worker_identity,
        runtime_kind=channel.runtime_kind, runtime_session_id=channel.runtime_session_id,
        capabilities=[], authority_label="connection-hub:fixture-card",
        control_plane_state="published", attended_project_refs=[PROJECT],
    )
    field.listen_worker(channel.worker_name)
    return host, channel, field


def _route(source, destination, receipt):
    """Only server transport is a fixture; use the real outbox and materializer."""
    outbound = source._outbox.read(receipt["outbox_id"])
    assert outbound["kind"] == "mail.route"
    mail = deepcopy(outbound["payload"])
    payload = {"mail": mail}
    control = {
        "ref": f"work:control:command-{receipt['outbox_id']}", "kind": "mail",
        "project_ref": outbound["project_ref"], "sender": mail["sender"],
        "recipient": mail["recipient"], "subject": mail["subject"],
        "payload": payload, "payload_hash": content_hash(payload),
        "sender_identity": {"kind": "worker", "label": mail["sender"]},
    }
    delivered = destination.materialize_control(control)
    replay = destination.materialize_control(control)
    assert replay["replayed"] is True
    assert replay["message_ref"] == delivered["message_ref"]
    return delivered


def test_cli_return_round_trip_across_separate_host_fields(tmp_path, monkeypatch):
    requester_host, requester, requester_field = _host(
        tmp_path / "requester", "requester-host", "11111111-1111-4111-8111-111111111111",
    )
    coordinator_host, coordinator, coordinator_field = _host(
        tmp_path / "coordinator", "coordinator-host", "22222222-2222-4222-8222-222222222222",
    )
    assert requester_field.root != coordinator_field.root
    current = {"state": "held", "holder": {"worker_name": coordinator.worker_name, "attending": True},
               "revision": 7, "since": "2026-11-08T08:00:00Z"}
    for field in (requester_field, coordinator_field):
        field.sync_project_mail_recipients("project-one", [
            {"worker_name": requester.worker_name}, {"worker_name": coordinator.worker_name},
        ])
        field.sync_project_coordinator("project-one", current)

    monkeypatch.setattr(cli, "_identity", lambda args: WorkerSessionIdentity.create(args.runtime_kind, args.runtime_session_id))
    def governed_read(args):
        assert (args.action, args.object_ref, args.route) == ("project.coordinator.get", PROJECT, "relay")
        return {"operation": args.action, "object": deepcopy(current)}
    monkeypatch.setattr(cli, "_coordinate_command", governed_read)
    parser = cli.build_parser()
    def arguments(host, channel, *values):
        return parser.parse_args([
            "worker", *values, "--config", str(host.path),
            "--runtime-kind", channel.runtime_kind, "--runtime-session-id", channel.runtime_session_id,
        ])
    def run(host, channel, *values):
        return cli._worker_command(arguments(host, channel, *values))

    requester_field.send_mail(
        "project-one", sender="control-plane", recipient=requester.worker_name,
        kind="request", subject="Old task", body="Ask current authority before resuming.",
        idempotency_key="old-task",
    )
    state = ReturnReconciliation(requester_field, requester.worker_name)
    state.record_cause("quota_hold", {"source": "fixture", "hold": "first"})
    received = run(requester_host, requester, "receive")
    [signal] = [s for s in received["signals"] if s["kind"] == "worker.return.reconciliation_required"]
    old = received["items"][0]["message"]
    request = run(requester_host, requester, "send", "--project-ref", PROJECT,
                  "--recipient", coordinator.worker_name, "--kind", "request",
                  "--correlation-id", signal["correlation_id"], "--subject", "Return census",
                  "--body", "One dated old task; current assignment checked. What remains?",
                  "--idempotency-key", "census")
    assert request["delivery_route"] == "remote"
    local_request = _route(requester_field, coordinator_field, request)
    assert local_request["message_ref"] != request["message_ref"], "recipient C differs from sender S"
    coordinator_input = run(coordinator_host, coordinator, "receive")
    [census] = [i["message"] for i in coordinator_input["items"]]
    assert census["message_ref"] == local_request["message_ref"]

    # Reply exactly as the remote coordinator sees the leased request. The
    # correlation is recovered from C; the worker need not copy private S.
    reply_args = arguments(coordinator_host, coordinator, "send", "--project-ref", PROJECT,
                           "--recipient", requester.worker_name, "--kind", "reply",
                           "--reply-to", census["message_ref"], "--subject", "Current resume list",
                           "--body", "The resume list is empty. The old task is superseded; record it obsolete.",
                           "--idempotency-key", "decision")
    reply = cli._worker_command(reply_args)
    assert cli._worker_command(reply_args)["replayed"] is True
    local_reply = _route(coordinator_field, requester_field, reply)
    decision_input = run(requester_host, requester, "receive", "--project-ref", PROJECT,
                         "--correlation-id", signal["correlation_id"], "--sender", coordinator.worker_name)
    [decision] = [i["message"] for i in decision_input["items"]]
    assert decision["message_ref"] == local_reply["message_ref"]
    for message, outcome in ((decision, "acknowledged"), (old, "obsolete")):
        run(requester_host, requester, "settle", "--project-ref", PROJECT,
            "--message-ref", message["message_ref"], "--lease-id", message["lease"]["lease_id"],
            "--outcome", outcome)

    # d65992a2 fails here with field_return_decision_mismatch: its decision
    # names C while the checkpoint requires S. Do not weaken that fence.
    confirmed = run(requester_host, requester, "return-confirm", "--project-ref", PROJECT,
                    "--episode-id", signal["episode_id"], "--decision-message-ref", decision["message_ref"])
    assert confirmed["state"] == "confirmed"
    assert decision["reply_to"] == request["message_ref"]
    assert state.signals() == []


@pytest.mark.parametrize("mismatch", ["sender", "correlation", "unadmitted_payload"])
def test_reply_source_alias_requires_matching_admitted_request(tmp_path, monkeypatch, mismatch):
    host, coordinator, field = _host(
        tmp_path, "coordinator-host", "22222222-2222-4222-8222-222222222222",
    )
    requester = "codex-11111111-1111-4111-8111-111111111111"
    field.sync_project_mail_recipients("project-one", [
        {"worker_name": requester}, {"worker_name": "codex-33333333-3333-4333-8333-333333333333"},
    ])
    correlation = PREFIX + "return-one"
    source_ref = "work:mail:source-one"
    if mismatch == "unadmitted_payload":
        delivered = field.send_mail(
            "project-one", sender=requester, recipient=coordinator.worker_name,
            kind="request", subject="Untrusted census", body="A source ref is not admission.",
            payload={"source_message_ref": source_ref}, correlation_id=correlation,
            idempotency_key="untrusted-request",
        )
    else:
        payload = {"mail": {"kind": "request", "subject": "Census", "body": "Waiting.",
                            "source_message_ref": source_ref, "correlation_id": correlation, "payload": {}}}
        delivered = field.materialize_control({
            "ref": "work:control:command-one", "kind": "mail", "subject": "Census",
            "project_ref": PROJECT, "sender": requester, "recipient": coordinator.worker_name,
            "payload": payload, "payload_hash": content_hash(payload),
        })
    monkeypatch.setattr(cli, "_identity", lambda args: WorkerSessionIdentity.create(args.runtime_kind, args.runtime_session_id))
    args = cli.build_parser().parse_args([
        "worker", "send", "--project-ref", PROJECT,
        "--recipient", "codex-33333333-3333-4333-8333-333333333333" if mismatch == "sender" else requester,
        "--kind", "reply", "--correlation-id", correlation + "-wrong" if mismatch == "correlation" else correlation,
        "--reply-to", delivered["message_ref"], "--subject", "Decision", "--body", "Empty resume list.",
        "--idempotency-key", "decision", "--config", str(host.path),
        "--runtime-kind", coordinator.runtime_kind, "--runtime-session-id", coordinator.runtime_session_id,
    ])
    with pytest.raises(DomainError) as error:
        cli._worker_command(args)
    assert error.value.code == ("field_return_reply_binding_missing" if mismatch == "unadmitted_payload"
                                else "field_return_reply_binding_mismatch")


def test_legacy_routed_delivery_replays_without_asserting_new_admission(tmp_path, monkeypatch):
    _config, coordinator, field = _host(
        tmp_path, "coordinator-host", "22222222-2222-4222-8222-222222222222",
    )
    requester = "codex-11111111-1111-4111-8111-111111111111"
    correlation = PREFIX + "legacy-return"
    payload = {"mail": {"kind": "request", "subject": "Census", "body": "Waiting.",
                        "source_message_ref": "work:mail:source-one", "correlation_id": correlation,
                        "payload": {}}}
    control = {"ref": "work:control:command-legacy", "kind": "mail", "subject": "Census",
               "project_ref": PROJECT, "sender": requester, "recipient": coordinator.worker_name,
               "payload": payload, "payload_hash": content_hash(payload)}
    send = field.send_mail
    def legacy_send(*args, **kwargs):
        kwargs.pop("admitted_mail_route", None)
        return send(*args, **kwargs)
    monkeypatch.setattr(field, "send_mail", legacy_send)
    legacy = field.materialize_control(control)
    monkeypatch.setattr(field, "send_mail", send)
    replay = field.materialize_control(control)
    assert replay["replayed"] is True
    assert replay["message_ref"] == legacy["message_ref"]
    # Old receipts still replay; they cannot retroactively attest a new ref
    # binding. A fresh census is required for this protocol's checkpoint.
    with pytest.raises(DomainError) as error:
        field.reconciliation_reply_to(
            "project-one", worker_name=coordinator.worker_name, message_ref=legacy["message_ref"],
            recipient=requester, correlation_id=correlation,
        )
    assert error.value.code == "field_return_reply_binding_missing"
