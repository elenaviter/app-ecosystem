from __future__ import annotations

import logging
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from project_board.client import entrypoint, relay_service
from project_board.client.relay_logging import (
    RELAY_CRASH_LOG_MAX_BYTES,
    prepare_relay_crash_log,
    prepare_relay_log,
    relay_log_status,
    rotating_relay_handler,
)
from project_board.client.relay_service import RelayService


def _service(tmp_path: Path, *, system: str) -> RelayService:
    logs = tmp_path / "target" / "logs"
    suffix = "plist" if system == "Darwin" else "service"
    return RelayService(
        config_path=tmp_path / "target" / "relay.json",
        executable=Path("/runtime/bin/python"),
        script=tmp_path / "entrypoint.py",
        system=system,
        service_id="relay.test",
        definition_path=tmp_path / f"relay.{suffix}",
        stdout_path=logs / "relay.stdout.log",
        stderr_path=logs / "relay.stderr.log",
        user_home=tmp_path,
        source_mode="snapshot",
        source_root=tmp_path / "client-source",
        module_entrypoint=True,
    )


def test_oversized_legacy_log_is_rotated_and_bounded_on_start(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "relay.stderr.log"
    path.parent.mkdir()
    legacy = bytes(range(256)) * 4
    path.write_bytes(legacy)
    path.with_name(f"{path.name}.1").write_bytes(b"previous")
    path.with_name(f"{path.name}.3").write_bytes(b"oldest")

    prepare_relay_log(path, max_bytes=128, backup_count=2)

    assert not path.exists()
    assert path.with_name(f"{path.name}.1").read_bytes() == legacy[-128:]
    assert path.with_name(f"{path.name}.2").read_bytes() == b"previous"
    assert not path.with_name(f"{path.name}.3").exists()


def test_rotating_handler_caps_active_log_and_removes_oldest(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "relay.stderr.log"
    handler = rotating_relay_handler(path, max_bytes=256, backup_count=2)
    logger = logging.Logger("relay-rotation-test", level=logging.INFO)
    logger.addHandler(handler)
    try:
        for index in range(40):
            logger.info("line-%02d %s", index, "x" * 40)
    finally:
        handler.close()

    status = relay_log_status(path, max_bytes=256, backup_count=2)
    files = status["files"]
    assert len(files) == 3
    assert status["total_size_bytes"] <= status["max_total_bytes"]
    assert all(item["size_bytes"] <= 256 for item in files)
    assert not path.with_name(f"{path.name}.3").exists()


def test_service_managers_use_a_separate_crash_log(tmp_path: Path) -> None:
    launchd_service = _service(tmp_path, system="Darwin")
    systemd_service = _service(tmp_path, system="Linux")
    launchd = plistlib.loads(launchd_service.render())
    systemd = systemd_service.render().decode("utf-8")

    assert launchd["StandardOutPath"] == os.devnull
    assert launchd["StandardErrorPath"] == str(launchd_service.crash_log_path)
    assert "StandardOutput=null\n" in systemd
    assert (
        f"StandardError=append:{systemd_service.crash_log_path}\n" in systemd
    )
    assert "append:relay.stderr.log" not in systemd


def test_crash_log_is_trimmed_in_place_and_reported(tmp_path: Path) -> None:
    relay_path = tmp_path / "logs" / "relay.stderr.log"
    crash_path = relay_path.with_name("relay.crash.log")
    crash_path.parent.mkdir()
    content = bytes(range(256)) * 8
    crash_path.write_bytes(content)
    inode = crash_path.stat().st_ino

    prepare_relay_crash_log(crash_path, max_bytes=256)

    assert crash_path.stat().st_ino == inode
    assert crash_path.read_bytes() == content[-256:]
    status = relay_log_status(relay_path)
    assert status["crash"] == {
        "path": str(crash_path),
        "size_bytes": 256,
        "max_bytes": RELAY_CRASH_LOG_MAX_BYTES,
    }


def test_uncaught_thread_exception_reaches_rotating_log(tmp_path: Path) -> None:
    config = tmp_path / "relay.json"
    script = f"""
import threading
from pathlib import Path
from project_board.client.relay_logging import configure_relay_logging

configure_relay_logging(Path({str(config)!r}), mirror_to_stderr=False)

def fail():
    raise RuntimeError("thread exploded")

thread = threading.Thread(target=fail, name="relay-crash-test")
thread.start()
thread.join()
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )

    rendered = (tmp_path / "logs" / "relay.stderr.log").read_text(
        encoding="utf-8"
    )
    assert "Uncaught exception in thread relay-crash-test" in rendered
    assert "RuntimeError: thread exploded" in rendered
    assert "RuntimeError: thread exploded" in result.stderr


def test_restart_reloads_launchd_definition_before_kickstart(
    tmp_path: Path, monkeypatch
) -> None:
    service = _service(tmp_path, system="Darwin")
    service.definition_path.write_text("stale", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command, *, check=True):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(relay_service, "_run", fake_run)
    monkeypatch.setattr(RelayService, "status", lambda self: {"running": True})

    result = service.restart()

    rendered = plistlib.loads(service.definition_path.read_bytes())
    assert rendered["StandardErrorPath"] == str(service.crash_log_path)
    assert [command[1] for command in calls] == [
        "print",
        "bootout",
        "bootstrap",
        "kickstart",
    ]
    assert result["restarted"] is True


def test_relay_logging_is_configured_before_source_dispatch(
    tmp_path: Path, monkeypatch
) -> None:
    config = tmp_path / "relay.json"
    events: list[tuple[str, Path | None]] = []

    monkeypatch.setattr(
        entrypoint.sys,
        "argv",
        ["pb", "relay", "--config", str(config)],
    )
    monkeypatch.setattr(entrypoint, "_config_argument", lambda argv: config)
    monkeypatch.setattr(
        entrypoint,
        "_configure_relay_logging",
        lambda selected: events.append(("logging", selected)),
    )

    def select(argv, *, config_path=None):
        events.append(("dispatch", config_path))
        return None

    monkeypatch.setattr(entrypoint, "_selected_command", select)
    monkeypatch.setattr(entrypoint.cli, "main", lambda: 0)

    assert entrypoint.main() == 0
    assert events == [("logging", config), ("dispatch", config)]
