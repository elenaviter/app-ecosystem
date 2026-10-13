"""The always-on Problem Board rules in an agent's own workspace (W779).

Operator, 2026-10-13: "workspace must get this in form of AGENTS.md so this
works for other type of agents. and if this is claude agent than simply
additionally also the CLAUDE.md which imports AGENTS.md".

A skill is loaded once and summarised away at compaction; a workspace
CLAUDE.md is re-read from disk after compaction, and AGENTS.md is the file
Codex reads. So the few rules an agent must never lose live in a managed
block of the workspace's AGENTS.md. A Claude workspace additionally gets a
CLAUDE.md whose managed block imports it. The six rules are SKILL.md's own
"Always-On Rules" list, copied byte for byte: SKILL.md stays their only
source. The project's instructions file is linked into the workspace, never
copied.

Every write is atomic (a temporary file in the same folder, then a replace).
Text outside the markers belongs to the user and is never changed. A
workspace inside a git work tree is never written: a managed file there is
one ``git add`` away from a commit.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

from ..contract.errors import DomainError

BEGIN = "<!-- problem-board:begin"
END = "<!-- problem-board:end -->"
_BLOCK = re.compile(r"<!-- problem-board:begin rev=(?P<rev>[^ >]*) -->\n(?P<body>.*?)\n<!-- problem-board:end -->", re.S)
RULES_HEADING = "## Always-On Rules"
LINK_DIR = ".problem-board"
LINK_NAME = "project-instructions.md"
CLAUDE_RUNTIMES = frozenset({"claude-code"})
STATES = ("ok", "missing", "edited", "stale", "project_instructions_missing", "workspace_is_git_worktree")


def always_on_rules(skill_text: str) -> list[str]:
    """SKILL.md's numbered Always-On Rules, exactly as written there."""

    lines = skill_text.splitlines()
    try:
        start = lines.index(RULES_HEADING)
    except ValueError as exc:
        raise DomainError("work_workspace_rules_missing", "SKILL.md has no Always-On Rules section.") from exc
    rules: list[str] = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        if re.match(r"^[1-9][0-9]*\. ", line):
            rules.append(line)
    if not rules:
        raise DomainError("work_workspace_rules_missing", "SKILL.md's Always-On Rules list is empty.")
    return rules


def project_pointer(runtime_kind: str, linked: bool) -> str:
    """The line naming the project's instructions: an import for Claude, a path to read for others."""

    target = f"{LINK_DIR}/{LINK_NAME}"
    if not linked:
        return "Project instructions: none recorded for this workspace's project."
    if runtime_kind in CLAUDE_RUNTIMES:
        return f"Project instructions (imported):\n@{target}"
    return f"Project instructions: read `{target}` at the start of every session."


def render_body(rules: list[str], runtime_kind: str, linked: bool) -> str:
    return "\n".join([
        "# Problem Board: always-on rules (managed; edit nothing between the markers)",
        "",
        "The full procedure is the problem-board-worker skill. Before any act whose rule text is not in "
        "your context, read that rule's file in the skill's references first.",
        "",
        *rules,
        "",
        project_pointer(runtime_kind, linked),
    ])


def render_block(revision: str, body: str) -> str:
    return f"{BEGIN} rev={revision} -->\n{body}\n{END}"


def _in_git_work_tree(path: Path) -> bool:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(probe), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


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


def _atomic_symlink(link: Path, target: str) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.parent / f".{link.name}.{os.getpid()}.tmp"
    temporary.unlink(missing_ok=True)
    os.symlink(target, temporary)
    os.replace(temporary, link)


def _with_block(existing: str, block: str) -> str:
    """The file with its managed block replaced, or appended when it has none; user text untouched."""

    if _BLOCK.search(existing):
        return _BLOCK.sub(lambda _: block, existing, count=1)
    if not existing:
        return block + "\n"
    return existing.rstrip("\n") + "\n\n" + block + "\n"


def _file_state(path: Path, revision: str, body: str) -> str:
    if not path.exists():
        return "missing"
    found = _BLOCK.search(path.read_text(encoding="utf-8"))
    if found is None:
        return "missing"
    if found.group("rev") != revision:
        return "stale"
    return "ok" if found.group("body") == body else "edited"


