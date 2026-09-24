"""Read the public account identity of the local coding-agent runtime.

Only account identifiers are returned. Credential values are neither returned
nor logged, and decoded token claims remain identification metadata rather than
proof of authority.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..contract.errors import DomainError
from ..contract.runtime_account import normalize_runtime_account


MAX_RUNTIME_AUTH_FILE_BYTES = 2 * 1024 * 1024


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _read_json_object(path: Path) -> Mapping[str, Any]:
    try:
        if path.stat().st_size > MAX_RUNTIME_AUTH_FILE_BYTES:
            raise DomainError(
                "work_runtime_account_unavailable",
                "The coding runtime account file is too large to read safely.",
                status=409,
            )
        value = json.loads(path.read_text(encoding="utf-8"))
    except DomainError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DomainError(
            "work_runtime_account_unavailable",
            "The coding runtime account file could not be read.",
            status=409,
        ) from exc
    if not isinstance(value, Mapping):
        raise DomainError(
            "work_runtime_account_unavailable",
            "The coding runtime account file is not an object.",
            status=409,
        )
    return value


def _jwt_claims(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, str):
        return {}
    parts = value.split(".")
    if len(parts) != 3:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, Mapping) else {}


def _codex_organization(claims: Mapping[str, Any], auth: Mapping[str, Any]) -> str:
    direct = _first_text(
        auth.get("chatgpt_organization_id"),
        auth.get("organization_id"),
        claims.get("organization_id"),
    )
    if direct:
        return direct
    organizations = claims.get("organizations")
    if not isinstance(organizations, list):
        return ""
    rows = [row for row in organizations if isinstance(row, Mapping)]
    preferred = next(
        (row for row in rows if row.get("is_default") or row.get("default")),
        rows[0] if rows else {},
    )
    return _first_text(
        preferred.get("id"),
        preferred.get("organization_id"),
        preferred.get("name"),
    )


def _codex_account(document: Mapping[str, Any]) -> dict[str, str]:
    tokens = _mapping(document.get("tokens"))
    claims = _jwt_claims(tokens.get("id_token") or document.get("id_token"))
    auth = _mapping(claims.get("https://api.openai.com/auth"))
    return normalize_runtime_account(
        {
            "account_id": _first_text(
                tokens.get("account_id"),
                document.get("account_id"),
                auth.get("chatgpt_account_id"),
                auth.get("account_id"),
                claims.get("account_id"),
            ),
            "email": _first_text(claims.get("email"), auth.get("email")),
            "organization": _codex_organization(claims, auth),
        },
        required=True,
    )


def _claude_account(document: Mapping[str, Any]) -> dict[str, str]:
    account = _mapping(document.get("oauthAccount"))
    return normalize_runtime_account(
        {
            "account_id": _first_text(
                account.get("accountUuid"),
                account.get("account_id"),
            ),
            "email": _first_text(
                account.get("emailAddress"),
                account.get("email"),
            ),
            "organization": _first_text(
                account.get("organizationUuid"),
                account.get("organization_id"),
            ),
        },
        required=True,
    )


def _read_runtime_account(runtime_kind: str, *, home: Path) -> dict[str, str]:
    kind = str(runtime_kind or "").strip().lower()
    if kind == "codex":
        return _codex_account(_read_json_object(home / ".codex" / "auth.json"))
    if kind in {"claude", "claude-code"}:
        return _claude_account(_read_json_object(home / ".claude.json"))
    raise DomainError(
        "work_runtime_account_unsupported",
        "This coding runtime does not publish a supported account identity.",
        status=409,
        details={"runtime_kind": kind},
    )


async def read_runtime_account(
    runtime_kind: str,
    *,
    home: Path | None = None,
) -> dict[str, str]:
    """Read account metadata without blocking the relay event loop."""

    root = (home or Path.home()).expanduser()
    return await asyncio.to_thread(_read_runtime_account, runtime_kind, home=root)


__all__ = ["read_runtime_account"]
