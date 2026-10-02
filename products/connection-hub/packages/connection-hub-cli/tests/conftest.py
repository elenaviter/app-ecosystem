from __future__ import annotations

import pytest

from connection_hub_cli.authorization.discovery import clear_discovery_cache


@pytest.fixture(autouse=True)
def _fresh_discovery_cache():
    """Discovery results are cached per process, so no test sees another's."""

    clear_discovery_cache()
    yield
    clear_discovery_cache()


@pytest.fixture(autouse=True)
def _private_credential_lock_dir(tmp_path, monkeypatch):
    """Credential locks of a test live in its own folder, never the user's cache (W464)."""

    from connection_hub.caller.authorization import profile_session

    locks = tmp_path / "credential-locks"
    monkeypatch.setattr(profile_session, "default_credential_lock_dir", lambda: locks, raising=False)
    yield locks
