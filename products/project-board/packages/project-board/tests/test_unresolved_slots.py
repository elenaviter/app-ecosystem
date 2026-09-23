"""A send never carries a template slot to a reader (W262, 2026-09-23).

A consultation result reached the operator and three workers with twenty
``{{CUI_P2}}``-style slots where votes and decisions belonged. The template
had been sent instead of the filled file. The check is a property of the
argument, like the single-line rule: it runs where the text enters ``pb``.
"""

from __future__ import annotations

import pytest

from project_board.client.prose_arguments import (
    refuse_unresolved_payload_slots,
    refuse_unresolved_slots,
)
from project_board.contract.errors import DomainError


def test_a_body_with_a_slot_is_refused_and_the_first_slot_is_named():
    body = "| P2 | ... | {{CUI_P2}} | yes | {{DEC_P2}} |\n| P4a | {{CUI_P4A}} |"
    with pytest.raises(DomainError) as refused:
        refuse_unresolved_slots(body, argument="--body-file")
    assert refused.value.code == "problem_board_unresolved_slot"
    assert refused.value.details["count"] == 3
    assert refused.value.details["slots"] == ["{{CUI_P2}}", "{{CUI_P4A}}", "{{DEC_P2}}"]
    assert "{{CUI_P2}}" in str(refused.value)
    assert "--body-file" in str(refused.value)


def test_ordinary_text_with_braces_passes():
    # JSON in a fenced block, a Jinja-looking but filled value, and a lone brace all pass.
    for text in (
        'payload: {"source_repositories": [{"repository_ref": "repo:a/b"}]}',
        "set {x} and {{ }} are not slots because a slot has a name",
        "a lone { brace",
        "",
    ):
        assert refuse_unresolved_slots(text, argument="--body") == text
    assert refuse_unresolved_slots(None, argument="--body") is None
    assert refuse_unresolved_slots(12, argument="--body") == 12


def test_payload_prose_fields_are_checked_and_other_fields_are_not():
    ok = {"work_ref": "work:plan:node:x", "text": "filled", "expected_revision": 3, "tags": ["{{not_prose}}"]}
    refuse_unresolved_payload_slots(ok, argument="--payload-file")
    with pytest.raises(DomainError) as refused:
        refuse_unresolved_payload_slots({"text": "decision: {{DEC_P8}}"}, argument="--payload-file")
    assert refused.value.details["argument"] == "--payload-file field 'text'"


def test_pb_worker_send_refuses_a_body_or_subject_with_a_slot_before_anything_leaves(tmp_path):
    from argparse import Namespace

    from project_board.client import cli
    from project_board.client.store import SharedFieldStore
    from relay_helpers import make_host

    host, identity, _channel = make_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.initialize(field_id="slots")
    field.register_worker(
        worker_name=identity.worker_name,
        worker_identity=identity.worker_identity,
        runtime_kind=identity.runtime_kind,
        runtime_session_id=identity.runtime_session_id,
        capabilities=[],
        authority_label="connection-hub:test-profile",
        control_plane_state="published",
    )
    template = tmp_path / "result.md"
    template.write_text("| P2 | {{CUI_P2}} | {{DEC_P2}} |\n", encoding="utf-8")
    values = {
        "command": "worker", "worker_command": "send",
        "runtime_kind": identity.runtime_kind, "runtime_session_id": identity.runtime_session_id,
        "config": str(host.path), "project_ref": None, "recipient": "operator", "route": "auto",
        "kind": "update", "subject": "Result", "body": None, "body_file": str(template),
        "payload_file": None, "work_ref": None, "correlation_id": None, "reply_to": None,
        "idempotency_key": "send-with-slots-1", "attach": None,
    }
    with pytest.raises(DomainError) as refused:
        cli._worker_command(Namespace(**values))
    assert refused.value.code == "problem_board_unresolved_slot"
    assert refused.value.details["argument"] == "--body-file"
    assert refused.value.details["slots"] == ["{{CUI_P2}}", "{{DEC_P2}}"]
    with pytest.raises(DomainError) as refused:
        cli._worker_command(Namespace(**{**values, "body_file": None, "body": "filled", "subject": "Re: {{TITLE}}"}))
    assert refused.value.details["argument"] == "--subject"


def test_a_named_slot_with_spaces_is_a_slot():
    with pytest.raises(DomainError):
        refuse_unresolved_slots("vote: {{ decision }}", argument="--body")
