"""An agent workspace's AGENTS.md / CLAUDE.md, made only from its project's files (W802).

Operator, 2026-10-13: "workspace must get this in form of AGENTS.md so this
works for other type of agents. and if this is claude agent than simply
additionally also the CLAUDE.md which imports AGENTS.md" (W779), then, after
the first new agent enrolled (W802): "not sure actually why we promise the
CLAUDE.md and AGENTS.md will be written. that is wrong.", "CLAUDE.md /
AGENTS.md made from project file (which we have multiple of them).", "and in
case of new project even NONE" and "the folders must be aliases".

So ``pb worker context`` writes one managed block into the workspace's
AGENTS.md from the project's files list, and only the files present in this
agent's clones: the instructions, facts and environment files (an ``@``
import for Claude, a path to read for Codex) and the further files with their
descriptions (read when the description fits). A Claude Code workspace also
gets a CLAUDE.md whose managed block imports AGENTS.md. Nothing is written at
enrollment, and a project that lists no files (or none present yet) has no
block: an earlier block is removed. A workspace is always ``<root>/<alias>``;
without an alias nothing is written.

Every write is atomic. Text outside the markers belongs to the user and is
never changed; a file left with nothing else is removed. A workspace inside a
git work tree, or one git cannot be asked about, is never written.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

BEGIN = "<!-- problem-board:begin"
END = "<!-- problem-board:end -->"
_BLOCK = re.compile(r"\n*<!-- problem-board:begin [^>]*-->\n.*?\n<!-- problem-board:end -->\n?", re.S)
_BLOCK_BODY = re.compile(r"<!-- problem-board:begin (?P<marker>[^>]*)-->\n(?P<body>.*?)\n<!-- problem-board:end -->", re.S)
# W779's block (the pb rules, "rev=") and its single instructions link are retired by W802.
LEGACY_LINK = Path(".problem-board") / "project-instructions.md"
CLAUDE_RUNTIMES = frozenset({"claude-code"})
PURPOSES = (("instructions", "Instructions"), ("facts", "Facts"), ("environment", "Environment"))


def _present(files: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [row for row in files if str(row.get("state") or "") == "present" and row.get("alias") and row.get("path")]


def render_body(files: Iterable[Mapping[str, Any]], runtime_kind: str) -> str:
    """The block for the files present in this agent's clones; "" when there are none."""

    present = _present(files)
    if not present:
        return ""
    claude = runtime_kind in CLAUDE_RUNTIMES
    lines = [
        "# Project files (managed by pb from the project card; edit nothing between the markers)",
        "",
        "Read the project's own files before you work. Paths are in this workspace.",
    ]
    for purpose, label in PURPOSES:
        row = next((item for item in present if item.get("purpose") == purpose), None)
        if row is None:
            continue
        local = f"{row['alias']}/{row['path']}"
        lines += ["", f"## {label}: `{local}`"]
        lines.append(f"@{local}" if claude else f"Read `{local}` at the start of every session.")
    further = [item for item in present if not item.get("purpose")]
    if further:
        lines += ["", "## Further files (read one when its description fits the task)"]
        for row in further:
            description = str(row.get("description") or "").strip()
            lines.append(f"- `{row['alias']}/{row['path']}`" + (f": {description}" if description else ""))
    return "\n".join(lines)


def _git_refusal(path: Path) -> str:
    """The named state that refuses this folder, or "" to write; fails closed when git cannot answer."""

    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(probe), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "workspace_git_check_failed"
    if result.returncode == 0 and result.stdout.strip() == "true":
        return "workspace_is_git_worktree"
    return ""


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _without_block(text: str) -> str:
    return _BLOCK.sub("\n", text).strip("\n")


def _set(path: Path, block: str) -> bool:
    """Put ``block`` (or no block) into ``path``; user text stays; an empty file is removed. True when changed."""

    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    user = _without_block(existing)
    if block:
        updated = (user + "\n\n" if user else "") + block + "\n"
    else:
        updated = (user + "\n") if user else ""
    if updated == existing:
        return False
    if updated:
        _atomic_write(path, updated)
    else:
        path.unlink(missing_ok=True)
    return True


def _block(marker: str, body: str) -> str:
    return f"{BEGIN} {marker} -->\n{body}\n{END}"


def ensure_project_files_block(
    workspace: str | Path, *, alias: str, runtime_kind: str, project_id: str, files_revision: int,
    files: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Write, refresh or remove the workspace's managed block from the project's files; report what changed."""

    root = Path(workspace)
    if not alias or root.name != alias:
        return {"workspace": str(root), "state": "workspace_is_not_an_alias_folder", "written": []}
    refusal = _git_refusal(root)
    if refusal:
        return {"workspace": str(root), "state": refusal, "written": []}
    body = render_body(files, runtime_kind)
    marker = f"project={project_id} files_revision={int(files_revision)}"
    written: list[str] = []
    if (root / LEGACY_LINK).is_symlink():
        (root / LEGACY_LINK).unlink()
        written.append(f"{LEGACY_LINK} (removed)")
    agents = _block(marker, body) if body else ""
    if (root / "AGENTS.md").exists() or agents:
        if _set(root / "AGENTS.md", agents):
            written.append("AGENTS.md" if agents else "AGENTS.md (block removed)")
    claude = _block(marker, "@AGENTS.md") if body and runtime_kind in CLAUDE_RUNTIMES else ""
    if (root / "CLAUDE.md").exists() or claude:
        if _set(root / "CLAUDE.md", claude):
            written.append("CLAUDE.md" if claude else "CLAUDE.md (block removed)")
    return {"workspace": str(root), "state": "written" if body else "none", "written": written}


def remove_legacy_rules(workspace: str | Path) -> dict[str, Any]:
    """Remove only W779's pb-rules blocks and instructions link; a project-files block stays."""

    root = Path(workspace)
    refusal = _git_refusal(root)
    if refusal:
        return {"workspace": str(root), "state": refusal, "written": []}
    written: list[str] = []
    for name in ("AGENTS.md", "CLAUDE.md"):
        path = root / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        legacy = [m for m in _BLOCK_BODY.finditer(text) if m.group("marker").startswith("rev=")]
        if not legacy:
            continue
        kept = text
        for match in legacy:
            kept = kept.replace(match.group(0), "")
        kept = re.sub(r"\n{3,}", "\n\n", kept).strip("\n")
        if kept:
            _atomic_write(path, kept + "\n")
        else:
            path.unlink()
        written.append(f"{name} (pb rules block removed)")
    if (root / LEGACY_LINK).is_symlink():
        (root / LEGACY_LINK).unlink()
        written.append(f"{LEGACY_LINK} (removed)")
    return {"workspace": str(root), "state": "ok", "written": written}


__all__ = ["ensure_project_files_block", "remove_legacy_rules", "render_body"]
