"""What a Data Bus session presents at the door.

Two things can open a session, and they are not the same kind of thing.

A ``DataBusClaim`` is a short-lived token minted for this session. It carries
its own expiry, and once minted it stands on its own: nothing behind it can be
withdrawn before it lapses.

A ``DelegatedCardCredential`` is the delegated card the caller already holds.
It is not exchanged for anything. The card stays the authority, the server
resolves it live on every operation, and revoking it ends the session rather
than waiting for a token to run out. That is the whole point of presenting it
directly: authorization that can be taken back.

A card has no client-side expiry to check, which is why ``expires_at`` is
allowed to be zero. That is not an unlimited session. It means this side does
not get a vote, because the card's current state is the answer and only the
server can see it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


@runtime_checkable
class DataBusCredential(Protocol):
    """What the client needs from anything that can open a session."""

    tenant: str
    project: str
    bundle_id: str
    expires_at: int

    def auth_payload(self) -> dict[str, Any]:
        """The Socket.IO handshake fields this credential presents."""


@dataclass(frozen=True, slots=True)
class DelegatedCardCredential:
    """A delegated card presented directly, with no exchange in between."""

    tenant: str
    project: str
    bundle_id: str
    resource: str
    bearer_token: str = field(repr=False)
    # A card is resolved live server-side, so this side has no expiry to check.
    expires_at: int = 0

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "DelegatedCardCredential":
        record = _mapping(value)
        required = {
            "tenant": str(record.get("tenant") or "").strip(),
            "project": str(record.get("project") or "").strip(),
            "bundle_id": str(record.get("bundle_id") or "").strip(),
            "resource": str(record.get("resource") or "").strip(),
            "bearer_token": str(record.get("bearer_token") or "").strip(),
        }
        missing = sorted(key for key, item in required.items() if not item)
        if missing:
            raise ValueError(
                "Delegated card credential is missing: " + ", ".join(missing)
            )
        return cls(**required)

    def auth_payload(self) -> dict[str, Any]:
        return {
            "tenant": self.tenant,
            "project": self.project,
            "bundle_id": self.bundle_id,
            # Deliberately not the key the platform bearer branch reads. A card
            # arriving there is not a card, it is a malformed platform token,
            # and it would fail somewhere that does not name the real cause.
            "delegated_bearer_token": self.bearer_token,
            "delegated_resource": self.resource,
            "client_role": "service",
        }


__all__ = ["DataBusCredential", "DelegatedCardCredential"]
