"""The endpoint hygiene gate sees its input while tests stay host-isolated."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


TESTS = Path(__file__).resolve().parent
PACKAGE = TESTS.parent
PROBE = "test_public_docs_have_no_private_paths.py"
ENDPOINT_TEST = f"{PROBE}::test_no_tracked_file_names_this_hosts_endpoint"


@pytest.mark.parametrize("selection", ["explicit", "default", "missing"])
@pytest.mark.parametrize("leaked", [False, True], ids=["clean", "leaked"])
def test_strict_endpoint_scan_preserves_host_isolation(tmp_path, selection, leaked):
    repo = tmp_path / "repo"
    tests = repo / "products" / "project-board" / "packages" / "project-board" / "tests"
    tests.mkdir(parents=True)
    for name in (PROBE, "conftest.py"):
        shutil.copyfile(TESTS / name, tests / name)
    (tests / "test_isolated.py").write_text(
        "import pytest\n"
        "from project_board.contract.errors import DomainError\n"
        "from project_board.client.host_config import resolve_host_config_path\n"
        "def test_host_configuration_stays_unavailable():\n"
        "    with pytest.raises(DomainError):\n"
        "        resolve_host_config_path()\n",
        encoding="utf-8",
    )
    hostname = "privacy-probe.example.invalid"
    (repo / "README.md").write_text(
        f"https://{hostname}/mcp\n" if leaked else "Public documentation.\n",
        encoding="utf-8",
    )
    for args in (["init", "-q"], ["add", "README.md"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    runtime = tmp_path / "runtime"
    config = tmp_path / "host.json"
    config.write_text(json.dumps({
        "schema": "problem-board.host-relay-config.v2",
        "target": {
            "id": "test", "endpoint": f"https://{hostname}/mcp",
            "tenant": "test", "project": "test", "bundle_id": "problem-board@1-0",
        },
        "field_root": str(tmp_path / "field"),
        "host": {"id": "test", "label": "Test host", "kind": "local"},
        "relay": {"id": "test"},
    }), encoding="utf-8")
    env = os.environ.copy()
    for name in ("PROBLEM_BOARD_CONFIG", "PB_PROFILE", "PYTEST_PLUGINS"):
        env.pop(name, None)
    env.update({
        "HOME": str(tmp_path / "home"),
        "KDCUBE_CLIENT_RUNTIME_HOME": str(runtime),
        "PB_REQUIRE_PRIVATE_NAMES": "1",
        "PYTEST_ADDOPTS": "",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": os.pathsep.join(filter(None, [str(PACKAGE / "src"), env.get("PYTHONPATH")])),
    })
    if selection == "explicit":
        env["PROBLEM_BOARD_CONFIG"] = str(config)
    elif selection == "default":
        pointer = runtime / "problem-board" / "default.json"
        pointer.parent.mkdir(parents=True)
        pointer.write_text(json.dumps({"config": str(config)}), encoding="utf-8")
    before = config.read_bytes()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--showlocals", "--tb=short",
         ENDPOINT_TEST, "test_isolated.py"],
        cwd=tests, env=env, capture_output=True, text=True, timeout=60,
    )
    expected_failure = leaked or selection == "missing"
    assert result.returncode == (1 if expected_failure else 0), result.stdout + result.stderr
    assert "test_host_configuration_stays_unavailable" not in result.stdout, result.stdout
    assert hostname not in result.stdout + result.stderr
    if selection == "missing":
        assert "no Problem Board host configuration" in result.stdout
    elif leaked:
        assert "README.md:1" in result.stdout
        assert "no Problem Board host configuration" not in result.stdout
    else:
        assert "2 passed" in result.stdout
    if expected_failure:
        assert "1 failed, 1 passed" in result.stdout
    assert config.read_bytes() == before
