from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Sequence

from ..contract.errors import DomainError


def _path(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if any(character in text for character in ("\x00", "\n", "\r")):
        raise DomainError(
            "work_session_resume_path_invalid",
            f"{field} contains unsupported control characters.",
        )
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise DomainError(
            "work_session_resume_path_invalid",
            f"{field} must be an absolute LOCAL path.",
        )
    return str(path.resolve())


def _command(segments: Sequence[Sequence[str]], *, working_directory: str = "") -> str:
    rendered = " \\\n  ".join(" ".join(shlex.quote(token) for token in segment) for segment in segments)
    if working_directory:
        return f"cd {shlex.quote(working_directory)} && \\\n  {rendered}"
    return rendered


def build_session_resume_command(
    *,
    runtime_kind: str,
    runtime_session_id: str,
    working_directory: str = "",
    allowed_roots: Sequence[str] = (),
    field_root: str | Path = "",
) -> dict[str, Any]:
    """Build a visible command; it is returned to the user and never executed.

    The roots come only from the host's reviewed receiver configuration. The
    generator selects no model and grants no full-disk or credential access.
    """

    runtime = str(runtime_kind or "").strip().lower()
    session_id = str(runtime_session_id or "").strip()
    if not session_id:
        raise DomainError(
            "work_session_resume_id_required",
            "The worker has no native resumable session id.",
        )
    if any(character in session_id for character in ("\x00", "\n", "\r")):
        raise DomainError(
            "work_session_resume_id_invalid",
            "The native resumable session id contains unsupported characters.",
        )

    cwd = _path(working_directory, field="worker.working_directory")
    roots: list[str] = []
    for value in allowed_roots:
        root = _path(value, field="allowed_root")
        if root and root not in roots:
            roots.append(root)
    if cwd:
        cwd_path = Path(cwd)
        if not any(
            cwd_path == Path(root) or cwd_path.is_relative_to(Path(root))
            for root in roots
        ):
            raise DomainError(
                "work_session_resume_working_directory_denied",
                "The recorded working directory is outside this host's approved roots.",
            )
    if field_root:
        host_state_root = str(Path(_path(field_root, field="field_root")).parent)
        if host_state_root not in roots:
            roots.append(host_state_root)

    if runtime == "codex":
        argv = ["codex", "resume", session_id]
        segments: list[list[str]] = [list(argv)]
        if cwd:
            argv.extend(["-C", cwd])
            segments.append(["-C", cwd])
        argv.extend(["-s", "workspace-write"])
        segments.append(["-s", "workspace-write"])
        argv.extend(["-c", "sandbox_workspace_write.network_access=true"])
        segments.append(["-c", "sandbox_workspace_write.network_access=true"])
        for root in roots:
            argv.extend(["--add-dir", root])
            segments.append(["--add-dir", root])
        command = _command(segments)
    elif runtime == "claude-code":
        argv = ["claude", "--resume", session_id]
        segments = [list(argv)]
        for root in roots:
            argv.extend(["--add-dir", root])
            segments.append(["--add-dir", root])
        command = _command(segments, working_directory=cwd)
    else:
        raise DomainError(
            "work_session_resume_runtime_unsupported",
            "This worker runtime has no Problem Board resume-command adapter.",
            details={"runtime_kind": runtime},
        )

    return {
        "runtime_kind": runtime,
        "runtime_session_id": session_id,
        "working_directory_recorded": bool(cwd),
        "root_count": len(roots),
        "argv": argv,
        "command": command,
    }


__all__ = ["build_session_resume_command"]
