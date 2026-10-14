"""Named authorities: who Connection Hub asks, configured by name (W374).

Connection Hub decides nothing about the scopes a host keeps (a host's projects,
for example). For each question it asks a configured authority by name:

- ``membership``: which roles a subject holds in a scope;
- ``agent_card``: may this person open or change this delegate Card;
- ``control_card``: may this person read or change this scope's Control Card;
- ``credential_release``: may this delegate use a provider credential for a resource.
  No person session travels with it, so the call carries the peer proof.

An authority is an in-platform app operation (``kind: app_operation``: a
``bundle_id`` and an ``operation``). ``kind: https`` is reserved for an external
authority and is refused by name until it is implemented. A missing authority
is not an error here: each port refuses by its own name when asked.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from connection_hub.delegated_credentials.named_service_policy import clean_text

AUTHORITY_KIND_APP_OPERATION = "app_operation"
AUTHORITY_KIND_HTTPS = "https"
AUTHORITY_KINDS = frozenset({AUTHORITY_KIND_APP_OPERATION, AUTHORITY_KIND_HTTPS})

MEMBERSHIP = "membership"
AGENT_CARD = "agent_card"
CONTROL_CARD = "control_card"
CREDENTIAL_RELEASE = "credential_release"
AUTHORITY_NAMES = frozenset({MEMBERSHIP, AGENT_CARD, CONTROL_CARD, CREDENTIAL_RELEASE})

_FIELDS = frozenset({"kind", "bundle_id", "operation", "peer_proof_secret_ref", "administrative_roles"})


class AuthorityConfigError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _roles(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    source: Iterable[Any]
    if isinstance(values, str):
        # Same reading as the legacy project_membership roles: commas or spaces.
        source = values.replace(",", " ").split()
    elif isinstance(values, (list, tuple, set, frozenset)):
        source = values
    else:
        raise AuthorityConfigError("authority_administrative_roles_invalid")
    if any(not isinstance(value, str) for value in source):
        raise AuthorityConfigError("authority_administrative_roles_invalid")
    return tuple(sorted({role for value in source if (role := clean_text(value).lower())}))


@dataclass(frozen=True)
class AuthorityConfig:
    """One named authority, exactly as configured; no field is inferred."""

    name: str
    kind: str = AUTHORITY_KIND_APP_OPERATION
    bundle_id: str = ""
    operation: str = ""
    peer_proof_secret_ref: str = ""
    administrative_roles: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, name: str, value: Any) -> "AuthorityConfig":
        if not isinstance(value, Mapping):
            raise AuthorityConfigError("authority_config_invalid")
        unknown = set(value) - _FIELDS
        if unknown:
            raise AuthorityConfigError("authority_config_invalid")
        kind = clean_text(value.get("kind")) or AUTHORITY_KIND_APP_OPERATION
        if kind not in AUTHORITY_KINDS:
            raise AuthorityConfigError("authority_kind_invalid")
        return cls(
            name=name,
            kind=kind,
            bundle_id=clean_text(value.get("bundle_id")),
            operation=clean_text(value.get("operation")),
            peer_proof_secret_ref=clean_text(value.get("peer_proof_secret_ref")),
            administrative_roles=_roles(value.get("administrative_roles")),
        )

    def require_supported(self) -> None:
        """Refuse by name what this release cannot call."""

        if self.kind != AUTHORITY_KIND_APP_OPERATION:
            raise AuthorityConfigError("authority_kind_unsupported")


def authorities_from_mapping(value: Any) -> dict[str, AuthorityConfig]:
    """Parse the descriptor's ``authorities`` block; unknown names are refused."""

    if not isinstance(value, Mapping):
        raise AuthorityConfigError("authorities_config_invalid")
    result: dict[str, AuthorityConfig] = {}
    for raw_name, raw in value.items():
        name = clean_text(raw_name)
        if name not in AUTHORITY_NAMES:
            raise AuthorityConfigError("authority_name_unknown")
        result[name] = AuthorityConfig.from_mapping(name, raw)
    return result


__all__ = [
    "AGENT_CARD",
    "AUTHORITY_KINDS",
    "AUTHORITY_KIND_APP_OPERATION",
    "AUTHORITY_KIND_HTTPS",
    "AUTHORITY_NAMES",
    "AuthorityConfig",
    "AuthorityConfigError",
    "CONTROL_CARD",
    "CREDENTIAL_RELEASE",
    "MEMBERSHIP",
    "authorities_from_mapping",
]
