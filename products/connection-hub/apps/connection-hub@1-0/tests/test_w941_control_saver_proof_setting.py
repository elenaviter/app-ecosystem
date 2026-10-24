"""W941 (review F2): the Hub issues a managed Control's saver proof only when its setting is on."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path


def _load_entrypoint_module():
    bundle_root = Path(__file__).resolve().parents[1]
    _module_name, module = load_dynamic_module_for_path(bundle_root / "entrypoint.py")
    return module


@pytest.mark.parametrize(("connections", "expected"), [
    ({}, False),
    ({"delegated_credentials": {}}, False),
    ({"delegated_credentials": {"control_saver_proof": False}}, False),
    ({"delegated_credentials": {"control_saver_proof": "true"}}, False),
    ({"delegated_credentials": {"control_saver_proof": 1}}, False),
    ({"delegated_credentials": {"control_saver_proof": True}}, True),
    ({"delegated_credentials": "malformed"}, False),
])
def test_only_a_literal_true_turns_the_control_saver_proof_on(connections, expected) -> None:
    module = _load_entrypoint_module()
    assert module._control_saver_proof(connections) is expected
