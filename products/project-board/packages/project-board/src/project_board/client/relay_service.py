from __future__ import annotations

import hashlib
import os
import plistlib
import platform
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from ..contract.errors import DomainError
from .diagnostics import host_relay_diagnostics
from .host_config import HostRelayConfig
from .relay_source import (
    CLIENT_SOURCE_PATHS,
    client_source_root,
    describe_source,
    read_startup_record,
)


SERVICE_SCHEMA = "problem-board.relay-service.v1"
STARTUP_RECORD_NAME = "relay.start.json"
# How long an activation waits for the new process to write its startup
# record. A relay start is a Python import and a config load, seconds, and
# a record that has not appeared in this long is a failed start.
STARTUP_WAIT_SECONDS = 30.0
STARTUP_POLL_SECONDS = 0.5


# The relay's file-descriptor limit, rendered into the LaunchAgent and the
# systemd user unit. A launchd agent otherwise inherits `launchctl limit
# maxfiles`, 256 soft on this host, and a user unit inherits the session's
# nofile limit. Neither was chosen for a process that holds a socket per
# worker session plus the control-plane and Data Bus connections and opens
# worker-row and receipt files on every cycle. Eleven `Errno 24` tracebacks
# sit in the relay log (io.read_json seven times, store._recover_expired_mail
# four), all in its unstamped part, and the ceiling was never raised (W199).
#
# Measured on 2026-09-20, relay pid 76474 at 135be994, 192 lsof readings from
# 11:55:40Z to 12:26:59Z with four attached worker channels: min 11, median 13,
# mean 13.3, p95 15, max 16 descriptors. Thirteen is the floor: nine fixed
# (null, two logs, kqueue, three unix, NPOLICY, systm) plus one Data Bus socket
# per attached worker channel. Everything above it was a field JSON read or a
# short control-plane connection, at most three at once, never a pipe.
#
# Model: 9 + 6 per worker, six being the persistent socket plus the most a
# worker's cycle holds at one instant (a control-plane connection, a field
# read, three pipes of a Codex queue listing). 1024 holds 169 such workers all
# at their peak at once, 42 times the four on this host, and stays under 1% of
# kern.maxfilesperproc (184,320), so it costs nothing to grant.
#
# What hit 256 was not load. 4,092 aiohttp "Unclosed client session" warnings
# sit in the log, every one before stamped logging began on 2026-09-19 at
# 11:09:57Z and none since. A leak of that class is a defect and no limit
# covers it. At today's four sessions and the 15 s per-session cycle floor
# (W197), a leak of one descriptor per session per cycle binds 256 in about
# 15 minutes and 1024 in about 63, long enough for the named failure code and
# the start line in cli.py to be read before the relay stops reading its
# field. Higher would only hide the same leak longer.
RELAY_FILE_DESCRIPTOR_LIMIT = 1024


def _service_suffix(config_path: Path) -> str:
    return hashlib.sha256(str(config_path).encode("utf-8")).hexdigest()[:12]


def _systemd_quote(value: str) -> str:
    """Quote one ExecStart argument; `%` is a unit specifier, so it is doubled."""

    return (
        '"'
        + _systemd_value(value).replace("\\", "\\\\").replace('"', '\\"')
        + '"'
    )


def _systemd_value(value: str) -> str:
    """A unit setting value that systemd reads verbatim.

    Settings that take one path (WorkingDirectory=, StandardOutput=append:)
    do not accept quotes: a quoted path is read as relative and the unit is
    refused as a bad setting, so the relay never starts.
    """

    if "\n" in value:
        raise DomainError(
            "work_relay_service_path_invalid",
            "A relay service path cannot contain a newline.",
        )
    return value.replace("%", "%%")


