# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""An owner shares one agent's Card with a named person, at view or edit (W319).

An agent is a resource of the person who authorized it (operator,
2026-09-25). Its owner may share it with another person:

- **view:** the person sees the agent in their pool, messages it and adds it
  to a project. Opening the Card shows it read-only.
- **edit:** everything view allows, plus changing the Card.

Connection Hub owns the grant. The share is stored next to the Card it is
about (``cards/store.py``), and a per-person index finds it. An unshare leaves
a ``revoked`` record, so it takes effect at once and the person can be told
why. No credential is copied: every read or change of a shared Card happens
under the owner's storage key, with the person recorded.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from connection_hub.delegated_credentials.cards.model import CARD_STATE_ACTIVE
from connection_hub.delegated_credentials.cards.store import (
    CardStorageError,
    subject_hash_for,
    validated_access_id,
)
from connection_hub.delegated_credentials.durable_io import read_json_or_none
from connection_hub.delegated_credentials.named_service_policy import clean_text

SHARE_VIEW = "view"
SHARE_EDIT = "edit"
SHARE_REVOKED = "revoked"
SHARE_LEVELS = (SHARE_VIEW, SHARE_EDIT)
AGENT_CARD_SHARE_SCHEMA = "connection_hub.agent_card_share.v1"


def _person(user: Mapping[str, Any]) -> str:
    for key in ("user_id", "sub", "id"):
        value = clean_text(user.get(key))
        if value and value != "anonymous" and not value.startswith("integration:"):
            return value
    return ""


def _refused(error: str, message: str, status: int) -> dict[str, Any]:
    return {"ok": False, "error": error, "message": message, "status": status}


def _public(share: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "access_id": clean_text(share.get("access_id")),
        "grantor_subject": clean_text(share.get("grantor_subject")),
        "grantee_subject": clean_text(share.get("grantee_subject")),
        "level": clean_text(share.get("level")),
        "label": clean_text(share.get("label")),
        "shared_at": int(share.get("shared_at") or 0),
        "revoked_at": int(share.get("revoked_at") or 0),
    }


