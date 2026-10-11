# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W661 lock (b), b-core: the Card's mutable objects as PostgreSQL rows fenced by compare-and-swap.

Design (Ops 19:28Z + 20:29Z, Spark OK 20:30Z with C1 and C2):

- The ONE mutable object per Card, its current pointer, is a row in ``connection_hub_card_pointers``. It holds
  the whole links-only pointer document (any of the four ``current.json`` schemas: plain, W578 transaction,
  issuer update, lifecycle), bounded, plus ``row_revision``. Every change is
  ``UPDATE ... WHERE key AND row_revision = <the revision this operation read>``. Zero rows is a stale writer,
  refused ``card_pointer_moved`` whatever its lock state: the advisory lock only serializes the normal case.
- The card-version txn marker is a row in ``connection_hub_card_version_txns`` under the same rule (R1).
- ``current.json`` and the marker file stay as HINTS, written after the row; once a row exists no reader
  prefers the file (C2). A missing row means nobody wrote that object since the cutover: a writer holding the
  Card lock then creates the row from the file (first touch, INSERT ... ON CONFLICT DO NOTHING, no scan); a
  reader that holds no lock never creates a row.
- The revision an operation read is remembered in its row context (locks.CardRowOperation, one per Card
  operation, opened by row_fenced_lock around whatever mutation lock serializes the normal case: the local flock,
  or the per-Card KDCube Redis cluster lock of option R), so a write can only fence against what this operation
  actually decided on. A write with nothing observed is refused. The lock is the performance layer; the CAS is
  the storage-level optimistic check the KDCube data-bus doc requires (operator 21:50Z: "R now, with P").

Rows hold links and ids only (revision names, checksums, versions, txn ids), never a Card body.
"""

from __future__ import annotations

import re
from typing import Any

from service_foundation.coordination.durable_wire import canonical_json_bytes, parse_canonical_json_bytes, sha256_hex

from .store import CardStorageError

TABLE_CARD_POINTERS = "connection_hub_card_pointers"
TABLE_CARD_VERSION_TXNS = "connection_hub_card_version_txns"
TABLE_CARD_POINTER_AUTHORITY = "connection_hub_card_pointer_authority"
POINTER_DOCUMENT_MAX_BYTES = 4096
MARKER_DOCUMENT_MAX_BYTES = 65536
# A removed pointer (an aborted create of a new id) stays a row, so row_revision never restarts (no ABA).
ABSENT_POINTER = {"schema": "connection_hub.card-current-absent.v1"}
_SCHEMA_PATTERN = re.compile(r"^kdcube_[a-z0-9_]{1,56}$")


def card_pointer_schema_sql(schema: str) -> str:
    if not _SCHEMA_PATTERN.fullmatch(str(schema or "")):
        raise ValueError("The exact validated tenant/project schema is required.")
    return f"""
CREATE SCHEMA IF NOT EXISTS {schema};

CREATE TABLE IF NOT EXISTS {schema}.{TABLE_CARD_POINTERS} (
    tenant        TEXT NOT NULL,
    project       TEXT NOT NULL,
    subject_hash  TEXT NOT NULL,
    access_id     TEXT NOT NULL,
    document      BYTEA NOT NULL CHECK (octet_length(document) BETWEEN 2 AND {POINTER_DOCUMENT_MAX_BYTES}),
    digest        CHAR(64) NOT NULL,
    row_revision  BIGINT NOT NULL CHECK (row_revision >= 1),
    fence         TEXT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant, project, subject_hash, access_id)
);
ALTER TABLE {schema}.{TABLE_CARD_POINTERS} ADD COLUMN IF NOT EXISTS fence TEXT NULL;

CREATE TABLE IF NOT EXISTS {schema}.{TABLE_CARD_VERSION_TXNS} (
    tenant        TEXT NOT NULL,
    project       TEXT NOT NULL,
    txn           TEXT NOT NULL,
    document      BYTEA NOT NULL CHECK (octet_length(document) BETWEEN 2 AND {MARKER_DOCUMENT_MAX_BYTES}),
    row_revision  BIGINT NOT NULL CHECK (row_revision >= 1),
    incarnation   TEXT NOT NULL DEFAULT gen_random_uuid()::text,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant, project, txn)
);
-- Infra's ABA HOLD (9cd86893): a deleted and re-created marker row restarts at row_revision 1, so the CAS
-- also names the row's incarnation (random, set at creation): a stale observation never matches a new row.
ALTER TABLE {schema}.{TABLE_CARD_VERSION_TXNS}
    ADD COLUMN IF NOT EXISTS incarnation TEXT NOT NULL DEFAULT gen_random_uuid()::text;
