from __future__ import annotations

import os

import pytest

from connection_hub_cli.errors import ConnectionHubCliError
from connection_hub_cli.management.secret_output import (
    validate_private_secret_output,
    write_private_secret,
)


def test_private_secret_write_is_atomic_private_and_exact(tmp_path) -> None:
    target = tmp_path / "provider.secret"

    result = write_private_secret(target, "line one\nline two\n")

    assert result == target.absolute()
    assert target.read_text() == "line one\nline two\n"
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob(".*.tmp")) == []


def test_private_secret_write_refuses_clobber_and_replaces_explicitly(tmp_path) -> None:
    target = tmp_path / "provider.secret"
    target.write_text("old")

    with pytest.raises(ConnectionHubCliError) as raised:
        write_private_secret(target, "new")
    assert raised.value.code == "secret_output_exists"
    assert target.read_text() == "old"

    write_private_secret(target, "new", replace=True)
    assert target.read_text() == "new"


def test_private_secret_write_failure_never_mentions_value(tmp_path) -> None:
    marker = "secret-output-marker"
    with pytest.raises(ConnectionHubCliError) as raised:
        write_private_secret(tmp_path / "missing" / "secret", marker)
    assert marker not in str(raised.value)


def test_private_secret_output_validation_rejects_invalid_targets(tmp_path) -> None:
    existing = tmp_path / "existing.secret"
    existing.write_text("existing")

    with pytest.raises(ConnectionHubCliError) as raised:
        validate_private_secret_output(existing)
    assert raised.value.code == "secret_output_exists"

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(ConnectionHubCliError) as raised:
        validate_private_secret_output(directory, replace=True)
    assert raised.value.code == "secret_output_not_regular_file"

    assert validate_private_secret_output(existing, replace=True) == existing


def test_private_secret_output_refuses_broken_symlink_without_replace(
    tmp_path,
) -> None:
    target = tmp_path / "secret-link"
    try:
        target.symlink_to(tmp_path / "missing-target")
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation is unavailable")

    assert not target.exists()
    with pytest.raises(ConnectionHubCliError) as raised:
        validate_private_secret_output(target)
    assert raised.value.code == "secret_output_exists"
    assert target.is_symlink()
