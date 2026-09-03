from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from connection_hub_cli.user_presence.errors import UserPresenceError

_CONTRACT_DOMAIN = "connection-hub-bound-http-operation-v2"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_HTTP_METHOD_RE = re.compile(r"^[A-Z][A-Z0-9!#$%&'*+.^_`|~-]{0,31}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ACCESS_ID_RE = re.compile(r"^[^\s\x00-\x1f\x7f]{1,256}$")
_MAX_BODY_BYTES = 8 * 1024 * 1024
_MAX_SYSTEM_PROMPT_CHARS = 240


def _invalid(message: str) -> UserPresenceError:
    return UserPresenceError("invalid_bound_operation", message)


def _bounded_text(value: str, *, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise _invalid(f"{field_name} must be text.")
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > maximum
        or any(unicodedata.category(char).startswith("C") for char in candidate)
    ):
        raise _invalid(
            f"{field_name} must contain between 1 and {maximum} visible characters."
        )
    try:
        candidate.encode("utf-8")
    except UnicodeEncodeError:
        raise _invalid(f"{field_name} must contain valid Unicode text.") from None
    return candidate


def _normalize_identifier(value: str, *, field_name: str) -> str:
    candidate = _bounded_text(value, field_name=field_name, maximum=64)
    if not _IDENTIFIER_RE.fullmatch(candidate):
        raise _invalid(
            f"{field_name} must start with a letter or digit and use only letters, "
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


def _normalize_hostname(hostname: str) -> str:
    candidate = hostname.rstrip(".").lower()
    if ":" in candidate:
        try:
            return f"[{ipaddress.IPv6Address(candidate).compressed}]"
        except ValueError:
            raise _invalid("target contains an invalid IPv6 address.") from None
    try:
        candidate = candidate.encode("idna").decode("ascii")
    except UnicodeError:
        raise _invalid("target contains an invalid hostname.") from None
    if not candidate or len(candidate) > 253:
        raise _invalid("target hostname must contain at most 253 characters.")
    return candidate


def _normalize_target(value: str) -> str:
    target = _bounded_text(value, field_name="target", maximum=2048)
    try:
        parsed = urlsplit(target)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise _invalid("target must be a valid absolute HTTP endpoint.") from None
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not hostname:
        raise _invalid("target must be an absolute HTTP or HTTPS endpoint.")
    if parsed.username is not None or parsed.password is not None:
        raise _invalid("target must not contain user information or credentials.")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise _invalid("target must contain only an origin; put the route in path.")
    if scheme == "http" and not _is_loopback(hostname):
        raise _invalid("plain HTTP targets must resolve to the local loopback host.")

    normalized_host = _normalize_hostname(hostname)
    normalized_port = port or (443 if scheme == "https" else 80)
    return f"{scheme}://{normalized_host}:{normalized_port}"


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


def _normalize_operation_fields(
    *,
    target_key: str,
    tenant: str,
    project: str,
    caller_profile: str,
    access_id: str,
    resource: str,
    operation: str,
    method: str,
    target: str,
    path: str,
    display_summary: str,
) -> dict[str, str]:
    return {
        "target_key": _bounded_text(
            target_key, field_name="target_key", maximum=512
        ),
        "tenant": _normalize_identifier(tenant, field_name="tenant"),
        "project": _normalize_identifier(project, field_name="project"),
        "caller_profile": _normalize_identifier(
            caller_profile, field_name="caller_profile"
        ),
        "access_id": _normalize_access_id(access_id),
        "resource": _bounded_text(resource, field_name="resource", maximum=128),
        "operation": _bounded_text(
            operation, field_name="operation", maximum=64
        ),
        "method": _normalize_method(method),
        "target": _normalize_target(target),
        "path": _normalize_path(path),
        "display_summary": _bounded_text(
            display_summary, field_name="display_summary", maximum=120
        ),
    }


def _canonical_payload(
    *,
    target_key: str,
    tenant: str,
    project: str,
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
        "project": project,
        "resource": resource,
        "target": target,
        "target_key": target_key,
        "tenant": tenant,
    }
    return json.dumps(
        value,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _operation_digest(
    normalized: dict[str, str], *, body_sha256: str, body_length: int
) -> str:
    return hashlib.sha256(
        _canonical_payload(
            **normalized,
            body_sha256=body_sha256,
            body_length=body_length,
        )
    ).hexdigest()


def canonical_operation_digest(
    *,
    target_key: str,
    tenant: str,
    project: str,
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
    normalized = _normalize_operation_fields(
        target_key=target_key,
        tenant=tenant,
        project=project,
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
    return _operation_digest(
        normalized,
        body_sha256=hashlib.sha256(body_value).hexdigest(),
        body_length=len(body_value),
    )


@dataclass(frozen=True, slots=True)
class BoundHttpOperation:
    target_key: str
    tenant: str
    project: str
    caller_profile: str
    access_id: str
    resource: str
    operation: str
    method: str
    target: str
    path: str
    body_sha256: str
    body_length: int
    operation_digest: str
    display_summary: str

    def __post_init__(self) -> None:
        normalized = _normalize_operation_fields(
            target_key=self.target_key,
            tenant=self.tenant,
            project=self.project,
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
        self._validate_derived_fields(normalized)

    def _validate_derived_fields(self, normalized: dict[str, str]) -> None:
        if (
            type(self.body_length) is not int
            or not 0 <= self.body_length <= _MAX_BODY_BYTES
        ):
            raise _invalid(f"body_length must be between 0 and {_MAX_BODY_BYTES}.")
        if not isinstance(self.body_sha256, str) or not _DIGEST_RE.fullmatch(
            self.body_sha256
        ):
            raise _invalid("body_sha256 must be a lowercase SHA-256 digest.")
        if not isinstance(self.operation_digest, str) or not _DIGEST_RE.fullmatch(
            self.operation_digest
        ):
            raise _invalid("operation_digest must be a lowercase SHA-256 digest.")
        expected = _operation_digest(
            normalized,
            body_sha256=self.body_sha256,
            body_length=self.body_length,
        )
        if not hmac.compare_digest(self.operation_digest, expected):
            raise _invalid(
                "operation_digest does not match the bound operation fields."
            )
        if len(self.system_prompt()) > _MAX_SYSTEM_PROMPT_CHARS:
            raise _invalid(
                "operation and deployment coordinates are too long for the system prompt."
            )

    @classmethod
    def bind(
        cls,
        *,
        target_key: str,
        tenant: str,
        project: str,
        caller_profile: str,
        access_id: str,
        resource: str,
        operation: str,
        method: str,
        target: str,
        path: str,
        body: bytes | bytearray | memoryview | str | None = None,
        display_summary: str,
    ) -> BoundHttpOperation:
        normalized = _normalize_operation_fields(
            target_key=target_key,
            tenant=tenant,
            project=project,
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
        return cls(
            **normalized,
            body_sha256=body_sha256,
            body_length=body_length,
            operation_digest=_operation_digest(
                normalized,
                body_sha256=body_sha256,
                body_length=body_length,
            ),
        )

    def validate_integrity(self) -> None:
        normalized = _normalize_operation_fields(
            target_key=self.target_key,
            tenant=self.tenant,
            project=self.project,
            caller_profile=self.caller_profile,
            access_id=self.access_id,
            resource=self.resource,
            operation=self.operation,
            method=self.method,
            target=self.target,
            path=self.path,
            display_summary=self.display_summary,
        )
        if any(getattr(self, name) != value for name, value in normalized.items()):
            raise _invalid("bound operation fields are not normalized.")
        self._validate_derived_fields(normalized)

    def matches_body(self, body: bytes) -> bool:
        return self.body_length == len(body) and hmac.compare_digest(
            self.body_sha256, hashlib.sha256(body).hexdigest()
        )

    def system_prompt(self) -> str:
        return (
            f"{self.operation} for {self.tenant}/{self.project} at {self.target}; "
            f"{self.method} {self.resource}"
        )

    def to_safe_dict(self) -> dict[str, Any]:
        return {
            "target_key": self.target_key,
            "tenant": self.tenant,
            "project": self.project,
            "caller_profile": self.caller_profile,
            "access_id": self.access_id,
            "resource": self.resource,
            "operation": self.operation,
            "method": self.method,
            "target": self.target,
            "path": self.path,
            "body_sha256": self.body_sha256,
            "body_length": self.body_length,
            "operation_digest": self.operation_digest,
            "display_summary": self.display_summary,
        }
