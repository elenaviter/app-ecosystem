from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.request_approval import (
    RequestApprovalTicket,
    RequestApprovalTicketError,
    issue_request_approval_ticket,
    peek_request_approval_ticket,
    verify_request_approval_ticket,
)


SECRET = "request-approval-secret-with-at-least-thirty-two-bytes"


def _ticket(**overrides) -> RequestApprovalTicket:
    values = {
        "service_id": "kdcube-management",
        "client_id": "connection-hub-cli",
        "access_id": "access-1",
        "resource": "urn:kdcube:management:deployment:tenant-a:project-a",
        "operation": "kdcube.management.application.reload",
        "invocation_id": "invocation-1",
        "request_digest": "a" * 64,
        "card_revision": 7,
        "authority_revision": "catalog-9",
        "issued_at": 100,
        "expires_at": 700,
        "approval_context": {"application_id": "workspace@1-0"},
    }
    values.update(overrides)
    return RequestApprovalTicket(**values)


def test_signed_request_approval_round_trip_preserves_exact_context():
    expected = _ticket()
    token = issue_request_approval_ticket(expected, secret=SECRET)

    assert peek_request_approval_ticket(token) == expected
    assert verify_request_approval_ticket(token, secret=SECRET, now=101) == expected


def test_request_approval_rejects_payload_or_signature_tampering():
    token = issue_request_approval_ticket(_ticket(), secret=SECRET)
    prefix, payload, signature = token.split(".")
    changed_payload = f"{payload[:-1]}{'A' if payload[-1] != 'A' else 'B'}"
    changed_signature = f"{'0' if signature[0] != '0' else '1'}{signature[1:]}"

    with pytest.raises(RequestApprovalTicketError) as payload_error:
        verify_request_approval_ticket(
            f"{prefix}.{changed_payload}.{signature}",
            secret=SECRET,
            now=101,
        )
    with pytest.raises(RequestApprovalTicketError) as signature_error:
        verify_request_approval_ticket(
            f"{prefix}.{payload}.{changed_signature}",
            secret=SECRET,
            now=101,
        )

    assert payload_error.value.reason == "request_approval_signature_invalid"
    assert signature_error.value.reason == "request_approval_signature_invalid"


def test_request_approval_rejects_expired_ticket():
    token = issue_request_approval_ticket(_ticket(), secret=SECRET)

    with pytest.raises(RequestApprovalTicketError) as raised:
        verify_request_approval_ticket(token, secret=SECRET, now=700)

    assert raised.value.reason == "request_approval_ticket_expired"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("request_digest", "not-a-digest", "request_approval_request_digest_invalid"),
        ("card_revision", 0, "request_approval_card_revision_invalid"),
        ("approval_context", {"Application": "x"}, "request_approval_context_invalid"),
    ],
)
def test_request_approval_rejects_invalid_authority_fields(field, value, reason):
    with pytest.raises(RequestApprovalTicketError) as raised:
        _ticket(**{field: value})

    assert raised.value.reason == reason
