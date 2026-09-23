"""One intended-scope line rides the working report (W278 part B, P4b).

The worker says, before its first edit, which module, path prefixes or
runtime surface it will change. It is one optional line on pb worker report,
kept on the assignment by the service. Nothing else changes about the report.
"""

from __future__ import annotations

import argparse

from project_board.client import outbox_outcomes
from project_board.client.cli import build_parser


class _Field:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def enqueue_assignment_report(self, project_id, **kwargs):
        self.calls.append({"project_id": project_id, **kwargs})
        return {"outbox_id": "outbox-1"}


def test_the_report_parser_takes_one_optional_scope_line():
    args = build_parser().parse_args([
        "worker", "report", "--project-ref", "work:project:p", "--assignment-ref", "work:assignment:a",
        "--ownership-version", "1", "--state", "working", "--summary", "started",
        "--source-event-ref", "work:mail:e", "--scope", "client/relay.py, services/control.py heartbeat",
    ])
    assert args.scope == "client/relay.py, services/control.py heartbeat"
    bare = build_parser().parse_args([
        "worker", "report", "--project-ref", "work:project:p", "--assignment-ref", "work:assignment:a",
        "--ownership-version", "1", "--state", "working", "--summary", "started", "--source-event-ref", "work:mail:e",
    ])
    assert bare.scope == ""


def test_the_submitter_passes_the_scope_to_the_field_and_omits_nothing_else(monkeypatch):
    field = _Field()
    monkeypatch.setattr(outbox_outcomes, "await_outbox_outcome", lambda *a, **k: {"state": "sent", "remote_disposition": "applied", "applied": True, "response": {}})
    monkeypatch.setattr(outbox_outcomes, "assignment_report_outcome", lambda *a, **k: {"ok": True})
    outbox_outcomes.submit_assignment_report(
        field, "project-one",
        worker_name="w", assignment_ref="work:assignment:a", ownership_version=1, state="working",
        summary="started", result_ref="", source_event_ref="work:mail:e",
        review_look_at=None, review_could_not_verify=None, wait_seconds=1.0,
        scope="client/relay.py",
    )
    assert field.calls[0]["scope"] == "client/relay.py"
    assert field.calls[0]["state"] == "working"
    # The default is the empty line, so an older field or service sees nothing new.
    outbox_outcomes.submit_assignment_report(
        field, "project-one",
        worker_name="w", assignment_ref="work:assignment:a", ownership_version=1, state="working",
        summary="started", result_ref="", source_event_ref="work:mail:e",
        review_look_at=None, review_could_not_verify=None, wait_seconds=1.0,
    )
    assert field.calls[1]["scope"] == ""
