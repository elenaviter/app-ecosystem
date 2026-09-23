from __future__ import annotations

import json
import os
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contract.errors import DomainError
from .io import (
    atomic_write_json,
    bounded_text,
    component,
    exclusive_lock,
    new_id,
    parse_utc,
    read_json,
    utc_now,
)


COORDINATE_REQUEST_SCHEMA = "problem-board.coordinate-request.v1"
COORDINATE_RESPONSE_SCHEMA = "problem-board.coordinate-response.v1"
COORDINATE_LEASE_LOST = "work_coordinate_lease_lost"
MAX_COORDINATE_REQUEST_BYTES = 64 * 1024
MAX_COORDINATE_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_COORDINATE_TIMEOUT_SECONDS = 90.0
COORDINATE_LEASE_SECONDS = 15.0


def _utc_after(seconds: float) -> str:
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc) + timedelta(seconds=max(0.0, float(seconds)))
    ).isoformat().replace("+00:00", "Z")


def _encoded_bytes(value: Mapping[str, Any]) -> int:
    """Measure the exact representation written by ``atomic_write_json``."""

    encoded = json.dumps(
        dict(value), ensure_ascii=True, sort_keys=True, indent=2
    ) + "\n"
    return len(encoded.encode("utf-8"))


def _sized_record(value: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
    """Include the record's final on-disk byte count without approximation."""

    row = dict(value)
    row["payload_bytes"] = 0
    while True:
        payload_bytes = _encoded_bytes(row)
        if row["payload_bytes"] == payload_bytes:
            return row, payload_bytes
        row["payload_bytes"] = payload_bytes


class CoordinateQueue:
    """Bounded request/response handoff between an agent and its login relay."""

    def __init__(self, field_root: str | Path) -> None:
        self.root = Path(field_root).expanduser().resolve() / ".problem-board" / "coordinate"

    def _worker_root(self, state: str, worker_name: str) -> Path:
        return self.root / state / component(worker_name, field="worker_name")

    def _path(self, state: str, worker_name: str, request_id: str) -> Path:
        return self._worker_root(state, worker_name) / (
            f"{component(request_id, field='request_id')}.json"
        )

    def _lock_path(self, worker_name: str) -> Path:
        return self.root / "locks" / (
            f"{component(worker_name, field='worker_name')}.lock"
        )

    @staticmethod
    def _request_address(worker_name: str, path: Path) -> dict[str, str]:
        return {
            "request_id": component(path.stem, field="request_id"),
            "worker_name": component(worker_name, field="worker_name"),
        }

    def submit(
        self,
        *,
        worker_name: str,
        worker_identity: str,
        runtime_kind: str,
        runtime_session_id: str,
        action: str,
        object_ref: str,
        payload: Mapping[str, Any],
        timeout_seconds: float = DEFAULT_COORDINATE_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        timeout = max(1.0, min(float(timeout_seconds), 600.0))
        request_id = new_id("coordinate")
        row = {
            "schema": COORDINATE_REQUEST_SCHEMA,
            "request_id": request_id,
            "worker_name": component(worker_name, field="worker_name"),
            "worker_identity": bounded_text(
                worker_identity,
                field="worker_identity",
                maximum=256,
                required=True,
            ),
            "runtime_kind": bounded_text(
                runtime_kind, field="runtime_kind", maximum=32, required=True
            ),
            "runtime_session_id": bounded_text(
                runtime_session_id,
                field="runtime_session_id",
                maximum=128,
                required=True,
            ),
            "action": bounded_text(
                action, field="action", maximum=128, required=True
            ),
            "object_ref": bounded_text(
                object_ref, field="object_ref", maximum=1000
            ),
            "payload": dict(payload),
            "created_at": utc_now(),
            "expires_at": _utc_after(timeout),
            "not_before": "",
            "claimed_once": False,
            "first_claimed_at": "",
            "transport_attempts": 0,
            "last_transport_error": {},
            "lease_id": "",
            "leased_at": "",
            "lease_expires_at": "",
        }
        row, payload_bytes = _sized_record(row)
        _, leased_payload_bytes = _sized_record(
            {
                **row,
                "claimed_once": True,
                "first_claimed_at": utc_now(),
                "transport_attempts": 999,
                "last_transport_error": {
                    "code": "data_bus_outcome_unknown",
                    "observed_at": utc_now(),
                },
                "lease_id": "coordinate-lease_" + ("0" * 32),
                "leased_at": utc_now(),
                "lease_expires_at": _utc_after(COORDINATE_LEASE_SECONDS),
            }
        )
        maximum_payload_bytes = max(payload_bytes, leased_payload_bytes)
        if maximum_payload_bytes > MAX_COORDINATE_REQUEST_BYTES:
            raise DomainError(
                "work_coordinate_request_too_large",
                "The governed operation request exceeds the relay queue limit.",
                status=413,
                details={
                    "payload_bytes": maximum_payload_bytes,
                    "maximum_bytes": MAX_COORDINATE_REQUEST_BYTES,
                },
            )
        atomic_write_json(
            self._path("pending", row["worker_name"], request_id), row
        )
        return row

    @staticmethod
    def expired(request: Mapping[str, Any]) -> bool:
        try:
            return parse_utc(str(request.get("expires_at") or "")) <= parse_utc(
                utc_now()
            )
        except DomainError:
            return True

    def _write_error(
        self,
        request: Mapping[str, Any],
        *,
        code: str,
        message: str,
        status: int,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.complete(
            request,
            error={
                "code": str(code or "work_coordinate_relay_failed"),
                "message": str(message or "The relay could not run the operation."),
                "status": int(status),
                "details": dict(details or {}),
            },
        )

    def _write_error_unlocked(
        self,
        request: Mapping[str, Any],
        *,
        code: str,
        message: str,
        status: int,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._complete_unlocked(
            request,
            error={
                "code": str(code or "work_coordinate_relay_failed"),
                "message": str(message or "The relay could not run the operation."),
                "status": int(status),
                "details": dict(details or {}),
            },
            enforce_lease=False,
        )

    def _require_current_lease_unlocked(
        self, request: Mapping[str, Any]
    ) -> dict[str, Any]:
        worker_name = component(request.get("worker_name"), field="worker_name")
        request_id = component(request.get("request_id"), field="request_id")
        lease_id = str(request.get("lease_id") or "")
        if not lease_id:
            raise DomainError(
                COORDINATE_LEASE_LOST,
                "The relay operation no longer owns this local request lease.",
                status=409,
                details={"worker_name": worker_name, "request_id": request_id},
            )
        try:
            current = read_json(
                self._path("leased", worker_name, request_id), required=True
            )
        except DomainError as exc:
            raise DomainError(
                COORDINATE_LEASE_LOST,
                "The relay operation no longer owns this local request lease.",
                status=409,
                details={"worker_name": worker_name, "request_id": request_id},
            ) from exc
        if (
            str(current.get("lease_id") or "") != lease_id
            or str(current.get("request_id") or "") != request_id
            or str(current.get("worker_name") or "") != worker_name
        ):
            raise DomainError(
                COORDINATE_LEASE_LOST,
                "The relay operation no longer owns this local request lease.",
                status=409,
                details={"worker_name": worker_name, "request_id": request_id},
            )
        return current

    def _recover_expired_leases_unlocked(self, worker_name: str) -> None:
        leased_root = self._worker_root("leased", worker_name)
        try:
            paths = sorted(leased_root.glob("*.json"))
        except OSError:
            return
        now = time.time()
        for path in paths:
            try:
                request = read_json(path, required=False)
            except DomainError:
                address = self._request_address(worker_name, path)
                self._write_error_unlocked(
                    address,
                    code="work_coordinate_request_invalid",
                    message="The relay queue request is not a readable JSON object.",
                    status=400,
                )
                continue
            if not request:
                continue
            request_id = str(request.get("request_id") or path.stem)
            response_path = self._path("responses", worker_name, request_id)
            if response_path.exists():
                path.unlink(missing_ok=True)
                continue
            lease_expires_at = str(request.get("lease_expires_at") or "")
            lease_expired = False
            if lease_expires_at:
                try:
                    lease_expired = parse_utc(lease_expires_at) <= parse_utc(utc_now())
                except DomainError:
                    lease_expired = True
            else:
                try:
                    lease_expired = now - path.stat().st_mtime >= COORDINATE_LEASE_SECONDS
                except OSError:
                    continue
            if not lease_expired:
                continue
            if self.expired(request):
                outcome_unknown = bool(request.get("claimed_once"))
                self._write_error_unlocked(
                    request,
                    code=(
                        "work_coordinate_outcome_unknown"
                        if outcome_unknown
                        else "work_coordinate_request_expired"
                    ),
                    message=(
                        "The relay claimed the governed operation but its result is unknown."
                        if outcome_unknown
                        else "The relay did not claim the governed operation before its deadline."
                    ),
                    status=504,
                    details={
                        "request_id": request_id,
                        "transport_attempts": int(
                            request.get("transport_attempts") or 0
                        ),
                    },
                )
                continue
            pending = self._path("pending", worker_name, request_id)
            pending.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.replace(path, pending)
            except FileNotFoundError:
                continue

    def claim(
        self,
        *,
        worker_name: str,
        limit: int = 8,
        lease_seconds: float = COORDINATE_LEASE_SECONDS,
    ) -> list[dict[str, Any]]:
        clean_worker = component(worker_name, field="worker_name")
        with exclusive_lock(self._lock_path(clean_worker)):
            return self._claim_unlocked(
                worker_name=clean_worker,
                limit=limit,
                lease_seconds=lease_seconds,
            )

    def _claim_unlocked(
        self,
        *,
        worker_name: str,
        limit: int,
        lease_seconds: float,
    ) -> list[dict[str, Any]]:
        clean_worker = component(worker_name, field="worker_name")
        self._recover_expired_leases_unlocked(clean_worker)
        pending_root = self._worker_root("pending", clean_worker)
        try:
            paths = sorted(
                pending_root.glob("*.json"),
                key=lambda path: (path.stat().st_mtime_ns, path.name),
            )
        except OSError:
            return []
        claimed: list[dict[str, Any]] = []
        leased_root = self._worker_root("leased", clean_worker)
        leased_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        selected_limit = max(1, min(int(limit), 32))
        for pending in paths:
            if len(claimed) >= selected_limit:
                break
            try:
                if pending.stat().st_mtime > time.time():
                    continue
            except OSError:
                continue
            leased = leased_root / pending.name
            try:
                os.replace(pending, leased)
            except FileNotFoundError:
                continue
            address = self._request_address(clean_worker, leased)
            try:
                stored_bytes = leased.stat().st_size
            except OSError:
                continue
            if stored_bytes > MAX_COORDINATE_REQUEST_BYTES:
                self._write_error_unlocked(
                    address,
                    code="work_coordinate_request_too_large",
                    message="The governed operation request exceeds the relay queue limit.",
                    status=413,
                    details={
                        "payload_bytes": stored_bytes,
                        "maximum_bytes": MAX_COORDINATE_REQUEST_BYTES,
                    },
                )
                continue
            try:
                request = read_json(leased, required=False)
            except DomainError:
                self._write_error_unlocked(
                    address,
                    code="work_coordinate_request_invalid",
                    message="The relay queue request is not a readable JSON object.",
                    status=400,
                )
                continue
            if not request:
                leased.unlink(missing_ok=True)
                continue
            request_id = str(request.get("request_id") or "")
            declared_bytes = request.get("payload_bytes")
            if (
                request.get("schema") != COORDINATE_REQUEST_SCHEMA
                or request_id != address["request_id"]
                or str(request.get("worker_name") or "") != clean_worker
                or isinstance(declared_bytes, bool)
                or not isinstance(declared_bytes, int)
                or declared_bytes != stored_bytes
            ):
                self._write_error_unlocked(
                    address,
                    code="work_coordinate_request_invalid",
                    message="The relay queue request envelope is invalid.",
                    status=400,
                    details={"request_id": address["request_id"]},
                )
                continue
            if self.expired(request):
                outcome_unknown = bool(request.get("claimed_once"))
                self._write_error_unlocked(
                    request,
                    code=(
                        "work_coordinate_outcome_unknown"
                        if outcome_unknown
                        else "work_coordinate_request_expired"
                    ),
                    message=(
                        "The relay claimed the governed operation but its result is unknown."
                        if outcome_unknown
                        else "The governed operation expired before the relay could run it."
                    ),
                    status=504,
                    details={
                        "request_id": request_id,
                        "transport_attempts": int(
                            request.get("transport_attempts") or 0
                        ),
                    },
                )
                continue
            not_before = str(request.get("not_before") or "")
            if not_before:
                try:
                    ready_at = parse_utc(not_before)
                except DomainError:
                    self._write_error_unlocked(
                        request,
                        code="work_coordinate_request_invalid",
                        message="The relay queue retry timestamp is invalid.",
                        status=400,
                        details={"request_id": request_id},
                    )
                    continue
                if ready_at > parse_utc(utc_now()):
                    os.replace(leased, pending)
                    retry_at = ready_at.timestamp()
                    os.utime(pending, (retry_at, retry_at))
                    continue
            request["not_before"] = ""
            request["claimed_once"] = True
            request["first_claimed_at"] = str(
                request.get("first_claimed_at") or utc_now()
            )
            request["lease_id"] = new_id("coordinate-lease")
            request["leased_at"] = utc_now()
            request["lease_expires_at"] = _utc_after(lease_seconds)
            request, leased_bytes = _sized_record(request)
            if leased_bytes > MAX_COORDINATE_REQUEST_BYTES:
                self._write_error_unlocked(
                    request,
                    code="work_coordinate_request_too_large",
                    message="The governed operation request exceeds the relay queue limit.",
                    status=413,
                    details={
                        "payload_bytes": leased_bytes,
                        "maximum_bytes": MAX_COORDINATE_REQUEST_BYTES,
                    },
                )
                continue
            atomic_write_json(leased, request)
            lease_expires_at = parse_utc(request["lease_expires_at"]).timestamp()
            os.utime(leased, (lease_expires_at, lease_expires_at))
            claimed.append(request)
        return claimed

    def mark_attempt(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """Persist that the relay may have submitted this request remotely."""

        worker_name = component(request.get("worker_name"), field="worker_name")
        with exclusive_lock(self._lock_path(worker_name)):
            self._require_current_lease_unlocked(request)
            row = dict(request)
            attempts = row.get("transport_attempts")
            if isinstance(attempts, bool) or not isinstance(attempts, int):
                attempts = 0
            row["transport_attempts"] = attempts + 1
            row, payload_bytes = _sized_record(row)
            if payload_bytes > MAX_COORDINATE_REQUEST_BYTES:
                raise DomainError(
                    "work_coordinate_request_too_large",
                    "The governed operation request exceeds the relay queue limit.",
                    status=413,
                    details={
                        "payload_bytes": payload_bytes,
                        "maximum_bytes": MAX_COORDINATE_REQUEST_BYTES,
                    },
                )
            leased = self._path("leased", row["worker_name"], row["request_id"])
            atomic_write_json(leased, row)
            lease_expires_at = parse_utc(row["lease_expires_at"]).timestamp()
            os.utime(leased, (lease_expires_at, lease_expires_at))
            return row

    def defer_after_unknown(
        self,
        request: Mapping[str, Any],
        *,
        error: DomainError,
    ) -> dict[str, Any]:
        """Retry an uncertain Data Bus outcome as the same transport request."""

        worker_name = component(request.get("worker_name"), field="worker_name")
        with exclusive_lock(self._lock_path(worker_name)):
            self._require_current_lease_unlocked(request)
            row = dict(request)
            attempts = max(1, int(row.get("transport_attempts") or 1))
            delay_seconds = min(5.0, 0.25 * (2 ** min(attempts - 1, 5)))
            row.update(
                {
                    "not_before": _utc_after(delay_seconds),
                    "leased_at": "",
                    "lease_expires_at": "",
                    "last_transport_error": {
                        "code": error.code,
                        "observed_at": utc_now(),
                    },
                }
            )
            row, payload_bytes = _sized_record(row)
            if payload_bytes > MAX_COORDINATE_REQUEST_BYTES:
                return self._write_error_unlocked(
                    request,
                    code="work_coordinate_request_too_large",
                    message="The governed operation request exceeds the relay queue limit.",
                    status=413,
                    details={
                        "payload_bytes": payload_bytes,
                        "maximum_bytes": MAX_COORDINATE_REQUEST_BYTES,
                    },
                )
            leased = self._path("leased", row["worker_name"], row["request_id"])
            pending = self._path("pending", row["worker_name"], row["request_id"])
            atomic_write_json(leased, row)
            pending.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(leased, pending)
            retry_at = parse_utc(row["not_before"]).timestamp()
            os.utime(pending, (retry_at, retry_at))
            return row

    def complete(
        self,
        request: Mapping[str, Any],
        *,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        worker_name = component(request.get("worker_name"), field="worker_name")
        with exclusive_lock(self._lock_path(worker_name)):
            return self._complete_unlocked(
                request,
                result=result,
                error=error,
                enforce_lease=True,
            )

    def _complete_unlocked(
        self,
        request: Mapping[str, Any],
        *,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
        enforce_lease: bool,
    ) -> dict[str, Any]:
        request_id = component(request.get("request_id"), field="request_id")
        worker_name = component(request.get("worker_name"), field="worker_name")
        if (result is None) == (error is None):
            raise ValueError("complete requires exactly one of result or error")
        if enforce_lease:
            self._require_current_lease_unlocked(request)
        response: dict[str, Any] = {
            "schema": COORDINATE_RESPONSE_SCHEMA,
            "request_id": request_id,
            "worker_name": worker_name,
            "ok": error is None,
            "completed_at": utc_now(),
        }
        if error is None:
            response["result"] = dict(result or {})
        else:
            response["error"] = dict(error)
        response, response_bytes = _sized_record(response)
        if response_bytes > MAX_COORDINATE_RESPONSE_BYTES:
            response = {
                "schema": COORDINATE_RESPONSE_SCHEMA,
                "request_id": request_id,
                "worker_name": worker_name,
                "ok": False,
                "completed_at": utc_now(),
                "error": {
                    "code": "work_coordinate_response_too_large",
                    "message": "The governed operation response exceeds the relay queue limit.",
                    "status": 502,
                    "details": {
                        "payload_bytes": response_bytes,
                        "maximum_bytes": MAX_COORDINATE_RESPONSE_BYTES,
                    },
                },
            }
            response, _ = _sized_record(response)
        atomic_write_json(
            self._path("responses", worker_name, request_id), response
        )
        self._path("leased", worker_name, request_id).unlink(missing_ok=True)
        self._path("pending", worker_name, request_id).unlink(missing_ok=True)
        return response

    def fail(self, request: Mapping[str, Any], error: BaseException) -> dict[str, Any]:
        if isinstance(error, DomainError):
            return self._write_error(
                request,
                code=error.code,
                message=str(error),
                status=error.status,
                details=error.details,
            )
        return self._write_error(
            request,
            code="work_coordinate_relay_failed",
            message="The worker relay failed while running the governed operation.",
            status=502,
            details={"error_type": type(error).__name__},
        )

    def take_response(
        self, *, worker_name: str, request_id: str
    ) -> dict[str, Any] | None:
        path = self._path("responses", worker_name, request_id)
        try:
            stored_bytes = path.stat().st_size
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise DomainError(
                "work_coordinate_response_invalid",
                "The worker relay response cannot be read.",
                status=502,
            ) from exc
        if stored_bytes > MAX_COORDINATE_RESPONSE_BYTES:
            path.unlink(missing_ok=True)
            raise DomainError(
                "work_coordinate_response_too_large",
                "The governed operation response exceeds the relay queue limit.",
                status=502,
                details={
                    "payload_bytes": stored_bytes,
                    "maximum_bytes": MAX_COORDINATE_RESPONSE_BYTES,
                },
            )
        try:
            response = read_json(path, required=False)
        except DomainError as exc:
            path.unlink(missing_ok=True)
            raise DomainError(
                "work_coordinate_response_invalid",
                "The worker relay response is not a readable JSON object.",
                status=502,
            ) from exc
        if not response:
            return None
        declared_bytes = response.get("payload_bytes")
        if (
            response.get("schema") != COORDINATE_RESPONSE_SCHEMA
            or str(response.get("request_id") or "") != component(
                request_id, field="request_id"
            )
            or str(response.get("worker_name") or "") != component(
                worker_name, field="worker_name"
            )
            or isinstance(declared_bytes, bool)
            or not isinstance(declared_bytes, int)
            or declared_bytes != stored_bytes
        ):
            path.unlink(missing_ok=True)
            raise DomainError(
                "work_coordinate_response_invalid",
                "The worker relay response envelope is invalid.",
                status=502,
            )
        path.unlink(missing_ok=True)
        return response

    def cancel_pending(
        self, *, worker_name: str, request_id: str
    ) -> dict[str, Any] | None:
        clean_worker = component(worker_name, field="worker_name")
        with exclusive_lock(self._lock_path(clean_worker)):
            path = self._path("pending", clean_worker, request_id)
            cancelled = self._path("cancelled", clean_worker, request_id)
            cancelled.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.replace(path, cancelled)
            except FileNotFoundError:
                return None
            try:
                return read_json(cancelled, required=False)
            finally:
                cancelled.unlink(missing_ok=True)

    def cancel_pending_if_unclaimed(
        self, *, worker_name: str, request_id: str
    ) -> dict[str, Any] | None:
        """Withdraw a pending request only if no relay has ever claimed it.

        Reads and removes under the worker lock, so a relay cannot claim it
        in between. A request that was claimed once and recovered to pending
        stays where it is: the relay may still finish or reconcile it.
        Returns the withdrawn request, or None when nothing was withdrawn.
        """

        clean_worker = component(worker_name, field="worker_name")
        with exclusive_lock(self._lock_path(clean_worker)):
            path = self._path("pending", clean_worker, request_id)
            request = read_json(path, required=False)
            if not isinstance(request, Mapping) or bool(request.get("claimed_once")):
                return None
            path.unlink(missing_ok=True)
            return dict(request)

    def pending_signature(self) -> tuple[int, int]:
        root = self.root / "pending"
        try:
            paths: Sequence[Path] = tuple(root.glob("*/*.json"))
        except OSError:
            return 0, 0
        newest = 0
        for path in paths:
            try:
                newest = max(newest, path.stat().st_mtime_ns)
            except OSError:
                continue
        return len(paths), newest

    def work_signature(
        self, *, worker_names: Sequence[str] = ()
    ) -> tuple[int, int, int, int]:
        selected = {
            component(worker_name, field="worker_name")
            for worker_name in worker_names
        }
        now = time.time()

        def paths_for(state: str) -> tuple[Path, ...]:
            root = self.root / state
            try:
                paths = tuple(root.glob("*/*.json"))
            except OSError:
                return ()
            if not selected:
                return paths
            return tuple(path for path in paths if path.parent.name in selected)

        pending = paths_for("pending")
        leased = paths_for("leased")
        newest = 0
        ready = 0
        recoverable = 0
        for path in pending:
            try:
                stat = path.stat()
            except OSError:
                continue
            newest = max(newest, stat.st_mtime_ns)
            ready += int(stat.st_mtime <= now)
        for path in leased:
            try:
                recoverable += int(path.stat().st_mtime <= now)
            except OSError:
                continue
        return len(pending), newest, ready, recoverable

    def has_ready_work(self, *, worker_names: Sequence[str] = ()) -> bool:
        _, _, ready, recoverable = self.work_signature(worker_names=worker_names)
        return bool(ready or recoverable)


__all__ = [
    "COORDINATE_REQUEST_SCHEMA",
    "COORDINATE_RESPONSE_SCHEMA",
    "COORDINATE_LEASE_LOST",
    "CoordinateQueue",
    "DEFAULT_COORDINATE_TIMEOUT_SECONDS",
    "MAX_COORDINATE_REQUEST_BYTES",
    "MAX_COORDINATE_RESPONSE_BYTES",
]
