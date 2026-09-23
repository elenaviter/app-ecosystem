from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import yaml
from packaging.requirements import Requirement


REPOSITORY_ROOT = Path(__file__).resolve().parents[5]
PROJECT_BOARD_ROOT = REPOSITORY_ROOT / "products/project-board/packages/project-board"
CONNECTION_HUB_PRODUCT = REPOSITORY_ROOT / "products/connection-hub"
CONNECTION_HUB_ROOT = CONNECTION_HUB_PRODUCT / "packages/connection-hub"
CONNECTION_HUB_CLI_ROOT = CONNECTION_HUB_PRODUCT / "packages/connection-hub-cli"
RELEASE_VERSION = "2026.09.23.0158"
KDCUBE_CLI_VERSION = "2026.09.13.0145"


def _metadata(root: Path) -> dict:
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def _requirements(metadata: dict) -> dict[str, Requirement]:
    parsed = (Requirement(value) for value in metadata["dependencies"])
    return {requirement.name: requirement for requirement in parsed}


def _module_version(path: Path) -> str:
    module = ast.parse(path.read_text(encoding="utf-8"))
    for statement in module.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if any(isinstance(target, ast.Name) and target.id == "__version__" for target in targets):
            value = statement.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
    raise AssertionError(f"No literal __version__ in {path}")


def test_published_route_has_one_resolvable_version_chain() -> None:
    project_board = _metadata(PROJECT_BOARD_ROOT)
    connection_hub = _metadata(CONNECTION_HUB_ROOT)
    connection_hub_cli = _metadata(CONNECTION_HUB_CLI_ROOT)
    product_release = yaml.safe_load(
        (CONNECTION_HUB_PRODUCT / "release.yaml").read_text(encoding="utf-8")
    )
    project_board_release = yaml.safe_load(
        (PROJECT_BOARD_ROOT / "release.yaml").read_text(encoding="utf-8")
    )

    assert project_board["version"] == RELEASE_VERSION
    assert connection_hub["version"] == RELEASE_VERSION
    assert connection_hub_cli["version"] == RELEASE_VERSION
    assert product_release["product"]["ref"] == RELEASE_VERSION
    assert product_release["config"]["version"] == RELEASE_VERSION
    assert product_release["components"]["python_package"]["version"] == RELEASE_VERSION
    assert product_release["components"]["cli_package"]["version"] == RELEASE_VERSION
    assert project_board_release["package"]["ref"] == RELEASE_VERSION
    assert project_board_release["components"]["python_package"]["version"] == RELEASE_VERSION

    project_board_requirements = _requirements(project_board)
    connection_hub_cli_requirements = _requirements(connection_hub_cli)
    assert str(project_board_requirements["connection-hub-cli"].specifier) == (
        f"<2027,>={RELEASE_VERSION}"
    )
    assert str(connection_hub_cli_requirements["connection-hub"].specifier) == (
        f"<2027,>={RELEASE_VERSION}"
    )
    assert str(connection_hub_cli_requirements["kdcube-cli"].specifier) == (
        f"<2027,>={KDCUBE_CLI_VERSION}"
    )

    assert _module_version(
        PROJECT_BOARD_ROOT / "src/project_board/__init__.py"
    ) == RELEASE_VERSION
    assert _module_version(
        CONNECTION_HUB_ROOT / "src/connection_hub/__init__.py"
    ) == RELEASE_VERSION
    assert _module_version(
        CONNECTION_HUB_CLI_ROOT / "src/connection_hub_cli/__init__.py"
    ) == RELEASE_VERSION
