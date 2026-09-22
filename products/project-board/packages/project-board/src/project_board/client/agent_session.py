from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from typing import Any

from ..contract.errors import DomainError
from ..contract.worker_identity import WorkerSessionIdentity, normalize_runtime_kind


RUNTIME_SESSION_ENV = {
    "codex": ("CODEX_SESSION_ID", "CODEX_THREAD_ID"),
    "claude-code": (
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_SESSION_ID",
        "PROBLEM_BOARD_CLAUDE_SESSION_ID",
    ),
}


def resolve_agent_session(
    *,
    runtime_kind: Any = "",
    runtime_session_id: Any = "",
    environ: Mapping[str, str] | None = None,
) -> WorkerSessionIdentity:
    """Resolve the identity of the coding session invoking the command."""

    env = dict(os.environ if environ is None else environ)
    requested_runtime = str(runtime_kind or "").strip()
    requested_session = str(runtime_session_id or "").strip()

    if requested_runtime:
        runtime = normalize_runtime_kind(requested_runtime)
        if not requested_session:
            requested_session = next(
                (
                    str(env.get(name) or "").strip()
                    for name in RUNTIME_SESSION_ENV.get(runtime, ())
                    if str(env.get(name) or "").strip()
                ),
                "",
            )
    else:
        runtime = ""
        for candidate, names in RUNTIME_SESSION_ENV.items():
            value = next(
                (
                    str(env.get(name) or "").strip()
                    for name in names
                    if str(env.get(name) or "").strip()
                ),
                "",
            )
            if value:
                runtime = candidate
                requested_session = requested_session or value
                break
        if not runtime:
            runtime = str(env.get("PROBLEM_BOARD_AGENT_RUNTIME") or "").strip()
            requested_session = requested_session or str(
                env.get("PROBLEM_BOARD_AGENT_SESSION_ID") or ""
            ).strip()

    if not runtime or not requested_session:
        raise DomainError(
            "field_agent_session_identity_required",
            "The selected agent must identify its runtime and native resumable session id.",
            details={
                "codex": "CODEX_SESSION_ID is detected automatically.",
                "claude_code": (
                    "Pass the UUID used by claude --resume when Claude Code does not "
                    "export CLAUDE_CODE_SESSION_ID."
                ),
            },
        )
    runtime = normalize_runtime_kind(runtime)
    if runtime == "claude-code" and requested_session.startswith("session_"):
        raise DomainError(
            "field_agent_resumable_session_id_required",
            "Use Claude Code's local UUID accepted by claude --resume, not a claude.ai attribution id.",
        )
    return WorkerSessionIdentity.create(runtime, requested_session)


def default_profile_name(
    identity: WorkerSessionIdentity, target_scope: str = ""
) -> str:
    material = f"{identity.worker_identity}\0{str(target_scope or '').strip()}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]
    runtime = "claude" if identity.runtime_kind == "claude-code" else identity.runtime_kind
    return f"problem-board-{runtime}-{digest}"


__all__ = ["RUNTIME_SESSION_ENV", "default_profile_name", "resolve_agent_session"]
