"""W926: rerunning setup with identical values succeeds, also when it names the host id or config path.

Yaroslav's onboarding (22 Oct): a second `pb setup` with the same values, naming its host id, ended in
ERROR work_relay_config_exists. An identical rerun now reuses the existing record without rewriting it;
different values stay refused.
"""

from __future__ import annotations

import pytest

from project_board.client import host_config
from project_board.contract.errors import DomainError


@pytest.fixture(autouse=True)
def _state_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PROBLEM_BOARD_CONFIG", raising=False)
    monkeypatch.delenv("PB_PROFILE", raising=False)


def _setup(root, **overrides):
    values = {
        "target_id": "target-a",
        "endpoint": "https://target-a.example/mcp",
        "tenant": "tenant",
        "platform_project": "project",
        "host_id": "host-one",
        "allowed_roots": [str(root)],
        "source_repositories": {},
    }
    values.update(overrides)
    return host_config.initialize_host_config(**values)


def test_an_identical_rerun_naming_the_host_id_reuses_the_record_unchanged(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    first = _setup(root)
    before = first.path.read_bytes()

    again = _setup(root)

    assert again.path == first.path
    assert again.path.read_bytes() == before, "the record is not rewritten"


def test_an_identical_rerun_naming_the_config_path_reuses_the_record(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    path = tmp_path / "configs" / "relay.json"
    first = _setup(root, host_id="", config_path=str(path))
    before = path.read_bytes()

    again = _setup(root, host_id="", config_path=str(tmp_path / "configs" / "." / "relay.json"))

    assert again.path == first.path
    assert path.read_bytes() == before


def test_different_roots_under_the_same_host_id_need_host_configure(tmp_path):
    root, other = tmp_path / "work", tmp_path / "other"
    root.mkdir()
    other.mkdir()
    _setup(root)

    with pytest.raises(DomainError) as refused:
        _setup(other)
    assert refused.value.code == "work_relay_config_update_required"


def test_another_target_at_the_same_host_path_is_still_refused(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    path = tmp_path / "configs" / "relay.json"
    _setup(root, host_id="", config_path=str(path))

    with pytest.raises(DomainError) as refused:
        _setup(root, host_id="", config_path=str(path), target_id="target-b", endpoint="https://target-b.example/mcp")
    assert refused.value.code == "work_relay_config_exists"


def test_another_host_id_at_the_same_path_is_still_refused(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    path = tmp_path / "configs" / "relay.json"
    _setup(root, host_id="host-one", config_path=str(path))

    with pytest.raises(DomainError) as refused:
        _setup(root, host_id="host-two", config_path=str(path))
    assert refused.value.code == "work_relay_config_exists"


def test_a_file_that_is_no_relay_record_keeps_the_existing_refusal(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    path = tmp_path / "configs" / "relay.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"schema": "something-else"}')

    with pytest.raises(DomainError) as refused:
        _setup(root, host_id="", config_path=str(path))
    assert refused.value.code == "work_relay_config_exists"
