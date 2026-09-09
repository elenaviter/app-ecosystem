# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""
Public OAuth client registry.

Claude Code is pre-registered as a public client (no secret,
``token_endpoint_auth_method = none``). Redirect-URI matching follows RFC 8252:
the loopback redirects (``localhost`` / ``127.0.0.1``) match on any port because
the native client binds a dynamic local port for its callback; all other
redirects must match exactly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from connection_hub.delegated_credentials.oauth.config import (
    DEFAULT_CLAUDE_REDIRECT_URIS,
    DEFAULT_DCR_REDIRECT_URIS,
    oauth_delegated_config,
)

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

CLIENT_REGISTRATION_PRE_REGISTERED = "pre_registered"
CLIENT_REGISTRATION_DYNAMIC = "dynamic_client_registration"
CLIENT_REGISTRATION_METADATA_DOCUMENT = "client_id_metadata_document"

MAX_PUBLIC_CLIENT_METADATA_KEYS = 64
MAX_PUBLIC_CLIENT_METADATA_DEPTH = 3
MAX_PUBLIC_CLIENT_METADATA_ITEMS = 64
MAX_PUBLIC_CLIENT_METADATA_KEY_CHARS = 128
MAX_PUBLIC_CLIENT_METADATA_VALUE_CHARS = 4096
MAX_PUBLIC_CLIENT_METADATA_BYTES = 16 * 1024

_METADATA_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SECRET_METADATA_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "client_secret",
    "client_secret_expires_at",
    "cookie",
    "credential",
    "id_token",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "software_statement",
    "token",
}
_SECRET_METADATA_SUFFIXES = (
    "_access_token",
    "_authorization",
    "_credential",
    "_id_token",
    "_password",
    "_private_key",
    "_refresh_token",
    "_secret",
    "_token",
)


def _metadata_key_is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return lowered in _SECRET_METADATA_KEYS or lowered.endswith(
        _SECRET_METADATA_SUFFIXES
    )


