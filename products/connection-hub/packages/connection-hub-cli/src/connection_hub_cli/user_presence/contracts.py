from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from connection_hub_cli.user_presence.errors import UserPresenceError

_CONTRACT_DOMAIN = "connection-hub-user-presence-request-v1"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_HTTP_METHOD_RE = re.compile(r"^[A-Z][A-Z0-9!#$%&'*+.^_`|~-]{0,31}$")
_PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ACCESS_ID_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,256}$")
_MAX_BODY_BYTES = 8 * 1024 * 1024


def _invalid(message: str) -> UserPresenceError:
    return UserPresenceError("invalid_approval_request", message)


def _bounded_text(value: str, *, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise _invalid(f"{field_name} must be text.")
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > maximum
        or any(ord(char) < 32 or ord(char) == 127 for char in candidate)
    ):
        raise _invalid(
            f"{field_name} must contain between 1 and {maximum} visible characters."
        )
    try:
        candidate.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid(f"{field_name} must contain valid Unicode text.") from None
    return candidate


def _normalize_profile(value: str) -> str:
    candidate = _bounded_text(value, field_name="caller_profile", maximum=64)
    if not _PROFILE_RE.fullmatch(candidate):
        raise _invalid(
            "caller_profile must start with a letter or digit and use only letters, "
            "digits, '.', '_', or '-'."
        )
    return candidate


def _normalize_access_id(value: str) -> str:
    candidate = _bounded_text(value, field_name="access_id", maximum=256)
    if not _ACCESS_ID_RE.fullmatch(candidate):
        raise _invalid("access_id must not contain whitespace or control characters.")
    return candidate


def _is_loopback(hostname: str) -> bool:
    lowered = hostname.rstrip(".").lower()
    if lowered == "localhost" or lowered.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


def _normalize_target(value: str) -> str:
    target = _bounded_text(value, field_name="target", maximum=2048)
    try:
        parsed = urlsplit(target)
        port = parsed.port
    except ValueError:
        raise _invalid("target must be a valid absolute HTTP endpoint.") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise _invalid("target must be an absolute HTTP or HTTPS endpoint.")
    if parsed.username is not None or parsed.password is not None:
        raise _invalid("target must not contain user information or credentials.")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise _invalid("target must contain only an origin; put the route in path.")
    if parsed.scheme == "http" and not _is_loopback(parsed.hostname):
        raise _invalid("plain HTTP targets must resolve to the local loopback host.")

    hostname = parsed.hostname.rstrip(".").lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    default_port = 443 if parsed.scheme == "https" else 80
    authority = hostname if port in {None, default_port} else f"{hostname}:{port}"
    return f"{parsed.scheme}://{authority}"


def _normalize_path(value: str) -> str:
    path = _bounded_text(value, field_name="path", maximum=4096)
    if not path.startswith("/") or path.startswith("//"):
        raise _invalid("path must be an origin-relative path beginning with one '/'.")
    if "#" in path or any(char.isspace() for char in path):
        raise _invalid("path must not contain fragments or unescaped whitespace.")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        raise _invalid("path must be an origin-relative HTTP path.")
    return path


def _normalize_method(value: str) -> str:
    if not isinstance(value, str):
        raise _invalid("method must be text.")
    method = value.strip().upper()
    if not _HTTP_METHOD_RE.fullmatch(method):
        raise _invalid("method must be a valid HTTP method token.")
    return method


def request_body_bytes(body: bytes | bytearray | memoryview | str | None) -> bytes:
    if body is None:
        value = b""
    elif isinstance(body, str):
        try:
            value = body.encode("utf-8")
        except UnicodeEncodeError:
            raise _invalid("body must contain valid Unicode text.") from None
    elif isinstance(body, (bytes, bytearray, memoryview)):
        value = bytes(body)
    else:
        raise _invalid("body must be bytes, text, or null.")
    if len(value) > _MAX_BODY_BYTES:
        raise _invalid(f"body must not exceed {_MAX_BODY_BYTES} bytes.")
    return value


def _canonical_payload(
    *,
    target_key: str,
    caller_profile: str,
    access_id: str,
    resource: str,
    operation: str,
    method: str,
    target: str,
    path: str,
    body_sha256: str,
    body_length: int,
    display_summary: str,
) -> bytes:
    value = {
        "access_id": access_id,
        "body_length": body_length,
        "body_sha256": body_sha256,
        "caller_profile": caller_profile,
        "display_summary": display_summary,
        "domain": _CONTRACT_DOMAIN,
        "method": method,
        "operation": operation,
        "path": path,
        "resource": resource,
        "target": target,
        "target_key": target_key,
    }
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def canonical_request_digest(
    *,
    target_key: str,
    caller_profile: str,
    access_id: str,
    resource: str,
    operation: str,
    method: str,
    target: str,
    path: str,
    body: bytes | bytearray | memoryview | str | None = None,
    display_summary: str,
) -> str:
    normalized = _normalize_request_fields(
        target_key=target_key,
        caller_profile=caller_profile,
        access_id=access_id,
        resource=resource,
        operation=operation,
        method=method,
        target=target,
        path=path,
        display_summary=display_summary,
    )
    body_value = request_body_bytes(body)
    return hashlib.sha256(
        _canonical_payload(
            **normalized,
            body_sha256=hashlib.sha256(body_value).hexdigest(),
            body_length=len(body_value),
        )
    ).hexdigest()


