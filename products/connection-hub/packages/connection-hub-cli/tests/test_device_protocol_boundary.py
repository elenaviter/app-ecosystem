from __future__ import annotations

import ast
from pathlib import Path


SOURCE_ROOT = Path(__file__).parents[1] / "src" / "connection_hub_cli"
WIRE_FORMAT_LITERALS = {"dpop+jwt", "ECDH-ES", "A256GCM"}
WIRE_CRYPTO_IMPORT_ROOTS = {"cryptography", "jose", "jwcrypto", "jwt"}


def _wire_implementation_signals(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    signals: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = {alias.name.partition(".")[0] for alias in node.names}
            signals.update(roots & WIRE_CRYPTO_IMPORT_ROOTS)
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.partition(".")[0]
            if root in WIRE_CRYPTO_IMPORT_ROOTS:
                signals.add(root)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in WIRE_FORMAT_LITERALS:
                signals.add(node.value)
    return signals


def test_device_wire_protocol_has_one_product_owned_implementation() -> None:
    violations = {
        str(path.relative_to(SOURCE_ROOT)): sorted(signals)
        for path in SOURCE_ROOT.rglob("*.py")
        if (signals := _wire_implementation_signals(path))
    }

    assert violations == {}, (
        "connection-hub-cli adapts the canonical "
        "connection_hub.delegated_credentials.devices contract; it must not "
        f"implement the proof or package wire formats itself: {violations}"
    )
