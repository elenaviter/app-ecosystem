"""Mail sent right after a link reaches the new agent (W304, join race, 2026-09-25 14:06Z).

The board linked codex-coord and issued its welcome at 14:06:37; the relay
refused it at 14:06:41 as "not linked", from a host record that predated the
link. The next attendance poll came at 14:07:47. The first "not linked" for a
control now triggers an immediate board attendance read in the same relay
cycle and retries the already-leased control. The refusal stands only when an
authoritative read after the deferral still says "not linked". Only this
relay's monotonic clock orders later-cycle retries, so a stale snapshot
re-stamped on resync and skew between hosts cannot decide it.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from project_board.client import relay as relay_module
from project_board.contract.errors import DomainError

WORKER = "claude-code-new"


def _welcome(created_at: str = "2026-09-25T14:06:37Z") -> dict:
    return {
        "ref": "work:control:command_welcome",
        "kind": "mail",
        "project_ref": "work:project:demo-project-0a1b2c3d",
        "sender": "claude-code-coordinator",
        "recipient": WORKER,
        "payload": {"mail": {"kind": "request", "subject": "You joined", "body": "Welcome."}},
        "created_at": created_at,
    }


def _second_welcome() -> dict:
    return {
        **_welcome(),
        "ref": "work:control:command_welcome_second",
    }


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Field:
    """A host whose record of the worker says "not linked" until the board is read."""

    def __init__(self):
        self.linked = False
        self.delivered = []
        self.attempted = []

    def materialize_control(self, item):
        self.attempted.append(item["ref"])
        if not self.linked:
            raise DomainError("field_worker_not_linked", "The addressed worker is not linked to this project.", status=403)
        self.delivered.append(item["ref"])
        return {"delivery_status": "pending", "message_ref": "work:mail:welcome"}

    def sync_worker_attendances(self, worker_name, project_refs):
        self.linked = "work:project:demo-project-0a1b2c3d" in project_refs


class Client:
    def __init__(self, items, *, attendances=None, heartbeat_error=None):
        self.items = items
        self.attendances = attendances
        self.heartbeat_error = heartbeat_error
        self.calls = []

    async def action(self, *, object_ref, action, payload):
        self.calls.append(action)
        if action == "control.pull":
            return {"lease_id": "lease-1", "items": list(self.items)}
        if action == "worker.heartbeat" and self.heartbeat_error is not None:
            raise self.heartbeat_error
        if action == "worker.heartbeat" and self.attendances is not None:
            return {"attendances": list(self.attendances)}
        return {"applied": True}


def _adapter(field, client, clock):
    adapter = relay_module.ProblemBoardHostRelayAdapter.__new__(relay_module.ProblemBoardHostRelayAdapter)
    adapter.config = SimpleNamespace(
        project_id="demo-project-0a1b2c3d", relay_id="relay-1", worker_name=WORKER,
        reconcile_ceiling_seconds=30, allow_session_resume_view=False,
        allowed_control_kinds=("mail", "request", "reply"), max_control_bytes=65536,
        allowed_peer_workers=("*",),
    )
    adapter._attendance_cache = {"initialized": True, "items": []}
    adapter._heartbeat_sent_at = {}
    adapter._published_interval = 30
    adapter._monotonic = clock
    adapter.client = client
    adapter.field = field

    async def no_runtime_account(payload):
        return None

    async def no_attachments(item):
        return None

    adapter._add_runtime_account = no_runtime_account
    adapter._fetch_attachments = no_attachments
    return adapter


def _board_read(adapter, *, linked: bool, field: Field):
    """The relay's attendance read from the board, as poll_attendances_once records it.

    Only the relay's cache changes here: the host record (``field.linked``)
    is written later in the cycle, after the control pull, which is the
    ordering a retry has to survive.
    """

    adapter._record_attendance_observation({"attendances": [{"project_ref": "work:project:demo-project-0a1b2c3d"}] if linked else []})


def test_link_then_welcome_is_delivered_within_one_relay_cycle():
    field, clock = Field(), Clock()
    client = Client(
        [_welcome()],
        attendances=[
            {"project_ref": "work:project:demo-project-0a1b2c3d"}
        ],
    )
    adapter = _adapter(field, client, clock)

    result = asyncio.run(adapter._pull_controls())

    assert result["controls_materialized"] == 1
    assert result["controls_deferred"] == 0
    assert result["controls_refused"] == 0
    assert "control.refuse" not in client.calls
    assert client.calls == [
        "control.pull",
        "worker.heartbeat",
        "control.acknowledge",
    ]
    assert field.delivered == ["work:control:command_welcome"]
    assert adapter._attendance_cache["not_linked_deferrals"] == {}


def test_immediate_board_read_that_confirms_absence_refuses_in_one_cycle():
    field, clock = Field(), Clock()
    client = Client([_welcome()], attendances=[])
    adapter = _adapter(field, client, clock)

    result = asyncio.run(adapter._pull_controls())

    assert result["controls_materialized"] == 0
    assert result["controls_deferred"] == 0
    assert result["controls_refused"] == 1
    assert client.calls == [
        "control.pull",
        "worker.heartbeat",
        "control.refuse",
    ]
    assert field.delivered == []
    assert adapter._attendance_cache["not_linked_deferrals"] == {}


@pytest.mark.parametrize(
    "heartbeat_error",
    [
        OSError("connection reset"),
        DomainError(
            "work_relay_unavailable",
            "The relay endpoint is unavailable.",
            status=503,
        ),
    ],
    ids=["transport", "domain-503"],
)
def test_failed_immediate_read_defers_not_linked_controls_and_continues_batch(
    heartbeat_error,
    caplog,
):
    caplog.set_level(logging.DEBUG, logger=relay_module.__name__)
    field, clock = Field(), Clock()
    items = [_welcome(), _second_welcome()]
    client = Client(items, heartbeat_error=heartbeat_error)
    adapter = _adapter(field, client, clock)

    result = asyncio.run(adapter._pull_controls())

    assert result["controls_materialized"] == 0
    assert result["controls_deferred"] == 2
    assert result["controls_refused"] == 0
    assert client.calls == ["control.pull", "worker.heartbeat"]
    assert field.attempted == [item["ref"] for item in items]
    deferred = [
        record.getMessage()
        for record in caplog.records
        if "control remains deferred after attendance read" in record.getMessage()
    ]
    assert len(deferred) == 2
    assert all(
        any(item["ref"] in message for message in deferred)
        for item in items
    )


def test_authoritative_absence_uses_one_attendance_read_for_the_batch():
    field, clock = Field(), Clock()
    client = Client([_welcome(), _second_welcome()], attendances=[])
    adapter = _adapter(field, client, clock)

    result = asyncio.run(adapter._pull_controls())

    assert result["controls_refused"] == 2
    assert client.calls.count("worker.heartbeat") == 1
    assert client.calls.count("control.refuse") == 2


def test_limbo_stops_the_pulled_batch():
    field, clock = Field(), Clock()
    items = [_welcome(), _second_welcome()]
    client = Client(items)
    adapter = _adapter(field, client, clock)

    async def enter_limbo():
        return "limbo"

    adapter._read_attendance_for_deferred_control = enter_limbo

    result = asyncio.run(adapter._pull_controls())

    assert result["controls_deferred"] == 1
    assert field.attempted == [items[0]["ref"]]
    assert client.calls == ["control.pull"]


def test_a_stale_absence_resynced_from_cache_does_not_refuse_it():
    # codex-ui's case: the unchanged pre-link snapshot is rematerialized and
    # re-stamped locally after the welcome. No board read happened since the
    # deferral, so the welcome keeps waiting instead of being refused.
    field, clock = Field(), Clock()
    client = Client([_welcome()])
    adapter = _adapter(field, client, clock)
    asyncio.run(adapter._pull_controls())
    clock.now = 150.0
    # A local resync (no board answer) touches nothing the decision reads.
    field.linked = False
    again = asyncio.run(adapter._pull_controls())
    assert again["controls_deferred"] == 1
    assert "control.refuse" not in client.calls


def test_an_unlink_the_board_confirms_after_the_deferral_refuses():
    field, clock = Field(), Clock()
    client = Client([_welcome()])
    adapter = _adapter(field, client, clock)
    asyncio.run(adapter._pull_controls())
    clock.now = 110.0
    _board_read(adapter, linked=False, field=field)
    clock.now = 111.0
    final = asyncio.run(adapter._pull_controls())
    assert final["controls_refused"] == 1
    assert "control.refuse" in client.calls


def test_the_message_clock_decides_nothing():
    # Opposing skew: a control stamped far in the future or the past is
    # handled the same way, because no host's wall clock is compared.
    for stamp in ("2099-01-01T00:00:00Z", "2001-01-01T00:00:00Z", ""):
        field, clock = Field(), Clock()
        client = Client([_welcome(created_at=stamp)])
        adapter = _adapter(field, client, clock)
        assert asyncio.run(adapter._pull_controls())["controls_deferred"] == 1
        clock.now = 101.0
        _board_read(adapter, linked=True, field=field)
        clock.now = 102.0
        assert asyncio.run(adapter._pull_controls())["controls_materialized"] == 1