def _plan(workspace: Path, runtime_kind: str, rules: list[str], project_instructions: str) -> dict[str, Any]:
    linked = bool(project_instructions)
    files = {"AGENTS.md": render_body(rules, runtime_kind, linked)}
    if runtime_kind in CLAUDE_RUNTIMES:
        files["CLAUDE.md"] = "@AGENTS.md"
    return {"files": files, "linked": linked}


def verify_workspace_instructions(
    workspace: str | Path, *, runtime_kind: str, revision: str, skill_text: str, project_instructions: str = "",
) -> dict[str, Any]:
    """Per file: ok, missing, edited or stale; plus the project link and the git-work-tree refusal."""

    root = Path(workspace)
    if _in_git_work_tree(root):
        return {"workspace": str(root), "state": "workspace_is_git_worktree", "files": {}}
    plan = _plan(root, runtime_kind, always_on_rules(skill_text), project_instructions)
    files = {name: _file_state(root / name, revision, body) for name, body in plan["files"].items()}
    link = root / LINK_DIR / LINK_NAME
    link_state = "none"
    if project_instructions or link.is_symlink():
        link_state = "ok" if link.is_symlink() and link.exists() else "project_instructions_missing"
    worst = next((files[name] for name in files if files[name] != "ok"), "ok")
    if worst == "ok" and link_state == "project_instructions_missing":
        worst = link_state
    return {"workspace": str(root), "state": worst, "files": files, "project_instructions": link_state}


def ensure_workspace_instructions(
    workspace: str | Path, *, runtime_kind: str, revision: str, skill_text: str, project_instructions: str = "",
) -> dict[str, Any]:
    """Write or refresh the managed blocks (and the project link); report what changed."""

    root = Path(workspace)
    if not str(workspace):
        raise DomainError("work_workspace_unknown", "This session has no workspace to write the rules into.")
    if _in_git_work_tree(root):
        return {"workspace": str(root), "state": "workspace_is_git_worktree", "written": []}
    target = str(project_instructions or "")
    if target and not Path(target).is_file():
        target = ""
    link = root / LINK_DIR / LINK_NAME
    if not target and link.is_symlink() and link.exists():
        # Install time knows no project; the link `pb worker listen` made stays.
        target = os.readlink(link)
    plan = _plan(root, runtime_kind, always_on_rules(skill_text), target)
    written: list[str] = []
    if target and (not link.is_symlink() or os.readlink(link) != target):
        _atomic_symlink(link, target)
        written.append(f"{LINK_DIR}/{LINK_NAME}")
    for name, body in plan["files"].items():
        path = root / name
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        updated = _with_block(existing, render_block(revision, body))
        if updated != existing:
            _atomic_write(path, updated)
            written.append(name)
    state = verify_workspace_instructions(
        root, runtime_kind=runtime_kind, revision=revision, skill_text=skill_text, project_instructions=target,
    )
    return {**state, "written": written}


def configured_workspaces(config: Any, kinds: list[str]) -> list[tuple[str, str]]:
    """Each enabled agent on this host of the given kinds: its workspace folder and runtime kind."""

    from .host_config import agent_workspace

    found: list[tuple[str, str]] = []
    for channel in getattr(config, "workers", ()) or ():
        if channel.state == "disabled" or channel.runtime_kind not in kinds:
            continue
        folder = agent_workspace(
            config,
            recorded=str(getattr(channel, "working_directory", "") or ""),
            alias=str(getattr(channel, "worker_alias", "") or ""),
            worker_name=channel.worker_name,
        )[0]
        if folder and (folder, channel.runtime_kind) not in found:
            found.append((folder, channel.runtime_kind))
    return found


def runtime_kind_of(config: Any, workspace: str) -> str:
    """The runtime kind of the agent whose workspace this is; a CLAUDE.md alone says claude-code."""

    target = Path(workspace).resolve()
    for folder, kind in configured_workspaces(config, ["codex", "claude-code"]) if config else []:
        if Path(folder).resolve() == target:
            return kind
    return "claude-code" if (target / "CLAUDE.md").exists() else "codex"


def instructions_target(context: Mapping[str, Any]) -> str:
    """The project's instructions file in this agent's clone: its instructions file, else its environment file."""

    for field in ("local_project_instructions", "local_project_environment"):
        value = str(context.get(field) or "")
        if value and Path(value).is_file():
            return value
    return ""


__all__ = [
    "always_on_rules", "configured_workspaces", "ensure_workspace_instructions", "instructions_target",
    "render_block", "render_body", "runtime_kind_of", "verify_workspace_instructions",
]