def _run(
    command: Sequence[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            check=check,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise DomainError(
            "work_relay_service_manager_missing",
            "This host does not provide the required user service manager.",
            details={"command": str(command[0])},
        ) from exc
    except subprocess.CalledProcessError as exc:
        is_launchd = bool(command and Path(command[0]).name == "launchctl")
        message = (
            "launchd could not change the login-scoped Problem Board relay. "
            "Run the same pb relay-service command from the logged-in user's "
            "normal Terminal; do not run it as root."
            if is_launchd
            else "The host user-service command failed."
        )
        details: dict[str, Any] = {
            "command": list(command),
            "returncode": exc.returncode,
            "stdout": exc.stdout.strip(),
            "stderr": exc.stderr.strip(),
        }
        if is_launchd:
            details["recovery"] = (
                "Use the logged-in desktop user's Terminal so launchd and the "
                "user's native credential store share the intended session."
            )
        raise DomainError(
            "work_relay_service_command_failed",
            message,
            details=details,
        ) from exc


@dataclass(frozen=True, slots=True)
class RelayService:
    config_path: Path
    executable: Path
    script: Path
    system: str
    service_id: str
    definition_path: Path
    stdout_path: Path
    stderr_path: Path
    user_home: Path
    # checkout: the entrypoint in the shared work tree, loaded as it is at
    # start. snapshot: the entrypoint under relay-source/current, one exported
    # commit (W202).
    source_mode: str
    source_root: Path
    module_entrypoint: bool

    @classmethod
    def create(
        cls,
        config_path: str | Path,
        *,
        executable: str | Path | None = None,
        script: str | Path | None = None,
        system: str | None = None,
        home: str | Path | None = None,
    ) -> "RelayService":
        selected_config = Path(config_path).expanduser().resolve()
        HostRelayConfig.load(selected_config)
        selected_system = str(system or platform.system()).strip()
        user_home = (
            Path(home).expanduser().resolve() if home else Path.home().resolve()
        )
        suffix = _service_suffix(selected_config)
        if selected_system == "Darwin":
            service_id = f"tech.kdcube.problem-board.relay.{suffix}"
            definition = user_home / "Library" / "LaunchAgents" / f"{service_id}.plist"
        elif selected_system == "Linux":
            service_id = f"kdcube-problem-board-relay-{suffix}.service"
            definition = user_home / ".config" / "systemd" / "user" / service_id
        else:
            raise DomainError(
                "work_relay_service_platform_unsupported",
                "Automatic relay supervision supports macOS launchd and Linux "
                "systemd user services.",
                details={"system": selected_system},
            )
        logs = selected_config.parent / "logs"
        selected_executable = Path(executable or sys.executable).expanduser()
        if not selected_executable.is_absolute():
            selected_executable = Path(os.path.abspath(selected_executable))
        source_root = client_source_root(selected_config)
        if script is not None:
            selected_script = Path(script).expanduser().resolve()
            source_mode = "checkout"
            module_entrypoint = False
        else:
            selected_script = Path(__file__).resolve().with_name("entrypoint.py")
            module_entrypoint = True
            from .source_control import effective_selection

            try:
                selected = effective_selection(source_root)
                source_mode = str(selected.get("mode") or "unknown")
            except DomainError:
                source_mode = "invalid"
        return cls(
            config_path=selected_config,
            # Preserve a virtualenv executable symlink. Resolving it would make
            # the service bypass the environment that owns relay dependencies.
            executable=selected_executable,
            script=selected_script,
            system=selected_system,
            service_id=service_id,
            definition_path=definition,
            stdout_path=logs / "relay.stdout.log",
            stderr_path=logs / "relay.stderr.log",
            user_home=user_home,
            source_mode=source_mode,
            source_root=source_root,
            module_entrypoint=module_entrypoint,
        )

    @property
    def startup_record_path(self) -> Path:
        return self.stdout_path.parent / STARTUP_RECORD_NAME

    @property
    def program_arguments(self) -> tuple[str, ...]:
        entrypoint = (
            ("-m", "project_board.client.entrypoint")
            if self.module_entrypoint
            else (str(self.script),)
        )
        return (
            str(self.executable),
            *entrypoint,
            "relay",
            "--config",
            str(self.config_path),
        )

    def render(self) -> bytes:
        if self.system == "Darwin":
            return plistlib.dumps(
                {
                    "Label": self.service_id,
                    "ProgramArguments": list(self.program_arguments),
                    "RunAtLoad": True,
                    "KeepAlive": True,
                    "WorkingDirectory": str(self.config_path.parent),
                    "StandardOutPath": str(self.stdout_path),
                    "StandardErrorPath": str(self.stderr_path),
                    "ProcessType": "Background",
                    "SoftResourceLimits": {"NumberOfFiles": RELAY_FILE_DESCRIPTOR_LIMIT},
                },
                sort_keys=True,
            )
        command = " ".join(_systemd_quote(part) for part in self.program_arguments)
        return (
            "[Unit]\n"
            "Description=KDCube Problem Board host relay\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n\n"
            "[Service]\n"
            f"ExecStart={command}\n"
            f"WorkingDirectory={_systemd_value(str(self.config_path.parent))}\n"
            "Restart=always\n"
            "RestartSec=5\n"
            f"LimitNOFILE={RELAY_FILE_DESCRIPTOR_LIMIT}\n"
            f"StandardOutput=append:{_systemd_value(str(self.stdout_path))}\n"
            f"StandardError=append:{_systemd_value(str(self.stderr_path))}\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        ).encode("utf-8")

    def _write_definition(self) -> None:
        self.definition_path.parent.mkdir(parents=True, exist_ok=True)
        self.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.definition_path.with_suffix(
            f"{self.definition_path.suffix}.tmp.{os.getpid()}"
        )
        temporary.write_bytes(self.render())
        os.replace(temporary, self.definition_path)

    def _launchd_target(self) -> str:
        return f"gui/{os.getuid()}/{self.service_id}"

    def install(self) -> dict[str, Any]:
        """Write the stable bootstrap definition and (re)start under it."""

        service = self
        if service.module_entrypoint:
            from .relay_source import read_selection, released_selection, write_selection
            from .source_control import installed_release_source

            if not read_selection(service.source_root):
                released = installed_release_source()
                write_selection(
                    service.source_root,
                    released_selection(str(released.get("version") or "")),
                )
                service = RelayService.create(
                    self.config_path,
                    executable=self.executable,
                    system=self.system,
                    home=self.user_home,
                )
        return service._install_definition()

    def _install_definition(self) -> dict[str, Any]:
        self._write_definition()
        if self.system == "Darwin":
            _run(["launchctl", "bootout", self._launchd_target()], check=False)
            _run(
                [
                    "launchctl",
                    "bootstrap",
                    f"gui/{os.getuid()}",
                    str(self.definition_path),
                ]
            )
            _run(["launchctl", "kickstart", "-k", self._launchd_target()])
        else:
            _run(["systemctl", "--user", "daemon-reload"])
            _run(["systemctl", "--user", "enable", "--now", self.service_id])
        return self.status()

    def start(self) -> dict[str, Any]:
        if not self.definition_path.exists():
            raise DomainError(
                "work_relay_service_not_installed",
                "Install the relay user service before starting it.",
                status=404,
            )
        if self.system == "Darwin":
            current = _run(
                ["launchctl", "print", self._launchd_target()], check=False
            )
            if current.returncode == 0:
                return {
                    **self.status(),
                    "already_running": True,
                    "command_output": "",
                }
            _run(
                [
                    "launchctl",
                    "bootstrap",
                    f"gui/{os.getuid()}",
                    str(self.definition_path),
                ]
            )
            result = _run(["launchctl", "kickstart", self._launchd_target()])
        else:
            result = _run(["systemctl", "--user", "start", self.service_id])
        return {
            **self.status(),
            "already_running": False,
            "command_output": result.stdout.strip(),
        }

    def stop(self) -> dict[str, Any]:
        if self.system == "Darwin":
            result = _run(
                ["launchctl", "bootout", self._launchd_target()], check=False
            )
        else:
            result = _run(
                ["systemctl", "--user", "stop", self.service_id], check=False
            )
        return {**self.status(), "command_output": result.stdout.strip()}

    def restart(self) -> dict[str, Any]:
        if self.system == "Darwin":
            if not self.definition_path.exists():
                raise DomainError(
                    "work_relay_service_not_installed",
                    "Install the relay user service before restarting it.",
                    status=404,
                )
            current = _run(
                ["launchctl", "print", self._launchd_target()], check=False
            )
            if current.returncode != 0:
                started = self.start()
                return {**started, "restarted": False}
            result = _run(
                ["launchctl", "kickstart", "-k", self._launchd_target()]
            )
        else:
            result = _run(["systemctl", "--user", "restart", self.service_id])
        return {
            **self.status(),
            "command_output": result.stdout.strip(),
            "restarted": True,
        }

    def definition_uses_bootstrap(self) -> bool:
        """Whether the installed service enters through the released module."""

        if not self.definition_path.is_file():
            return False
        expected = list(self.program_arguments)
        if not self.module_entrypoint:
            expected = [
                str(self.executable),
                "-m",
                "project_board.client.entrypoint",
                "relay",
                "--config",
                str(self.config_path),
            ]
        try:
            if self.system == "Darwin":
                value = plistlib.loads(self.definition_path.read_bytes())
                return list(value.get("ProgramArguments") or []) == expected
            text = self.definition_path.read_text(encoding="utf-8")
        except (OSError, ValueError, plistlib.InvalidFileException):
            return False
        command = " ".join(_systemd_quote(part) for part in expected)
        return f"ExecStart={command}\n" in text

    def uninstall(self) -> dict[str, Any]:
        if self.system == "Darwin":
            _run(["launchctl", "bootout", self._launchd_target()], check=False)
        else:
            _run(
                ["systemctl", "--user", "disable", "--now", self.service_id],
                check=False,
            )
            _run(["systemctl", "--user", "daemon-reload"], check=False)
        self.definition_path.unlink(missing_ok=True)
        return self.status()

    def await_source(
        self,
        expected: dict[str, Any] | Any,
        *,
        since: str,
        wait_seconds: float,
    ) -> dict[str, Any]:
        """The new process's own record naming the selected source, or why not."""

        from .source_control import source_matches

        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        last: dict[str, Any] = {}
        while True:
            record = read_startup_record(self.startup_record_path)
            started_at = str(record.get("started_at") or "")
            if record and started_at >= since:
                source = record.get("source") if isinstance(record.get("source"), dict) else {}
                if source_matches(source, expected):
                    return {"state": "started", "pid": record.get("pid"), "started_at": started_at}
                return {
                    "state": "source_mismatch",
                    "pid": record.get("pid"),
                    "started_at": started_at,
                    "observed": source,
                    "expected": dict(expected),
                }
            last = record
            if time.monotonic() >= deadline:
                return {
                    "state": "startup_record_missing",
                    "waited_seconds": wait_seconds,
                    "last_record_started_at": str(last.get("started_at") or ""),
                }
            time.sleep(STARTUP_POLL_SECONDS)

    def status(self) -> dict[str, Any]:
        if self.system == "Darwin":
            process = _run(["launchctl", "print", self._launchd_target()], check=False)
        else:
            process = _run(
                ["systemctl", "--user", "is-active", self.service_id], check=False
            )
        config = HostRelayConfig.load(self.config_path)
        if self.module_entrypoint:
            from .source_control import effective_selection, installed_release_source

            try:
                selected_source = effective_selection(self.source_root)
                selection_error = ""
            except DomainError as exc:
                selected_source = {"mode": "invalid"}
                selection_error = exc.code
            try:
                bootstrap_source = installed_release_source()
            except DomainError as exc:
                bootstrap_source = {"mode": "invalid", "error": exc.code}
        else:
            selected_source = describe_source(self.script)
            bootstrap_source = selected_source
            selection_error = ""
        return {
            "schema": SERVICE_SCHEMA,
            "service_id": self.service_id,
            "system": self.system,
            "installed": self.definition_path.exists(),
            "running": process.returncode == 0,
            "definition": str(self.definition_path),
            "config": str(self.config_path),
            "program_arguments": list(self.program_arguments),
            "source_mode": str(selected_source.get("mode") or self.source_mode),
            "source_root": str(self.source_root),
            "source": selected_source,
            "source_selection_error": selection_error,
            "bootstrap_source": bootstrap_source,
            "startup_record": read_startup_record(self.startup_record_path),
            "stdout": str(self.stdout_path),
            "stderr": str(self.stderr_path),
            "manager_status": process.stdout.strip(),
            "manager_error": process.stderr.strip(),
            "relay_diagnostics": host_relay_diagnostics(config),
        }


__all__ = ["RelayService", "SERVICE_SCHEMA"]
