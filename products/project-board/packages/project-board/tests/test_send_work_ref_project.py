"""A send with --work-ref and no --project-ref says which flag is missing (W304 finding 49).

On 2026-09-24 claude-ops sent the operator a plan to review with
``--work-ref`` and no ``--project-ref``. The plan-item check ran against an
empty project id and failed as ``field_component_invalid``: "project_id must
contain only letters, digits, dot, underscore, or hyphen", which names neither
flag. A work ref belongs to one project's plan, so the send refuses before
anything else and names ``--project-ref``.
"""

from __future__ import annotations

from argparse import Namespace

import pytest

from project_board.contract.errors import DomainError


def test_a_work_ref_without_a_project_ref_is_refused_naming_the_flag(tmp_path):
    from project_board.client import cli
    from project_board.client.store import SharedFieldStore
    from relay_helpers import make_host

    host, identity, _channel = make_host(tmp_path)
    field = SharedFieldStore(host.field_root)
    field.initialize(field_id="work-ref-project")
    field.register_worker(
        worker_name=identity.worker_name,
        worker_identity=identity.worker_identity,
        runtime_kind=identity.runtime_kind,
        runtime_session_id=identity.runtime_session_id,
        capabilities=[],
        authority_label="connection-hub:test-profile",
        control_plane_state="published",
    )
    values = {
        "command": "worker", "worker_command": "send",
        "runtime_kind": identity.runtime_kind, "runtime_session_id": identity.runtime_session_id,
        "config": str(host.path), "project_ref": None, "recipient": "operator", "route": "auto",
        "kind": "question", "subject": "Plan", "body": "Please review.", "body_file": None,
        "payload_file": None,
        "work_ref": "work:plan:node:20260924T195220Z:w313:hand-the-coordinator-role-to-another-agent",
        "correlation_id": None, "reply_to": None,
        "idempotency_key": "send-work-ref-no-project-1", "attach": None,
    }
    with pytest.raises(DomainError) as refused:
        cli._worker_command(Namespace(**values))
    assert refused.value.code == "field_mail_work_ref_requires_project"
    assert "--project-ref" in str(refused.value)
    assert refused.value.details["argument"] == "--project-ref"
