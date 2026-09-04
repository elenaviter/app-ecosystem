from __future__ import annotations

import json
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from filelock import FileLock, Timeout

from connection_hub_cli.errors import (
    ClientConfigurationError,
    HostControlError,
    ProfileError,
    StateError,
)
from connection_hub_cli.filesystem import apply_open_file_mode
from connection_hub_cli.models import CallerProfile, HostSelection, ManagedInstallation

_T = TypeVar("_T")


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        try:
            os.close(directory_fd)
        except OSError:
            pass


class AtomicJsonState:
    def __init__(self, path: Path, *, schema: str, collection: str) -> None:
        self.path = Path(path)
        self.schema = schema
        self.collection = collection
        self.lock_path = self.path.with_suffix(f"{self.path.suffix}.lock")

    def _empty(self) -> dict[str, Any]:
        return {"schema": self.schema, self.collection: {}}

    def _prepare_parent(self) -> None:
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError as exc:
            raise StateError(
                "state_directory_permissions",
                f"Cannot secure the Connection Hub state directory: {self.path.parent}.",
            ) from exc

    @staticmethod
    def _reject_symlink(path: Path) -> None:
        if path.is_symlink():
            raise StateError(
                "state_symlink_rejected",
                f"Connection Hub refuses to use a symbolic link as a state file: {path}.",
            )

    def _load_unlocked(self) -> dict[str, Any]:
        self._reject_symlink(self.path)
        if not self.path.exists():
            return self._empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StateError(
                "invalid_state_file",
                f"Connection Hub cannot read its state file: {self.path}.",
            ) from exc
        if (
            not isinstance(value, dict)
            or value.get("schema") != self.schema
            or not isinstance(value.get(self.collection), dict)
        ):
            raise StateError(
                "invalid_state_schema",
                f"Connection Hub state has an unsupported schema: {self.path}.",
            )
        return value

    def _write_unlocked(self, value: dict[str, Any]) -> None:
        encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            apply_open_file_mode(descriptor, temporary, 0o600)
            stream = os.fdopen(descriptor, "wb", closefd=True)
            descriptor = -1
            with stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            raise StateError(
                "state_write_failed",
                f"Connection Hub cannot update its state file: {self.path}.",
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
        _fsync_directory(self.path.parent)

    def _with_lock(
        self, operation: Callable[[dict[str, Any]], _T], *, write: bool
    ) -> _T:
        self._prepare_parent()
        self._reject_symlink(self.lock_path)
        lock = FileLock(str(self.lock_path), timeout=10, mode=0o600)
        try:
            with lock:
                try:
                    os.chmod(self.lock_path, 0o600)
                except OSError as exc:
                    raise StateError(
                        "state_lock_permissions",
                        f"Cannot secure the Connection Hub state lock: {self.lock_path}.",
                    ) from exc
                value = self._load_unlocked()
                result = operation(value)
                if write:
                    self._write_unlocked(value)
                return result
        except Timeout as exc:
            raise StateError(
                "state_lock_timeout",
                f"Timed out waiting for the Connection Hub state lock: {self.lock_path}.",
            ) from exc

    def read(self) -> dict[str, Any]:
        self._reject_symlink(self.path)
        if not self.path.exists():
            return self._empty()
        return self._with_lock(lambda value: value, write=False)

    def mutate(self, operation: Callable[[dict[str, Any]], _T]) -> _T:
        return self._with_lock(operation, write=True)

    def mode(self) -> int | None:
        self._reject_symlink(self.path)
        if not self.path.exists():
            return None
        return stat.S_IMODE(self.path.stat().st_mode)


class ProfileStore:
    SCHEMA = "connection_hub_cli.profiles.v1"

    def __init__(self, path: Path) -> None:
        self.document = AtomicJsonState(path, schema=self.SCHEMA, collection="profiles")

    @property
    def path(self) -> Path:
        return self.document.path

    def list(self) -> list[CallerProfile]:
        raw = self.document.read()["profiles"]
        return [CallerProfile.from_dict(raw[name]) for name in sorted(raw)]

    def get(self, name: str) -> CallerProfile | None:
        raw = self.document.read()["profiles"].get(name)
        return CallerProfile.from_dict(raw) if isinstance(raw, dict) else None

    def require(self, name: str) -> CallerProfile:
        profile = self.get(name)
        if profile is None:
            raise ProfileError(
                "profile_not_found", f"Caller profile '{name}' does not exist."
            )
        return profile

    def add(self, profile: CallerProfile) -> None:
        def add_to(value: dict[str, Any]) -> None:
            profiles = value["profiles"]
            if profile.name in profiles:
                raise ProfileError(
                    "profile_exists",
                    f"Caller profile '{profile.name}' already exists.",
                )
            profiles[profile.name] = profile.to_dict()

        self.document.mutate(add_to)

    def update(self, profile: CallerProfile) -> None:
        def update_in(value: dict[str, Any]) -> None:
            profiles = value["profiles"]
            if profile.name not in profiles:
                raise ProfileError(
                    "profile_not_found",
                    f"Caller profile '{profile.name}' does not exist.",
                )
            profiles[profile.name] = profile.to_dict()

        self.document.mutate(update_in)

    def remove(self, name: str) -> CallerProfile:
        def remove_from(value: dict[str, Any]) -> CallerProfile:
            raw = value["profiles"].pop(name, None)
            if not isinstance(raw, dict):
                raise ProfileError(
                    "profile_not_found", f"Caller profile '{name}' does not exist."
                )
            return CallerProfile.from_dict(raw)

        return self.document.mutate(remove_from)


class InstallationStore:
    SCHEMA = "connection_hub_cli.client_installations.v1"

    def __init__(self, path: Path) -> None:
        self.document = AtomicJsonState(
            path, schema=self.SCHEMA, collection="installations"
        )

    @property
    def path(self) -> Path:
        return self.document.path

    def list(self) -> list[ManagedInstallation]:
        raw = self.document.read()["installations"]
        return [ManagedInstallation.from_dict(raw[key]) for key in sorted(raw)]

    def for_profile(self, profile: str) -> list[ManagedInstallation]:
        return [item for item in self.list() if item.profile == profile]

    def get(self, client: str, server_name: str) -> ManagedInstallation | None:
        key = f"{client}:{server_name}"
        raw = self.document.read()["installations"].get(key)
        return ManagedInstallation.from_dict(raw) if isinstance(raw, dict) else None

    def add(self, installation: ManagedInstallation) -> None:
        def add_to(value: dict[str, Any]) -> None:
            installations = value["installations"]
            if installation.registry_key in installations:
                raise ClientConfigurationError(
                    "installation_exists",
                    f"Connection Hub already manages '{installation.server_name}' for {installation.client}.",
                )
            installations[installation.registry_key] = installation.to_dict()

        self.document.mutate(add_to)

    def remove(self, client: str, server_name: str) -> ManagedInstallation:
        key = f"{client}:{server_name}"

        def remove_from(value: dict[str, Any]) -> ManagedInstallation:
            raw = value["installations"].pop(key, None)
            if not isinstance(raw, dict):
                raise ClientConfigurationError(
                    "installation_not_found",
                    f"Connection Hub does not manage '{server_name}' for {client}.",
                )
            return ManagedInstallation.from_dict(raw)

        return self.document.mutate(remove_from)


class HostStore:
    SCHEMA = "connection_hub_cli.host.v1"

    def __init__(self, path: Path) -> None:
        self.document = AtomicJsonState(path, schema=self.SCHEMA, collection="hosts")

    @property
    def path(self) -> Path:
        return self.document.path

    def get(self) -> HostSelection | None:
        raw = self.document.read()["hosts"].get("active")
        return HostSelection.from_dict(raw) if isinstance(raw, dict) else None

    def put(self, selection: HostSelection, *, replace: bool = False) -> bool:
        def update(value: dict[str, Any]) -> bool:
            raw = value["hosts"].get("active")
            current = HostSelection.from_dict(raw) if isinstance(raw, dict) else None
            if current is not None and current.target_key != selection.target_key:
                if not replace:
                    raise HostControlError(
                        "host_already_selected",
                        "Another Connection Hub application host is already selected. Use --replace to change it.",
                    )
            if current is not None and current.target_key == selection.target_key:
                value["hosts"]["active"] = selection.refreshed().to_dict()
                return False
            value["hosts"]["active"] = selection.to_dict()
            return True

        return self.document.mutate(update)
