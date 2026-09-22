"""Console entry point for the Project Board client."""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from project_board.client import cli
from project_board.client.host_config import resolve_host_config_path
from project_board.client.relay_source import (
    CLIENT_SOURCE_PATHS,
    client_source_root,
    describe_source,
    selected_release,
)
from project_board.client.render import render_envelope
from project_board.client.source_control import effective_selection, source_matches
from project_board.contract.errors import DomainError


class _UtcPerLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        timestamp = (
            datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        prefix = f"{timestamp} {record.levelname} {record.name}: "
        return "\n".join(
            f"{prefix}{line}" for line in (rendered.splitlines() or [""])
        )


def _top_level_command(argv: list[str]) -> str:
    skip_value = False
    for token in argv:
        if skip_value:
            skip_value = False
            continue
        if token == "--format":
            skip_value = True
            continue
        if token.startswith("--format=") or token.startswith("-"):
            continue
        return token
    return ""


def _configure_relay_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_UtcPerLineFormatter("%(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def _config_argument(argv: list[str]) -> Path | None:
    selected = ""
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--config" and index + 1 < len(argv):
            selected = argv[index + 1]
            index += 2
            continue
        if token.startswith("--config="):
            selected = token.partition("=")[2]
        index += 1
    try:
        return resolve_host_config_path(selected or None)
    except DomainError:
        return None


def _selected_command(
    argv: list[str],
    *,
    current_source: Mapping[str, object] | None = None,
    config_path: Path | None = None,
) -> tuple[str, ...] | None:
    """The selected snapshot invocation, or ``None`` for this source.

    Source-management commands always execute in the released bootstrap, so a
    bad code snapshot cannot prevent an operator from selecting the release.
    Checkout and snapshot entry points are explicit development/pinned paths
    and therefore never re-dispatch themselves.
    """

    if _top_level_command(argv) == "source":
        return None
    observed = dict(
        current_source
        or describe_source(Path(__file__), scope_paths=CLIENT_SOURCE_PATHS)
    )
    if observed.get("mode") != "released":
        return None
    config = config_path or _config_argument(argv)
    if config is None:
        return None
    root = client_source_root(config)
    selected = effective_selection(root, release_source=observed)
    if selected.get("mode") == "released":
        if not source_matches(observed, selected):
            raise DomainError(
                "work_client_release_selection_mismatch",
                "The installed project-board package differs from the selected released version. Run pb source use-release with the approved installed version.",
                details={"selected": selected, "installed": observed},
            )
        return None
    release = selected_release(root, selected)
    return (sys.executable, str(release.script), *argv)


def _brief_requested(argv: list[str]) -> bool:
    for index, token in enumerate(argv):
        if token == "--format" and index + 1 < len(argv):
            return argv[index + 1] == "brief"
        if token.startswith("--format="):
            return token.partition("=")[2] == "brief"
    return False


def _render_startup_error(error: DomainError, argv: list[str]) -> int:
    envelope = {"ok": False, "error": error.to_dict()}
    if _brief_requested(argv):
        print(render_envelope(envelope), end="")
    else:
        import json

        print(json.dumps(envelope, sort_keys=True), file=sys.stderr)
    return 1


def main() -> int:
    argv = sys.argv[1:]
    try:
        selected = _selected_command(argv)
    except DomainError as exc:
        return _render_startup_error(exc, argv)
    if selected is not None:
        os.execv(selected[0], list(selected))
        return 1
    if _top_level_command(argv) == "relay":
        _configure_relay_logging()
    return int(cli.main())


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