class AgentCardShares:
    """Share, unshare and look up shares of agent Cards."""

    def __init__(self, store: Any) -> None:
        self._store = store

    async def _owned_card(self, owner: str, access_id: str) -> Any | None:
        loaded = await self._store.read_current_authority(
            subject_hash=subject_hash_for(owner), access_id=access_id
        )
        return loaded[1] if loaded else None

    async def _owner_request(
        self, user: Mapping[str, Any], access_id: str
    ) -> tuple[str, str, Any] | dict[str, Any]:
        owner = _person(user)
        if not owner:
            return _refused("agent_card_share_requires_person", "A signed-in person shares an agent.", 401)
        try:
            access_id = validated_access_id(access_id)
        except CardStorageError:
            return _refused("agent_card_share_access_id_invalid", "The agent's Card id is not valid.", 400)
        card = await self._owned_card(owner, access_id)
        if card is None:
            # The same answer whether the Card is absent or someone else's.
            return _refused("agent_card_share_not_owner", "Only the agent's owner shares it.", 404)
        return owner, access_id, card

    async def share(
        self, user: Mapping[str, Any], *, access_id: str, grantee_subject: str, level: str
    ) -> dict[str, Any]:
        owned = await self._owner_request(user, access_id)
        if isinstance(owned, dict):
            return owned
        owner, access_id, card = owned
        grantee = clean_text(grantee_subject)
        level = clean_text(level).lower()
        if level not in SHARE_LEVELS:
            return _refused("agent_card_share_level_invalid", "Share at view or edit level.", 400)
        if not grantee or grantee.startswith("integration:"):
            return _refused("agent_card_share_grantee_invalid", "Name the person to share with.", 400)
        if grantee == owner:
            return _refused("agent_card_share_grantee_is_owner", "The owner already has the agent.", 400)
        if str(getattr(card, "state", CARD_STATE_ACTIVE) or CARD_STATE_ACTIVE) != CARD_STATE_ACTIVE:
            return _refused("agent_card_share_card_not_active", "A revoked agent cannot be shared.", 409)
        share = {
            "schema": AGENT_CARD_SHARE_SCHEMA,
            "access_id": access_id,
            "grantor_subject": owner,
            "grantee_subject": grantee,
            "level": level,
            "label": clean_text(getattr(card, "label", "")),
            "shared_at": int(time.time()),
            "revoked_at": 0,
        }
        await self._store.write_share(
            subject_hash=subject_hash_for(owner),
            access_id=access_id,
            grantee_hash=subject_hash_for(grantee),
            share=share,
        )
        return {"ok": True, "share": _public(share)}

    async def unshare(
        self, user: Mapping[str, Any], *, access_id: str, grantee_subject: str
    ) -> dict[str, Any]:
        owned = await self._owner_request(user, access_id)
        if isinstance(owned, dict):
            return owned
        owner, access_id, _card = owned
        grantee = clean_text(grantee_subject)
        if not grantee:
            return _refused("agent_card_share_grantee_invalid", "Name the person to stop sharing with.", 400)
        current = await self._store.read_share(
            subject_hash=subject_hash_for(owner),
            access_id=access_id,
            grantee_hash=subject_hash_for(grantee),
        )
        if not isinstance(current, dict) or current.get("level") not in SHARE_LEVELS:
            return {"ok": True, "removed": False}
        revoked = {**current, "level": SHARE_REVOKED, "revoked_at": int(time.time())}
        await self._store.write_share(
            subject_hash=subject_hash_for(owner),
            access_id=access_id,
            grantee_hash=subject_hash_for(grantee),
            share=revoked,
        )
        return {"ok": True, "removed": True, "share": _public(revoked)}

    async def shares(self, user: Mapping[str, Any], *, access_id: str) -> dict[str, Any]:
        owned = await self._owner_request(user, access_id)
        if isinstance(owned, dict):
            return owned
        owner, access_id, _card = owned
        rows = await self._store.list_shares(subject_hash=subject_hash_for(owner), access_id=access_id)
        return {
            "ok": True,
            "items": sorted(
                (_public(row) for row in rows if row.get("level") in SHARE_LEVELS),
                key=lambda row: row["grantee_subject"],
            ),
        }

    async def share_for(self, grantee_subject: str, access_id: str) -> dict[str, Any] | None:
        """The share of this Card with this person, revoked included, or None.

        The record next to the Card decides; the index only locates it.
        """

        grantee = clean_text(grantee_subject)
        try:
            access_id = validated_access_id(access_id)
        except CardStorageError:
            return None
        if not grantee:
            return None
        entry = await self._read_index(grantee, access_id)
        if entry is None:
            return None
        share = await self._store.read_share(
            subject_hash=clean_text(entry.get("grantor_hash")),
            access_id=access_id,
            grantee_hash=subject_hash_for(grantee),
        )
        if not isinstance(share, dict) or clean_text(share.get("grantee_subject")) != grantee:
            return None
        if subject_hash_for(clean_text(share.get("grantor_subject"))) != clean_text(entry.get("grantor_hash")):
            return None
        if share.get("level") in SHARE_LEVELS:
            card = await self._owned_card(clean_text(share.get("grantor_subject")), access_id)
            if card is None or str(getattr(card, "state", CARD_STATE_ACTIVE)) != CARD_STATE_ACTIVE:
                return None
        return _public(share)

    async def _read_index(self, grantee: str, access_id: str) -> dict[str, Any] | None:
        entry = await read_json_or_none(
            self._store.grantee_share_path(grantee_hash=subject_hash_for(grantee), access_id=access_id)
        )
        return entry if isinstance(entry, dict) else None

    async def shared_with_me(self, user: Mapping[str, Any]) -> dict[str, Any]:
        """Every agent shared with this person now, and those whose share was revoked."""

        grantee = _person(user)
        if not grantee:
            return _refused("agent_card_share_requires_person", "A signed-in person reads their shares.", 401)
        items: list[dict[str, Any]] = []
        revoked: list[dict[str, Any]] = []
        for entry in await self._store.list_shared_with(grantee_hash=subject_hash_for(grantee)):
            share = await self.share_for(grantee, clean_text(entry.get("access_id")))
            if share is None:
                continue
            (revoked if share["level"] == SHARE_REVOKED else items).append(share)
        return {"ok": True, "items": items, "revoked": revoked}


__all__ = [
    "AGENT_CARD_SHARE_SCHEMA",
    "AgentCardShares",
    "SHARE_EDIT",
    "SHARE_LEVELS",
    "SHARE_REVOKED",
    "SHARE_VIEW",
]
