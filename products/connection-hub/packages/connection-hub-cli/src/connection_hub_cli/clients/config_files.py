from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import json5
import yaml
from filelock import FileLock, Timeout

from connection_hub_cli.errors import ClientConfigurationError


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise ClientConfigurationError(
            "client_config_symlink_rejected",
            f"Connection Hub refuses to edit or inspect a symbolic-link client configuration: {path}.",
        )


def _read_text(path: Path) -> str | None:
    _reject_symlink(path)
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ClientConfigurationError(
            "client_config_read_failed",
            f"The client configuration could not be read: {path}.",
        ) from exc


def read_json_object(
    path: Path, *, json5_allowed: bool = False
) -> dict[str, Any] | None:
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        value = json5.loads(raw) if json5_allowed else json.loads(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ClientConfigurationError(
            "invalid_client_config",
            f"The client configuration is not valid JSON: {path}.",
        ) from exc
    if not isinstance(value, dict):
        raise ClientConfigurationError(
            "invalid_client_config",
            f"The client configuration root must be an object: {path}.",
        )
    return value


def read_yaml_object(path: Path) -> dict[str, Any] | None:
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ClientConfigurationError(
            "invalid_client_config",
            f"The client configuration is not valid YAML: {path}.",
        ) from exc
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ClientConfigurationError(
            "invalid_client_config",
            f"The client configuration root must be an object: {path}.",
        )
    return value


def nested_value(value: dict[str, Any] | None, path: Sequence[str]) -> Any:
    current: Any = value
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def mutate_json_object(path: Path, operation: Callable[[dict[str, Any]], bool]) -> bool:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink(path)
    lock_path = path.with_suffix(f"{path.suffix}.connection-hub.lock")
    _reject_symlink(lock_path)
    lock = FileLock(str(lock_path), timeout=10, mode=0o600)
    try:
        with lock:
            before = read_json_object(path) or {}
            result = operation(before)
            if not result:
                return False
            encoded = (json.dumps(before, indent=2) + "\n").encode("utf-8")
            original_mode = (
                stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
            )
            descriptor, temporary_name = tempfile.mkstemp(
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
            )
            temporary = Path(temporary_name)
            try:
                os.fchmod(descriptor, original_mode)
                stream = os.fdopen(descriptor, "wb", closefd=True)
                descriptor = -1
                with stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            except OSError as exc:
                raise ClientConfigurationError(
                    "client_config_write_failed",
                    f"The client configuration could not be updated: {path}.",
                ) from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                temporary.unlink(missing_ok=True)
            return result
    except Timeout as exc:
        raise ClientConfigurationError(
            "client_config_lock_timeout",
            f"Timed out waiting to update the client configuration: {path}.",
        ) from exc
