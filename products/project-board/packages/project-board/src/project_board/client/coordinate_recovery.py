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

Every request id keeps its own outcome (``attempts``): unknown until its
answer arrives, then applied, refused or not sent. The key is released only
when every attempt is proved to have had no effect, because a later attempt's
refusal says nothing about an earlier attempt that may have applied. An
applied receipt is final for the key: no later submission, uncertainty or
refusal downgrades or deletes it. Each decision is taken under the lock.

An attempt is registered as publishing before the queue can expose it, and
becomes its request id once queued. A key with a publishing attempt is never
released, so a concurrent retry that is already in the queue keeps the key
held even when every attempt registered so far was refused.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any, Mapping

from ..contract.errors import DomainError
from ..contract.operation_identity import transport_request_hash
from .io import atomic_write_json, component, exclusive_lock, read_json, utc_now

COORDINATE_RECOVERY_SCHEMA = "problem-board.coordinate-recovery.v1"
# A record older than this is past any retry a worker makes of the same call.
RECOVERY_RETENTION_SECONDS = 7 * 24 * 3600


def coordinate_request_hash(
    action: str, object_ref: str, payload: Mapping[str, Any]
) -> str:
    """The hash of the exact serialized request a key stands for.

    It is the shared transport identity the board's ledger records (W574), so
    a lost reply can be looked up by the same value.
    """

    return transport_request_hash(action, object_ref, payload)


def mutation_idempotency_key(payload: Mapping[str, Any]) -> str:
    value = payload.get("idempotency_key")
    return value.strip() if isinstance(value, str) else ""


STATE_APPLIED = "applied"
ATTEMPT_UNKNOWN = "unknown"
NO_EFFECT_OUTCOMES = frozenset({"refused", "not_sent"})
ATTEMPT_OUTCOMES = frozenset({STATE_APPLIED, ATTEMPT_UNKNOWN, *NO_EFFECT_OUTCOMES})


def _publishing(record: Mapping[str, Any]) -> list[str]:
    """Attempts registered and not yet queued, or not yet known to have failed."""

    return [str(value) for value in record.get("publishing") or []]


