"""The caller layer runs without KDCube and without the command line (W322).

Operator, 2026-09-25: Problem Board's pb depends on connection-hub[client], not
on connection-hub-cli, and needs no KDCube package. The layer pb uses (sign-in,
profiles, credentials, an authenticated MCP connection) moved here from the
command line, which is now a thin shell over it.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

SOURCE = Path(__file__).parents[1] / "src" / "connection_hub" / "caller"
FORBIDDEN = {"kdcube_cli", "kdcube_ai_app", "connection_hub_cli"}


def _import_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            roots.add(node.module.partition(".")[0])
    return roots


def test_no_caller_module_imports_kdcube_or_the_command_line() -> None:
    violations = {
        str(path.relative_to(SOURCE)): sorted(_import_roots(path) & FORBIDDEN)
        for path in SOURCE.rglob("*.py")
        if _import_roots(path) & FORBIDDEN
    }
    assert violations == {}


def test_the_caller_services_build_with_kdcube_and_the_command_line_unimportable(tmp_path) -> None:
    script = textwrap.dedent(
        f"""
        import importlib.abc, sys

        class Refuse(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name.partition(".")[0] in {sorted(FORBIDDEN)!r}:
                    raise ImportError("refused in this test: " + name)
                return None

        sys.meta_path.insert(0, Refuse())
        from pathlib import Path
        from connection_hub.caller.paths import StatePaths
        from connection_hub.caller.profile_connection import connect_profile_tools, resolve_profile_bearer
        from connection_hub.caller.services import build_caller_services

        services = build_caller_services(paths=StatePaths(Path({str(tmp_path)!r})))
        assert services.profiles.list() == []
        loaded = sorted(n for n in sys.modules if n.partition(".")[0] in {sorted(FORBIDDEN)!r})
        assert loaded == [], loaded
        print("ok")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring"},
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_stored_state_keeps_its_schema_names() -> None:
    # Hosts already hold files written under these names; the move must not
    # change them, or every existing profile and session is refused.
    from connection_hub.caller.authorization.session import OAuthSessionStore
    from connection_hub.caller.state import HostStore, InstallationStore, ProfileStore

    assert ProfileStore.SCHEMA == "connection_hub_cli.profiles.v1"
    assert InstallationStore.SCHEMA == "connection_hub_cli.client_installations.v1"
    assert HostStore.SCHEMA == "connection_hub_cli.host.v1"
    assert OAuthSessionStore.SCHEMA == "connection_hub_cli.oauth_sessions.v1"
    models = (SOURCE / "authorization" / "models.py").read_text(encoding="utf-8")
    assert '"connection_hub_cli.oauth_token.v1"' in models
