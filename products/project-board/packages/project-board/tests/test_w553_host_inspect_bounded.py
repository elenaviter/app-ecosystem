"""W553: `pb host inspect` prints a bounded summary; relay diagnostics expire.

Operator, 2026-10-05: "when i do ~/.local/bin/pb host inspect I get just huge
amount of something. i see attempts in this output. why its there? its huge! if
the agents read it they spend a lot of tokens on only reading this." and "is
there any limit in this huge attempts lists?" On dev-main the plain command
printed 712,474 bytes, 396,741 of them relay diagnostics: 18 channels, gone
sessions included, each with 20 stored intervals of up to 20 attempts, the
oldest a week old. The default now prints one summary per served channel, the
stored lists only with --diagnostics, and every list ages out after
RELAY_DIAGNOSTIC_RETENTION_DAYS, in the writers and in housekeeping.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from project_board.client import cli, host_config, local_state_maintenance
from project_board.client.diagnostics import host_relay_diagnostics
from project_board.client.io import atomic_write_json
from project_board.client.render import render_envelope
from project_board.client.store import (
    RELAY_DIAGNOSTIC_MAX_RECORDS,
    RELAY_DIAGNOSTIC_RETENTION_DAYS,
    RELAY_DIAGNOSTIC_SCHEMA,
    SharedFieldStore,
)
from project_board.contract.worker_identity import WorkerSessionIdentity

NOW = datetime.now(timezone.utc).replace(microsecond=0)
SERVED, GONE = 12, 6


def _at(days: float) -> str:
    return (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _attempts(days: float) -> list[dict]:
    request = {"operation": "data_bus.connect", "target": "https://former-endpoint.example/mcp", "elapsed_seconds": 30.0}
    return [{"at": _at(days + index / 1000), "request": request} for index in range(RELAY_DIAGNOSTIC_MAX_RECORDS)]


def _stored_diagnostic() -> dict:
    """A full record: half its intervals older than the age bound."""

    old = RELAY_DIAGNOSTIC_RETENTION_DAYS + 3
    recent = [
        {
            "code": "work_relay_channel_connection_failed",
            "started_at": _at(days + 0.01),
            "ended_at": _at(days),
            "published_at": "",
            "failure_attempts": RELAY_DIAGNOSTIC_MAX_RECORDS,
            "retry_attempts": RELAY_DIAGNOSTIC_MAX_RECORDS - 1,
            "retry_outcome": "succeeded",
            "recovery_scope": "channel_cycle",
            "attempts": _attempts(days),
            "message": "The relay channel could not connect. " * 4,
        }
        for days in [old + index for index in range(10, 0, -1)] + [index * 0.2 + 0.1 for index in range(10, 0, -1)]
    ]
    return {
        "schema": RELAY_DIAGNOSTIC_SCHEMA,
        "state": "degraded",
        "code": "work_relay_channel_connection_failed",
        "started_at": _at(0.05),
        "last_attempt_at": _at(0.01),
        "failure_attempts": 40,
        "retry_attempts": 39,
        "retryable": True,
        "message": "The relay channel could not connect. " * 20,
        "attempts": _attempts(old) + _attempts(0.01),
        "recent": recent,
    }


def _store(field: SharedFieldStore, name: str, diagnostic: dict) -> None:
    row = field.read_worker(name)
    row["relay_diagnostic"] = diagnostic
    atomic_write_json(field._worker_path(name), row)  # noqa: SLF001 - seed a stored record


def _host(tmp_path):
    host = host_config.initialize_host_config(
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
    field = SharedFieldStore(host.field_root)
    names: list[str] = []
    for index in range(SERVED + GONE):
        identity = WorkerSessionIdentity.create("codex", f"11111111-1111-4111-8111-{index:012d}")
        host_config.enroll_worker_channel(
            host.path, identity=identity, profile=f"problem-board-codex-{index}", authorized=True
        )
        field.register_worker(
            worker_name=identity.worker_name,
            worker_alias=f"worker-{index}",
            worker_identity=identity.worker_identity,
            runtime_kind=identity.runtime_kind,
            runtime_session_id=identity.runtime_session_id,
            capabilities=[],
            authority_label="connection-hub:test",
            host_id=host.host_id,
            host_label=host.host_label,
            host_kind=host.host_kind,
            relay_id=host.relay_id,
        )
        _store(field, identity.worker_name, _stored_diagnostic())
        if index >= SERVED:
            host_config.set_worker_channel_state(host.path, identity=identity, state="disabled")
        names.append(identity.worker_name)
    return host_config.HostRelayConfig.load(host.path), field, names


def _size(value) -> int:
    return len(json.dumps(value, separators=(",", ":")))


def test_plain_host_inspect_prints_a_bounded_summary_and_names_the_full_view(tmp_path):
    host, field, names = _host(tmp_path)
    stored = sum(_size(field.read_worker(name)["relay_diagnostic"]) for name in names)

    view = cli._host_view(host)  # noqa: SLF001 - the command's own view
    diagnostics = view["relay"]["diagnostics"]

    # The pinned size: 18 stored records of ~33 KB each become a few KB in all.
    assert stored > 500_000
    assert _size(diagnostics) < 16_000, _size(diagnostics)
    assert _size(view) < 40_000, _size(view)
    assert json.dumps(diagnostics).count('"attempts"') == 0
    assert diagnostics["full_diagnostics"] == ["pb", "host", "inspect", "--diagnostics"]
    # Gone sessions are counted, not printed.
    assert [channel["worker_name"] for channel in diagnostics["channels"]] == names[:SERVED]
    assert diagnostics["disabled_channels_not_shown"] == GONE
    # Per channel: its state, and its last error with time.
    summary = diagnostics["channels"][0]["relay_diagnostic"]
    assert summary["state"] == "degraded" and summary["code"] == "work_relay_channel_connection_failed"
    assert summary["started_at"] == _at(0.05) and summary["last_attempt_at"] == _at(0.01)
    assert len(summary["message"]) <= 300
    assert summary["recent_count"] == 10, "only intervals within the age bound count"
    assert summary["latest"]["ended_at"] == _at(0.3) and summary["latest"]["code"]

    brief = render_envelope(
        {"ok": True, "result": {"schema": "problem-board.relay-service.v1", "relay_diagnostics": diagnostics}}
    )
    assert "full diagnostics: pb host inspect --diagnostics" in brief
    assert f"disabled channels not shown: {GONE}" in brief


def test_the_diagnostics_option_prints_the_stored_lists_within_their_age_bound(tmp_path):
    host, _field, names = _host(tmp_path)
    parsed = cli.build_parser().parse_args(["host", "inspect", "--diagnostics", "--config", str(host.path)])
    assert parsed.diagnostics is True

    full = cli._host_command(parsed)["relay"]["diagnostics"]  # noqa: SLF001

    assert [channel["worker_name"] for channel in full["channels"]] == names[:SERVED]
    cutoff = _at(RELAY_DIAGNOSTIC_RETENTION_DAYS)
    for channel in full["channels"]:
        diagnostic = channel["relay_diagnostic"]
        assert len(diagnostic["recent"]) == 10
        assert all(item["ended_at"] > cutoff for item in diagnostic["recent"])
        assert all(attempt["at"] > cutoff for attempt in diagnostic["attempts"])
        assert all(
            attempt["at"] > cutoff for item in diagnostic["recent"] for attempt in item["attempts"]
        )
    assert "full_diagnostics" not in full


def test_a_write_ages_out_the_old_intervals_as_well_as_counting_them(tmp_path):
    host, field, names = _host(tmp_path)

    field.record_relay_channel_recovered(names[0])
    recovered = field.read_worker(names[0])["relay_diagnostic"]
    cutoff = _at(RELAY_DIAGNOSTIC_RETENTION_DAYS)
    # The ten old intervals are gone; the ten current ones and the closed one stay.
    assert len(recovered["recent"]) == 11
    assert all(item["ended_at"] > cutoff for item in recovered["recent"])
    assert all(attempt["at"] > cutoff for item in recovered["recent"] for attempt in item["attempts"])

    _store(field, names[1], _stored_diagnostic())
    field.record_relay_channel_degraded(names[1], code="work_relay_channel_connection_failed")
    repeated = field.read_worker(names[1])["relay_diagnostic"]
    assert len(repeated["attempts"]) == RELAY_DIAGNOSTIC_MAX_RECORDS
    assert all(attempt["at"] > cutoff for attempt in repeated["attempts"])
    assert len(repeated["recent"]) == 10


def test_housekeeping_drops_gone_channels_and_ages_out_a_channel_that_stopped_writing(tmp_path):
    host, field, names = _host(tmp_path)
    served = [channel.worker_name for channel in host.workers if channel.state != "disabled"]
    revisions = {name: field.read_worker(name)["revision"] for name in names}

    summary = local_state_maintenance.run_local_state_maintenance(field, serving_channels=served)

    assert summary["retention"]["relay_diagnostics"] == {"pruned": SERVED, "dropped": GONE}
    for name in names[SERVED:]:
        assert "relay_diagnostic" not in field.read_worker(name)
    cutoff = _at(RELAY_DIAGNOSTIC_RETENTION_DAYS)
    for name in names[:SERVED]:
        row = field.read_worker(name)
        assert row["revision"] == revisions[name] + 1
        assert len(row["relay_diagnostic"]["recent"]) == 10
        assert all(item["ended_at"] > cutoff for item in row["relay_diagnostic"]["recent"])

    # A second pass finds nothing to change and writes nothing.
    again = field.expire_relay_diagnostics(serving=served)
    assert again == {"pruned": 0, "dropped": 0}
    assert all(field.read_worker(name)["revision"] == revisions[name] + 1 for name in names[:SERVED])


def test_housekeeping_without_the_host_config_only_ages_and_drops_nothing(tmp_path):
    _host_config, field, names = _host(tmp_path)

    assert field.expire_relay_diagnostics(serving=None) == {"pruned": SERVED + GONE, "dropped": 0}
    assert all("relay_diagnostic" in field.read_worker(name) for name in names)


def test_relay_service_status_carries_the_same_summary(tmp_path):
    host, _field, _names = _host(tmp_path)

    status_view = host_relay_diagnostics(host)  # what `pb relay-service status` prints
    assert json.dumps(status_view).count('"attempts"') == 0
    assert status_view == cli._host_view(host)["relay"]["diagnostics"]  # noqa: SLF001
