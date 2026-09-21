from __future__ import annotations

import pytest

from connection_hub_cli.authorization.discovery import clear_discovery_cache


@pytest.fixture(autouse=True)
def _fresh_discovery_cache():
    """Discovery results are cached per process, so no test sees another's."""

    clear_discovery_cache()
    yield
    clear_discovery_cache()
