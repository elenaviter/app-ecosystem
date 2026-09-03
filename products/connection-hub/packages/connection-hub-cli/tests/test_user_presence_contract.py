from __future__ import annotations

from dataclasses import replace

import pytest
from connection_hub_cli import user_presence
from connection_hub_cli.user_presence import (
    BoundHttpOperation,
    UnavailableUserPresenceBackend,
    UserPresenceError,
    canonical_operation_digest,
)


def _operation_values() -> dict[str, object]:
    return {
        "target_key": "endpoint:https://host.example:tenant-a:project-a",
        "tenant": "tenant-a",
        "project": "project-a",
        "caller_profile": "human-admin",
        "access_id": "access-123",
        "resource": "kdcube.host",
        "operation": "host.reload",
        "method": "POST",
        "target": "https://host.example",
        "path": "/api/manage/reload?wait=true",
        "body": b'{"source":"git"}',
        "display_summary": "Reload the selected KDCube host",
    }


def _operation(**overrides: object) -> BoundHttpOperation:
    values = _operation_values()
    values.update(overrides)
    return BoundHttpOperation.bind(**values)  # type: ignore[arg-type]


def test_canonical_operation_digest_is_deterministic() -> None:
    values = _operation_values()

    first = canonical_operation_digest(**values)  # type: ignore[arg-type]
    second = canonical_operation_digest(**values)  # type: ignore[arg-type]
    operation = BoundHttpOperation.bind(**values)  # type: ignore[arg-type]

    assert first == second == operation.operation_digest
    assert len(first) == 64
    assert operation.target == "https://host.example:443"


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("target_key", "endpoint:https://other.example:tenant-a:project-a"),
        ("tenant", "tenant-b"),
        ("project", "project-b"),
        ("caller_profile", "other-admin"),
        ("access_id", "access-456"),
        ("resource", "kdcube.bundle"),
        ("operation", "host.status"),
        ("method", "PUT"),
        ("target", "https://other.example"),
        ("path", "/api/manage/reload?wait=false"),
        ("body", b'{"source":"descriptor"}'),
        ("display_summary", "Reload another KDCube host"),
    ],
)
def test_each_bound_field_changes_the_digest(field: str, replacement: object) -> None:
    assert (
        _operation(**{field: replacement}).operation_digest
        != _operation().operation_digest
    )


def test_operation_retains_only_body_hash_and_length() -> None:
    marker = "raw-request-body-must-not-survive"
    operation = _operation(body=marker)

    assert marker not in repr(operation)
    assert marker not in str(operation.to_safe_dict())
    assert "body" not in operation.to_safe_dict()
    assert operation.body_length == len(marker)


def test_invalid_unicode_body_is_not_rendered_in_the_failure() -> None:
    marker = "raw-body-marker"

    with pytest.raises(UserPresenceError) as raised:
        _operation(body=f"{marker}\ud800")

    assert raised.value.code == "invalid_bound_operation"
    assert marker not in str(raised.value)
    assert marker not in repr(raised.value.__cause__)
    assert raised.value.__cause__ is None


def test_forged_operation_digest_is_rejected() -> None:
    with pytest.raises(UserPresenceError) as raised:
        replace(_operation(), operation_digest="0" * 64)

    assert raised.value.code == "invalid_bound_operation"


def test_integrity_is_rechecked_before_execution() -> None:
    operation = _operation()
    object.__setattr__(operation, "method", "GET")

    with pytest.raises(UserPresenceError) as raised:
        operation.validate_integrity()

    assert raised.value.code == "invalid_bound_operation"


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_key": "x" * 513},
        {"tenant": "contains spaces"},
        {"project": "contains/slash"},
        {"caller_profile": "contains spaces"},
        {"access_id": "contains spaces"},
        {"resource": "x" * 129},
        {"operation": "x" * 65},
        {"method": "INVALID METHOD"},
        {"target": "http://not-loopback.example"},
        {"target": "https://user:secret@host.example"},
        {"path": "https://other.example/path"},
        {"path": "/path with spaces"},
        {"body": b"x" * (8 * 1024 * 1024 + 1)},
        {"display_summary": "x" * 121},
        {"display_summary": "hidden\u202etext"},
    ],
)
def test_invalid_or_oversized_fields_fail_during_binding(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(UserPresenceError) as raised:
        _operation(**overrides)

    assert raised.value.code == "invalid_bound_operation"


def test_system_prompt_starts_with_structured_operation_and_target() -> None:
    marker = "caller-summary-must-not-drive-system-prompt"
    operation = _operation(
        target="https://host.example:8443",
        tenant="tenant-visible",
        project="project-visible",
        operation="host.status",
        display_summary=marker,
    )

    assert operation.system_prompt().startswith(
        "host.status for tenant-visible/project-visible at "
        "https://host.example:8443;"
    )
    assert marker not in operation.system_prompt()
    assert len(operation.system_prompt()) <= 240


def test_same_host_targets_with_different_ports_have_distinct_prompts() -> None:
    first = _operation(target="https://host.example:7443")
    second = _operation(target="https://host.example:8443")

    assert first.system_prompt() != second.system_prompt()
    assert ":7443" in first.system_prompt()
    assert ":8443" in second.system_prompt()


def test_unsigned_approval_api_is_removed() -> None:
    assert not hasattr(user_presence, "ApprovalResult")
    assert not hasattr(user_presence, "ApprovalRequest")
    assert not hasattr(user_presence, "require_matching_approval")
    assert not hasattr(user_presence.MacOSUserPresenceBackend, "approve")
    assert not hasattr(user_presence.MacOSUserPresenceBackend, "execute_http")


def test_unsupported_backend_fails_closed_for_execution() -> None:
    backend = UnavailableUserPresenceBackend(platform_name="Linux")

    assert backend.available() is False
    with pytest.raises(UserPresenceError) as raised:
        backend.execute(_operation(), body=b'{"source":"git"}')
    assert raised.value.code == "user_presence_unsupported_platform"


def test_unsupported_backend_does_not_render_an_untrusted_platform_label() -> None:
    marker = "platform-label-must-not-render"
    backend = UnavailableUserPresenceBackend(platform_name=f"bad/{marker}")

    with pytest.raises(UserPresenceError) as raised:
        backend.execute(_operation(), body=b'{"source":"git"}')

    assert marker not in str(raised.value)
    assert backend.platform_name == "this platform"
