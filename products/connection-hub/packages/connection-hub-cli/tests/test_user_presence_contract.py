from __future__ import annotations

from dataclasses import asdict, replace

import pytest
from connection_hub_cli.user_presence import (
    ApprovalRequest,
    ApprovalResult,
    UnavailableUserPresenceBackend,
    UserPresenceError,
    canonical_request_digest,
    require_matching_approval,
)


def _request_values() -> dict[str, object]:
    return {
        "target_key": "endpoint:https://host.example:tenant-a:project-a",
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


def _request(**overrides: object) -> ApprovalRequest:
    values = _request_values()
    values.update(overrides)
    return ApprovalRequest.bind(**values)  # type: ignore[arg-type]


def test_canonical_request_digest_is_deterministic() -> None:
    values = _request_values()

    first = canonical_request_digest(**values)  # type: ignore[arg-type]
    second = canonical_request_digest(**values)  # type: ignore[arg-type]
    request = ApprovalRequest.bind(**values)  # type: ignore[arg-type]

    assert first == second == request.request_digest
    assert len(first) == 64


@pytest.mark.parametrize(
    "field,replacement",
    [
        ("target_key", "endpoint:https://other.example:tenant-a:project-a"),
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
    assert _request(**{field: replacement}).request_digest != _request().request_digest


def test_request_retains_only_body_hash_and_length() -> None:
    marker = "raw-request-body-must-not-survive"
    request = _request(body=marker)

    assert marker not in repr(request)
    assert marker not in str(request.to_safe_dict())
    assert "body" not in request.to_safe_dict()
    assert request.body_length == len(marker)


def test_invalid_unicode_body_is_not_rendered_in_the_failure() -> None:
    marker = "raw-body-marker"

    with pytest.raises(UserPresenceError) as raised:
        _request(body=f"{marker}\ud800")

    assert raised.value.code == "invalid_approval_request"
    assert marker not in str(raised.value)
    assert marker not in repr(raised.value.__cause__)
    assert raised.value.__cause__ is None


def test_forged_request_digest_is_rejected() -> None:
    with pytest.raises(UserPresenceError) as raised:
        replace(_request(), request_digest="0" * 64)

    assert raised.value.code == "invalid_approval_request"


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_key": "x" * 1025},
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
        {"display_summary": "x" * 181},
    ],
)
def test_invalid_or_oversized_fields_fail_during_binding(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(UserPresenceError) as raised:
        _request(**overrides)

    assert raised.value.code == "invalid_approval_request"


def test_result_authorizes_only_the_matching_digest() -> None:
    request = _request()
    other = _request(operation="host.status")
    result = ApprovalResult(
        approved=True,
        mechanism="test-user-presence",
        request_digest=request.request_digest,
    )

    assert result.authorizes(request) is True
    assert result.authorizes(other) is False
    require_matching_approval(request, result)
    with pytest.raises(UserPresenceError) as raised:
        require_matching_approval(other, result)
    assert raised.value.code == "approval_digest_mismatch"


def test_result_safe_serialization_contains_no_proof_bytes() -> None:
    result = ApprovalResult(
        approved=True,
        mechanism="test-user-presence",
        request_digest=_request().request_digest,
        signed_proof=b"non-secret-proof",
    )

    assert result.signed_proof == b"non-secret-proof"
    assert "non-secret-proof" not in repr(result)
    assert result.to_safe_dict()["signed_proof_present"] is True
    assert "signed_proof" not in result.to_safe_dict()
    assert asdict(result)["signed_proof"] == b"non-secret-proof"


def test_unsupported_backend_fails_closed() -> None:
    backend = UnavailableUserPresenceBackend(platform_name="Linux")

    assert backend.available() is False
    with pytest.raises(UserPresenceError) as raised:
        backend.approve(_request())
    assert raised.value.code == "user_presence_unsupported_platform"


def test_unsupported_backend_does_not_render_an_untrusted_platform_label() -> None:
    marker = "platform-label-must-not-render"
    backend = UnavailableUserPresenceBackend(platform_name=f"bad/{marker}")

    with pytest.raises(UserPresenceError) as raised:
        backend.approve(_request())

    assert marker not in str(raised.value)
    assert backend.platform_name == "this platform"
