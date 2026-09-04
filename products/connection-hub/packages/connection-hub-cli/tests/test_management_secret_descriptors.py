from __future__ import annotations

import os

import pytest

import connection_hub_cli.management.secret_descriptors as descriptor_module
from connection_hub_cli.errors import ConnectionHubCliError
from connection_hub_cli.management import ExportedSecret, ManagementSecretTarget
from connection_hub_cli.management.secret_descriptors import write_secret_descriptors


def _exported(*, key: str, value: str) -> ExportedSecret:
    return ExportedSecret(
        target=ManagementSecretTarget.create(scope="platform", key=key),
        value=value,
    )


def test_descriptor_export_rejects_scalar_mapping_conflicts_before_writing(
    tmp_path,
) -> None:
    output = tmp_path / "export"

    with pytest.raises(ConnectionHubCliError) as raised:
        write_secret_descriptors(
            output,
            [
                _exported(key="provider", value="secret-a"),
                _exported(key="provider.api_key", value="secret-b"),
            ],
        )

    assert raised.value.code == "secret_export_descriptor_conflict"
    assert "secret-a" not in str(raised.value)
    assert "secret-b" not in str(raised.value)
    assert not output.exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_descriptor_export_never_clobbers_an_existing_directory(tmp_path) -> None:
    output = tmp_path / "export"
    output.mkdir()
    existing = output / "sentinel"
    existing.write_text("keep", encoding="utf-8")

    with pytest.raises(ConnectionHubCliError) as raised:
        write_secret_descriptors(
            output,
            [_exported(key="provider.api_key", value="secret-marker")],
        )

    assert raised.value.code == "secret_export_output_exists"
    assert existing.read_text(encoding="utf-8") == "keep"


def test_descriptor_export_writes_complete_private_pair(tmp_path) -> None:
    output = tmp_path / "export"

    result = write_secret_descriptors(
        output,
        [_exported(key="provider.api_key", value="secret-marker")],
    )

    assert result.directory == output.absolute()
    assert result.platform_count == 1
    assert result.bundle_count == 0
    assert result.platform_path.read_text(encoding="utf-8") == (
        "provider:\n  api_key: secret-marker\n"
    )
    assert result.bundles_path.read_text(encoding="utf-8") == (
        "bundles:\n  version: '1'\n  items: []\n"
    )
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o700
        assert result.platform_path.stat().st_mode & 0o777 == 0o600
        assert result.bundles_path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(".*.tmp")) == []


def test_descriptor_export_detects_target_created_during_staging(
    tmp_path,
    monkeypatch,
) -> None:
    output = tmp_path / "export"
    real_mkdir = descriptor_module.Path.mkdir

    def race_mkdir(path, *args, **kwargs):
        if path == output.absolute():
            real_mkdir(path)
            (path / "sentinel").write_text("keep", encoding="utf-8")
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(descriptor_module.Path, "mkdir", race_mkdir)

    with pytest.raises(ConnectionHubCliError) as raised:
        write_secret_descriptors(
            output,
            [_exported(key="provider.api_key", value="secret-marker")],
        )

    assert raised.value.code == "secret_export_output_exists"
    assert (output / "sentinel").read_text(encoding="utf-8") == "keep"
    assert list(tmp_path.glob(".*.tmp")) == []


def test_descriptor_export_removes_owned_partial_destination(
    tmp_path,
    monkeypatch,
) -> None:
    output = tmp_path / "export"
    real_replace = descriptor_module.os.replace
    calls = 0

    def fail_second_replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated second-file failure")
        return real_replace(source, destination)

    monkeypatch.setattr(descriptor_module.os, "replace", fail_second_replace)

    with pytest.raises(ConnectionHubCliError) as raised:
        write_secret_descriptors(
            output,
            [_exported(key="provider.api_key", value="secret-marker")],
        )

    assert raised.value.code == "secret_export_output_write_failed"
    assert not output.exists()
    assert list(tmp_path.glob(".*.tmp")) == []


def test_descriptor_export_closes_file_when_private_mode_application_fails(
    tmp_path,
    monkeypatch,
) -> None:
    opened: list[int] = []
    closed: list[int] = []
    real_close = descriptor_module.os.close

    def fail_mode(descriptor, _path, _mode):
        opened.append(descriptor)
        raise OSError("mode unavailable")

    def record_close(descriptor):
        closed.append(descriptor)
        real_close(descriptor)

    monkeypatch.setattr(descriptor_module, "apply_open_file_mode", fail_mode)
    monkeypatch.setattr(descriptor_module.os, "close", record_close)

    with pytest.raises(ConnectionHubCliError) as raised:
        write_secret_descriptors(
            tmp_path / "export",
            [_exported(key="provider.api_key", value="secret-marker")],
        )

    assert raised.value.code == "secret_export_output_write_failed"
    assert opened and opened[0] in closed
    assert list(tmp_path.glob(".*.tmp")) == []
