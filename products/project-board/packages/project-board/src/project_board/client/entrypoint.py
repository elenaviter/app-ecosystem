"""Console entry point for the Project Board client."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Mapping

from project_board.client import cli
from project_board.client.host_config import resolve_host_config_path
from project_board.client.relay_source import (
    CLIENT_SOURCE_PATHS,
    client_release_root,
    client_source_root,
    describe_source,
    selected_release,
)
from project_board.client.release_install import VENV_DIR, default_release_root
from project_board.client.relay_logging import (
    configure_relay_logging as configure_relay_file_logging,
)
from project_board.client.claude_settings import INVOKED_PB_ENV
from project_board.client.render import render_envelope
from project_board.client.source_control import effective_selection, source_matches
from project_board.contract.errors import DomainError


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


def _procedure_install(argv: list[str]) -> bool:
    """Whether this is `pb procedure install` (W554)."""

    words: list[str] = []
    skip_value = False
    for token in argv:
        if skip_value:
            skip_value = False
            continue
        if token == "--format":
            skip_value = True  # `--format brief` may stand anywhere on the line
            continue
        if token.startswith("-"):
            continue
        words.append(token)
    return words[:2] == ["procedure", "install"]


def _configure_relay_logging(config_path: Path | None) -> None:
    configure_relay_file_logging(
        config_path,
        mirror_to_stderr=bool(getattr(sys.stderr, "isatty", lambda: False)()),
    )


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

    Source-management commands execute in the current installed environment,
    so a bad target receipt cannot prevent an operator from selecting a release.
    Checkout and snapshot entry points are explicit development/pinned paths
    and therefore never re-dispatch themselves.
    """

    if _top_level_command(argv) == "source" or "--version" in argv:
        return None
    observed = dict(
        current_source
        or describe_source(Path(__file__), scope_paths=CLIENT_SOURCE_PATHS)
    )
    if observed.get("mode") != "released" or observed.get("release_id"):
        return None
    config = config_path or _config_argument(argv)
    if config is None:
        return None
    root = client_source_root(config)
    selected = effective_selection(root, release_source=observed)
    if selected.get("mode") == "released":
        if not source_matches(observed, selected) and not _procedure_install(argv):
            # W554: `pb procedure install` run by the package just installed
            # makes it this host's pb, so it runs here instead of refusing.
            raise DomainError(
                "work_client_release_selection_mismatch",
                "The installed project-board package differs from the selected released version. Run pb source use-release with the approved installed version.",
                details={"selected": selected, "installed": observed},
            )
        return None
    try:
        release = selected_release(
            root,
            selected,
            release_roots=(client_release_root(config), default_release_root()),
        )
    except DomainError as exc:
        if exc.code != "work_client_source_release_missing":
            raise
        raise _missing_snapshot(exc, observed) from exc
    # W378: the snapshot runs in the environment the host installed for it,
    # when there is one, not in the released bootstrap that found it.
    python = release.path / VENV_DIR / "bin" / "python"
    return (str(python) if python.is_file() else sys.executable, str(release.script), *argv)


def _missing_snapshot(error: DomainError, observed: Mapping[str, object]) -> DomainError:
    """The refusal a person can act on (W378): what was found, and the one command next."""

    version = str(observed.get("version") or "")
    launcher = Path.home() / ".local" / "bin" / "pb"
    next_command = f"pb source use-release --expect-version {version}" if version else "pb source versions"
    lines = [str(error)]
    if launcher.exists() and Path(sys.argv[0]).resolve() != launcher.resolve():
        lines.append(f"This machine's own pb is {launcher}; run that one to keep the snapshot.")
    lines.append(f"To run this released pb {version} instead, run: {next_command}")
    return DomainError(
        error.code,
        " ".join(lines),
        details={
            **dict(error.details or {}),
            "installed_version": version,
            "launcher": str(launcher) if launcher.exists() else "",
            "next_command": next_command,
        },
    )


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
    relay_command = _top_level_command(argv) == "relay"
    config_path = _config_argument(argv) if relay_command else None
    if relay_command:
        # Configure before source dispatch so bootstrap failures and the first
        # selected-source line use the same bounded host log.
        _configure_relay_logging(config_path)
    try:
        selected = _selected_command(argv, config_path=config_path)
    except DomainError as exc:
        return _render_startup_error(exc, argv)
    if selected is not None:
        # The release runs as a script, so its argv[0] names no executable;
        # hand it the pb that launched it, for the hooks that must name this
        # host's pb by path (W304: pb procedure install through the launcher).
        invoked = os.path.abspath(sys.argv[0])
        if not invoked.endswith(".py"):
            os.environ[INVOKED_PB_ENV] = invoked
        os.execv(selected[0], list(selected))
        return 1
    return int(cli.main())


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
