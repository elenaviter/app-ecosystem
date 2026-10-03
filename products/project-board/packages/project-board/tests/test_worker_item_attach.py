"""W479: pb worker item-attach asks for its upload slot in the shape the catalog declares.

On 2026-10-03 every `pb worker item-attach` was refused locally with
work_coordinate_shape_invalid: it asked for `attachment.request_upload` on
`work:worker:self`, while the operation is declared on `work:project:<id>`
(the W404 shape check, 07c83bf9, refuses that before anything is sent). The
relay's own mail upload calls the action directly, without the shape check,
which is why `pb worker send --attach` kept working.

These tests run item-attach through the real `_coordinate_command` path,
shape check and coordinate queue included; only the far side of the queue is
played by a fake relay and service, and the HTTP upload is recorded.
"""

from __future__ import annotations

import argparse
import hashlib
from typing import Any, Mapping

import pytest

from project_board.client import cli
from project_board.client import relay as relay_module
from project_board.client.coordinate_queue import CoordinateQueue
from project_board.contract.errors import DomainError

from relay_helpers import make_host

PROJECT = "work:project:quickstart-one"
ITEM_REF = "work:plan:node:20261003T000000Z:w1:one"


class FakeService:
    """The control plane as item-attach sees it: one item, slots, item updates."""

    def __init__(self, *, refuse_upload: Mapping[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.refuse_upload = refuse_upload
        self.refs: list[str] = []

    def answer(self, request: Mapping[str, Any]) -> tuple[dict | None, dict | None]:
        action, object_ref, payload = request["action"], request["object_ref"], request["payload"]
        self.calls.append({"action": action, "object_ref": object_ref, "payload": dict(payload)})
        if action == "project.plan.item":
            return {"operation": action, "status": "ok", "object": {
                "item_ref": ITEM_REF, "revision": 3, "attachment_refs": list(self.refs)}}, None
        if action == "attachment.request_upload":
            if self.refuse_upload is not None:
                return None, dict(self.refuse_upload)
            return {"operation": action, "status": "ok", "object": {
                "upload_url": "https://runtime.example/upload/slot-1",
                "staged_ref": "staged:slot-1:" + payload["filename"],
                "max_bytes": 25 * 1024 * 1024}}, None
        if action == "plan.item.update":
            self.refs = list(payload["changes"]["attachment_refs"])
            return {"operation": action, "status": "ok", "state": "applied", "object": {
                "item_ref": ITEM_REF, "revision": 4, "attachment_refs": list(self.refs),
                "attachment_count": len(self.refs)}}, None
        raise AssertionError(f"unexpected action {action}")


@pytest.fixture
def board(monkeypatch, tmp_path):
    host, identity, channel = make_host(tmp_path)
    service = FakeService()
    uploads: list[dict[str, Any]] = []
    original_await = cli._await_coordinate_response

    def relay_then_wait(queue, path, *, worker_name, request_id, timeout_seconds):
        # Play the relay for exactly the request this call queued.
        for request in queue.claim(worker_name=worker_name):
            result, error = service.answer(request)
            queue.complete(request, result=result, error=error)
        return original_await(queue, path, worker_name=worker_name,
                              request_id=request_id, timeout_seconds=timeout_seconds)

    async def record_upload(url, data, mime):
        uploads.append({"url": url, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "mime": mime})

    monkeypatch.setattr(cli, "channel_reconnect_state", lambda *a, **k: None)  # a connected channel
    monkeypatch.setattr(cli, "_raise_if_channel_not_usable", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_await_coordinate_response", relay_then_wait)
    monkeypatch.setattr(relay_module, "_http_upload", record_upload)

    def args(file, *, project_ref=PROJECT):
        return argparse.Namespace(
            project_ref=project_ref, item_key="W1", file=str(file),
            runtime_kind=identity.runtime_kind, runtime_session_id=identity.runtime_session_id,
            config=str(host.path),
        )

    return {"service": service, "uploads": uploads, "args": args, "tmp": tmp_path, "monkeypatch": monkeypatch}


def test_item_attach_asks_for_the_slot_on_its_project_and_attaches_the_file(board):
    note = board["tmp"] / "note.txt"
    note.write_bytes(b"one small synthetic note\n")

    result = cli._worker_item_attach(board["args"](note))

    (slot,) = [c for c in board["service"].calls if c["action"] == "attachment.request_upload"]
    assert slot["object_ref"] == PROJECT, "the declared shape, not work:worker:self"
    assert slot["payload"] == {"filename": "note.txt"}
    (upload,) = board["uploads"]
    assert upload["bytes"] == len(note.read_bytes()) and upload["url"].endswith("/slot-1")
    (update,) = [c for c in board["service"].calls if c["action"] == "plan.item.update"]
    assert update["object_ref"] == PROJECT
    assert update["payload"]["changes"]["attachment_refs"] == ["staged:slot-1:note.txt"]
    assert result["file_ref"] == "staged:slot-1:note.txt"
    assert result["content_hash"] == hashlib.sha256(note.read_bytes()).hexdigest()


def test_a_genuine_permission_refusal_reaches_the_person_unchanged_and_nothing_is_attached(board):
    board["service"].refuse_upload = {
        "code": "work_worker_unavailable", "message": "The caller is not an active worker.", "status": 403}
    note = board["tmp"] / "note.txt"
    note.write_bytes(b"refused\n")

    with pytest.raises(DomainError) as refused:
        cli._worker_item_attach(board["args"](note))

    assert refused.value.code == "work_worker_unavailable"
    assert board["uploads"] == []
    assert not [c for c in board["service"].calls if c["action"] == "plan.item.update"]


def test_type_and_size_refusals_are_unchanged_and_take_no_slot(board):
    big = board["tmp"] / "big.txt"
    big.write_bytes(b"x" * (10 * 1024 * 1024 + 1))  # text over the platform's 10 MiB

    with pytest.raises(DomainError) as refused:
        cli._worker_item_attach(board["args"](big))

    assert refused.value.code == "work_attachment_too_large"
    assert refused.value.details["kind"] == "text"
    assert not [c for c in board["service"].calls if c["action"] == "attachment.request_upload"]
    assert board["uploads"] == []


def test_a_call_in_the_wrong_shape_is_refused_before_anything_is_queued(board, monkeypatch):
    queued: list[Any] = []
    original_submit = CoordinateQueue.submit

    def counting_submit(self, **values):
        queued.append(values["action"])
        return original_submit(self, **values)

    monkeypatch.setattr(CoordinateQueue, "submit", counting_submit)

    with pytest.raises(DomainError) as refused:
        cli._reference_mapping_request(
            board["args"](board["tmp"] / "unused"), action="attachment.request_upload",
            object_ref="work:worker:self", payload={"filename": "note.txt"},
        )

    assert refused.value.code == "work_coordinate_shape_invalid"
    assert queued == [] and board["service"].calls == []


def test_the_reference_mapping_upload_uses_the_same_project_shape(board):
    # The other worker-side caller of attachment.request_upload.
    asked: list[dict[str, Any]] = []

    def stop_after_slot(args, *, action, object_ref, payload):
        asked.append({"action": action, "object_ref": object_ref})
        cli.require_coordinate_shape(action, object_ref, payload)
        raise RuntimeError("stop: the shape was accepted")

    board["monkeypatch"].setattr(cli, "_reference_mapping_request", stop_after_slot)
    with pytest.raises(RuntimeError, match="shape was accepted"):
        cli._upload_reference_mapping(board["args"](board["tmp"] / "unused"), project_ref=PROJECT,
                                      reference_mapping={})
    assert asked == [{"action": "attachment.request_upload", "object_ref": PROJECT}]
