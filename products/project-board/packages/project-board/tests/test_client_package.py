from __future__ import annotations

import importlib
import json
import pkgutil
import tomllib
from pathlib import Path

import project_board.client
from project_board.client.entrypoint import _top_level_command
from project_board.client.first_run import _client_source
from project_board.client.procedures import (
    source_package,
    source_package_path,
    source_revision_ledger_path,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
CLIENT_ROOT = Path(project_board.client.__file__).resolve().parent


def test_every_client_module_imports_from_the_distribution() -> None:
    discovered = {
        module.name
        for module in pkgutil.iter_modules(project_board.client.__path__)
        if not module.ispkg
    }

    assert "cli" in discovered
    assert "entrypoint" in discovered
    for module_name in sorted(discovered):
        imported = importlib.import_module(f"project_board.client.{module_name}")
        assert imported.__name__ == f"project_board.client.{module_name}"


def test_distribution_installs_the_pb_console_script() -> None:
    metadata = tomllib.loads((PACKAGE_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["scripts"]["pb"] == "project_board.client.entrypoint:main"
    assert _top_level_command(["--format", "brief", "worker", "receive"]) == "worker"


def test_pb_automation_uses_direct_mcp_and_data_bus_transports() -> None:
    sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in CLIENT_ROOT.glob("*.py")
    }
    combined = "\n".join(sources.values())

    assert "ProblemBoardNamedServicesClient" not in combined
    assert "named_services_action" not in combined
    assert "ProblemBoardMcpClient" in sources["mcp_client.py"]
    assert "ProblemBoardDataBusClient" in sources["cli.py"]


def test_worker_procedure_is_package_data_owned_by_project_board() -> None:
    package = source_package()

    assert source_package_path().is_relative_to(Path(project_board.client.__file__).resolve().parents[1])
    assert package["package_id"] == "problem-board-worker"
    assert package["entrypoint"] == "SKILL.md"
    assert package["revision"]
    assert package["references"]


def test_worker_procedure_revision_records_its_exact_content() -> None:
    package = source_package()
    ledger = json.loads(
        source_revision_ledger_path().read_text(encoding="utf-8")
    )

    assert package["revision"] == "2026.09.22.13"
    assert ledger[package["revision"]] == package["source_digest"]


def test_worker_procedure_owns_released_and_code_source_guidance() -> None:
    root = source_package_path()
    first_run = (root / "references" / "first-run.md").read_text(encoding="utf-8")
    runtime = (root / "references" / "runtime-actions.md").read_text(
        encoding="utf-8"
    )
    runtime_words = " ".join(runtime.split())

    assert 'pipx install "project-board==<approved-version>"' in first_run
    assert "This section is the owning definition" in first_run
    assert "pb source use-release --expect-version <version>" in runtime
    assert "pb source use-code --repository <app-ecosystem>" in runtime
    assert "`client.pinned: false`" in runtime
    assert "does not rewrite the per-target source selector" in runtime_words


def test_checkout_status_marks_the_client_as_not_pinned() -> None:
    client = _client_source()

    assert client["pinned"] is False
    assert client["source"]["mode"] == "checkout"
    assert client["source"]["head"]
