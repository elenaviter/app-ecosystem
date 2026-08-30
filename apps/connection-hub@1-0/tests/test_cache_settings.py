# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from pathlib import Path

import yaml

from connection_hub.delegated_credentials.cache_settings import DelegatedCacheSettings


def _descriptor_connections(path: Path) -> dict:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    found: list[dict] = []

    def walk(node):
        if isinstance(node, dict):
            candidate = node.get("connections")
            if isinstance(candidate, dict) and "delegated_credentials" in candidate:
                found.append(candidate)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(document)
    assert found, f"no connections block with delegated_credentials in {path}"
    return found[0]


def test_app_template_declares_connection_hub_cache_defaults():
    template = Path(__file__).resolve().parents[1] / "config/bundles.template.yaml"
    settings = DelegatedCacheSettings.from_connections(
        _descriptor_connections(template)
    )
    assert settings == DelegatedCacheSettings()
