"""Publish refused reconciliation receipts again once the Card holds the grant (W287 item 8).

A receipt whose publication the service refused is filed under ``refused``
and kept: its evidence never reached the service, so retention never removes
it. The usual cause is a Card whose operation list predates
``mail.reconciliation.publish``, and the fix is a re-approval the relay cannot
see. Nothing local says when the Card holds the operation again, so the
service is asked:

- **probe:** at most once per ``PROBE_INTERVAL_SECONDS`` per project and
  agent, the oldest refused receipt is queued again. Its refusal is quiet:
  the row settles as refused and this module logs one line, nothing reaches
  the board.
- **batch:** once the probe is published, which proves the grant, up to
  ``BATCH_SIZE`` refused receipts are queued again on every housekeeping pass
  until none is left.

A replayed batch gets a new outbox id (the content id with ``_r<n>``), because
the content id already names the refused row. Batches the service accepted
keep their ids. The receipt moves back to ``pending/``, and recovery and
settlement then treat it like any other receipt.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..contract.mailbox_reconciliation_contract import normalize_receipt
from ..contract.mailbox_reconciliation_publication import publication_batches
from .io import atomic_write_json, content_hash, exclusive_lock, read_json, utc_now
from .local_store import PartitionedStore
from .outbox_store import OutboxStore
from .reconciliation_publication import (
    OUTBOX_KIND,
    PUBLICATION_MISSING,
    PUBLICATION_PUBLISHED,
    PUBLICATION_REFUSED,
    publication_outbox_id,
    publication_state,
)
from .reconciliation_receipts import (
    PENDING,
    STORE,
    _hour_partitions,
    _publication_of,
    pending_path,
    settle_if_terminal,
    store_root,
)

logger = logging.getLogger(__name__)

# One refused call per agent per hour while the grant is missing: a refusal
# costs the service a request and the relay a settled row, and a re-approval
# is a person's action, so an hour of delay after it is acceptable
# (coordinator, 2026-09-26: "at most one refused call per agent per hour").
PROBE_INTERVAL_SECONDS = 3600
# Receipts queued again per agent and pass once the grant is proven. A pass
# runs every housekeeping interval, so this bounds the outbox growth per pass.
BATCH_SIZE = 100
MARKER = "replay.json"
_REPLAY_SUFFIX = re.compile(r"_r(\d+)$")
MARKER_SCHEMA = "problem-board.reconciliation-replay.v1"


def replay_refused_receipts(
    field: Any,
    project_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """One housekeeping pass: probe or batch-replay each agent's refused receipts."""

    current = now or datetime.now(timezone.utc)
    root = store_root(field, project_id)
    totals = {"probes": 0, "replayed": 0, "waiting": 0}
    if not root.is_dir():
        return totals
    for agent_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        result = _replay_agent(field, project_id, agent_dir, current)
        for key in totals:
            totals[key] += result.get(key, 0)
    return totals


def _replay_agent(field: Any, project_id: str, agent_dir: Path, now: datetime) -> dict[str, int]:
    agent = agent_dir.name
    marker_path = agent_dir / MARKER
    marker = dict(read_json(marker_path, required=False) or {})
    probe_id = str(marker.get("probe_receipt_id") or "")
    proven = bool(marker.get("proven_at"))
    if probe_id and not proven:
        outcome = _probe_outcome(field, project_id, agent_dir, probe_id)
        if outcome == PUBLICATION_PUBLISHED:
            marker["proven_at"] = _stamp(now)
            proven = True
            logger.info(
                "relay store replay worker=%s store=%s op=probe key=%s outcome=published",
                agent, STORE, probe_id,
            )
        elif outcome == "pending":
            return {"waiting": 1}
        elif outcome == PUBLICATION_REFUSED:
            # Quiet by design: one log line, no board notice per hour.
            logger.info(
                "relay store replay worker=%s store=%s op=probe key=%s outcome=refused",
                agent, STORE, probe_id,
            )
            marker["probe_receipt_id"] = ""
    if not proven and _seconds_since(marker.get("probe_at"), now) < PROBE_INTERVAL_SECONDS:
        _write_marker(marker_path, marker)
        return {}

    budget = BATCH_SIZE if proven else 1
    started = time.monotonic()
    history = PartitionedStore(store_root(field, project_id), store=STORE)
    replayed: list[str] = []
    with history.reading("replay") as read:
        for hour_dir, hour_start in _hour_partitions(agent_dir):
            refused = sorted(
                path for path in hour_dir.glob("*.json")
                if _publication_of(path.name) == PUBLICATION_REFUSED
            )
            if not refused:
                continue
            read.opened(agent, hour_start.strftime("%Y-%m-%dT%H"), len(refused))
            for path in refused[: budget - len(replayed)]:
                receipt_id = _requeue(field, project_id, agent, path)
                if receipt_id:
                    replayed.append(receipt_id)
            if len(replayed) >= budget:
                break

    if not replayed:
        # Nothing refused is left: the next refusal starts from a probe again.
        marker_path.unlink(missing_ok=True)
        return {}
    if proven:
        marker.update(probe_receipt_id="", replayed=int(marker.get("replayed") or 0) + len(replayed))
    else:
        marker.update(probe_receipt_id=replayed[0], probe_at=_stamp(now))
    _write_marker(marker_path, marker)
    logger.info(
        "relay store replay worker=%s store=%s op=%s replayed=%d ms=%d",
        agent, STORE, "batch" if proven else "probe", len(replayed),
        int((time.monotonic() - started) * 1000),
    )
    return {"replayed": len(replayed)} if proven else {"probes": 1}


