#!/usr/bin/env python3
"""Install the Project Board host client from clean, approved source exports."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

SOURCE_MANIFEST_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "project_board"
    / "client"
    / "source_manifest.py"
)
RELEASE_INSTALL_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "project_board"
    / "client"
    / "release_install.py"
)


def _load_module(name: str, path: Path):
    # The installer runs before the package dependencies exist, so load the
    # owning modules without executing project_board.client.__init__.
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Cannot load the Project Board installer module at {path}.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_SOURCE_MANIFEST = _load_module(
    "_project_board_source_manifest", SOURCE_MANIFEST_PATH
)
_RELEASE_INSTALL = _load_module(
    "_project_board_release_install", RELEASE_INSTALL_PATH
)
APP_ECOSYSTEM_COMPONENT = _SOURCE_MANIFEST.APP_ECOSYSTEM_COMPONENT
KDCUBE_COMPONENT = _SOURCE_MANIFEST.KDCUBE_COMPONENT
SOURCE_PATHS_BY_COMPONENT = _SOURCE_MANIFEST.SOURCE_PATHS_BY_COMPONENT

LAUNCHER_MARKER = _RELEASE_INSTALL.LAUNCHER_MARKER
LEGACY_LAUNCHER_MARKER = _RELEASE_INSTALL.LEGACY_LAUNCHER_MARKERS[-1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Install all first-party Project Board client distributions from "
            "clean App Ecosystem and KDCube source exports."
        )
    )
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--kdcube-source-root", required=True)
    parser.add_argument(
        "--release-root",
        default=str(_RELEASE_INSTALL.default_release_root()),
    )
    parser.add_argument(
        "--command-dir",
        default=str(Path.home() / ".local" / "bin"),
    )
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--force-launcher", action="store_true")
    return parser


def _first_party_packages(
    source_root: Path, kdcube_source_root: Path
) -> tuple[Path, ...]:
    roots = {
        APP_ECOSYSTEM_COMPONENT: source_root,
        KDCUBE_COMPONENT: kdcube_source_root,
    }
    packages = tuple(
        roots[component] / relative
        for component in (APP_ECOSYSTEM_COMPONENT, KDCUBE_COMPONENT)
        for relative in SOURCE_PATHS_BY_COMPONENT[component]
    )
    missing = [str(path) for path in packages if not path.is_dir()]
    if missing:
        raise SystemExit(
            "The source exports are missing required package directories: "
            + ", ".join(missing)
        )
    return packages


def _launcher_text(pb_command: Path) -> str:
    return _RELEASE_INSTALL._launcher_text(pb_command)


def _launcher_is_owned(launcher: Path, pb_command: Path) -> bool:
    return _RELEASE_INSTALL._launcher_is_owned(launcher, pb_command)


def _install_launcher(
    *,
    command_dir: Path,
    pb_command: Path,
    force: bool,
) -> Path:
    try:
        return _RELEASE_INSTALL.install_launcher(
            command_dir / "pb",
            expected_pb=pb_command,
            force=force,
        )
    except _RELEASE_INSTALL.ReleaseInstallError as exc:
        raise SystemExit(f"{exc}; use --force-launcher after reviewing it.") from exc


def _launcher_path(*, command_dir: Path, pb_command: Path, force: bool) -> Path:
    try:
        return _RELEASE_INSTALL.validate_launcher(
            command_dir / "pb",
            expected_pb=pb_command,
            force=force,
        )
    except _RELEASE_INSTALL.ReleaseInstallError as exc:
        raise SystemExit(f"{exc}; use --force-launcher after reviewing it.") from exc


def install(
    *,
    source_root: Path,
    kdcube_source_root: Path,
    release_root: Path,
    command_dir: Path,
    base_python: Path,
    force_launcher: bool,
) -> dict[str, object]:
    packages = _first_party_packages(source_root, kdcube_source_root)
    labels = tuple(
        relative
        for component in (APP_ECOSYSTEM_COMPONENT, KDCUBE_COMPONENT)
        for relative in SOURCE_PATHS_BY_COMPONENT[component]
    )
    release_id = _RELEASE_INSTALL.source_release_id(zip(labels, packages))
    source = {
        "mode": "snapshot",
        "release_id": release_id,
        "bootstrap": True,
        "packages": list(labels),
    }
    try:
        installation = _RELEASE_INSTALL.install_release_environment(
            root=release_root,
            release_id=release_id,
            requirements=packages,
            source=source,
            base_python=base_python,
        )
    except _RELEASE_INSTALL.ReleaseInstallError as exc:
        raise SystemExit(f"{exc.code}: {exc}") from exc
    pb_command = _RELEASE_INSTALL.active_pb(release_root)
    _launcher_path(
        command_dir=command_dir,
        pb_command=pb_command,
        force=force_launcher,
    )
    try:
        previous = _RELEASE_INSTALL.activate_installed_release(
            release_root, release_id
        )
    except _RELEASE_INSTALL.ReleaseInstallError as exc:
        raise SystemExit(f"{exc.code}: {exc}") from exc
    try:
        launcher = _install_launcher(
            command_dir=command_dir,
            pb_command=pb_command,
            force=force_launcher,
        )
    except BaseException:
        _RELEASE_INSTALL.activate_installed_release(release_root, previous)
        raise
    pruned = _RELEASE_INSTALL.prune_installed_releases(
        release_root,
        keep=(release_id, previous),
    )
    return {
        "command": str(launcher),
        "release_root": str(release_root),
        "release_id": release_id,
        "environment": installation["environment"],
        "previous_release_id": previous,
        "pruned_releases": pruned,
        "first_party_packages": [str(path) for path in packages],
        "installed_source": source,
        "ready": True,
    }


def main() -> int:
    args = _parser().parse_args()
    result = install(
        source_root=Path(args.source_root).expanduser().resolve(),
        kdcube_source_root=Path(args.kdcube_source_root).expanduser().resolve(),
        release_root=Path(args.release_root).expanduser().resolve(),
        command_dir=Path(args.command_dir).expanduser().resolve(),
        base_python=Path(args.python).expanduser().resolve(),
        force_launcher=bool(args.force_launcher),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
