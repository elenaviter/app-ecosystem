from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PACKAGE_ROOT / "scripts" / "prepare_test_runner.py"


def _module():
    spec = importlib.util.spec_from_file_location("prepare_test_runner", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sources(root: Path) -> tuple[Path, Path]:
    kdcube = root / "kdcube-ai-app"
    kdcube.mkdir(parents=True)
    (kdcube / "requirements-aws.txt").write_text("boto3\n", encoding="utf-8")
    (kdcube / "requirements-chat-processor.txt").write_text(
        "requests==2.32.5\n"
        "connection-hub>=2026\n"
        "project-board>=2026\n"
        "app-foundation[mcp]>=2026\n"
        "-r requirements-aws.txt\n",
        encoding="utf-8",
    )
    app_ecosystem = root / "app-ecosystem"
    project_board = app_ecosystem / "products/project-board/packages/project-board"
    project_board.mkdir(parents=True)
    (project_board / "pyproject.toml").write_text(
        "[project]\n"
        "name = \"project-board\"\n"
        "[project.optional-dependencies]\n"
        "test = [\n"
        "  \"pytest>=8,<10\",\n"
        "  \"pytest-asyncio>=0.23,<2\",\n"
        "  \"pytest-xdist>=3.8,<4\",\n"
        "]\n",
        encoding="utf-8",
    )
    return kdcube, app_ecosystem


def test_filtered_platform_requirements_replace_the_include_and_source_packages(
    tmp_path: Path,
) -> None:
    module = _module()
    kdcube, _app_ecosystem = _sources(tmp_path)

    generated, source = module._filtered_platform_requirements(kdcube)

    assert source == kdcube / "requirements-chat-processor.txt"
    assert "requests==2.32.5" in generated
    assert f"-r {(kdcube / 'requirements-aws.txt').resolve()}" in generated
    assert "connection-hub" not in generated
    assert "project-board" not in generated
    assert "app-foundation" not in generated

    (kdcube / "requirements-chat-processor.txt").write_text(
        "project-board\nrequests==2.32.5\n",
        encoding="utf-8",
    )
    generated, _source = module._filtered_platform_requirements(kdcube)
    assert generated == "requests==2.32.5\n"


def test_project_board_test_extra_is_the_xdist_source_of_truth(tmp_path: Path) -> None:
    module = _module()
    _kdcube, app_ecosystem = _sources(tmp_path)
    pyproject = (
        app_ecosystem
        / "products/project-board/packages/project-board/pyproject.toml"
    )

    assert module._read_test_requirements(pyproject) == (
        "pytest>=8,<10",
        "pytest-asyncio>=0.23,<2",
        "pytest-xdist>=3.8,<4",
    )
    assert not any("xdist" in value for value in module.OVERLAY_RUNTIME_REQUIREMENTS)


def test_project_board_test_extra_must_supply_xdist(tmp_path: Path) -> None:
    module = _module()
    _kdcube, app_ecosystem = _sources(tmp_path)
    pyproject = (
        app_ecosystem
        / "products/project-board/packages/project-board/pyproject.toml"
    )
    pyproject.write_text(
        "[project.optional-dependencies]\ntest = [\"pytest>=8,<10\"]\n",
        encoding="utf-8",
    )

    with pytest.raises(module.RunnerError, match="must declare pytest-xdist"):
        module._read_test_requirements(pyproject)


def test_prepare_reuses_a_complete_matching_runner_without_pip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    kdcube, app_ecosystem = _sources(tmp_path / "sources")
    runner_root = tmp_path / "runner"
    base_python = Path(sys.executable)
    calls: list[tuple[str, ...]] = []
    probe = {
        "interpreter": {
            "executable": str(runner_root / "venv/bin/python"),
            "implementation": "CPython",
            "version": "3.13.3",
        },
        "installed_distributions": {
            "execnet": "2.1.1",
            "pytest": "8.4.2",
            "pytest-asyncio": "1.2.0",
            "pytest-xdist": "3.8.0",
        },
        "test_dependencies": {
            "execnet": "2.1.1",
            "pytest": "8.4.2",
            "pytest-asyncio": "1.2.0",
            "pytest-xdist": "3.8.0",
        },
        "failures": [],
    }

    monkeypatch.setattr(module, "_git_commit", lambda _path: "a" * 40)
    monkeypatch.setattr(module, "_environment_probe", lambda *_args: probe)

    real_run = module._run

    def fake_run(arguments, *, capture_output=False):
        command = tuple(str(value) for value in arguments)
        if command[1:3] == ("-m", "venv"):
            runner_python = Path(command[3]) / "bin/python"
            runner_python.parent.mkdir(parents=True)
            runner_python.write_text("", encoding="utf-8")
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[1:4] == ("-m", "pip", "install"):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        return real_run(arguments, capture_output=capture_output)

    monkeypatch.setattr(module, "_run", fake_run)

    first = module.prepare(
        runner_root=runner_root,
        kdcube_root=kdcube,
        app_ecosystem_root=app_ecosystem,
        base_python=base_python,
    )
    first_calls = list(calls)
    second = module.prepare(
        runner_root=runner_root,
        kdcube_root=kdcube,
        app_ecosystem_root=app_ecosystem,
        base_python=base_python,
    )

    assert first["action"] == "prepared"
    assert second["action"] == "reused"
    assert calls == first_calls
    assert sum(command[1:3] == ("-m", "venv") for command in calls) == 1
    assert sum(command[1:4] == ("-m", "pip", "install") for command in calls) == 2
    receipt = json.loads((runner_root / "receipt.json").read_text(encoding="utf-8"))
    assert receipt["action"] == "reused"
    assert receipt["test_dependencies"]["pytest-xdist"] == "3.8.0"
    assert receipt["test_dependencies"]["execnet"] == "2.1.1"


def test_check_fails_before_pytest_when_the_dependency_inputs_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    kdcube, app_ecosystem = _sources(tmp_path / "sources")
    runner_root = tmp_path / "runner"
    runner_root.mkdir()
    (runner_root / "receipt.json").write_text(
        json.dumps(
            {
                "schema_version": module.SCHEMA_VERSION,
                "dependency_fingerprint": "stale",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "_git_commit", lambda _path: "b" * 40)

    with pytest.raises(module.RunnerError, match="run prepare first"):
        module.check(
            runner_root=runner_root,
            kdcube_root=kdcube,
            app_ecosystem_root=app_ecosystem,
            base_python=Path(sys.executable),
        )


def test_check_requires_prepare_after_a_source_commit_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    kdcube, app_ecosystem = _sources(tmp_path / "sources")
    runner_root = tmp_path / "runner"
    runner_root.mkdir()
    monkeypatch.setattr(module, "_git_commit", lambda _path: "a" * 40)
    inputs, _platform_text, _test_requirements = module._inputs(
        kdcube_root=kdcube,
        app_ecosystem_root=app_ecosystem,
        base_python=Path(sys.executable),
    )
    (runner_root / "receipt.json").write_text(
        json.dumps(
            {
                "schema_version": module.SCHEMA_VERSION,
                "dependency_fingerprint": inputs["dependency_fingerprint"],
                "inputs": inputs,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "_git_commit", lambda _path: "b" * 40)

    with pytest.raises(module.RunnerError, match="current source commits"):
        module.check(
            runner_root=runner_root,
            kdcube_root=kdcube,
            app_ecosystem_root=app_ecosystem,
            base_python=Path(sys.executable),
        )


def test_path_command_prints_only_the_receipted_interpreter(tmp_path: Path) -> None:
    runner_root = tmp_path / "runner"
    runner_python = runner_root / "venv/bin/python"
    runner_python.parent.mkdir(parents=True)
    runner_python.write_text("", encoding="utf-8")
    (runner_root / "receipt.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runner_python": str(runner_python),
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        (sys.executable, SCRIPT, "path", "--runner-root", runner_root),
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == str(runner_python)
