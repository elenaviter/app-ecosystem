"""A new host accepts mail from its project teammates (W304 decision 3).

During the 2026-09-24 rehearsal a new host's empty peer list refused the
coordinator's mail twice (findings 38 and 44), and every host ended up at
"*". The operator ruled on 2026-09-26 that "*" is the default. The board lets
only agents that share a project address each other, so "*" grants nothing
beyond that. `--deny-all-peers` still denies everyone.
"""

from __future__ import annotations

import json

from project_board.client import host_config


def _new_host(tmp_path):
    return host_config.initialize_host_config(
        target_id="target",
        endpoint="https://runtime.example/mcp",
        tenant="tenant",
        platform_project="project",
        host_id="host-one",
        allowed_roots=[str(tmp_path)],
        source_repositories={},
        config_path=tmp_path / "relay.json",
        state_root=tmp_path / "state",
    )


def test_a_new_host_accepts_its_project_teammates():
    assert host_config.DEFAULT_ALLOWED_PEER_WORKERS == ("*",)


def test_setup_writes_the_default_and_the_relay_reads_it(tmp_path):
    host = _new_host(tmp_path)
    written = json.loads(host.path.read_text(encoding="utf-8"))
    assert written["receiver_policy"]["allowed_peer_workers"] == ["*"]
    assert host_config.HostRelayConfig.load(host.path).allowed_peer_workers == ("*",)


def test_deny_all_stays_possible_and_is_kept(tmp_path):
    host = _new_host(tmp_path)
    host_config.update_host_config(host.path, allowed_peer_workers=[])
    assert host_config.HostRelayConfig.load(host.path).allowed_peer_workers == ()


def test_a_file_without_the_key_takes_the_default_and_an_explicit_list_is_kept(tmp_path):
    host = _new_host(tmp_path)
    value = json.loads(host.path.read_text(encoding="utf-8"))
    del value["receiver_policy"]["allowed_peer_workers"]
    host.path.write_text(json.dumps(value), encoding="utf-8")
    assert host_config.HostRelayConfig.load(host.path).allowed_peer_workers == ("*",)

    host_config.update_host_config(host.path, allowed_peer_workers=["claude-code-coordinator"])
    assert host_config.HostRelayConfig.load(host.path).allowed_peer_workers == ("claude-code-coordinator",)


def test_the_relay_reads_a_missing_key_as_the_default_and_keeps_an_explicit_empty_list(tmp_path):
    # The relay's own parser follows the host file's rule (review on #155).
    from project_board.client import relay

    def mapping(**receiver_policy):
        value = {
            "schema": "problem-board.host-relay-config.v1",
            "field_root": str(tmp_path / "field"),
            "project_id": "project-one",
            "connection_hub": {"profile": "problem-board-worker"},
            "worker": {"name": "codex-api", "runtime_kind": "codex", "host": {"id": "host-01", "label": "Host one", "kind": "local"}, "relay_id": "relay-01"},
        }
        if receiver_policy:
            value["receiver_policy"] = receiver_policy
        return value

    assert relay.RelayConfig.from_mapping(mapping()).allowed_peer_workers == ("*",)
    assert relay.RelayConfig.from_mapping(mapping(allowed_control_kinds=["ping"])).allowed_peer_workers == ("*",)
    assert relay.RelayConfig.from_mapping(mapping(allowed_peer_workers=[])).allowed_peer_workers == ()
