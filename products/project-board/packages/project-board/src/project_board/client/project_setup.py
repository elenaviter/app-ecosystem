"""A project's setup, as the project declares it (W262).

A project names its instructions file and its runtimes in ``project-setup.json``
at the root of its journal home, reviewed like any journal change. The fields
are the ones the project's Control Card will hold once setup is configured
there; until then a person writes this file by hand, and only where the file is
read from changes later.

A runtime is a named place where the project's system runs and the team acts
on it (a KDCube deployment, a staging server, a device). Each of its actions
names who may trigger it and, per repository it loads, the git ref it releases: an action never stages
whatever a working tree holds. A runtime may name a profile, a procedure
document with the commands for that kind of runtime, so the generic worker
procedure carries none of them.

Reading never fails the context. A missing file gives empty fields; an entry
that cannot be read is left out and named in ``project_setup_issues``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Mapping

PROJECT_SETUP_FILE = "project-setup.json"
PROJECT_SETUP_SCHEMA = "problem-board.project-setup.v1"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# A repository alias, as in repo:<alias>/... refs.
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# A git ref as a person writes it: a branch, a tag, origin/main, or a commit.
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,199}$")
_TEXT_MAX = 200
MAX_RUNTIMES = 16
MAX_ACTIONS = 32

Resolver = Callable[[str], str]


def empty_project_setup() -> dict[str, Any]:
    return {
        "project_setup_ref": "",
        "project_instructions_ref": "",
        "local_project_instructions": "",
        "runtimes": [],
        "project_setup_issues": [],
    }


def _text(value: Any) -> str:
    text = str(value or "").strip() if isinstance(value, (str, int)) else ""
    if not text or len(text) > _TEXT_MAX:
        return ""
    # One line of printable text: a control character is never a name or a ref.
    return "" if any(ord(char) < 32 or ord(char) == 127 for char in text) else text


def _repo_ref(value: Any) -> str:
    text = _text(value)
    return text if text.startswith("repo:") else ""


def _local(resolve: Resolver, ref: str) -> str:
    """The file on this host, or "" when its checkout is not mapped here."""

    try:
        path = resolve(ref)
    except Exception:  # noqa: BLE001 - an unmapped alias is a normal state
        return ""
    return path if path and Path(path).is_file() else ""


def _action(name: str, raw: Any, issues: list[str], where: str) -> dict[str, Any] | None:
    if not _NAME_RE.fullmatch(name):
        issues.append(f"{where}: action name {name!r} is not a short lowercase name")
        return None
    if not isinstance(raw, Mapping):
        issues.append(f"{where}.{name}: not an object")
        return None
    # An action releases one ref per repository it loads: a platform refresh
    # loads the platform and the packages it stages, an app reload its app.
    # Without the repository, "the result names the commit" names nothing.
    releases_raw = raw.get("releases")
    if not isinstance(releases_raw, list) or not releases_raw:
        issues.append(f"{where}.{name}: releases must list each repository and the git ref it releases")
        return None
    releases = []
    for release in releases_raw:
        repository = _text(release.get("repository")) if isinstance(release, Mapping) else ""
        ref = _text(release.get("ref")) if isinstance(release, Mapping) else ""
        if not _ALIAS_RE.fullmatch(repository) or not _REF_RE.fullmatch(ref):
            issues.append(f"{where}.{name}: each release names a repository alias and the git ref it releases")
            return None
        releases.append({"repository": repository, "ref": ref})
    who = raw.get("who")
    if not isinstance(who, list) or not who or not all(_text(item) for item in who):
        issues.append(f"{where}.{name}: who must list who may trigger it")
        return None
    return {"name": name, "who": [_text(item) for item in who], "releases": releases}


def _runtime(raw: Any, index: int, resolve: Resolver, issues: list[str]) -> dict[str, Any] | None:
    where = f"runtimes[{index}]"
    if not isinstance(raw, Mapping):
        issues.append(f"{where}: not an object")
        return None
    name = _text(raw.get("name"))
    if not _NAME_RE.fullmatch(name):
        issues.append(f"{where}: name must be a short lowercase name")
        return None
    host = _text(raw.get("host"))
    if not host:
        issues.append(f"{where} ({name}): host must name where actions are triggered")
        return None
    profile_ref = ""
    if raw.get("profile_ref") not in (None, ""):
        profile_ref = _repo_ref(raw.get("profile_ref"))
        if not profile_ref:
            issues.append(f"{where} ({name}): profile_ref must be a repo: reference")
            return None
    actions_raw = raw.get("actions") or {}
    if not isinstance(actions_raw, Mapping):
        issues.append(f"{where} ({name}): actions must map an action name to its rule")
        return None
    actions = []
    if len(actions_raw) > MAX_ACTIONS:
        issues.append(f"{where} ({name}): only the first {MAX_ACTIONS} actions are read")
    for action_name, action_raw in list(actions_raw.items())[:MAX_ACTIONS]:
        action = _action(str(action_name), action_raw, issues, f"{where} ({name}).actions")
        if action is not None:
            actions.append(action)
    return {
        "name": name,
        "host": host,
        "kind": _text(raw.get("kind")),
        "profile_ref": profile_ref,
        "local_profile": _local(resolve, profile_ref) if profile_ref else "",
        "actions": actions,
    }


def read_project_setup(path: Path, *, setup_ref: str, resolve: Resolver) -> dict[str, Any]:
    """The project's declared setup, from ``project-setup.json`` at ``path``."""

    result = empty_project_setup()
    if not path.is_file():
        return result
    result["project_setup_ref"] = setup_ref
    issues: list[str] = result["project_setup_issues"]
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        issues.append(f"{PROJECT_SETUP_FILE} is not readable JSON: {type(exc).__name__}")
        return result
    if not isinstance(value, Mapping) or value.get("schema") != PROJECT_SETUP_SCHEMA:
        issues.append(f"{PROJECT_SETUP_FILE} must be an object with schema {PROJECT_SETUP_SCHEMA}")
        return result
    if value.get("instructions_ref") not in (None, ""):
        instructions = _repo_ref(value.get("instructions_ref"))
        if instructions:
            result["project_instructions_ref"] = instructions
            result["local_project_instructions"] = _local(resolve, instructions)
        else:
            issues.append("instructions_ref must be a repo: reference")
    runtimes_raw = value.get("runtimes") or []
    if not isinstance(runtimes_raw, list):
        issues.append("runtimes must be a list")
        runtimes_raw = []
    seen: set[str] = set()
    if len(runtimes_raw) > MAX_RUNTIMES:
        issues.append(f"only the first {MAX_RUNTIMES} runtimes are read")
    for index, raw in enumerate(runtimes_raw[:MAX_RUNTIMES]):
        runtime = _runtime(raw, index, resolve, issues)
        if runtime is None:
            continue
        if runtime["name"] in seen:
            issues.append(f"runtimes[{index}]: name {runtime['name']!r} is declared twice")
            continue
        seen.add(runtime["name"])
        result["runtimes"].append(runtime)
    return result


__all__ = [
    "PROJECT_SETUP_FILE",
    "PROJECT_SETUP_SCHEMA",
    "empty_project_setup",
    "read_project_setup",
]
