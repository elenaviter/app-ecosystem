"""Returning workers can enumerate and inspect requests without holding leases."""
from types import SimpleNamespace

import pytest

from project_board.client.cli import _worker_inbox
from project_board.client.store import SharedFieldStore
from project_board.contract.errors import DomainError


@pytest.fixture
def inbox(tmp_path):
    field = SharedFieldStore(tmp_path / "field")
    field.initialize(field_id="test")
    field.register_worker(worker_name="worker-one", runtime_kind="codex", capabilities=[],
                          authority_label="test", attended_project_refs=["work:project:project-one"])
    field.create_project(project_id="project-one", title="Test", goal="Test", owner="operator")
    identity = SimpleNamespace(worker_name="worker-one", runtime_session_id="native-one")
    return field, identity


def args(**extra):
    return SimpleNamespace(**{**dict(limit=2, kind=[], sender="", since="", cursor="", message_ref=""), **extra})


def send(field, key):
    return field.send_mail("project-one", sender="control-plane", recipient="worker-one", kind="request",
                           subject="Task " + key, body="Read the current item before acting.",
                           idempotency_key=key)


def test_all_tied_headers_are_reachable_once_without_a_lease(inbox):
    field, identity = inbox
    expected = {send(field, str(i))["message_ref"] for i in range(7)}
    refs, cursor = [], ""
    while True:
        page = _worker_inbox(field, identity, args(cursor=cursor))
        assert page["matched"] == 7
        refs.extend(h["message_ref"] for h in page["headers"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(refs) == len(set(refs)) == 7
    assert set(refs) == expected
    assert field.list_worker_mail_leases("worker-one", lease_owner="native-one")["items"] == []


def test_exact_preview_preserves_request_time_and_never_claims(inbox):
    field, identity = inbox
    message = send(field, "one")
    before = field.pending_mail_headers("worker-one")
    preview = _worker_inbox(field, identity, args(message_ref=message["message_ref"]))
    assert preview["inspection_only"] is True
    assert preview["message"]["body"] == "Read the current item before acting."
    assert preview["message"]["created_at"] == before[0]["created_at"]
    assert preview["message"]["task_deadline"].startswith("unknown")
    assert field.pending_mail_headers("worker-one") == before
    assert field.list_worker_mail_leases("worker-one", lease_owner="native-one")["items"] == []


def test_cursor_refuses_a_different_filter_or_worker(inbox):
    field, identity = inbox
    for i in range(3):
        send(field, str(i))
    cursor = _worker_inbox(field, identity, args())["next_cursor"]
    with pytest.raises(DomainError):
        _worker_inbox(field, identity, args(cursor=cursor, kind=["request"]))
    with pytest.raises(DomainError):
        _worker_inbox(field, SimpleNamespace(worker_name="worker-one", runtime_session_id="native-two"), args(cursor=cursor))
