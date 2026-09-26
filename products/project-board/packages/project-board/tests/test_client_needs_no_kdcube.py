"""pb needs connection-hub[client] only: no command line, no KDCube (W322 Step 1).

Operator, 2026-09-25: pb depends on connection-hub[client], not on
connection-hub-cli; the client environment contains no KDCube package. The
command line imported kdcube_cli at load, so pb could not start without it.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

CLIENT = Path(__file__).resolve().parents[1] / "src" / "project_board" / "client"
FORBIDDEN = {"kdcube_cli", "connection_hub_cli"}


def _import_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            roots.add(node.module.partition(".")[0])
    return roots


def test_no_client_module_imports_the_command_line_or_kdcube_cli() -> None:
    violations = {
        str(path.relative_to(CLIENT)): sorted(_import_roots(path) & FORBIDDEN)
        for path in CLIENT.rglob("*.py")
        if _import_roots(path) & FORBIDDEN
    }
    assert violations == {}


def test_the_client_command_loads_with_the_command_line_and_kdcube_cli_unimportable() -> None:
    script = textwrap.dedent(
        f"""
        import importlib.abc, sys

        class Refuse(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name.partition(".")[0] in {sorted(FORBIDDEN)!r}:
                    raise ImportError("refused in this test: " + name)
                return None

        sys.meta_path.insert(0, Refuse())
        # The release smoke imports every runtime surface of the client.
        import project_board.client.release_smoke
        print("ok")
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
