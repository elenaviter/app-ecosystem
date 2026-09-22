from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from .errors import DomainError


RUNTIME_KINDS = {"codex", "claude-code", "resident", "relay"}
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
WORKER_ADDRESS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def normalize_runtime_kind(value: Any) -> str:
    runtime = str(value or "").strip().lower().replace("_", "-")
    aliases = {
        "claude": "claude-code",
        "claudecode": "claude-code",
        "openai-codex": "codex",
    }
    runtime = aliases.get(runtime, runtime)
    if runtime not in RUNTIME_KINDS:
        raise DomainError(
            "work_worker_runtime_kind_invalid",
            "Worker runtime_kind must identify a supported runtime adapter.",
            details={"allowed": sorted(RUNTIME_KINDS)},
        )
    return runtime


def normalize_runtime_session_id(value: Any) -> str:
    session_id = str(value or "").strip()
    if not SESSION_ID_RE.fullmatch(session_id):
        raise DomainError(
            "work_worker_session_id_invalid",
            "A worker requires its coding runtime's resumable session id.",
        )
    return session_id


def worker_identity(runtime_kind: Any, runtime_session_id: Any) -> str:
    runtime = normalize_runtime_kind(runtime_kind)
    session_id = normalize_runtime_session_id(runtime_session_id)
    return f"{runtime}:{session_id}"


def worker_address(runtime_kind: Any, runtime_session_id: Any) -> str:
    """Return a stable, ref-safe address derived from the resumable session."""

    runtime = normalize_runtime_kind(runtime_kind)
    session_id = normalize_runtime_session_id(runtime_session_id).lower()
    candidate = f"{runtime}-{session_id}"
    if len(candidate) <= 64 and WORKER_ADDRESS_RE.fullmatch(candidate):
        return candidate
    digest = hashlib.sha256(f"{runtime}:{session_id}".encode("utf-8")).hexdigest()[:24]
    prefix = runtime[: min(len(runtime), 32)]
    return f"{prefix}-{digest}"


def normalize_worker_alias(value: Any, *, required: bool = False) -> str:
    alias = " ".join(str(value or "").strip().split())
    if not alias and not required:
        return ""
    if not alias:
        raise DomainError(
            "work_worker_alias_invalid",
            "Worker alias is required.",
        )
    if len(alias.encode("utf-8")) > 160 or any(
        ord(character) < 32 or ord(character) == 127 for character in alias
    ):
        raise DomainError(
            "work_worker_alias_invalid",
            "Worker aliases are readable labels of at most 160 bytes.",
        )
    return alias


@dataclass(frozen=True)
class WorkerSessionIdentity:
    runtime_kind: str
    runtime_session_id: str
    worker_identity: str
    worker_name: str

    @classmethod
    def create(cls, runtime_kind: Any, runtime_session_id: Any) -> "WorkerSessionIdentity":
        runtime = normalize_runtime_kind(runtime_kind)
        session_id = normalize_runtime_session_id(runtime_session_id)
        return cls(
            runtime_kind=runtime,
            runtime_session_id=session_id,
            worker_identity=worker_identity(runtime, session_id),
            worker_name=worker_address(runtime, session_id),
        )


__all__ = [
    "RUNTIME_KINDS",
    "SESSION_ID_RE",
    "WORKER_ADDRESS_RE",
    "WorkerSessionIdentity",
    "normalize_runtime_kind",
    "normalize_runtime_session_id",
    "normalize_worker_alias",
    "worker_address",
    "worker_identity",
]
