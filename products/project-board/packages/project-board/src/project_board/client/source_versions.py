"""The published project-board versions, and which of them this machine has (W322).

`pb source versions` answers the three questions of an update or a roll back:
what is published, what is installed here, and what runs now. It reads the
package index through its standard JSON simple API (PEP 691), the same index
`pip` and `pb source use-release` install from, and it reads the installed
releases from the host's release store. It changes nothing; it prints the
exact `pb source use-release --expect-version <version>` line for the chosen
version, the newest by default.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from packaging.version import InvalidVersion, Version

from ..contract.errors import DomainError
from .release_install import active_release_id, installed_environment, releases_path

SCHEMA = "project-board.client-source-versions.v1"
DISTRIBUTION = "project-board"
DEFAULT_INDEX = "https://pypi.org/simple"
SIMPLE_JSON = "application/vnd.pypi.simple.v1+json"

Fetch = Callable[[str], Mapping[str, Any]]


def index_url() -> str:
    """The index `pip` would use: PIP_INDEX_URL when set, else PyPI."""

    return (os.environ.get("PIP_INDEX_URL") or DEFAULT_INDEX).rstrip("/")


def fetch_simple_json(url: str) -> Mapping[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": SIMPLE_JSON})
    try:
        with urllib.request.urlopen(request, timeout=30) as reply:
            return json.load(reply)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise DomainError(
            "work_client_versions_index_unavailable",
            f"The package index did not answer at {url}; the installed versions are still listed by pb source status.",
            status=503,
            details={"index": url, "reason": type(exc).__name__},
        ) from exc


def _yanked(files: list[Mapping[str, Any]], version: str) -> bool:
    """A version is yanked when every file of it is (PEP 592 in the JSON API)."""

    marked = []
    for row in files:
        name = str(row.get("filename") or "")
        if f"-{version}-" in name or f"-{version}.tar" in name or name.endswith(f"-{version}.zip"):
            marked.append(bool(row.get("yanked")))
    return bool(marked) and all(marked)


def published_versions(fetch: Fetch = fetch_simple_json, *, index: str | None = None) -> tuple[str, list[str], list[str]]:
    """(index, published versions newest first, yanked versions)."""

    base = (index or index_url()).rstrip("/")
    answer = fetch(f"{base}/{DISTRIBUTION}/")
    parsed: list[Version] = []
    for value in answer.get("versions") or []:
        try:
            parsed.append(Version(str(value)))
        except InvalidVersion:
            continue
    files = [row for row in answer.get("files") or [] if isinstance(row, Mapping)]
    ordered = [str(version) for version in sorted(set(parsed), reverse=True)]
    yanked = [version for version in ordered if _yanked(files, version)]
    return base, ordered, yanked


def installed_versions(root: str | Path) -> tuple[dict[str, str], str, dict[str, Any]]:
    """({version: release id} installed from the index, active version, active source)."""

    installed: dict[str, str] = {}
    releases = releases_path(root)
    if releases.is_dir():
        for entry in sorted(releases.iterdir()):
            if entry.is_symlink() or not entry.is_dir() or entry.name.startswith("."):
                continue
            source = dict(installed_environment(entry).get("source") or {})
            if source.get("mode") == "released" and source.get("version"):
                installed[str(Version(str(source["version"])))] = entry.name
    active_id = active_release_id(root)
    active_source: dict[str, Any] = {}
    if active_id:
        active_source = dict(installed_environment(releases / active_id).get("source") or {})
    active_version = (
        str(Version(str(active_source["version"])))
        if active_source.get("mode") == "released" and active_source.get("version")
        else ""
    )
    return installed, active_version, active_source


def use_release_line(version: str) -> str:
    return f"pb source use-release --expect-version {version}"


def source_versions(
    root: str | Path,
    *,
    choose: str = "",
    fetch: Fetch = fetch_simple_json,
    index: str | None = None,
) -> dict[str, Any]:
    base, published, yanked = published_versions(fetch, index=index)
    installed, active_version, active_source = installed_versions(root)
    offered = [version for version in published if version not in yanked]
    if choose:
        try:
            chosen = str(Version(choose))
        except InvalidVersion as exc:
            raise DomainError(
                "work_client_version_invalid",
                f"{choose!r} is not a package version.",
                status=400,
                details={"version": choose},
            ) from exc
        if chosen not in published:
            raise DomainError(
                "work_client_version_not_published",
                f"project-board {chosen} is not on the package index ({base}).",
                status=404,
                details={"version": chosen, "index": base, "published": published[:10]},
            )
    else:
        chosen = offered[0] if offered else ""
    rows = [
        {
            "version": version,
            "installed": version in installed,
            "active": version == active_version,
            "yanked": version in yanked,
        }
        for version in published
    ]
    active = (
        {"mode": "released", "version": active_version}
        if active_version
        else {"mode": str(active_source.get("mode") or ""), "release_id": active_release_id(root)}
    )
    return {
        "schema": SCHEMA,
        "index": base,
        "release_root": str(Path(root)),
        "versions": rows,
        "latest": offered[0] if offered else "",
        "active": active,
        "chosen": chosen,
        "use_release": use_release_line(chosen) if chosen else "",
        "note": (
            "Already active; nothing to change."
            if chosen and chosen == active_version
            else "Run the use_release line to switch pb and the relay to the chosen version. "
            "The most recent releases stay installed, so the same command with the "
            "previous version returns to it."
            if chosen
            else "The index lists no version of project-board."
        ),
    }
