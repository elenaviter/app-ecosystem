from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from service_foundation.host_relay import discovery


class _Adapter:
    adapter_id = "fixture"

    async def poll_once(self) -> Mapping[str, Any]:
        return {"ok": True}


class _EntryPoint:
    name = "fixture"

    @staticmethod
    def load():
        return lambda _config: _Adapter()


def test_discovery_constructs_adapter_and_checks_identity(monkeypatch) -> None:
    monkeypatch.setattr(discovery, "_entry_points", lambda: (_EntryPoint(),))

    adapter = discovery.create_host_relay_adapter("fixture", {})

    assert adapter.adapter_id == "fixture"


def test_discovery_rejects_unknown_adapter(monkeypatch) -> None:
    monkeypatch.setattr(discovery, "_entry_points", lambda: ())

    with pytest.raises(LookupError, match="not installed"):
        discovery.create_host_relay_adapter("missing", {})
