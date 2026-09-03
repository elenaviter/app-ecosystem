from __future__ import annotations

from pathlib import Path

from connection_hub_cli.paths import STATE_DIRECTORY_ENV, StatePaths


def test_default_paths_use_explicit_state_directory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "isolated-state"
    monkeypatch.setenv(STATE_DIRECTORY_ENV, str(state_root))

    paths = StatePaths.default()

    assert paths.root == state_root
    assert paths.host == state_root / "host.json"
    assert paths.oauth_sessions == state_root / "oauth-sessions.json"


def test_default_paths_expand_user_in_explicit_state_directory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(STATE_DIRECTORY_ENV, "~/connection-hub-state")

    assert StatePaths.default().root == tmp_path / "connection-hub-state"