def _attempts(record: Mapping[str, Any]) -> dict[str, str]:
    """Each request id's outcome; an id with none recorded is unknown."""

    known = record.get("attempts")
    known = dict(known) if isinstance(known, Mapping) else {}
    return {
        str(request_id): str(known.get(str(request_id)) or ATTEMPT_UNKNOWN)
        for request_id in record.get("request_ids") or []
    }


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

    def lookup_existing(
        self, worker_name: str, key: str, *, action: str,
        object_ref: str, payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Read only an existing exact request, without reserving or pruning.

        Receipt recovery is local and remains available during a reconnect.
        It must not allocate a key for work the channel cannot yet admit.
        Expired records remain subject to the usual retention boundary.
        """
        path = self._path(worker_name, key)
        if not path.parent.exists():
            return None
        with exclusive_lock(self._lock(worker_name)):
            try:
                if path.stat().st_mtime < time.time() - RECOVERY_RETENTION_SECONDS:
                    return None
            except FileNotFoundError:
                return None
            prior = self.read(worker_name, key)
            if prior is None:
                return None
            request_hash = coordinate_request_hash(action, object_ref, payload)
            if prior.get("request_hash") != request_hash:
                raise idempotency_key_reused(key, prior)
            if (
                prior.get("action") != action or prior.get("object_ref") != object_ref
                or prior.get("payload") != dict(payload)
            ):
                raise idempotency_key_reused(key, prior)
            return prior

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
                "publishing": [],
                "state": "reserved",
                "first_sent_at": utc_now(),
                "updated_at": utc_now(),
            }
            atomic_write_json(path, record)
            return record, True

    def begin_attempt(
        self,
        worker_name: str,
        key: str,
        *,
        action: str,
        object_ref: str,
        payload: Mapping[str, Any],
        request_id: str,
    ) -> str:
        """Register one attempt as publishing, before the queue can expose it.

        The attempt is registered under the queue request id it will be
        published with, so an interruption at any point leaves a known
        identity. The key is reserved again for this exact request when an
        earlier release freed it, and a different request under a held key is
        refused here, before anything is sent. Returns the request id.
        """

        path = self._path(worker_name, key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        request_hash = coordinate_request_hash(action, object_ref, payload)
        token = str(request_id)
        with exclusive_lock(self._lock(worker_name)):
            record = self.read(worker_name, key)
            if record is None:
                record = {
                    "schema": COORDINATE_RECOVERY_SCHEMA,
                    "idempotency_key": key,
                    "action": str(action),
                    "object_ref": str(object_ref),
                    "payload": dict(payload),
                    "request_hash": request_hash,
                    "request_ids": [],
                    "publishing": [],
                    "state": "reserved",
                    "first_sent_at": utc_now(),
                }
            elif record.get("request_hash") != request_hash:
                raise idempotency_key_reused(key, record)
            record["publishing"] = [*_publishing(record), token]
            record["updated_at"] = utc_now()
            atomic_write_json(path, record)
            return token

    def abandon_attempt(self, worker_name: str, key: str, token: str) -> None:
        """Drop an attempt the queue definitely did not accept.

        The record goes with it only when nothing was ever queued under the
        key and no other attempt is publishing.
        """

        with exclusive_lock(self._lock(worker_name)):
            record = self.read(worker_name, key)
            if record is None:
                return
            record["publishing"] = [value for value in _publishing(record) if value != token]
            path = self._path(worker_name, key)
            if not record.get("request_ids") and not record["publishing"]:
                path.unlink(missing_ok=True)
                return
            record["updated_at"] = utc_now()
            atomic_write_json(path, record)

    def record_submission(
        self,
        worker_name: str,
        key: str,
        *,
        action: str,
        object_ref: str,
        payload: Mapping[str, Any],
        request_id: str,
        token: str = "",
    ) -> dict[str, Any]:
        """Add the queue request id this exact request went out under.

        ``token`` names the publishing attempt the request id replaces.
        """

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
            record["publishing"] = [value for value in _publishing(record) if value != token]
            attempts = _attempts(record)
            attempts.setdefault(request_id, ATTEMPT_UNKNOWN)
            record["attempts"] = attempts
            if record.get("state") != STATE_APPLIED:
                record["state"] = "submitted"
            record["updated_at"] = utc_now()
            atomic_write_json(self._path(worker_name, key), record)
            return record

    def settle_attempt(
        self,
        worker_name: str,
        key: str,
        request_id: str,
        outcome: str,
        *,
        receipt: Mapping[str, Any] | None = None,
        expected_request_hash: str = "",
    ) -> dict[str, Any] | None:
        """Record one attempt's outcome and decide the key, under the lock.

        ``outcome`` is ``applied`` (with its receipt), ``refused``,
        ``not_sent`` or ``unknown``. Returns the record, or None once every
        attempt is proved to have had no effect and the key is released.
        Late recovery supplies its original hash and request id; a released
        and reused key cannot acquire the old request's answer.
        """

        if outcome not in ATTEMPT_OUTCOMES:
            raise ValueError(f"unknown attempt outcome {outcome!r}")
        with exclusive_lock(self._lock(worker_name)):
            record = self.read(worker_name, key)
            if record is None:
                return None
            if expected_request_hash:
                if record.get("request_hash") != expected_request_hash:
                    raise idempotency_key_reused(key, record)
                if request_id not in record.get("request_ids", []):
                    raise DomainError(
                        "work_coordinate_recovery_missing",
                        "The recovered response is not an attempt of the current exact request.",
                        status=409,
                    )
            attempts = _attempts(record)
            if request_id:
                attempts[request_id] = outcome
            record["attempts"] = attempts
            record["updated_at"] = utc_now()
            path = self._path(worker_name, key)
            if record.get("state") == STATE_APPLIED:
                # A known receipt is final for the key.
                atomic_write_json(path, record)
                return record
            if outcome == STATE_APPLIED:
                record["state"] = STATE_APPLIED
                record["receipt"] = dict(receipt or {})
                atomic_write_json(path, record)
                return record
            if (
                attempts
                and not _publishing(record)
                and all(value in NO_EFFECT_OUTCOMES for value in attempts.values())
            ):
                path.unlink(missing_ok=True)
                return None
            record["state"] = "outcome_unknown"
            atomic_write_json(path, record)
            return record

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


# Queue, relay and client codes that prove this request id never reached the
# service: it was refused, withdrawn or expired before any relay sent it.
NOT_SENT_CODES = frozenset(
    {
        "work_coordinate_request_expired",
        "work_coordinate_request_invalid",
        "work_coordinate_request_too_large",
        "work_coordinate_worker_mismatch",
        "work_coordinate_transport_identity_unavailable",
        "work_coordinate_relay_unavailable",
        "work_coordinate_channel_reconnecting",
    }
)


def error_outcome(error: DomainError) -> str:
    """What an error proves about the mutation it answered.

    ``not_sent``: this request id never reached the service. ``refused``: the
    service answered with a domain refusal, so nothing applied under the key.
    ``unknown``: anything else, including a claimed request that expired, a
    transport failure, a result too large to queue, an invalid, missing or
    mixed result, and a server error. Only proof frees a key; the default keeps it.
    """

    code = str(error.code or "")
    if code in NOT_SENT_CODES:
        return "not_sent"
    if (
        not code or code in {"domain_error", "work_operation_mixed"}
        or code.startswith(("data_bus_", "work_coordinate_"))
    ):
        return "unknown"
    return "refused" if 400 <= int(error.status or 0) < 500 else "unknown"


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


RECEIPT_READ_OPERATION = "operation.receipt.get"


def lookup_remote_receipt(record: Mapping[str, Any], read: Any) -> dict[str, Any] | None:
    """Read the board's durable receipt of a request whose outcome is unknown (W574).

    ``read(action, object_ref, payload)`` performs ``operation.receipt.get``
    and returns its result. This is a status read, not a mutation send.
    Returns the receipt (``state`` applied, refused, in_progress or
    no_record), ``state`` unavailable when the board predates the read
    operation (refused with ``work_worker_stream_operation_denied`` for it)
    and so cannot confirm the outcome, or None when nothing is unknown.
    """

    if record.get("state") == STATE_APPLIED:
        return None
    if not any(outcome == ATTEMPT_UNKNOWN for outcome in _attempts(record).values()):
        return None
    action = str(record.get("action") or "")
    object_ref = str(record.get("object_ref") or "")
    payload = record.get("payload") if isinstance(record.get("payload"), Mapping) else {}
    try:
        response = read(
            RECEIPT_READ_OPERATION,
            object_ref,
            {
                "operation": action,
                "idempotency_key": str(record.get("idempotency_key") or ""),
                "request_hash": transport_request_hash(action, object_ref, payload),
            },
        )
    except DomainError as exc:
        details = exc.details if isinstance(exc.details, Mapping) else {}
        if exc.code == "work_worker_stream_operation_denied" and details.get("operation") == RECEIPT_READ_OPERATION:
            return {"state": "unavailable"}
        raise
    body = response.get("object") if isinstance(response, Mapping) else None
    if not isinstance(body, Mapping) or str(body.get("state") or "") not in (
        "applied", "refused", "in_progress", "no_record",
    ):
        raise DomainError(
            "work_coordinate_receipt_invalid",
            "The receipt read returned no known state.",
            status=502,
        )
    return dict(body)


def recovery_identity(record: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    """What a caller is shown about the original request it recovered."""

    return {
        "source": source,
        "idempotency_key": str(record.get("idempotency_key") or ""),
        "request_hash": str(record.get("request_hash") or ""),
        "request_ids": list(record.get("request_ids") or []),
        "attempts": _attempts(record),
        "publishing": len(_publishing(record)),
        "first_sent_at": str(record.get("first_sent_at") or ""),
        "state": str(record.get("state") or ""),
    }


__all__ = [
    "NOT_SENT_CODES",
    "RECEIPT_READ_OPERATION",
    "error_outcome",
    "COORDINATE_RECOVERY_SCHEMA",
    "CoordinateRecovery",
    "coordinate_request_hash",
    "idempotency_key_reused",
    "lookup_remote_receipt",
    "mutation_idempotency_key",
    "recovery_identity",
]
