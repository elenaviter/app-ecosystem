"""A host sheds the channel rows of sessions that are gone (W304 finding 18).

Detach and retirement only ever disable a channel row, so a host's relay
config collected every session it had run; spark1 kept the 2026-09-22 rows of
sessions that had long been re-enrolled. `pb host configure
--remove-disabled-channels` removes the disabled rows and keeps every active
or pending one.
"""

from __future__ import annotations

from project_board.client import cli, host_config
from project_board.contract.worker_identity import WorkerSessionIdentity
from relay_helpers import make_host


def test_only_disabled_channel_rows_are_removed_and_named(tmp_path):
    host, identity, _channel = make_host(tmp_path)
    gone = WorkerSessionIdentity.create("claude-code", "22222222-2222-4222-8222-222222222222")
    pending = WorkerSessionIdentity.create("claude-code", "33333333-3333-4333-8333-333333333333")
    host_config.enroll_worker_channel(host.path, identity=gone, profile="problem-board-claude-gone", authorized=True)
    host_config.enroll_worker_channel(host.path, identity=pending, profile="problem-board-claude-pending")
    host_config.set_worker_channel_state(host.path, identity=gone, state="disabled")

    result = cli._host_command(  # noqa: SLF001 - the command under test
        cli.build_parser().parse_args(
            ["host", "configure", "--config", str(host.path), "--remove-disabled-channels"]
        )
    )

    assert result["removed_channels"] == [gone.worker_name]
    kept = {channel.worker_name: channel.state for channel in host_config.HostRelayConfig.load(host.path).workers}
    assert kept == {identity.worker_name: "active", pending.worker_name: "pending_authorization"}


def test_configure_without_the_flag_keeps_disabled_rows(tmp_path):
    host, _identity, _channel = make_host(tmp_path)
    gone = WorkerSessionIdentity.create("claude-code", "22222222-2222-4222-8222-222222222222")
    host_config.enroll_worker_channel(host.path, identity=gone, profile="problem-board-claude-gone", authorized=True)
    host_config.set_worker_channel_state(host.path, identity=gone, state="disabled")

    host_config.update_host_config(host.path, host_label="Renamed")

    states = {channel.worker_name: channel.state for channel in host_config.HostRelayConfig.load(host.path).workers}
    assert states[gone.worker_name] == "disabled"
