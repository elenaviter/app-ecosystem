"""Run Project Board from one exported multi-repository source manifest.

This file is executed directly from a content-addressed release. It adds only
the package sources named by that release marker, then enters the normal
console entry point. The live checkout is never placed on ``sys.path``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


RELEASE_MARKER = "release.json"
MARKER_SEARCH_DEPTH = 16


def _release_root(script: Path) -> tuple[Path, dict[str, object]]:
    probe = script.resolve().parent
    for _ in range(MARKER_SEARCH_DEPTH):
        marker = probe / RELEASE_MARKER
        if marker.is_file():
            data = json.loads(marker.read_text(encoding="utf-8"))
            if str(data.get("schema") or "").startswith(
                "project-board.client-source-release."
            ):
                return probe, data
        if probe.parent == probe:
            break
        probe = probe.parent
    raise RuntimeError("The Project Board code entry point has no release marker.")


def _source_roots(release: Path, marker: dict[str, object]) -> list[str]:
    roots: list[str] = []
    raw_paths = marker.get("source_paths")
    if not isinstance(raw_paths, list):
        raise RuntimeError("The Project Board release marker has no source_paths list.")
    for raw in raw_paths:
        relative = Path(str(raw or ""))
        if not str(relative) or relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError("The Project Board release marker has an unsafe source path.")
        source = release / relative / "src"
        if not source.is_dir():
            raise RuntimeError(f"The Project Board release source is missing: {relative}/src")
        roots.append(str(source))
    return roots


def main() -> int:
    release, marker = _release_root(Path(__file__))
    sys.dont_write_bytecode = True
    sys.path[:0] = _source_roots(release, marker)
    from project_board.client.entrypoint import main as client_main

    return int(client_main())


if __name__ == "__main__":
    raise SystemExit(main())
