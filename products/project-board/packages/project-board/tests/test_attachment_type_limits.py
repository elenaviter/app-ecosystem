# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W475: the host refuses at send what the platform would refuse at upload,
and a permanent upload refusal ends the delivery instead of being retried.

The platform's upload preflight (KDCube safe_preflight) accepts text up to
10 MiB and SVG up to 2 MiB. The host checked only its general 25 MiB ceiling,
queued such a file, and turned the platform's 4xx refusal into a 502 that the
outbox retried without end.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from project_board.client import relay
from project_board.client.outbox_store import OutboxStore
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError
from project_board.contract.mail_attachments import (
    MAX_MAIL_ATTACHMENT_BYTES,
    SVG_MAX_BYTES,
    TEXT_MAX_BYTES,
    attachment_kind,
    validate_mail_attachment,
)

MIB = 1024 * 1024
SVG_HEAD = b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1">'


def _text(size: int) -> bytes:
    return (b"line of text\n" * (size // 13 + 1))[:size]


def _svg(size: int) -> bytes:
    body = SVG_HEAD + b"<!--"
    return (body + b"x" * size)[: size - 9] + b"--></svg>"


def _binary(size: int) -> bytes:
    return (b"\x00\x01\x02\xff" * (size // 4 + 1))[:size]


# Kind: the type the service hands to the platform check ------------------------


@pytest.mark.parametrize(
    ("data", "mime", "kind"),
    [
        (b"plain words", "text/plain", "text"),
        (b"{}", "application/json", "text"),
        (b"a,b", "text/csv; charset=utf-8", "text"),
        (b"<svg/>", "image/svg+xml", "svg"),
        (b"\x89PNG\r\n", "image/png", "other"),
        (b"plain words", "", "text"),
        (b"plain words", "application/octet-stream", "text"),
        # A script type with text content is routed as text by the service.
        (b"#!/bin/sh\necho hi\n", "application/x-sh", "text"),
        # Without its declared type an SVG is plain text to the service.
        (SVG_HEAD, "", "text"),
        (b"\x00\x01binary", "", "other"),
        (b"text with a \x01 control byte", "", "other"),
        (b"%PDF-1.7 plain", "", "other"),
        # A declared image type is kept, whatever the bytes are.
        (b"plain words", "image/png", "other"),
    ],
)
def test_the_kind_follows_the_service_routing(data, mime, kind):
    assert attachment_kind(data, mime=mime) == kind


def test_a_name_alone_never_makes_binary_bytes_text():
    data = _binary(TEXT_MAX_BYTES + 1)
    assert attachment_kind(data, mime="") == "other"
    validate_mail_attachment(data, filename="notes.txt", mime="")  # the 25 MiB ceiling only


def test_multibyte_text_is_text():
    assert attachment_kind(("é" * 5000).encode("utf-8"), mime="") == "text"


# Ceilings ---------------------------------------------------------------------


@pytest.mark.parametrize(("mime", "filename"), [("text/plain", "log.txt"), ("", "log.txt"), ("", "")])
def test_text_is_accepted_to_ten_mib_and_refused_above(mime, filename):
    if not mime and not filename:
        # Without a name or a type the host keeps its earlier, general check.
        validate_mail_attachment(_text(TEXT_MAX_BYTES + 1))
        return
    validate_mail_attachment(_text(TEXT_MAX_BYTES), filename=filename, mime=mime)
    with pytest.raises(DomainError) as refused:
        validate_mail_attachment(_text(TEXT_MAX_BYTES + 1), filename=filename, mime=mime)
    assert refused.value.code == "field_attachment_too_large"
    assert refused.value.details == {"maximum_bytes": TEXT_MAX_BYTES, "kind": "text", "size": TEXT_MAX_BYTES + 1}


def test_svg_is_accepted_to_two_mib_and_refused_above():
    validate_mail_attachment(_svg(SVG_MAX_BYTES), filename="d.svg", mime="image/svg+xml")
    with pytest.raises(DomainError) as refused:
        validate_mail_attachment(_svg(SVG_MAX_BYTES + 1), filename="d.svg", mime="image/svg+xml")
    assert refused.value.details["kind"] == "svg"
    assert refused.value.details["maximum_bytes"] == SVG_MAX_BYTES


def test_an_svg_without_its_type_meets_the_text_ceiling_as_on_the_service():
    validate_mail_attachment(_svg(SVG_MAX_BYTES + 1), filename="d", mime="")


def test_a_raster_image_keeps_the_general_ceiling():
    validate_mail_attachment(_binary(3 * MIB), filename="shot.png", mime="image/png")
    with pytest.raises(DomainError) as refused:
        validate_mail_attachment(_binary(MAX_MAIL_ATTACHMENT_BYTES + 1), filename="shot.png", mime="image/png")
    assert refused.value.details == {"maximum_bytes": MAX_MAIL_ATTACHMENT_BYTES}


def test_the_executable_rule_still_applies_with_a_kind():
    with pytest.raises(DomainError) as refused:
        validate_mail_attachment(b"\x7fELF" + b"\x00" * 64, filename="tool", mime="")
    assert refused.value.code == "field_attachment_executable_binary_refused"


# At send: the file is refused before anything is queued -------------------------

WORKER = "codex-api"
PROJECT = "project-one"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    store.register_worker(worker_name=WORKER, runtime_kind="codex", capabilities=[], authority_label="authority:codex-api")
    store.create_project(project_id=PROJECT, title="Limits", goal="Refuse early.", owner="operator")
    return store


def test_sending_an_oversized_text_file_is_refused_before_it_is_queued(field, tmp_path):
    big = tmp_path / "trace.log"
    big.write_bytes(_text(TEXT_MAX_BYTES + 1))

    with pytest.raises(DomainError) as refused:
        field.enqueue_remote_mail(
            PROJECT, sender=WORKER, recipient="operator", kind="update",
            subject="A trace", body="Attached.", idempotency_key="trace-1",
            attachments=[{"path": str(big)}],
        )

    assert refused.value.code == "field_attachment_too_large"
    assert refused.value.details["kind"] == "text"
    assert field.pull_outbox(relay_id="relay-01", kinds={"mail.route"}) == []


def test_a_text_file_within_the_ceiling_is_queued(field, tmp_path):
    small = tmp_path / "notes.md"
    small.write_bytes(_text(MIB))

    field.enqueue_remote_mail(
        PROJECT, sender=WORKER, recipient="operator", kind="update",
        subject="Notes", body="Attached.", idempotency_key="notes-1",
        attachments=[{"path": str(small)}],
    )

    [claimed] = field.pull_outbox(relay_id="relay-01", kinds={"mail.route"})
    [queued] = claimed["payload"]["attachment_files"]
    assert queued["mime"] == "text/markdown"


# At upload: permanent refusals are not retried ----------------------------------


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.content = self
        self._body = body

    async def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


class _Session:
    def __init__(self, response: _Response) -> None:
        self.response = response

    def post(self, url: str, data: bytes, headers: dict[str, str]) -> _Response:
        return self.response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


def _answer(monkeypatch, status: int, body: bytes = b"") -> None:
    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: _Session(_Response(status, body)))


def _service_answer(code: str, reasons: list[str], message: str = "The file failed the platform's security checks.") -> bytes:
    """The Problem Board upload endpoint's JSON refusal shape."""
    import json

    return json.dumps(
        {"ok": False, "error": {"code": code, "message": message, "details": {"reasons": reasons}}}
    ).encode("utf-8")


# A body may echo anything; none of these may reach a notice or a log.
LEAKS = (
    "https://board.example/inbox_file_upload?object_ref=x&upload_token=TOKEN-CANARY",
    "Authorization: Bearer BEARER-CANARY",
    "<html><body>proxy error</body></html>",
)


@pytest.mark.parametrize(
    ("status", "code"),
    [(413, "work_attachment_too_large"), (415, ""), (422, "work_attachment_rejected"),
     (400, "work_attachment_empty"), (400, "work_attachment_too_large")],
)
def test_a_permanent_upload_refusal_is_refused_with_allowlisted_detail_only(monkeypatch, status, code):
    body = _service_answer(code, ["Text too large: 11534337>10485760", *LEAKS], message=" ".join(LEAKS))
    _answer(monkeypatch, status, body)

    with pytest.raises(DomainError) as refused:
        asyncio.run(relay._http_upload("https://board.example/slot?upload_token=TOKEN-CANARY", b"x", "text/plain"))

    assert refused.value.code == "work_attachment_upload_refused"
    assert refused.value.status == status
    assert refused.value.details == {
        "upload_status": status,
        "code": code,
        "reasons": ["Text too large: 11534337>10485760"],
    }
    seen = str(refused.value) + repr(refused.value.to_dict())
    for canary in ("TOKEN-CANARY", "BEARER-CANARY", "board.example", "<html", "Authorization"):
        assert canary not in seen


@pytest.mark.parametrize("body", [b"", b"not json", b"<html>413 Request Entity Too Large</html>",
                                  b'{"error": {"code": "../../etc", "details": {"reasons": "nope"}}}'])
def test_an_unreadable_or_foreign_body_falls_back_to_a_fixed_sentence(monkeypatch, body):
    _answer(monkeypatch, 413, body)

    with pytest.raises(DomainError) as refused:
        asyncio.run(relay._http_upload("https://board.example/slot", b"x", "text/plain"))

    assert str(refused.value) == "The file is larger than the platform accepts."
    assert refused.value.details == {"upload_status": 413, "code": "", "reasons": []}


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (408, b""), (425, b""), (429, b""), (500, b""), (502, b""), (503, b""), (504, b""),
        # A rejected or expired upload token, a missing slot or a malformed
        # request: the next attempt asks for a new slot, so it keeps retrying.
        (401, b""), (403, b'{"ok": false, "error": {"code": "work_attachment_token_rejected"}}'),
        (404, b""), (400, b'{"ok": false, "error": {"code": "work_attachment_request_invalid"}}'),
    ],
)
def test_other_upload_answers_keep_the_earlier_retry(monkeypatch, status, body):
    _answer(monkeypatch, status, body)

    with pytest.raises(DomainError) as failed:
        asyncio.run(relay._http_upload("https://board.example/slot", b"x", "text/plain"))

    assert failed.value.code == "work_attachment_upload_failed"
    assert failed.value.status == 502


def test_a_successful_upload_returns(monkeypatch):
    _answer(monkeypatch, 204)
    asyncio.run(relay._http_upload("https://upload.example/slot", b"x", "text/plain"))


def test_the_outbox_refuses_a_permanently_refused_upload_once_and_never_retries(tmp_path, monkeypatch):
    from test_connected_degraded_admission import _fixture

    host, _identity, channel, supervisor, session, client = _fixture(tmp_path)
    uploads: list[str] = []

    async def action(**arguments):
        client.calls.append(arguments)
        if arguments.get("action") == "attachment.request_upload":
            return {"object": {"upload_url": "https://upload.example/slot", "staged_ref": "staged:1"}}
        return {"object": {"ref": "work:message:delivered"}}

    # The real uploader, against a platform that refuses the file: the
    # classification under test is _http_upload's own.
    import aiohttp

    class _CountingSession(_Session):
        def post(self, url: str, data: bytes, headers: dict[str, str]) -> _Response:
            uploads.append(headers.get("Content-Type", ""))
            return super().post(url, data, headers)

    monkeypatch.setattr(
        aiohttp, "ClientSession",
        lambda: _CountingSession(_Response(422, _service_answer("work_attachment_rejected", ["Unsupported or unknown type: application/x-unknown"]))),
    )

    client.action = action
    field = SharedFieldStore(host.field_root)
    notices: list[dict[str, Any]] = []

    def report_mail_delivery_failure(project_id: str, **kwargs: Any) -> dict[str, Any]:
        # The notice itself is covered by the delivery-failure tests; here it
        # is enough that the sender is told exactly once.
        notices.append({"project_id": project_id, **kwargs})
        return {"reported": True}

    field.report_mail_delivery_failure = report_mail_delivery_failure
    session.adapter = relay.ProblemBoardHostRelayAdapter(
        config=relay.RelayConfig.from_host_channel(host, channel, project_id="attendance"),
        field=field,
        client=client,
        trace=supervisor._trace,
        outbox_drain_lock=supervisor._outbox_drain_lock(channel.worker_name),
    )
    snapshot = tmp_path / "queued" / "blob.bin"
    snapshot.parent.mkdir()
    snapshot.write_bytes(b"\x01\x02\x03 not text")
    import hashlib

    outbox = OutboxStore(host.field_root / ".problem-board")
    from project_board.client.io import utc_now

    outbox.write_pending({
        "outbox_id": "outbox_refused_upload",
        "kind": "mail.route",
        "worker_name": channel.worker_name,
        "project_ref": "work:project:one",
        "object_ref": "work:project:one",
        "payload": {
            "recipient": "operator", "kind": "update", "subject": "A file", "body": "Attached.",
            "source_message_ref": "work:mail:20261003T000000Z:mail_refused_upload:a-file",
            "attachment_files": [{
                "filename": "blob.bin", "path": str(snapshot), "mime": "application/octet-stream",
                "size": snapshot.stat().st_size,
                "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
            }],
        },
        "state": "pending",
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "next_attempt_at": "",
    })

    async def drain() -> None:
        if supervisor.serve_outbox_once():
            await asyncio.gather(*supervisor._outbox_draining.values())

    asyncio.run(drain())
    asyncio.run(drain())

    assert uploads == ["application/octet-stream"], "the refused upload was tried exactly once"
    assert not any(call.get("action") == "mail.route" for call in client.calls)
    row = outbox.read("outbox_refused_upload")
    assert row["state"] == "refused"
    assert len(notices) == 1 and notices[0]["error_code"] == "work_attachment_upload_refused"
    assert row["remote_result"]["error"]["code"] == "work_attachment_upload_refused"
    assert row["remote_result"]["error"]["details"] == {
        "upload_status": 422,
        "code": "work_attachment_rejected",
        "reasons": ["Unsupported or unknown type: application/x-unknown"],
    }
