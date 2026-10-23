# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Entry-bound catalog rows: a Card admitted only under the URL it was issued for.

A catalog row usually matches many concrete doors through one pattern, and a
Card keyed by that pattern is admitted at every one of them. A row marked
``entry_bound`` narrows that: a Card holding it is admitted only for requests
at or under the Card's own ``entry_resource``, the concrete URL its OAuth grant
named. One published A2A agent's token therefore never reaches another's door.

Both sides are compared in one canonical form, and the host is the Hub's public
base, never a request header. Anything that cannot be canonicalized refuses.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urlsplit

from connection_hub.delegated_credentials.cards.identity import CARD_KIND_CONNECTOR
from connection_hub.delegated_credentials.resource_operations import normalize_resource, resource_matches

RESOURCE_OUTSIDE_ENTRY = "resource_outside_entry"

_DEFAULT_PORTS = {"https": 443, "http": 80}
_UNRESERVED = re.compile(r"[A-Za-z0-9\-._~]")
_ESCAPE = re.compile(r"%([0-9A-Fa-f]{2})")
# Escapes that would change the path's shape once decoded: "/" and "\".
_SHAPE_ESCAPES = {"2F", "5C"}


def _normalize_escapes(segment: str) -> str | None:
    """Decode unreserved escapes and upper-case the rest; refuse a stray ``%``."""

    if "%" in _ESCAPE.sub("", segment):
        return None

    def replace(match: re.Match[str]) -> str:
        hex_value = match.group(1).upper()
        char = chr(int(hex_value, 16))
        if _UNRESERVED.fullmatch(char) and hex_value not in _SHAPE_ESCAPES:
            return char
        return f"%{hex_value}"

    return _ESCAPE.sub(replace, segment)


def canonical_entry_url(value: Any) -> str | None:
    """The canonical form of an absolute http(s) URL, or None to refuse.

    Lower-case scheme and host, no default port, unreserved escapes decoded and
    the others upper-cased; query, fragment and one trailing slash dropped.
    Refused: userinfo, ``.`` or ``..`` segments (plain or encoded), an encoded
    ``/`` or ``\\`` in a segment, empty inner segments.
    """

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        return None
    host = (parts.hostname or "").lower()
    if not host or parts.username is not None or parts.password is not None:
        return None
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    path = parts.path or "/"
    if not path.startswith("/"):
        return None
    raw_segments = path[1:].split("/")
    if raw_segments and raw_segments[-1] == "":
        raw_segments = raw_segments[:-1]
    segments: list[str] = []
    for raw in raw_segments:
        if raw == "":
            return None
        segment = _normalize_escapes(raw)
        if segment is None:
            return None
        decoded = unquote(segment)
        if decoded in {".", ".."} or "/" in decoded or "\\" in decoded:
            return None
        segments.append(segment)
    return f"{scheme}://{netloc}" + "".join(f"/{segment}" for segment in segments)


def _origin(canonical: str) -> str:
    parts = urlsplit(canonical)
    return f"{parts.scheme}://{parts.netloc}"


def entry_covers(entry: Any, request: Any, *, public_base: Any) -> bool:
    """Whether ``request`` is at or under ``entry``, both on the Hub's public base.

    Fails closed: a missing or uncanonical value on any side refuses.
    """

    canonical_entry = canonical_entry_url(entry)
    canonical_request = canonical_entry_url(request)
    canonical_base = canonical_entry_url(public_base)
    if not canonical_entry or not canonical_request or not canonical_base:
        return False
    origin = _origin(canonical_base)
    if _origin(canonical_entry) != origin or _origin(canonical_request) != origin:
        return False
    return canonical_request == canonical_entry or canonical_request.startswith(canonical_entry + "/")


def entry_bound_row(rows: Iterable[Any], *, resource: str, request_resource: str = "") -> bool:
    """Whether any entry-bound row governs this Card key or this request.

    Independent of declaration order: a broader, unbound row declared first
    must not hide the bound one. A row counts when it equals or matches the
    Card's key, or matches the request URL.
    """

    key = normalize_resource(resource)
    request = normalize_resource(request_resource)
    for row in rows or ():
        if not bool(getattr(row, "entry_bound", False)):
            continue
        pattern = normalize_resource(getattr(row, "resource", ""))
        if not pattern:
            continue
        if key and (pattern == key or resource_matches(pattern, key)):
            return True
        if request and resource_matches(pattern, request):
            return True
    return False


def entry_binding_refusal(
    *,
    bound: bool,
    grant_record: Mapping[str, Any] | None,
    request_resource: str,
    matched_resource: str,
    granted_operations: Any,
    public_base: str,
) -> str:
    """The refusal reason for an entry-bound row, or "" when the row is not bound or admits.

    Only a connector Card with a concrete entry and explicit operations on the
    row is admitted; whole-Card, Control and entry-less Cards are refused.
    """

    if not bound:
        return ""
    record = dict(grant_record or {})
    if str(record.get("card_kind") or "").strip() != CARD_KIND_CONNECTOR:
        return RESOURCE_OUTSIDE_ENTRY
    if not entry_covers(record.get("entry_resource"), request_resource, public_base=public_base):
        return RESOURCE_OUTSIDE_ENTRY
    explicit = dict(record.get("resource_operations") or {}).get(matched_resource)
    if not explicit or "*" in set(explicit) or not granted_operations:
        return "operation_not_consented"
    return ""


__all__ = [
    "RESOURCE_OUTSIDE_ENTRY",
    "canonical_entry_url",
    "entry_binding_refusal",
    "entry_bound_row",
    "entry_covers",
]
