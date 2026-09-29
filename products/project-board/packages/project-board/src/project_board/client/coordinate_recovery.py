"""Remember each governed mutation by its idempotency key (W404).

A mutation that ``pb coordinate`` sends carries an ``idempotency_key``. When
its first wait ends without a result, the relay may still apply it and write
the response later. The retry therefore has to find that response, or resend
the exact same request, and never send a different request under the same
key: the service refuses that as an idempotency conflict, although the first
request applied.

This ledger keeps, per worker and key, the exact request (operation, object
and payload, with their hash), every queue request id it went out under, and
the receipt once one arrived. A retry reads it first:

- a receipt already recorded is returned from here, with no new request;
- a late relay response for an earlier request id is taken and returned;
- an outcome still unknown is resent unchanged under the same key;
- a different request under a key already used is refused locally, naming
  the original request, before anything is sent.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping

from .io import atomic_write_json, component, exclusive_lock, read_json, utc_now

COORDINATE_RECOVERY_SCHEMA = "problem-board.coordinate-recovery.v1"
# A record older than this is past any retry a worker makes of the same call.
RECOVERY_RETENTION_SECONDS = 7 * 24 * 3600


def coordinate_request_hash(
    action: str, object_ref: str, payload: Mapping[str, Any]
) -> str:
    """The hash of the exact serialized request a key stands for."""

    encoded = json.dumps(
        {"action": str(action), "object_ref": str(object_ref), "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def mutation_idempotency_key(payload: Mapping[str, Any]) -> str:
    value = payload.get("idempotency_key")
    return value.strip() if isinstance(value, str) else ""


class CoordinateRecovery:
    """Per-worker records of mutations sent by idempotency key."""

    def __init__(self, field_root: str | Path) -> None:
        self.root = Path(field_root) / "coordinate-recovery"

    def _path(self, worker_name: str, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:40]
        return self.root / component(worker_name, field="worker_name") / f"{digest}.json"

    def _lock(self, worker_name: str) -> Path:
        return self.root / component(worker_name, field="worker_name") / ".lock"

    def read(self, worker_name: str, key: str) -> dict[str, Any] | None:
        record = read_json(self._path(worker_name, key), required=False)
        if not record or record.get("schema") != COORDINATE_RECOVERY_SCHEMA:
            return None
        if str(record.get("idempotency_key") or "") != key:
            return None
        return dict(record)

    def record_submission(
        self,
        worker_name: str,
        key: str,
        *,
        action: str,
        object_ref: str,
        payload: Mapping[str, Any],
        request_id: str,
    ) -> dict[str, Any]:
        path = self._path(worker_name, key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with exclusive_lock(self._lock(worker_name)):
            self._prune_unlocked(worker_name)
            prior = self.read(worker_name, key) or {}
            request_ids = [str(value) for value in prior.get("request_ids") or []]
            if request_id not in request_ids:
                request_ids.append(request_id)
            record = {
                "schema": COORDINATE_RECOVERY_SCHEMA,
                "idempotency_key": key,
                "action": str(action),
                "object_ref": str(object_ref),
                "payload": dict(payload),
                "request_hash": coordinate_request_hash(action, object_ref, payload),
                "request_ids": request_ids,
                "state": "submitted",
                "first_sent_at": str(prior.get("first_sent_at") or utc_now()),
                "updated_at": utc_now(),
            }
            atomic_write_json(path, record)
            return record

    def record_state(
        self,
        worker_name: str,
        key: str,
        state: str,
        *,
        receipt: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        with exclusive_lock(self._lock(worker_name)):
            record = self.read(worker_name, key)
            if record is None:
                return None
            record["state"] = state
            record["updated_at"] = utc_now()
            if receipt is not None:
                record["receipt"] = dict(receipt)
            atomic_write_json(self._path(worker_name, key), record)
            return record

    def forget(self, worker_name: str, key: str) -> None:
        """Drop a record the service refused or never received."""

        with exclusive_lock(self._lock(worker_name)):
            self._path(worker_name, key).unlink(missing_ok=True)

    def _prune_unlocked(self, worker_name: str) -> None:
        folder = self.root / component(worker_name, field="worker_name")
        cutoff = time.time() - RECOVERY_RETENTION_SECONDS
        try:
            entries = list(folder.glob("*.json"))
        except OSError:
            return
        for entry in entries:
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink(missing_ok=True)
            except OSError:
                continue


def recovery_identity(record: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    """What a caller is shown about the original request it recovered."""

    return {
        "source": source,
        "idempotency_key": str(record.get("idempotency_key") or ""),
        "request_hash": str(record.get("request_hash") or ""),
        "request_ids": list(record.get("request_ids") or []),
        "first_sent_at": str(record.get("first_sent_at") or ""),
        "state": str(record.get("state") or ""),
    }


__all__ = [
    "COORDINATE_RECOVERY_SCHEMA",
    "CoordinateRecovery",
    "coordinate_request_hash",
    "mutation_idempotency_key",
    "recovery_identity",
]
