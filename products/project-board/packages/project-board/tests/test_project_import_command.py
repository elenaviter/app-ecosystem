"""pb worker project-import: begin, parts, seal, start (the import operations are the board's)."""

from __future__ import annotations

import argparse
import hashlib

import pytest

from project_board.client import cli


def _args(tmp_path, data, **extra):
    archive = tmp_path / "export.tar.gz"
    archive.write_bytes(data)
    return argparse.Namespace(project_ref="work:project:target", file=str(archive), client_key="", wait=False,
                              **extra)


def test_the_command_uploads_every_part_seals_and_starts(tmp_path, monkeypatch):
    data = b"x" * 20
    calls, posted = [], []

    def request(args, *, action, object_ref, payload):
        calls.append((action, dict(payload)))
        if action == "project.import.upload.begin":
            return {"object": {"upload_id": "u1", "part_bytes": 8, "part_count": 3, "sealed": False,
                               "part_url": "https://board.example/part?upload_id=u1&part_token=t"}}
        if action == "project.import.upload.seal":
            return {"object": {"upload_id": "u1", "sealed": True}}
        return {"object": {"import_id": "i1", "state": "admitted"}}

    async def upload(part_url, payload, size, count):
        posted.append((part_url, payload, size, count))

    monkeypatch.setattr(cli, "_reference_mapping_request", request)
    monkeypatch.setattr(cli, "_upload_import_parts", upload)
    result = cli._worker_project_import(_args(tmp_path, data))
    digest = hashlib.sha256(data).hexdigest()
    assert [action for action, _ in calls] == ["project.import.upload.begin", "project.import.upload.seal",
                                               "project.import"]
    assert calls[0][1] == {"total_bytes": 20, "archive_sha256": digest, "client_key": f"pb-import-{digest[:24]}"}
    assert posted == [("https://board.example/part?upload_id=u1&part_token=t", data, 8, 3)]
    assert result["import_id"] == "i1" and result["state"] == "admitted"


def test_a_sealed_upload_is_not_sent_again_and_a_misfit_upload_is_refused(tmp_path, monkeypatch):
    def sealed(args, *, action, object_ref, payload):
        if action == "project.import.upload.begin":
            return {"object": {"upload_id": "u1", "sealed": True}}
        return {"object": {"import_id": "i1", "state": "committed"}}

    async def never(*args):
        raise AssertionError("no part is sent for a sealed upload")

    monkeypatch.setattr(cli, "_reference_mapping_request", sealed)
    monkeypatch.setattr(cli, "_upload_import_parts", never)
    assert cli._worker_project_import(_args(tmp_path, b"abc"))["state"] == "committed"

    def misfit(args, *, action, object_ref, payload):
        return {"object": {"upload_id": "u1", "part_bytes": 8, "part_count": 9, "sealed": False,
                           "part_url": "https://board.example/part"}}

    monkeypatch.setattr(cli, "_reference_mapping_request", misfit)
    with pytest.raises(cli.DomainError) as refused:
        cli._worker_project_import(_args(tmp_path, b"abc"))
    assert refused.value.code == "work_import_upload_response_invalid"


def test_a_busy_start_is_retried_with_the_same_key(tmp_path, monkeypatch):
    starts = []

    def request(args, *, action, object_ref, payload):
        if action == "project.import.upload.begin":
            return {"object": {"upload_id": "u1", "sealed": True}}
        starts.append(payload["client_key"])
        if len(starts) < 3:
            raise cli.DomainError("work_import_busy", "Another import is running.", status=503,
                                  details={"retry_after_seconds": 0})
        return {"object": {"import_id": "i1", "state": "admitted"}}

    monkeypatch.setattr(cli, "_reference_mapping_request", request)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    assert cli._worker_project_import(_args(tmp_path, b"abc"))["import_id"] == "i1"
    assert len(starts) == 3 and len(set(starts)) == 1


def test_wait_follows_this_import_not_the_project(tmp_path, monkeypatch):
    seen = []
    states = iter(["admitted", "files_written", "failed"])

    def request(args, *, action, object_ref, payload):
        if action == "project.import.upload.begin":
            return {"object": {"upload_id": "u1", "sealed": True}}
        if action == "project.import":
            return {"object": {"import_id": "i1", "state": "admitted"}}
        seen.append(payload)
        # The project is no longer importable (filled by hand) while this import failed: it is not "finished".
        return {"object": {"can_import": False, "under_import": False, "import": {"import_id": "i1",
                "state": next(states), "error": "work_import_plan_changed"}}}

    monkeypatch.setattr(cli, "_reference_mapping_request", request)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    args = _args(tmp_path, b"abc")
    args.wait = True
    result = cli._worker_project_import(args)
    assert (result["state"], result["error"]) == ("failed", "work_import_plan_changed")
    assert seen and all(p == {"import_id": "i1"} for p in seen)
