from __future__ import annotations

import ast
from pathlib import Path


SOURCE_ROOT = Path(__file__).parents[1] / "src" / "service_foundation"
FORBIDDEN_IMPORT_ROOTS = {
    "app_foundation",
    "connection_hub",
    "connection_hub_cli",
    "kdcube_ai_app",
}


def _import_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.partition(".")[0])
    return roots


def test_service_foundation_does_not_import_products_platform_or_app_foundation() -> None:
    violations = {
        str(path.relative_to(SOURCE_ROOT)): sorted(
            _import_roots(path) & FORBIDDEN_IMPORT_ROOTS
        )
        for path in SOURCE_ROOT.rglob("*.py")
        if _import_roots(path) & FORBIDDEN_IMPORT_ROOTS
    }

    assert violations == {}
