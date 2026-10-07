# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W606: a committed Card edit moves its handle binding, never the credential.

A credential-bearing Card's PostgreSQL handle row names the Card revision and
expiry it serves, and the strict reader refuses any other. A coordinated edit
commits a new Card revision; this module moves only those two serving fields
of the ACTIVE row to the committed AFTER, by compare-and-set on the row's
whole identity as the edit's intent pinned it. The bearer, session id,
resident secret reference and fingerprint stay byte-identical: the token is a
pointer, and what it may do is resolved from the live Card when it is
presented (operator, 7 October 2026). A row that is gone, ended or no longer
that identity is ``superseded``: nothing is written, nothing is revived.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from connection_hub.delegated_credentials.cards.handle_metadata import HANDLE_STATE_ACTIVE, CardHandleMetadata
from connection_hub.delegated_credentials.cards.handle_schema import TABLE_CARD_HANDLE_METADATA

BINDING_APPLIED = "applied"
BINDING_SUPERSEDED = "superseded"


def binding_identity_digest(metadata: CardHandleMetadata) -> str:
    """The digest of a row's whole serving identity (Card id, revision, expiry, secret, session, state)."""
    return hashlib.sha256(json.dumps(list(metadata.payload_identity), separators=(",", ":"),
                                     ensure_ascii=True).encode("utf-8")).hexdigest()


class PostgresCardHandleBindingAuthority:
    """The one compare-and-set that advances a binding; uses the record authority's row lock."""

    def __init__(self, *, records: Any) -> None:
        self._records = records

    async def advance(self, access_id: str, *, from_identity: str, from_revision: int, from_expires_at: int,
                      to_revision: int, to_expires_at: int) -> str:
        """``applied`` (now, or on an exact replay) or ``superseded``; never revives or re-mints."""
        schema = self._records.schema
        async with self._records.pool.acquire() as connection, connection.transaction():
            current = await self._records.locked(connection, access_id)
            if current is None or current.state != HANDLE_STATE_ACTIVE:
                return BINDING_SUPERSEDED
            if (current.card_revision, current.expires_at) == (to_revision, to_expires_at):
                # A replay: the same row, already moved by this effect, is the identity pinned before it.
                before = replace(current, card_revision=from_revision, expires_at=from_expires_at)
                return BINDING_APPLIED if binding_identity_digest(before) == from_identity else BINDING_SUPERSEDED
            if binding_identity_digest(current) != from_identity:
                return BINDING_SUPERSEDED
            await connection.execute(
                f"""
                UPDATE {schema}.{TABLE_CARD_HANDLE_METADATA}
                   SET card_revision = $2,
                       expires_at = to_timestamp($3),
                       revision = revision + 1,
                       updated_at = now()
                 WHERE access_id = $1 AND state = 'active'
                """,
                current.access_id, int(to_revision), int(to_expires_at),
            )
        return BINDING_APPLIED


__all__ = ["BINDING_APPLIED", "BINDING_SUPERSEDED", "PostgresCardHandleBindingAuthority",
           "binding_identity_digest"]