def _normalize_request_fields(
    *,
    target_key: str,
    caller_profile: str,
    access_id: str,
    resource: str,
    operation: str,
    method: str,
    target: str,
    path: str,
    display_summary: str,
) -> dict[str, Any]:
    return {
        "target_key": _bounded_text(
            target_key, field_name="target_key", maximum=1024
        ),
        "caller_profile": _normalize_profile(caller_profile),
        "access_id": _normalize_access_id(access_id),
        "resource": _bounded_text(resource, field_name="resource", maximum=128),
        "operation": _bounded_text(
            operation, field_name="operation", maximum=64
        ),
        "method": _normalize_method(method),
        "target": _normalize_target(target),
        "path": _normalize_path(path),
        "display_summary": _bounded_text(
            display_summary, field_name="display_summary", maximum=180
        ),
    }


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    target_key: str
    caller_profile: str
    access_id: str
    resource: str
    operation: str
    method: str
    target: str
    path: str
    body_sha256: str
    body_length: int
    request_digest: str
    display_summary: str

    def __post_init__(self) -> None:
        normalized = _normalize_request_fields(
            target_key=self.target_key,
            caller_profile=self.caller_profile,
            access_id=self.access_id,
            resource=self.resource,
            operation=self.operation,
            method=self.method,
            target=self.target,
            path=self.path,
            display_summary=self.display_summary,
        )
        for name, value in normalized.items():
            object.__setattr__(self, name, value)
        if (
            not isinstance(self.body_length, int)
            or not 0 <= self.body_length <= _MAX_BODY_BYTES
        ):
            raise _invalid(f"body_length must be between 0 and {_MAX_BODY_BYTES}.")
        if not isinstance(self.body_sha256, str) or not _DIGEST_RE.fullmatch(
            self.body_sha256
        ):
            raise _invalid("body_sha256 must be a lowercase SHA-256 digest.")
        if not isinstance(self.request_digest, str) or not _DIGEST_RE.fullmatch(
            self.request_digest
        ):
            raise _invalid("request_digest must be a lowercase SHA-256 digest.")
        expected = hashlib.sha256(
            _canonical_payload(
                **normalized,
                body_sha256=self.body_sha256,
                body_length=self.body_length,
            )
        ).hexdigest()
        if not hmac.compare_digest(self.request_digest, expected):
            raise _invalid("request_digest does not match the bound request fields.")

    @classmethod
    def bind(
        cls,
        *,
        target_key: str,
        caller_profile: str,
        access_id: str,
        resource: str,
        operation: str,
        method: str,
        target: str,
        path: str,
        body: bytes | bytearray | memoryview | str | None = None,
        display_summary: str,
    ) -> ApprovalRequest:
        normalized = _normalize_request_fields(
            target_key=target_key,
            caller_profile=caller_profile,
            access_id=access_id,
            resource=resource,
            operation=operation,
            method=method,
            target=target,
            path=path,
            display_summary=display_summary,
        )
        body_value = request_body_bytes(body)
        body_sha256 = hashlib.sha256(body_value).hexdigest()
        body_length = len(body_value)
        request_digest = hashlib.sha256(
            _canonical_payload(
                **normalized,
                body_sha256=body_sha256,
                body_length=body_length,
            )
        ).hexdigest()
        return cls(
            **normalized,
            body_sha256=body_sha256,
            body_length=body_length,
            request_digest=request_digest,
        )

    def matches_body(
        self, body: bytes | bytearray | memoryview | str | None
    ) -> bool:
        body_value = request_body_bytes(body)
        return self.body_length == len(body_value) and hmac.compare_digest(
            self.body_sha256, hashlib.sha256(body_value).hexdigest()
        )

    def system_prompt(self) -> str:
        host = urlsplit(self.target).hostname or self.target
        return (
            f"{self.display_summary}. Operation: {self.operation}; resource: "
            f"{self.resource}; host: {host}; profile: {self.caller_profile}."
        )

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "target_key": self.target_key,
            "caller_profile": self.caller_profile,
            "access_id": self.access_id,
            "resource": self.resource,
            "operation": self.operation,
            "method": self.method,
            "target": self.target,
            "path": self.path,
            "body_sha256": self.body_sha256,
            "body_length": self.body_length,
            "request_digest": self.request_digest,
            "display_summary": self.display_summary,
        }


@dataclass(frozen=True, slots=True)
class ApprovalResult:
    approved: bool
    mechanism: str
    request_digest: str
    signed_proof: bytes | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.approved, bool):
            raise UserPresenceError(
                "invalid_approval_result", "approved must be a Boolean value."
            )
        mechanism = _bounded_text(
            self.mechanism, field_name="mechanism", maximum=128
        )
        object.__setattr__(self, "mechanism", mechanism)
        if not isinstance(self.request_digest, str) or not _DIGEST_RE.fullmatch(
            self.request_digest
        ):
            raise UserPresenceError(
                "invalid_approval_result",
                "request_digest must be a lowercase SHA-256 digest.",
            )
        if self.signed_proof is not None and (
            not isinstance(self.signed_proof, bytes)
            or len(self.signed_proof) > 65536
        ):
            raise UserPresenceError(
                "invalid_approval_result",
                "signed_proof must be bytes no larger than 65536 bytes.",
            )

    def authorizes(self, request: ApprovalRequest) -> bool:
        return self.approved and hmac.compare_digest(
            self.request_digest, request.request_digest
        )

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "mechanism": self.mechanism,
            "request_digest": self.request_digest,
            "signed_proof_present": self.signed_proof is not None,
        }


def require_matching_approval(
    request: ApprovalRequest, result: ApprovalResult
) -> None:
    if not result.approved:
        raise UserPresenceError("approval_denied", "User presence was not approved.")
    if not result.authorizes(request):
        raise UserPresenceError(
            "approval_digest_mismatch",
            "The user-presence result does not match this request.",
        )