-- Infra's post-owner-guard HOLD (6e0320df): a locked read claims the row (fence := its operation id) in the
-- same statement, and every CAS requires that fence: the latest locked reader owns the row.
ALTER TABLE {schema}.{TABLE_CARD_VERSION_TXNS} ADD COLUMN IF NOT EXISTS fence TEXT NULL;

CREATE TABLE IF NOT EXISTS {schema}.{TABLE_CARD_POINTER_AUTHORITY} (
    tenant        TEXT NOT NULL,
    project       TEXT NOT NULL,
    activated_at  TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (tenant, project)
);
"""


def _encode(document: Any, *, limit: int) -> bytes:
    if not isinstance(document, dict):
        raise CardStorageError("card_row_document_invalid")
    encoded = canonical_json_bytes(document)
    if not 2 <= len(encoded) <= limit:
        raise CardStorageError("card_row_document_too_large")
    return encoded


def _session():
    from .locks import current_lock_session

    return current_lock_session()


class PostgresCardRows:
    """The pointer and txn-marker rows of one tenant/project. ``pool`` is a dedicated, bounded asyncpg pool."""

    def __init__(self, pool: Any, *, schema: str, tenant: str, project: str) -> None:
        if not _SCHEMA_PATTERN.fullmatch(str(schema or "")):
            raise ValueError("The exact validated tenant/project schema is required.")
        self.pool, self.schema, self.tenant, self.project = pool, schema, str(tenant), str(project)

    async def ensure_schema(self) -> None:
        """Idempotent bootstrap, guarded as KDCube prescribes for concurrent proc replicas (critical-section-README,
        "Database Bootstrap Critical Section"): one transaction under pg_advisory_xact_lock, on ONE connection."""
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
                                         f"{self.schema}:connection-hub-card-rows-schema")
                await connection.execute(card_pointer_schema_sql(self.schema))

    # ---- one statement, on this operation's lock session when there is one --------------------------------

    async def _fetchrow(self, query: str, *args: Any) -> Any:
        # Each statement on its own pool connection: the fence is the row CAS, not a session (option P under R).
        async with self.pool.acquire() as connection:
            return await connection.fetchrow(query, *args)

    # ---- the cutover gate (C1) -----------------------------------------------------------------------------

    async def activate(self) -> None:
        """Record that this scope's Card pointers are row-authoritative (idempotent; at b-core startup)."""
        await self._fetchrow(
            f"""INSERT INTO {self.schema}.{TABLE_CARD_POINTER_AUTHORITY} (tenant, project) VALUES ($1, $2)
                ON CONFLICT DO NOTHING RETURNING tenant""", self.tenant, self.project)

    async def activation_recorded(self) -> bool:
        """Whether this scope was ever activated, WITHOUT creating anything: no authority table means never
        activated (a scope where P is off gets no DDL; Ops' N3)."""
        row = await self._fetchrow("SELECT to_regclass($1) IS NOT NULL AS present",
                                   f"{self.schema}.{TABLE_CARD_POINTER_AUTHORITY}")
        return bool(row and row["present"]) and await self.is_activated()

    async def is_activated(self) -> bool:
        row = await self._fetchrow(
            f"""SELECT 1 AS active FROM {self.schema}.{TABLE_CARD_POINTER_AUTHORITY}
                WHERE tenant=$1 AND project=$2""", self.tenant, self.project)
        return row is not None

    # ---- generic fenced rows -------------------------------------------------------------------------------

    def _observed(self, kind: str, key: tuple, *, required: bool) -> int | None:
        session = _session()
        if session is None:
            if required:
                raise CardStorageError("card_lock_not_held")
            return None
        if required and (kind, key) not in session.observed:
            raise CardStorageError("card_row_unobserved")
        return session.observed.get((kind, key))

    def _observe(self, kind: str, key: tuple, revision: int) -> None:
        session = _session()
        if session is not None:
            session.observed[(kind, key)] = revision

    async def read_pointer(self, *, subject_hash: str, access_id: str) -> tuple[dict | None, int | None]:
        """(document, row_revision) or (None, None) when no row exists. Records the revision read. Under a Card
        operation the read CLAIMS the row (fence := the operation id) in the same statement; a lock-free read
        claims nothing."""
        session = _session()
        if session is None:
            row = await self._fetchrow(
                f"""SELECT document, row_revision FROM {self.schema}.{TABLE_CARD_POINTERS}
                    WHERE tenant=$1 AND project=$2 AND subject_hash=$3 AND access_id=$4""",
                self.tenant, self.project, subject_hash, access_id)
        else:
            row = await self._fetchrow(
                f"""UPDATE {self.schema}.{TABLE_CARD_POINTERS} SET fence=$5
                    WHERE tenant=$1 AND project=$2 AND subject_hash=$3 AND access_id=$4
                    RETURNING document, row_revision""",
                self.tenant, self.project, subject_hash, access_id, session.op_id)
        if row is None:
            self._observe("pointer", (subject_hash, access_id), 0)
            return None, None
        self._observe("pointer", (subject_hash, access_id), int(row["row_revision"]))
        return parse_canonical_json_bytes(bytes(row["document"])), int(row["row_revision"])

    async def adopt_pointer(self, *, subject_hash: str, access_id: str, document: dict) -> tuple[dict, int]:
        """First touch under the Card lock: create the row from the file's document unless one exists."""
        self._observed("pointer", (subject_hash, access_id), required=True)  # only a lock holder adopts
        encoded = _encode(document, limit=POINTER_DOCUMENT_MAX_BYTES)
        await self._fetchrow(
            f"""INSERT INTO {self.schema}.{TABLE_CARD_POINTERS}
                (tenant, project, subject_hash, access_id, document, digest, row_revision, fence)
                VALUES ($1, $2, $3, $4, $5, $6, 1, $7) ON CONFLICT DO NOTHING RETURNING row_revision""",
            self.tenant, self.project, subject_hash, access_id, encoded, sha256_hex(encoded), _session().op_id)
        found, revision = await self.read_pointer(subject_hash=subject_hash, access_id=access_id)
        if found is None or revision is None:
            raise CardStorageError("card_pointer_adoption_failed")
        return found, revision

    async def write_pointer(self, *, subject_hash: str, access_id: str, document: dict) -> int:
        """CAS from the revision this operation read (0: no row yet). Zero rows: card_pointer_moved."""
        key = (subject_hash, access_id)
        expected = self._observed("pointer", key, required=True)
        encoded = _encode(document, limit=POINTER_DOCUMENT_MAX_BYTES)
        op_id = _session().op_id
        if expected == 0:
            row = await self._fetchrow(
                f"""INSERT INTO {self.schema}.{TABLE_CARD_POINTERS}
                    (tenant, project, subject_hash, access_id, document, digest, row_revision, fence)
                    VALUES ($1, $2, $3, $4, $5, $6, 1, $7) ON CONFLICT DO NOTHING RETURNING row_revision""",
                self.tenant, self.project, subject_hash, access_id, encoded, sha256_hex(encoded), op_id)
        else:
            # The CAS names the revision this operation read AND its own fence: a later locked reader's claim
            # (the successor) refuses this write even if no new revision was written yet.
            row = await self._fetchrow(
                f"""UPDATE {self.schema}.{TABLE_CARD_POINTERS}
                    SET document=$5, digest=$6, row_revision=row_revision+1, updated_at=clock_timestamp()
                    WHERE tenant=$1 AND project=$2 AND subject_hash=$3 AND access_id=$4 AND row_revision=$7
                      AND fence=$8
                    RETURNING row_revision""",
                self.tenant, self.project, subject_hash, access_id, encoded, sha256_hex(encoded), expected, op_id)
        if row is None:
            raise CardStorageError("card_pointer_moved")
        self._observe("pointer", key, int(row["row_revision"]))
        return int(row["row_revision"])

    async def read_marker(self, txn: str) -> tuple[dict | None, int | None]:
        session = _session()
        if session is None:
            row = await self._fetchrow(
                f"""SELECT document, row_revision, incarnation FROM {self.schema}.{TABLE_CARD_VERSION_TXNS}
                    WHERE tenant=$1 AND project=$2 AND txn=$3""", self.tenant, self.project, txn)
        else:  # a locked read claims the row (see read_pointer)
            row = await self._fetchrow(
                f"""UPDATE {self.schema}.{TABLE_CARD_VERSION_TXNS} SET fence=$4
                    WHERE tenant=$1 AND project=$2 AND txn=$3
                    RETURNING document, row_revision, incarnation""", self.tenant, self.project, txn, session.op_id)
        if row is None:
            self._observe("marker", (txn,), (0, None))
            return None, None
        self._observe("marker", (txn,), (int(row["row_revision"]), str(row["incarnation"])))
        return parse_canonical_json_bytes(bytes(row["document"])), int(row["row_revision"])

    async def adopt_marker(self, txn: str, document: dict) -> tuple[dict, int]:
        """First touch of a marker written before the cutover: only an operation holding a Card lock adopts."""
        self._observed("marker", (txn,), required=True)
        encoded = _encode(document, limit=MARKER_DOCUMENT_MAX_BYTES)
        await self._fetchrow(
            f"""INSERT INTO {self.schema}.{TABLE_CARD_VERSION_TXNS} (tenant, project, txn, document, row_revision, fence)
                VALUES ($1, $2, $3, $4, 1, $5) ON CONFLICT DO NOTHING RETURNING row_revision""",
            self.tenant, self.project, txn, encoded, _session().op_id)
        found, revision = await self.read_marker(txn)
        if found is None or revision is None:
            raise CardStorageError("card_version_marker_adoption_failed")
        return found, revision

    async def write_marker(self, txn: str, document: dict) -> int:
        """R1: INSERT when this operation saw no row, else CAS on the revision it read. Outside any lock only a
        create is possible (a ROLLBACK of a txn with no links holds no Card lock): INSERT ... ON CONFLICT DO
        NOTHING is itself the fence, so it never replaces a marker someone else wrote."""
        session = _session()
        expected, incarnation = (0, None) if session is None else self._observed("marker", (txn,), required=True)
        op_id = None if session is None else session.op_id
        encoded = _encode(document, limit=MARKER_DOCUMENT_MAX_BYTES)
        if expected == 0:
            row = await self._fetchrow(
                f"""INSERT INTO {self.schema}.{TABLE_CARD_VERSION_TXNS} (tenant, project, txn, document, row_revision, fence)
                    VALUES ($1, $2, $3, $4, 1, $5) ON CONFLICT DO NOTHING RETURNING row_revision, incarnation""",
                self.tenant, self.project, txn, encoded, op_id)
        else:
            # The CAS names the exact row this operation read (revision AND incarnation, Infra's ABA) and this
            # operation's own fence (a successor's locked read refuses it, Infra's post-owner-guard HOLD).
            row = await self._fetchrow(
                f"""UPDATE {self.schema}.{TABLE_CARD_VERSION_TXNS}
                    SET document=$4, row_revision=row_revision+1, updated_at=clock_timestamp()
                    WHERE tenant=$1 AND project=$2 AND txn=$3 AND row_revision=$5 AND incarnation=$6 AND fence=$7
                    RETURNING row_revision, incarnation""",
                self.tenant, self.project, txn, encoded, expected, incarnation, op_id)
        if row is None:
            raise CardStorageError("card_version_marker_moved")
        self._observe("marker", (txn,), (int(row["row_revision"]), str(row["incarnation"])))
        return int(row["row_revision"])

    async def delete_marker(self, txn: str) -> None:
        """R1: the marker's removal is a CAS too; a stale deleter never removes a newer state."""
        expected, incarnation = self._observed("marker", (txn,), required=True)
        if expected == 0:
            return  # this operation saw it absent
        row = await self._fetchrow(
            f"""DELETE FROM {self.schema}.{TABLE_CARD_VERSION_TXNS}
                WHERE tenant=$1 AND project=$2 AND txn=$3 AND row_revision=$4 AND incarnation=$5 AND fence=$6
                RETURNING txn""",
            self.tenant, self.project, txn, expected, incarnation, _session().op_id)
        if row is None:
            raise CardStorageError("card_version_marker_moved")
        self._observe("marker", (txn,), (0, None))


__all__ = ["ABSENT_POINTER", "MARKER_DOCUMENT_MAX_BYTES", "POINTER_DOCUMENT_MAX_BYTES", "PostgresCardRows",
           "TABLE_CARD_POINTERS", "TABLE_CARD_POINTER_AUTHORITY", "TABLE_CARD_VERSION_TXNS", "card_pointer_schema_sql"]
