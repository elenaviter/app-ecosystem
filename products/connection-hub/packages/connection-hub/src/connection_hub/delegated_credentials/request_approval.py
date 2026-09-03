# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Signed browser handoff for one request-bound delegated approval."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping


REQUEST_APPROVAL_SCHEMA = "connection_hub.request_approval.v1"
REQUEST_APPROVAL_TOKEN_PREFIX = "chra1"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SERVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class RequestApprovalTicketError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = str(reason or "request_approval_ticket_invalid")
        super().__init__(self.reason)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(f"{value}{padding}".encode("ascii"))
    except Exception as exc:
        raise RequestApprovalTicketError("request_approval_ticket_encoding_invalid") from exc


def _bounded_text(value: Any, *, field_name: str, maximum: int) -> str:
    text = _text(value)
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        raise RequestApprovalTicketError(f"request_approval_{field_name}_invalid")
    return text


@dataclass(frozen=True)
class RequestApprovalTicket:
    service_id: str
    client_id: str
    access_id: str
    resource: str
    operation: str
    invocation_id: str
    request_digest: str
    card_revision: int
    authority_revision: str
    issued_at: int
    expires_at: int
    approval_context: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        service_id = _bounded_text(
            self.service_id,
            field_name="service_id",
            maximum=128,
        )
        if not _SERVICE_ID_RE.fullmatch(service_id):
            raise RequestApprovalTicketError("request_approval_service_id_invalid")
        values = {
            "client_id": _bounded_text(
                self.client_id,
                field_name="client_id",
                maximum=512,
            ),
            "access_id": _bounded_text(
                self.access_id,
                field_name="access_id",
                maximum=512,
            ),
            "resource": _bounded_text(
                self.resource,
                field_name="resource",
                maximum=2048,
            ),
            "operation": _bounded_text(
                self.operation,
                field_name="operation",
                maximum=512,
            ),
            "invocation_id": _bounded_text(
                self.invocation_id,
                field_name="invocation_id",
                maximum=256,
            ),
            "authority_revision": _bounded_text(
                self.authority_revision,
                field_name="authority_revision",
                maximum=512,
            ),
        }
        if any(ord(char) > 126 for char in values["invocation_id"]):
            raise RequestApprovalTicketError("request_approval_invocation_id_invalid")
        request_digest = _text(self.request_digest).lower()
        if not _DIGEST_RE.fullmatch(request_digest):
            raise RequestApprovalTicketError("request_approval_request_digest_invalid")
        card_revision = int(self.card_revision)
        issued_at = int(self.issued_at)
        expires_at = int(self.expires_at)
        if card_revision < 1:
            raise RequestApprovalTicketError("request_approval_card_revision_invalid")
        if issued_at < 1 or expires_at <= issued_at:
            raise RequestApprovalTicketError("request_approval_expiry_invalid")
        context = {
            _text(key): _text(value)
            for key, value in dict(self.approval_context or {}).items()
            if _text(key) and _text(value)
        }
        if len(context) > 16:
            raise RequestApprovalTicketError("request_approval_context_invalid")
        for key, value in context.items():
            if (
                not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key)
                or len(value) > 512
                or any(ord(char) < 32 for char in value)
            ):
                raise RequestApprovalTicketError("request_approval_context_invalid")

        object.__setattr__(self, "service_id", service_id)
        for key, value in values.items():
            object.__setattr__(self, key, value)
        object.__setattr__(self, "request_digest", request_digest)
        object.__setattr__(self, "card_revision", card_revision)
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "approval_context", dict(sorted(context.items())))

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "RequestApprovalTicket":
        if _text(value.get("schema")) != REQUEST_APPROVAL_SCHEMA:
            raise RequestApprovalTicketError("request_approval_schema_mismatch")
        context = value.get("approval_context")
        if context is not None and not isinstance(context, Mapping):
            raise RequestApprovalTicketError("request_approval_context_invalid")
        return cls(
            service_id=value.get("service_id", ""),
            client_id=value.get("client_id", ""),
            access_id=value.get("access_id", ""),
            resource=value.get("resource", ""),
            operation=value.get("operation", ""),
            invocation_id=value.get("invocation_id", ""),
            request_digest=value.get("request_digest", ""),
            card_revision=int(value.get("card_revision") or 0),
            authority_revision=value.get("authority_revision", ""),
            issued_at=int(value.get("issued_at") or 0),
            expires_at=int(value.get("expires_at") or 0),
            approval_context=dict(context or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": REQUEST_APPROVAL_SCHEMA,
            "service_id": self.service_id,
            "client_id": self.client_id,
            "access_id": self.access_id,
            "resource": self.resource,
            "operation": self.operation,
            "invocation_id": self.invocation_id,
            "request_digest": self.request_digest,
            "card_revision": self.card_revision,
            "authority_revision": self.authority_revision,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "approval_context": dict(self.approval_context),
        }


def issue_request_approval_ticket(
    ticket: RequestApprovalTicket,
    *,
    secret: str,
) -> str:
    secret_bytes = str(secret or "").encode("utf-8")
    if len(secret_bytes) < 32:
        raise RequestApprovalTicketError("request_approval_secret_invalid")
    encoded = _encode(_canonical_json(ticket.to_dict()))
    signature = hmac.new(
        secret_bytes,
        f"{REQUEST_APPROVAL_TOKEN_PREFIX}.{encoded}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{REQUEST_APPROVAL_TOKEN_PREFIX}.{encoded}.{signature}"


def _token_parts(token: str) -> tuple[str, str]:
    raw = _text(token)
    if len(raw) > 16384:
        raise RequestApprovalTicketError("request_approval_ticket_too_large")
    try:
        prefix, encoded, signature = raw.split(".", 2)
    except ValueError as exc:
        raise RequestApprovalTicketError("request_approval_ticket_invalid") from exc
    if prefix != REQUEST_APPROVAL_TOKEN_PREFIX or not re.fullmatch(
        r"[0-9a-f]{64}", signature
    ):
        raise RequestApprovalTicketError("request_approval_ticket_invalid")
    if not encoded or not re.fullmatch(r"[A-Za-z0-9_-]+", encoded):
        raise RequestApprovalTicketError("request_approval_ticket_invalid")
    return encoded, signature


def _payload_from_encoded(encoded: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(_decode(encoded))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestApprovalTicketError("request_approval_ticket_payload_invalid") from exc
    if not isinstance(payload, Mapping):
        raise RequestApprovalTicketError("request_approval_ticket_payload_invalid")
    return payload


def peek_request_approval_ticket(token: str) -> RequestApprovalTicket:
    """Parse a bounded ticket so its service key can be selected before verify."""

    encoded, _signature = _token_parts(token)
    payload = _payload_from_encoded(encoded)
    return RequestApprovalTicket.from_mapping(payload)


def verify_request_approval_ticket(
    token: str,
    *,
    secret: str,
    now: int | None = None,
) -> RequestApprovalTicket:
    secret_bytes = str(secret or "").encode("utf-8")
    if len(secret_bytes) < 32:
        raise RequestApprovalTicketError("request_approval_secret_invalid")
    encoded, received = _token_parts(token)
    expected = hmac.new(
        secret_bytes,
        f"{REQUEST_APPROVAL_TOKEN_PREFIX}.{encoded}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(received, expected):
        raise RequestApprovalTicketError("request_approval_signature_invalid")
    payload = _payload_from_encoded(encoded)
    ticket = RequestApprovalTicket.from_mapping(payload)
    moment = int(time.time() if now is None else now)
    if ticket.expires_at <= moment:
        raise RequestApprovalTicketError("request_approval_ticket_expired")
    return ticket


__all__ = [
    "REQUEST_APPROVAL_SCHEMA",
    "REQUEST_APPROVAL_TOKEN_PREFIX",
    "RequestApprovalTicket",
    "RequestApprovalTicketError",
    "issue_request_approval_ticket",
    "peek_request_approval_ticket",
    "verify_request_approval_ticket",
]
