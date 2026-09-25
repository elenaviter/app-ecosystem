from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from project_board.client import release_install, relay_source


def _wheel(
    root: Path,
    *,
    distribution: str,
    version: str,
    files: dict[str, str],
    requires: tuple[str, ...] = (),
    scripts: dict[str, str] | None = None,
) -> Path:
    normalized = distribution.replace("-", "_")
    dist_info = f"{normalized}-{version}.dist-info"
    wheel = root / f"{normalized}-{version}-py3-none-any.whl"
    metadata = [
        "Metadata-Version: 2.1",
        f"Name: {distribution}",
        f"Version: {version}",
        *(f"Requires-Dist: {requirement}" for requirement in requires),
        "",
    ]
    payload = {
        **files,
        f"{dist_info}/METADATA": "\n".join(metadata),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: project-board-tests\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ),
    }
    if scripts:
        payload[f"{dist_info}/entry_points.txt"] = "[console_scripts]\n" + "".join(
            f"{name} = {target}\n" for name, target in scripts.items()
        )
    payload[f"{dist_info}/RECORD"] = "".join(
        f"{name},,\n" for name in [*payload, f"{dist_info}/RECORD"]
    )
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in payload.items():
            archive.writestr(name, content)
    return wheel


def test_a_release_environment_resolves_and_smokes_a_new_dependency(
    tmp_path: Path,
) -> None:
    dependency = _wheel(
        tmp_path,
        distribution="fixture-dependency",
        version="4.2",
        files={"fixture_dependency.py": "VALUE = 'installed with release'\n"},
    )
    project_board = _wheel(
        tmp_path,
        distribution="project-board",
        version="1.2.3",
        files={
            "project_board/__init__.py": (
                "from importlib.metadata import version\n"
                "def main():\n"
                "    print(f\"problem-board {version('project-board')}\")\n"
                "    return 0\n"
            )
        },
        requires=(f"fixture-dependency @ {dependency.as_uri()}",),
        scripts={"pb": "project_board:main"},
    )
    root = tmp_path / "client"
    release_id = "a" * 64

    try:
        installed = release_install.install_release_environment(
            root=root,
            release_id=release_id,
            requirements=(project_board,),
            source={"mode": "released", "version": "1.2.3"},
            base_python=Path(sys.executable),
            smoke_imports=("project_board", "fixture_dependency"),
            expected_project_board_version="1.2.3",
        )
    except release_install.ReleaseInstallError as exc:
        pytest.fail(json.dumps(exc.details, indent=2, sort_keys=True))

    python = Path(installed["environment"]["python"])
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONHOME", "PYTHONPATH"}
    }
    dependency_probe = subprocess.run(
        (str(python), "-c", "import fixture_dependency; print(fixture_dependency.VALUE)"),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    package_probe = subprocess.run(
        (
            str(python),
            "-c",
            "import project_board; print(project_board.__file__)",
        ),
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    source = relay_source.describe_source(Path(package_probe.stdout.strip()))

    assert dependency_probe.stdout.strip() == "installed with release"
    assert installed["environment"]["project_board_version"] == "1.2.3"
    assert source["mode"] == "released"
    assert source["release_id"] == release_id


def test_default_published_smoke_uses_the_candidate_owned_runtime_contract(
    tmp_path: Path,
) -> None:
    project_board = _wheel(
        tmp_path,
        distribution="project-board",
        version="2.0",
        files={
            "project_board/__init__.py": (
                "from importlib.metadata import version\n"
                "def main():\n"
                "    print(f\"problem-board {version('project-board')}\")\n"
                "    return 0\n"
            ),
            "project_board/client/__init__.py": "",
            "project_board/client/release_smoke.py": "CONTRACT = 'candidate'\n",
        },
        scripts={"pb": "project_board:main"},
    )

    installed = release_install.install_release_environment(
        root=tmp_path / "client",
        release_id="d" * 64,
        requirements=(project_board,),
        source={"mode": "released", "version": "2.0"},
        base_python=Path(sys.executable),
        expected_project_board_version="2.0",
    )

    assert installed["environment"]["imports"] == [
        "project_board.client.release_smoke"
    ]


def test_broken_noncurrent_complete_environment_is_rebuilt(tmp_path: Path) -> None:
    project_board = _wheel(
        tmp_path,
        distribution="project-board",
        version="2.1",
        files={
            "project_board/__init__.py": (
                "from importlib.metadata import version\n"
                "def main():\n"
                "    print(f\"problem-board {version('project-board')}\")\n"
                "    return 0\n"
            ),
            "project_board/client/__init__.py": "",
            "project_board/client/release_smoke.py": "CONTRACT = 'candidate'\n",
        },
        scripts={"pb": "project_board:main"},
    )
    root = tmp_path / "client"
    release_id = "e" * 64
    first = release_install.install_release_environment(
        root=root,
        release_id=release_id,
        requirements=(project_board,),
        source={"mode": "released", "version": "2.1"},
        base_python=Path(sys.executable),
        expected_project_board_version="2.1",
    )
    pb = Path(first["environment"]["pb"])
    pb.write_text("#!/bin/sh\nexit 23\n", encoding="utf-8")
    pb.chmod(0o755)

    rebuilt = release_install.install_release_environment(
        root=root,
        release_id=release_id,
        requirements=(project_board,),
        source={"mode": "released", "version": "2.1"},
        base_python=Path(sys.executable),
        expected_project_board_version="2.1",
    )

    assert rebuilt["reused"] is False
    clean_environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONHOME", "PYTHONPATH"}
    }
    assert subprocess.run(
        (str(rebuilt["environment"]["pb"]), "--version"),
        check=True,
        capture_output=True,
        text=True,
        env=clean_environment,
    ).stdout.strip() == "problem-board 2.1"


def test_smoke_import_cannot_be_satisfied_by_the_release_working_directory(
    tmp_path: Path,
) -> None:
    project_board = _wheel(
        tmp_path,
        distribution="project-board",
        version="2.2",
        files={
            "project_board/__init__.py": (
                "from importlib.metadata import version\n"
                "def main():\n"
                "    print(f\"problem-board {version('project-board')}\")\n"
                "    return 0\n"
            )
        },
        scripts={"pb": "project_board:main"},
    )
    root = tmp_path / "client"
    release_id = "f" * 64
    release = release_install.release_path(root, release_id)
    release.mkdir(parents=True)
    (release / "cwd_only.py").write_text("VALUE = 'not installed'\n", encoding="utf-8")

    with pytest.raises(release_install.ReleaseInstallError) as failure:
        release_install.install_release_environment(
            root=root,
            release_id=release_id,
            requirements=(project_board,),
            source={"mode": "released", "version": "2.2"},
            base_python=Path(sys.executable),
            smoke_imports=("cwd_only",),
            expected_project_board_version="2.2",
        )

    assert failure.value.code == "work_client_release_install_failed"


def test_failed_smoke_keeps_the_previous_release_active(tmp_path: Path) -> None:
    root = tmp_path / "client"
    previous_id = "b" * 64
    previous = release_install.release_path(root, previous_id)
    commands = previous / "venv" / "bin"
    commands.mkdir(parents=True)
    for name in ("python", "pb"):
        command = commands / name
        command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        command.chmod(0o755)
    release_install._atomic_write_json(
        previous / release_install.INSTALLATION_MARKER,
        {
            "schema": release_install.INSTALLATION_SCHEMA,
            "release_id": previous_id,
            "source": {"mode": "released", "version": "1"},
            "installed_at": "2026-09-25T00:00:00Z",
            "activated_at": "",
            "environment": {
                "path": str(previous / "venv"),
                "python": str(commands / "python"),
                "pb": str(commands / "pb"),
            },
            "launcher_version": release_install.LAUNCHER_VERSION,
        },
    )
    release_install.activate_installed_release(root, previous_id)
    broken = _wheel(
        tmp_path,
        distribution="project-board",
        version="2.0",
        files={"project_board/__init__.py": "VALUE = 2\n"},
        scripts={"pb": "project_board:main"},
    )

    with pytest.raises(release_install.ReleaseInstallError) as failure:
        release_install.install_release_environment(
            root=root,
            release_id="c" * 64,
            requirements=(broken,),
            source={"mode": "released", "version": "2.0"},
            base_python=Path(sys.executable),
            smoke_imports=("project_board", "missing_dependency"),
            expected_project_board_version="2.0",
        )

    assert failure.value.code == "work_client_release_install_failed"
    assert release_install.active_release_id(root) == previous_id
    assert not (release_install.release_path(root, "c" * 64) / "venv").exists()


def test_activation_metadata_failure_keeps_the_previous_release_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "client"
    identities = ("a" * 64, "b" * 64)
    for identity in identities:
        release = release_install.release_path(root, identity)
        commands = release / "venv" / "bin"
        commands.mkdir(parents=True)
        for name in ("python", "pb"):
            command = commands / name
            command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            command.chmod(0o755)
        release_install._atomic_write_json(
            release / release_install.INSTALLATION_MARKER,
            {
                "schema": release_install.INSTALLATION_SCHEMA,
                "release_id": identity,
                "source": {"mode": "released", "version": identity[0]},
                "installed_at": "2026-09-25T00:00:00Z",
                "activated_at": "",
                "environment": {
                    "path": str(release / "venv"),
                    "python": str(commands / "python"),
                    "pb": str(commands / "pb"),
                },
                "launcher_version": release_install.LAUNCHER_VERSION,
            },
        )
    release_install.activate_installed_release(root, identities[0])

    def fail_write(_path: Path, _value: object) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(release_install, "_atomic_write_json", fail_write)

    with pytest.raises(OSError, match="disk unavailable"):
        release_install.activate_installed_release(root, identities[1])

    assert release_install.active_release_id(root) == identities[0]


def test_retention_keeps_current_and_the_three_latest_environments(
    tmp_path: Path,
) -> None:
    root = tmp_path / "client"
    identities = [character * 64 for character in "abcd"]
    for index, identity in enumerate(identities):
        release = release_install.release_path(root, identity)
        commands = release / "venv" / "bin"
        commands.mkdir(parents=True)
        for name in ("python", "pb"):
            (commands / name).write_text("command\n", encoding="utf-8")
        release_install._atomic_write_json(
            release / release_install.INSTALLATION_MARKER,
            {
                "schema": release_install.INSTALLATION_SCHEMA,
                "release_id": identity,
                "source": {"mode": "released", "version": str(index)},
                "installed_at": f"2026-09-25T00:00:0{index}Z",
                "activated_at": f"2026-09-25T00:00:0{index}Z",
                "environment": {
                    "path": str(release / "venv"),
                    "python": str(commands / "python"),
                    "pb": str(commands / "pb"),
                },
                "launcher_version": release_install.LAUNCHER_VERSION,
            },
        )
    release_install.activate_installed_release(root, identities[-1])

    removed = release_install.prune_installed_releases(root, retain=3)

    assert removed == [identities[0]]
    assert release_install.active_release_id(root) == identities[-1]
    assert all(
        release_install.release_path(root, identity).is_dir()
        for identity in identities[1:]
    )