def _public_metadata_value(value: Any, *, depth: int) -> Any:
    if depth > MAX_PUBLIC_CLIENT_METADATA_DEPTH:
        raise ValueError("client_metadata_too_deep")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("client_metadata_number_invalid")
        return value
    if isinstance(value, str):
        if len(value) > MAX_PUBLIC_CLIENT_METADATA_VALUE_CHARS or any(
            ord(character) < 0x20 or ord(character) == 0x7F for character in value
        ):
            raise ValueError("client_metadata_value_invalid")
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_PUBLIC_CLIENT_METADATA_ITEMS:
            raise ValueError("client_metadata_too_many_items")
        return [_public_metadata_value(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        if len(value) > MAX_PUBLIC_CLIENT_METADATA_KEYS:
            raise ValueError("client_metadata_too_many_keys")
        normalized: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key or "").strip()
            if not _METADATA_KEY_RE.fullmatch(key):
                raise ValueError("client_metadata_key_invalid")
            if _metadata_key_is_sensitive(key):
                raise ValueError("client_metadata_secret_field")
            normalized[key] = _public_metadata_value(child, depth=depth + 1)
        return normalized
    raise ValueError("client_metadata_value_invalid")


def normalize_public_client_metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Bound JSON-safe metadata a client presents about itself.

    These values are retained for operator identification and search only.
    They are self-asserted and never participate in authorization decisions.
    Secret-bearing fields are rejected rather than persisted accidentally.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("client_metadata_not_object")
    normalized = _public_metadata_value(value, depth=0)
    encoded = json.dumps(
        normalized,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > MAX_PUBLIC_CLIENT_METADATA_BYTES:
        raise ValueError("client_metadata_too_large")
    return normalized

# Redirect URIs a dynamically-registered (RFC 7591) client may register. DCR is
# open (it runs before the user authenticates), so without this an attacker could
# register a client pointing at their own server. Restricting it to claude.ai's
# MCP callback + loopback (any port, matched by redirect_uri_allowed) means a
# stolen auth code can only reach claude.ai or the victim's own machine.
@dataclass(frozen=True)
class PublicClient:
    client_id: str
    redirect_uris: Tuple[str, ...]
    token_endpoint_auth_method: str = "none"
    application_type: str = "native"
    registration_kind: str = CLIENT_REGISTRATION_PRE_REGISTERED
    client_name: str = ""
    client_uri: str = ""
    logo_uri: str = ""
    client_metadata: Mapping[str, Any] = field(default_factory=dict)

    def snapshot(self) -> Dict[str, Any]:
        payload = {
            "client_id": self.client_id,
            "redirect_uris": list(self.redirect_uris),
            "token_endpoint_auth_method": self.token_endpoint_auth_method,
            "application_type": self.application_type,
            "registration_kind": self.registration_kind,
            "client_name": self.client_name,
            "client_uri": self.client_uri,
            "logo_uri": self.logo_uri,
        }
        metadata = normalize_public_client_metadata(self.client_metadata)
        if metadata:
            payload["client_metadata"] = metadata
        return payload

    def snapshot_digest(self) -> str:
        """Stable fingerprint for binding the displayed client to consent."""

        encoded = json.dumps(
            self.snapshot(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def client_from_record(record: dict) -> "PublicClient":
    """Build a PublicClient from a stored DCR registration record."""
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    return PublicClient(
        client_id=record["client_id"],
        redirect_uris=tuple(record.get("redirect_uris") or ()),
        token_endpoint_auth_method=record.get("token_endpoint_auth_method", "none"),
        application_type=record.get("application_type", "native"),
        registration_kind=CLIENT_REGISTRATION_DYNAMIC,
        client_name=str(metadata.get("client_name") or ""),
        client_uri=str(metadata.get("client_uri") or ""),
        logo_uri=str(metadata.get("logo_uri") or ""),
        client_metadata=normalize_public_client_metadata(
            metadata.get("client_metadata")
            if isinstance(metadata.get("client_metadata"), Mapping)
            else {}
        ),
    )


CLAUDE_CLIENT = PublicClient(
    client_id="claude",
    redirect_uris=DEFAULT_CLAUDE_REDIRECT_URIS,
)

_REGISTRY = {CLAUDE_CLIENT.client_id: CLAUDE_CLIENT}


def get_client(client_id: str, source: Any | None = None) -> Optional[PublicClient]:
    if source is None:
        return _REGISTRY.get(client_id)
    cfg = oauth_delegated_config(source)
    for client in cfg.public_clients:
        if client.client_id == client_id:
            return PublicClient(
                client_id=client.client_id,
                redirect_uris=client.redirect_uris,
                token_endpoint_auth_method=client.token_endpoint_auth_method,
                application_type=client.application_type,
                registration_kind=CLIENT_REGISTRATION_PRE_REGISTERED,
                client_name=client.client_name,
                client_uri=client.client_uri,
                logo_uri=client.logo_uri,
            )
    return None


def _dcr_allowed_redirects(source: Any | None = None) -> Tuple[str, ...]:
    if source is None:
        return DEFAULT_DCR_REDIRECT_URIS
    return oauth_delegated_config(source).dynamic_client_registration.allowed_redirect_uris


def dcr_redirect_allowed(
    uri: str,
    source: Any | None = None,
    *,
    application_type: str = "native",
) -> bool:
    """True iff ``uri`` is a permitted redirect for dynamic client registration."""
    if application_type not in {"native", "web"}:
        return False
    parsed = urlsplit(uri)
    if application_type == "web" and (
        parsed.scheme != "https" or parsed.hostname in _LOOPBACK_HOSTS
    ):
        return False
    allowlist = PublicClient(
        client_id="__dcr__",
        redirect_uris=_dcr_allowed_redirects(source),
        application_type=application_type,
    )
    return redirect_uri_allowed(allowlist, uri)


def redirect_uri_allowed(client: Optional[PublicClient], uri: str) -> bool:
    if client is None or not uri:
        return False
    if uri in client.redirect_uris:
        return True
    if client.application_type == "web":
        return False
    got = urlsplit(uri)
    if got.hostname not in _LOOPBACK_HOSTS:
        return False
    # A metadata document is authored by the client, so an explicit port there
    # is a constraint to honour. A portless loopback URI cannot pin the
    # ephemeral port a native client binds at runtime (RFC 8252 section 7.3),
    # which is the form Claude Code publishes.
    document_client = client.registration_kind == CLIENT_REGISTRATION_METADATA_DOCUMENT
    for allowed in client.redirect_uris:
        a = urlsplit(allowed)
        if (
            a.hostname in _LOOPBACK_HOSTS
            and a.hostname == got.hostname
            and a.scheme == got.scheme
            and a.path == got.path
            and not (document_client and a.port is not None)
        ):
            return True
    return False