def _probe_outcome(field: Any, project_id: str, agent_dir: Path, receipt_id: str) -> str:
    """``pending``, ``published``, ``refused``, or ``""`` when the probe is gone."""

    pending = agent_dir / PENDING / f"{receipt_id}.json"
    if pending.is_file():
        # Settle it now if its row is terminal, so the outcome is known this pass.
        record = settle_if_terminal(field, project_id, worker_name=agent_dir.name, path=pending)
        state = str((record.get("publication") or {}).get("state") or "")
        return state if state in {PUBLICATION_PUBLISHED, PUBLICATION_REFUSED} else "pending"
    for hour_dir, _start in reversed(list(_hour_partitions(agent_dir))):
        for path in hour_dir.glob(f"*_{receipt_id}.json"):
            return _publication_of(path.name)
    return ""


def _requeue(field: Any, project_id: str, agent: str, path: Path) -> str:
    """Queue a refused receipt again under new ids for its refused batches."""

    record = read_json(path, required=False)
    if not record:
        return ""
    receipt = normalize_receipt(record.get("receipt") or {})
    # Missing: the refused rows outlived outbox retention. Still refused evidence.
    if publication_state(field, record) not in {PUBLICATION_REFUSED, PUBLICATION_MISSING}:
        return ""
    publication = dict(record.get("publication") or {})
    previous = [str(value) for value in publication.get("outbox_ids") or []]
    # The highest replay either recorded or named by an id: an id is never reused.
    seen = [int(match.group(1)) for value in previous if (match := _REPLAY_SUFFIX.search(value))]
    replays = max([int(publication.get("replays") or 0), *seen]) + 1
    outbox = OutboxStore(field.control)
    outbox_ids: list[str] = []
    with exclusive_lock(outbox.lock):
        for index, batch in enumerate(publication_batches(receipt)):
            batch_hash = content_hash(batch)
            earlier = previous[index] if index < len(previous) else publication_outbox_id(batch_hash)
            row = outbox.read(earlier, worker_name=agent, project_ref=receipt["project_ref"])
            if row and str(row.get("state") or "") in {"sent", "ignored"}:
                outbox_ids.append(earlier)
                continue
            outbox_id = f"{publication_outbox_id(batch_hash)}_r{replays}"
            outbox.write_pending(
                {
                    "schema": "problem-board.service-outbox.v1",
                    "outbox_id": outbox_id,
                    "kind": OUTBOX_KIND,
                    "worker_name": agent,
                    "project_ref": receipt["project_ref"],
                    "content_hash": batch_hash,
                    "payload": batch,
                    "state": "pending",
                    "created_at": utc_now(),
                    "retry_count": 0,
                    "next_attempt_at": "",
                    "replay_of": earlier,
                }
            )
            outbox_ids.append(outbox_id)
    target = pending_path(field, project_id, agent, receipt["receipt_id"])
    with exclusive_lock(field._project_lock(project_id)):
        record["publication"] = {
            "kind": OUTBOX_KIND,
            "outbox_ids": outbox_ids,
            "batch_count": len(outbox_ids),
            "queued_at": str(publication.get("queued_at") or utc_now()),
            "replays": replays,
            "replayed_at": utc_now(),
        }
        record.pop("settled_at", None)
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(target, record)
        path.unlink(missing_ok=True)
    return str(receipt["receipt_id"])


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seconds_since(stamp: Any, now: datetime) -> float:
    text = str(stamp or "")
    if not text:
        return float("inf")
    try:
        then = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    return (now - then).total_seconds()


def _write_marker(path: Path, marker: dict[str, Any]) -> None:
    if not marker:
        return
    marker["schema"] = MARKER_SCHEMA
    marker["updated_at"] = utc_now()
    atomic_write_json(path, marker)


__all__ = ["BATCH_SIZE", "PROBE_INTERVAL_SECONDS", "replay_refused_receipts"]
