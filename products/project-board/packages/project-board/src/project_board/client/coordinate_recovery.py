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

The key is reserved for one exact request, under the worker's lock, before
that request can reach the relay: two calls racing with one key and different
requests cannot both be sent.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping

from ..contract.errors import DomainError
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

    def reserve(
        self,
        worker_name: str,
        key: str,
        *,
        action: str,
        object_ref: str,
        payload: Mapping[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """Hold the key for this exact request before it can reach the relay.

        Returns the record and whether this call created it. A record for a
        different request under the same key is refused here, before anything
        is sent.
        """

        path = self._path(worker_name, key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        request_hash = coordinate_request_hash(action, object_ref, payload)
        with exclusive_lock(self._lock(worker_name)):
            self._prune_unlocked(worker_name)
            prior = self.read(worker_name, key)
            if prior is not None:
                if prior.get("request_hash") != request_hash:
                    raise idempotency_key_reused(key, prior)
                return prior, False
            record = {
                "schema": COORDINATE_RECOVERY_SCHEMA,
                "idempotency_key": key,
                "action": str(action),
                "object_ref": str(object_ref),
                "payload": dict(payload),
                "request_hash": request_hash,
                "request_ids": [],
                "state": "reserved",
                "first_sent_at": utc_now(),
                "updated_at": utc_now(),
            }
            atomic_write_json(path, record)
            return record, True

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
        """Add the queue request id this exact request went out under."""

        request_hash = coordinate_request_hash(action, object_ref, payload)
        with exclusive_lock(self._lock(worker_name)):
            record = self.read(worker_name, key)
            if record is None:
                raise DomainError(
                    "work_coordinate_recovery_missing",
                    f"The idempotency key {key} has no reservation for the request just queued.",
                    status=409,
                    details={"idempotency_key": key, "request_id": request_id},
                )
            if record.get("request_hash") != request_hash:
                raise idempotency_key_reused(key, record)
            request_ids = [str(value) for value in record.get("request_ids") or []]
            if request_id not in request_ids:
                request_ids.append(request_id)
            record["request_ids"] = request_ids
            record["state"] = "submitted"
            record["updated_at"] = utc_now()
            atomic_write_json(self._path(worker_name, key), record)
            return record

    def release_unsent(self, worker_name: str, key: str) -> None:
        """Drop a reservation no request ever went out under."""

        with exclusive_lock(self._lock(worker_name)):
            record = self.read(worker_name, key)
            if record is not None and not record.get("request_ids"):
                self._path(worker_name, key).unlink(missing_ok=True)

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


def idempotency_key_reused(key: str, prior: Mapping[str, Any]) -> DomainError:
    """The local refusal of a different request under a key already used."""

    return DomainError(
        "work_coordinate_idempotency_key_reused",
        (
            f"The idempotency key {key} was already used for a different request "
            f"({prior.get('action')} on {prior.get('object_ref')}, "
            f"state {prior.get('state')}). Run that request unchanged to recover "
            "its receipt, or use a new key for a different change. Nothing was sent."
        ),
        status=409,
        details={
            "original_request": {
                "action": prior.get("action"),
                "object_ref": prior.get("object_ref"),
                "payload": prior.get("payload"),
                "request_hash": prior.get("request_hash"),
            },
            "recovery": recovery_identity(prior, source="ledger"),
        },
    )


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
    "idempotency_key_reused",
    "mutation_idempotency_key",
    "recovery_identity",
]
