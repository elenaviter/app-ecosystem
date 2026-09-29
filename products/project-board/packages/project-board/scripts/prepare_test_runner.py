#!/usr/bin/env python3
"""Prepare one reusable, isolated Problem Board test runner per host."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None  # type: ignore[assignment]


SCHEMA_VERSION = 1
DEFAULT_RUNNER_ROOT = Path.home() / ".kdcube" / "test-runners" / "problem-board"
PLATFORM_REQUIREMENTS = "requirements-chat-processor.txt"
AWS_REQUIREMENTS = "requirements-aws.txt"
PROJECT_BOARD_RELATIVE = Path("products/project-board/packages/project-board")
RECEIPT_NAME = "receipt.json"
FIRST_PARTY_REQUIREMENT = re.compile(
    r"^\s*(?:connection-hub|project-board|app-foundation)(?:\[|\s|[<>=!~;@]|$)",
    re.IGNORECASE,
)
OVERLAY_RUNTIME_REQUIREMENTS = (
    "filelock>=3.16,<4",
    "json5>=0.12,<1",
    "keyring>=25,<26",
    "mcp==2.0.0",
    "packaging>=23,<27",
    "platformdirs>=4,<5",
)
class RunnerError(RuntimeError):
    """A preparation or preflight failure with an operator-facing message."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _run(
    arguments: Sequence[str | Path],
    *,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    command = tuple(str(argument) for argument in arguments)
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=capture_output,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RunnerError(f"Command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise RunnerError(f"Command failed ({' '.join(command)}){suffix}") from exc


def _python_identity(python: Path) -> dict[str, object]:
    code = """
import json
import platform
import sys
print(json.dumps({
    "executable": sys.executable,
    "implementation": platform.python_implementation(),
    "version": platform.python_version(),
    "version_info": list(sys.version_info[:3]),
}))
"""
    result = _run((python, "-c", code), capture_output=True)
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RunnerError(f"Could not inspect Python interpreter {python}.") from exc
    if not isinstance(value, dict):
        raise RunnerError(f"Could not inspect Python interpreter {python}.")
    return value


def _git_commit(path: Path) -> str:
    result = _run(("git", "-C", path, "rev-parse", "HEAD"), capture_output=True)
    commit = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RunnerError(f"Could not read a full Git commit for {path}.")
    return commit


def _read_test_requirements(pyproject: Path) -> tuple[str, ...]:
    try:
        raw = pyproject.read_bytes()
    except OSError as exc:
        raise RunnerError(f"Cannot read Project Board metadata at {pyproject}.") from exc
    if tomllib is None:
        raise RunnerError(
            "The preparation interpreter needs Python 3.11+ (tomllib) to read "
            "the Project Board test extra."
        )
    try:
        data = tomllib.loads(raw.decode("utf-8"))
        values = data["project"]["optional-dependencies"]["test"]
    except (KeyError, TypeError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise RunnerError(
            f"Project Board has no readable project.optional-dependencies.test at {pyproject}."
        ) from exc
    if not isinstance(values, list) or not values or not all(
        isinstance(value, str) and value.strip() for value in values
    ):
        raise RunnerError(
            f"Project Board's test extra at {pyproject} must be a non-empty string list."
        )
    requirements = tuple(value.strip() for value in values)
    if not any(
        re.match(r"^pytest-xdist(?:\[|\s|[<>=!~;@]|$)", value, re.IGNORECASE)
        for value in requirements
    ):
        raise RunnerError(
            "Project Board's test extra must declare pytest-xdist before the "
            "parallel test runner can be prepared."
        )
    return requirements


def _filtered_platform_requirements(kdcube_root: Path) -> tuple[str, Path]:
    source = kdcube_root / PLATFORM_REQUIREMENTS
    aws = (kdcube_root / AWS_REQUIREMENTS).resolve()
    if not source.is_file():
        raise RunnerError(f"Missing platform requirements file: {source}")
    if not aws.is_file():
        raise RunnerError(f"Missing included platform requirements file: {aws}")
    output: list[str] = []
    for line in source.read_text(encoding="utf-8").splitlines():
        if FIRST_PARTY_REQUIREMENT.match(line):
            continue
        if re.match(r"^\s*-r\s+requirements-aws\.txt\s*$", line):
            output.append(f"-r {aws}")
        else:
            output.append(line)
    return "\n".join(output) + "\n", source


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class _PreparationLock(AbstractContextManager["_PreparationLock"]):
    def __init__(self, runner_root: Path) -> None:
        self.path = runner_root / ".prepare.lock"

    def __enter__(self) -> "_PreparationLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.mkdir()
        except FileExistsError as exc:
            owner = self.path / "owner.json"
            detail = (
                owner.read_text(encoding="utf-8").strip()
                if owner.is_file()
                else "unknown"
            )
            raise RunnerError(
                f"Another preparation owns {self.path}; owner receipt: {detail}"
            ) from exc
        _atomic_write(
            self.path / "owner.json",
            _json_bytes(
                {
                    "host": socket.gethostname(),
                    "pid": os.getpid(),
                    "started_at": _utc_now(),
                }
            ),
        )
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        (self.path / "owner.json").unlink(missing_ok=True)
        try:
            self.path.rmdir()
        except FileNotFoundError:
            pass


def _validate_runner_root(runner_root: Path, base_python: Path) -> None:
    root = runner_root.expanduser().resolve()
    base_environment = base_python.expanduser().resolve().parent.parent
    if (
        root == base_environment
        or root in base_environment.parents
        or base_environment in root.parents
    ):
        raise RunnerError(
            "The test runner root must be separate from the selected base "
            f"Python environment ({base_environment})."
        )
    if "client-runtime" in root.parts:
        raise RunnerError(
            "The test runner root must be separate from the Problem Board client runtime."
        )


def _inputs(
    *,
    kdcube_root: Path,
    app_ecosystem_root: Path,
    base_python: Path,
) -> tuple[dict[str, object], str, tuple[str, ...]]:
    project_board_root = app_ecosystem_root / PROJECT_BOARD_RELATIVE
    pyproject = project_board_root / "pyproject.toml"
    platform_text, platform_source = _filtered_platform_requirements(kdcube_root)
    test_requirements = _read_test_requirements(pyproject)
    python = _python_identity(base_python)
    dependency_input = {
        "python": {
            "implementation": python["implementation"],
            "version_info": python["version_info"],
        },
        "platform_requirements_sha256": _sha256_text(platform_text),
        "overlay_runtime_requirements": list(OVERLAY_RUNTIME_REQUIREMENTS),
        "project_board_test_requirements": list(test_requirements),
    }
    model: dict[str, object] = {
        "base_python": python,
        "dependency_input": dependency_input,
        "dependency_fingerprint": _sha256_text(
            json.dumps(dependency_input, sort_keys=True, separators=(",", ":"))
        ),
        "platform_requirements": {
            "source": str(platform_source.resolve()),
            "filtered_sha256": _sha256_text(platform_text),
        },
        "project_board_test_extra": {
            "source": str(pyproject.resolve()),
            "requirements": list(test_requirements),
        },
        "overlay_runtime_requirements": list(OVERLAY_RUNTIME_REQUIREMENTS),
        "sources": {
            "app_ecosystem": {
                "commit": _git_commit(app_ecosystem_root),
                "root": str(app_ecosystem_root.resolve()),
            },
            "kdcube": {
                "commit": _git_commit(kdcube_root),
                "root": str(kdcube_root.resolve()),
            },
        },
    }
    return model, platform_text, test_requirements


def _environment_probe(
    python: Path,
    test_requirements: Sequence[str],
) -> dict[str, object]:
    requirements = tuple(test_requirements) + ("execnet",)
    code = r"""
import importlib
import importlib.metadata
import json
import platform
import sys
from packaging.requirements import Requirement

inventory = {}
for distribution in importlib.metadata.distributions():
    name = distribution.metadata.get("Name")
    if name:
        inventory[name.lower().replace("_", "-")] = distribution.version

failures = []
selected = {}
for raw in sys.argv[1:]:
    requirement = Requirement(raw)
    name = requirement.name.lower().replace("_", "-")
    version = inventory.get(name)
    selected[name] = version
    if version is None:
        failures.append(f"missing distribution: {requirement.name}")
    elif requirement.specifier and version not in requirement.specifier:
        failures.append(
            f"{requirement.name} {version} does not satisfy {requirement.specifier}"
        )
for module in ("xdist", "execnet"):
    try:
        importlib.import_module(module)
    except Exception as exc:
        failures.append(f"cannot import {module}: {type(exc).__name__}: {exc}")

print(json.dumps({
    "interpreter": {
        "executable": sys.executable,
        "implementation": platform.python_implementation(),
        "version": platform.python_version(),
    },
    "installed_distributions": dict(sorted(inventory.items())),
    "test_dependencies": selected,
    "failures": failures,
}))
"""
    result = _run((python, "-c", code, *requirements), capture_output=True)
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RunnerError(
            f"Test runner {python} returned an unreadable preflight."
        ) from exc
    if not isinstance(value, dict):
        raise RunnerError(f"Test runner {python} returned an unreadable preflight.")
    failures = value.get("failures")
    if not isinstance(failures, list):
        raise RunnerError(f"Test runner {python} returned an unreadable preflight.")
    if failures:
        raise RunnerError(
            f"Test runner {python} is incomplete: "
            + "; ".join(str(item) for item in failures)
        )
    return value


def _read_receipt(runner_root: Path) -> dict[str, Any]:
    receipt_path = runner_root / RECEIPT_NAME
    try:
        value = json.loads(receipt_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RunnerError(
            f"No prepared test runner receipt exists at {receipt_path}; run prepare first."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError(
            f"The test runner receipt at {receipt_path} is unreadable."
        ) from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise RunnerError(f"The test runner receipt at {receipt_path} is incompatible.")
    return value


def _source_commits(inputs: dict[str, object]) -> dict[str, str]:
    sources = inputs.get("sources")
    if not isinstance(sources, dict):
        raise RunnerError("The test runner source receipt is incomplete; run prepare again.")
    commits: dict[str, str] = {}
    for name, value in sources.items():
        if not isinstance(value, dict) or not isinstance(value.get("commit"), str):
            raise RunnerError(
                "The test runner source receipt is incomplete; run prepare again."
            )
        commits[str(name)] = value["commit"]
    return commits


def _activate_runner(runner_root: Path, environment: Path) -> None:
    active = runner_root / "venv"
    temporary = runner_root / f".venv.{os.getpid()}"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(environment.relative_to(runner_root), target_is_directory=True)
    try:
        os.replace(temporary, active)
    except IsADirectoryError as exc:
        raise RunnerError(
            f"Refusing to replace non-managed test environment directory {active}."
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def prepare(
    *,
    runner_root: Path,
    kdcube_root: Path,
    app_ecosystem_root: Path,
    base_python: Path,
) -> dict[str, object]:
    runner_root = runner_root.expanduser().resolve()
    kdcube_root = kdcube_root.expanduser().resolve()
    app_ecosystem_root = app_ecosystem_root.expanduser().resolve()
    base_python = base_python.expanduser().resolve()
    _validate_runner_root(runner_root, base_python)
    with _PreparationLock(runner_root):
        inputs, platform_text, test_requirements = _inputs(
            kdcube_root=kdcube_root,
            app_ecosystem_root=app_ecosystem_root,
            base_python=base_python,
        )
        identity = inputs["base_python"]
        assert isinstance(identity, dict)
        version_info = identity["version_info"]
        assert isinstance(version_info, list)
        environment = (
            runner_root
            / "environments"
            / f"python-{version_info[0]}.{version_info[1]}"
        )
        runner_python = environment / "bin" / "python"
        generated = runner_root / "inputs" / "requirements-chat-processor.txt"
        _atomic_write(generated, platform_text.encode("utf-8"))

        action = "prepared"
        existing: dict[str, Any] | None = None
        try:
            existing = _read_receipt(runner_root)
        except RunnerError:
            pass
        fingerprint = str(inputs["dependency_fingerprint"])
        reusable = bool(
            existing
            and existing.get("dependency_fingerprint") == fingerprint
            and runner_python.is_file()
        )
        probe: dict[str, object] | None = None
        if reusable:
            try:
                probe = _environment_probe(runner_python, test_requirements)
            except RunnerError:
                reusable = False
        if reusable:
            action = "reused"
        else:
            if not runner_python.is_file():
                environment.parent.mkdir(parents=True, exist_ok=True)
                _run((base_python, "-m", "venv", environment))
            _run((runner_python, "-m", "pip", "install", "--upgrade", "pip"))
            _run(
                (
                    runner_python,
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    generated,
                    *OVERLAY_RUNTIME_REQUIREMENTS,
                    *test_requirements,
                )
            )
            probe = _environment_probe(runner_python, test_requirements)
        assert probe is not None
        _activate_runner(runner_root, environment)
        receipt: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "runner_kind": "problem-board-host-test-runner",
            "state": "ready",
            "action": action,
            "host": socket.gethostname(),
            "verified_at": _utc_now(),
            "runner_root": str(runner_root),
            "runner_python": str((runner_root / "venv" / "bin" / "python")),
            "dependency_fingerprint": fingerprint,
            "inputs": inputs,
            "test_dependencies": probe["test_dependencies"],
            "installed_distributions": probe["installed_distributions"],
        }
        _atomic_write(runner_root / RECEIPT_NAME, _json_bytes(receipt))
        return receipt


def check(
    *,
    runner_root: Path,
    kdcube_root: Path,
    app_ecosystem_root: Path,
    base_python: Path,
) -> dict[str, object]:
    runner_root = runner_root.expanduser().resolve()
    receipt = _read_receipt(runner_root)
    inputs, _platform_text, test_requirements = _inputs(
        kdcube_root=kdcube_root.expanduser().resolve(),
        app_ecosystem_root=app_ecosystem_root.expanduser().resolve(),
        base_python=base_python.expanduser().resolve(),
    )
    expected = str(inputs["dependency_fingerprint"])
    if receipt.get("dependency_fingerprint") != expected:
        raise RunnerError(
            "The prepared runner does not match the current dependency inputs; run prepare first."
        )
    receipt_inputs = receipt.get("inputs")
    if not isinstance(receipt_inputs, dict) or _source_commits(
        receipt_inputs
    ) != _source_commits(inputs):
        raise RunnerError(
            "The prepared runner receipt does not verify the current source commits; "
            "run prepare first."
        )
    runner_python = runner_root / "venv" / "bin" / "python"
    probe = _environment_probe(runner_python, test_requirements)
    if probe["installed_distributions"] != receipt.get("installed_distributions"):
        raise RunnerError(
            "The prepared runner's installed distributions differ from its exact receipt; "
            "run prepare again."
        )
    return {
        "state": "ready",
        "receipt": str(runner_root / RECEIPT_NAME),
        "verified_at": receipt.get("verified_at"),
        "source_commits": _source_commits(inputs),
        "interpreter": probe["interpreter"],
        "test_dependencies": probe["test_dependencies"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("prepare", "check"):
        child = subparsers.add_parser(command)
        child.add_argument("--kdcube-root", required=True, type=Path)
        child.add_argument("--app-ecosystem-root", required=True, type=Path)
        child.add_argument("--runner-root", type=Path, default=DEFAULT_RUNNER_ROOT)
        child.add_argument("--python", type=Path, default=Path(sys.executable))
    path_parser = subparsers.add_parser("path")
    path_parser.add_argument("--runner-root", type=Path, default=DEFAULT_RUNNER_ROOT)
    return parser


def _preparation_summary(receipt: dict[str, object]) -> dict[str, object]:
    inputs = receipt["inputs"]
    assert isinstance(inputs, dict)
    installed = receipt["installed_distributions"]
    assert isinstance(installed, dict)
    return {
        "state": receipt["state"],
        "action": receipt["action"],
        "receipt": str(Path(str(receipt["runner_root"])) / RECEIPT_NAME),
        "runner_python": receipt["runner_python"],
        "dependency_fingerprint": receipt["dependency_fingerprint"],
        "installed_distribution_count": len(installed),
        "source_commits": _source_commits(inputs),
        "test_dependencies": receipt["test_dependencies"],
        "verified_at": receipt["verified_at"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            value = prepare(
                runner_root=arguments.runner_root,
                kdcube_root=arguments.kdcube_root,
                app_ecosystem_root=arguments.app_ecosystem_root,
                base_python=arguments.python,
            )
            print(json.dumps(_preparation_summary(value), indent=2, sort_keys=True))
        elif arguments.command == "check":
            value = check(
                runner_root=arguments.runner_root,
                kdcube_root=arguments.kdcube_root,
                app_ecosystem_root=arguments.app_ecosystem_root,
                base_python=arguments.python,
            )
            print(json.dumps(value, indent=2, sort_keys=True))
        else:
            receipt = _read_receipt(arguments.runner_root.expanduser().resolve())
            runner_python = Path(str(receipt.get("runner_python", "")))
            if not runner_python.is_file():
                raise RunnerError(
                    f"The prepared runner interpreter is absent at {runner_python}."
                )
            print(runner_python)
    except RunnerError as exc:
        print(f"problem_board_test_runner_incomplete: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
