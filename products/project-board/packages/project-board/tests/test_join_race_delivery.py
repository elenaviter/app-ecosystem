"""Mail sent right after a link reaches the new agent (W304, join race, 2026-09-25 14:06Z).

The board linked codex-coord and issued its welcome at 14:06:37; the relay
refused it at 14:06:41 as "not linked", from a host record that predated the
link. The next attendance poll came at 14:07:47. The first "not linked" for a
control now defers it and forces an attendance read from the board; the
refusal stands only when a read that completed after that deferral still says
"not linked". Only this relay's monotonic clock orders the two, so a stale
snapshot re-stamped on resync and skew between hosts cannot decide it
(codex-ui review of ae#139).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from project_board.client import relay as relay_module
from project_board.contract.errors import DomainError

WORKER = "claude-code-new"


def _welcome(created_at: str = "2026-09-25T14:06:37Z") -> dict:
    return {
        "ref": "work:control:command_welcome",
        "kind": "mail",
        "project_ref": "work:project:quickstart-works-mttfmgqu",
        "sender": "claude-code-coordinator",
        "recipient": WORKER,
        "payload": {"mail": {"kind": "request", "subject": "You joined", "body": "Welcome."}},
        "created_at": created_at,
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

    def materialize_control(self, item):
        if not self.linked:
            raise DomainError("field_worker_not_linked", "The addressed worker is not linked to this project.", status=403)
        self.delivered.append(item["ref"])
        return {"delivery_status": "pending", "message_ref": "work:mail:welcome"}

    def sync_worker_attendances(self, worker_name, project_refs):
        self.linked = "work:project:quickstart-works-mttfmgqu" in project_refs


class Client:
    def __init__(self, items):
        self.items = items
        self.calls = []

    async def action(self, *, object_ref, action, payload):
        self.calls.append(action)
        if action == "control.pull":
            return {"lease_id": "lease-1", "items": list(self.items)}
        return {"applied": True}


def _adapter(field, client, clock):
    adapter = relay_module.ProblemBoardHostRelayAdapter.__new__(relay_module.ProblemBoardHostRelayAdapter)
    adapter.config = SimpleNamespace(
        project_id="quickstart-works-mttfmgqu", relay_id="relay-1", worker_name=WORKER,
        reconcile_ceiling_seconds=30, allow_session_resume_view=False,
        allowed_control_kinds=("mail", "request", "reply"), max_control_bytes=65536,
        allowed_peer_workers=("*",),
    )
    adapter._attendance_cache = {"initialized": True, "items": []}
    adapter._monotonic = clock
    adapter.client = client
    adapter.field = field

    async def no_attachments(item):
        return None

    adapter._fetch_attachments = no_attachments
    return adapter


def _board_read(adapter, *, linked: bool, field: Field):
    """The relay's attendance read from the board, as poll_attendances_once records it.

    Only the relay's cache changes here: the host record (``field.linked``)
    is written later in the cycle, after the control pull, which is the
    ordering a retry has to survive.
    """

    adapter._record_attendance_observation({"attendances": [{"project_ref": "work:project:quickstart-works-mttfmgqu"}] if linked else []})


def test_the_welcome_waits_for_the_board_and_is_delivered_after_the_link_is_read():
    field, clock = Field(), Clock()
    client = Client([_welcome()])
    adapter = _adapter(field, client, clock)

    first = asyncio.run(adapter._pull_controls())
    assert first["controls_deferred"] == 1 and first.get("controls_refused", 0) == 0
    assert "control.refuse" not in client.calls
    assert adapter._attendance_cache["initialized"] is False  # the next cycle reads the board

    clock.now = 101.0
    _board_read(adapter, linked=True, field=field)
    clock.now = 102.0
    second = asyncio.run(adapter._pull_controls())
    assert second["controls_materialized"] == 1
    assert field.delivered == ["work:control:command_welcome"]
    assert adapter._attendance_cache["not_linked_deferrals"] == {}


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
