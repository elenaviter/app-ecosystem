"""Bounded file logging for the supervised Problem Board relay."""

from __future__ import annotations

import logging
import os
import stat
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


RELAY_LOG_FILENAME = "relay.stderr.log"
# One active file plus three backups bounds each host target at 40 MiB while
# retaining several recent relay windows for diagnosis.
RELAY_LOG_MAX_BYTES = 10 * 1024 * 1024
RELAY_LOG_BACKUP_COUNT = 3


class UtcPerLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        rendered = super().format(record)
        timestamp = (
            datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        prefix = f"{timestamp} {record.levelname} {record.name}: "
        return "\n".join(
            f"{prefix}{line}" for line in (rendered.splitlines() or [""])
        )


def relay_log_path(config_path: str | Path) -> Path:
    return Path(config_path).expanduser().resolve().parent / "logs" / RELAY_LOG_FILENAME


def _backup_path(path: Path, index: int) -> Path:
    return path.with_name(f"{path.name}.{index}")


def _trim_to_tail(path: Path, max_bytes: int) -> None:
    current = path.stat()
    if current.st_size <= max_bytes:
        return
    temporary = path.with_name(f".{path.name}.trim.{os.getpid()}")
    try:
        with path.open("rb") as source:
            source.seek(-max_bytes, os.SEEK_END)
            tail = source.read(max_bytes)
        temporary.write_bytes(tail)
        os.chmod(temporary, stat.S_IMODE(current.st_mode))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_relay_log(
    path: Path,
    *,
    max_bytes: int = RELAY_LOG_MAX_BYTES,
    backup_count: int = RELAY_LOG_BACKUP_COUNT,
) -> None:
    """Bound legacy files before ``RotatingFileHandler`` opens the active log."""

    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if backup_count < 0:
        raise ValueError("backup_count cannot be negative")

    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = f"{path.name}."
    for candidate in path.parent.glob(f"{path.name}.*"):
        suffix = candidate.name.removeprefix(prefix)
        if suffix.isdigit() and int(suffix) > backup_count:
            candidate.unlink(missing_ok=True)

    if path.is_file() and path.stat().st_size > max_bytes:
        if backup_count == 0:
            path.unlink()
        else:
            _backup_path(path, backup_count).unlink(missing_ok=True)
            for index in range(backup_count - 1, 0, -1):
                source = _backup_path(path, index)
                if source.exists():
                    os.replace(source, _backup_path(path, index + 1))
            os.replace(path, _backup_path(path, 1))

    for index in range(1, backup_count + 1):
        backup = _backup_path(path, index)
        if backup.is_file():
            _trim_to_tail(backup, max_bytes)


def rotating_relay_handler(
    path: Path,
    *,
    max_bytes: int = RELAY_LOG_MAX_BYTES,
    backup_count: int = RELAY_LOG_BACKUP_COUNT,
) -> RotatingFileHandler:
    prepare_relay_log(path, max_bytes=max_bytes, backup_count=backup_count)
    handler = RotatingFileHandler(
        path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(UtcPerLineFormatter("%(message)s"))
    return handler


def configure_relay_logging(
    config_path: Path | None,
    *,
    mirror_to_stderr: bool,
) -> Path | None:
    handlers: list[logging.Handler] = []
    path: Path | None = None
    if config_path is not None:
        path = relay_log_path(config_path)
        handlers.append(rotating_relay_handler(path))
    if mirror_to_stderr or not handlers:
        stream = logging.StreamHandler()
        stream.setFormatter(UtcPerLineFormatter("%(message)s"))
        handlers.append(stream)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    return path


def relay_log_status(
    path: Path,
    *,
    max_bytes: int = RELAY_LOG_MAX_BYTES,
    backup_count: int = RELAY_LOG_BACKUP_COUNT,
) -> dict[str, object]:
    files = []
    candidates = (
        path,
        *(_backup_path(path, i) for i in range(1, backup_count + 1)),
    )
    for candidate in candidates:
        if candidate.is_file():
            files.append(
                {"path": str(candidate), "size_bytes": candidate.stat().st_size}
            )
    return {
        "path": str(path),
        "size_bytes": path.stat().st_size if path.is_file() else 0,
        "max_bytes": max_bytes,
        "backup_count": backup_count,
        "max_total_bytes": max_bytes * (backup_count + 1),
        "total_size_bytes": sum(int(item["size_bytes"]) for item in files),
        "files": files,
    }


__all__ = [
    "RELAY_LOG_BACKUP_COUNT",
    "RELAY_LOG_FILENAME",
    "RELAY_LOG_MAX_BYTES",
    "UtcPerLineFormatter",
    "configure_relay_logging",
    "prepare_relay_log",
    "relay_log_path",
    "relay_log_status",
    "rotating_relay_handler",
]
