"""W485: the client keeps no signed download link it was handed for delivery.

A board that keeps attachment links out of its stored controls mints one link
per file when a relay pulls the control, and sends the stored original beside
it as ``canonical_payload``. The relay uses the link to fetch the file; the
local copy keeps the original as its proof and keeps no link. A control from
an older board, whose served payload is its own proof, is kept byte-exact.
Links here are synthetic.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from project_board.client.io import content_hash
from project_board.client.store import SharedFieldStore

WORKER = "codex-11111111-1111-4111-8111-111111111111"
FILE_REF = "pbfile:owner/conversation/turn/20260930-evidence.txt"
LINK = "https://board.example/download?ref=synthetic&for=worker"


@pytest.fixture
def field(tmp_path: Path) -> SharedFieldStore:
    store = SharedFieldStore(tmp_path / "shared-field")
    store.initialize(field_id="test-field")
    store.register_worker(worker_name=WORKER, runtime_kind="codex", capabilities=[], authority_label="synthetic")
    store.listen_worker(WORKER)
    store.create_project(project_id="project-one", title="One", goal="Files.", owner="operator")
    store.sync_worker_attendances(WORKER, ["work:project:project-one"])
    return store


def _deliver(field: SharedFieldStore, ref: str, *, served: dict, canonical: dict | None, kind: str = "request") -> dict:
    path = field._mail_root("project-one", WORKER) / "attachments" / "evidence.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"abc")
    envelope = {
        "ref": ref, "kind": kind, "project_ref": "work:project:project-one", "recipient": WORKER,
        "subject": "Files", "payload": served, "payload_hash": content_hash(served),
        "sender_identity": {"kind": "user", "label": "Operator"},
        "attachment_local_paths": {FILE_REF: str(path)},
    }
    if canonical is not None:
        envelope["canonical_payload"] = canonical
        envelope["canonical_payload_hash"] = content_hash(canonical)
    delivered = field.materialize_control(envelope)
    return json.loads((field._mail_root("project-one", WORKER) / "inbox" / (delivered["message_id"] + ".json")).read_text())


def _attachment(**extra) -> dict:
    return {"filename": "evidence.txt", "mime": "text/plain", "size": 3, "file_ref": FILE_REF,
            "sha256": hashlib.sha256(b"abc").hexdigest(), **extra}


@pytest.mark.parametrize("kind", ["request", "ping", "stop"])
def test_a_link_minted_for_delivery_is_not_kept_and_the_original_is_the_proof(field, kind):
    # Any control kind can carry an operator's files; the original is the proof for all of them.
    canonical = {"body": "See the file.", "attachments": [_attachment()], "attachment_custody": "delivery"}
    served = {**canonical, "attachments": [_attachment(download_url=LINK)]}
    saved = _deliver(field, f"work:control:custody-{kind}", served=served, canonical=canonical, kind=kind)
    assert saved["payload"]["retirement_command"] == canonical
    assert LINK not in json.dumps(saved), "no copy of the delivery link is kept"
    assert saved["payload"]["command"]["attachments"][0]["file_ref"] == FILE_REF


def test_a_legacy_control_whose_served_payload_is_its_proof_stays_exact(field):
    served = {"body": "See the file.", "attachments": [_attachment(download_url=LINK)]}
    saved = _deliver(field, "work:control:legacy", served=served, canonical=None)
    assert saved["payload"]["command"] == served, "an older board's proof is not rewritten"
    assert "retirement_command" not in saved["payload"]
