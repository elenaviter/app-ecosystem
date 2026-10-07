# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W603: an original OAuth issuance's plan and credential reservations (PostgreSQL).

An authorization-code exchange mints its credentials before the Card decision
that grants them commits. They are therefore RESERVED here, bound to that
decision's transaction and slot, and stay in no table a reader looks at: no
family, generation or access binding exists for them. Only the COMMIT's
``credential_issue`` effect activates a reservation, by inserting the real
family and generation (``refresh``) or access binding (``access``) under the
Card's family lock; an ABORT, a superseded Card or an expired reservation
leaves nothing usable.

Every deadline is read from PostgreSQL's ``clock_timestamp()``. The plan is
written once, before the decision begins (the first writer wins), so a replay
rebuilds the same decision and returns the same deadlines; a reservation copies
its trusted fields from that stored plan, never from the caller. Bearers are
present only as SHA-256 digests. This is a mixin of
``PostgresOAuthAuthorityStore``: it uses that store's pool, schema and scope.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Mapping

from connection_hub.delegated_credentials.oauth.authority_schema import (
    TABLE_ACCESS_BINDINGS,
    TABLE_FAMILIES,
    TABLE_ISSUANCE_PLANS,
    TABLE_ISSUANCE_RESERVATIONS,
    TABLE_REFRESH_GENERATIONS,
)

ISSUANCE_SLOTS = ("access", "refresh")
# The longest token lifetime a minter may ask a reservation to carry; the
# Card's absolute expiry caps it in any case.
MAX_ISSUANCE_TTL_SECONDS = 400 * 86400
# How long one completion's claim lasts before it must be renewed; a dead holder's claim lapses after it.
COMPLETION_CLAIM_SECONDS = 60
_HEX64 = re.compile(r"[0-9a-f]{64}")


class IssuanceStoreRefused(ValueError):
    """A named refusal; the reason never carries a bearer or a record value."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def reservation_digest(token_sha256: str, record: Mapping[str, Any], ttl_seconds: int) -> str:
    """What a reservation replay must repeat exactly: the bearer digest, the record and its lifetime."""
    return hashlib.sha256(_canonical({"token_sha256": token_sha256, "record": dict(record),
                                      "ttl_seconds": ttl_seconds}).encode("utf-8")).hexdigest()


class IssuanceReservationStore:
    """The plan and reservation statements; mixed into ``PostgresOAuthAuthorityStore``."""

    _pool: Any
    schema: str
    tenant: str
    project: str

    async def issuance_clock(self) -> int:
        """PostgreSQL's current time, whole seconds: the one clock every issuance deadline uses."""
        async with self._pool.acquire() as connection:
            return int(await connection.fetchval(
                "SELECT floor(extract(epoch from clock_timestamp()))::bigint"))

    async def put_issuance_plan(self, *, decision_request_id: str, original_input_digest: str,
                                plan: Mapping[str, Any], reserved_until: int) -> dict[str, Any]:
        """Store the plan unless one exists; return the stored one (the first writer's).

        A stored plan for the same request with another input digest refuses
        ``issuance_replay_changed``: the same original request never plans twice.
        """
        if not _HEX64.fullmatch(str(decision_request_id)) or not _HEX64.fullmatch(str(original_input_digest)):
            raise IssuanceStoreRefused("issuance_plan_invalid")
        if type(reserved_until) is not int or reserved_until <= 0:
            raise IssuanceStoreRefused("issuance_plan_invalid")
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.execute(
                    f"""
                    INSERT INTO {self.schema}.{TABLE_ISSUANCE_PLANS} (
                        tenant, project, decision_request_id, original_input_digest, plan, reserved_until
                    ) VALUES ($1, $2, $3, $4, ($5::text)::jsonb, to_timestamp($6::bigint))
                    ON CONFLICT (tenant, project, decision_request_id) DO NOTHING
                    """,
                    self.tenant, self.project, decision_request_id, original_input_digest,
                    _canonical(dict(plan)), reserved_until,
                )
                row = await connection.fetchrow(
                    f"""
                    SELECT original_input_digest, transaction_id, plan
                    FROM {self.schema}.{TABLE_ISSUANCE_PLANS}
                    WHERE tenant = $1 AND project = $2 AND decision_request_id = $3
                    """,
                    self.tenant, self.project, decision_request_id,
                )
        if row is None:
            raise IssuanceStoreRefused("issuance_plan_unavailable")
        if str(row["original_input_digest"]) != original_input_digest:
            raise IssuanceStoreRefused("issuance_replay_changed")
        return self._plan_row(row)

    @staticmethod
    def _plan_row(row: Any) -> dict[str, Any]:
        plan = row["plan"]
        plan = json.loads(plan) if isinstance(plan, str) else dict(plan)
        return {"plan": plan, "transaction_id": str(row["transaction_id"] or "")}

    async def bind_issuance_plan_transaction(self, *, decision_request_id: str, transaction_id: str) -> None:
        """Record the decision's transaction id on its plan; the same id again is a no-op."""
        if not _HEX64.fullmatch(str(transaction_id)):
            raise IssuanceStoreRefused("issuance_transaction_invalid")
        async with self._pool.acquire() as connection:
            bound = await connection.fetchval(
                f"""
                UPDATE {self.schema}.{TABLE_ISSUANCE_PLANS}
                   SET transaction_id = $4
                 WHERE tenant = $1 AND project = $2 AND decision_request_id = $3
                   AND (transaction_id IS NULL OR transaction_id = $4)
                RETURNING 1
                """,
                self.tenant, self.project, decision_request_id, transaction_id,
            )
        if bound is None:
            raise IssuanceStoreRefused("issuance_plan_transaction_conflict")

    async def read_issuance_plan_request(self, decision_request_id: str) -> dict[str, Any] | None:
        """The stored plan of one original request (with its input digest), or None."""
        if not _HEX64.fullmatch(str(decision_request_id)):
            return None
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT original_input_digest, transaction_id, plan
                FROM {self.schema}.{TABLE_ISSUANCE_PLANS}
                WHERE tenant = $1 AND project = $2 AND decision_request_id = $3
                """,
                self.tenant, self.project, decision_request_id,
            )
        if row is None:
            return None
        return {**self._plan_row(row), "original_input_digest": str(row["original_input_digest"])}

    async def read_issuance_plan(self, transaction_id: str) -> dict[str, Any] | None:
        """The stored plan of a decision's transaction, or None."""
        if not _HEX64.fullmatch(str(transaction_id)):
            return None
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT original_input_digest, transaction_id, plan
                FROM {self.schema}.{TABLE_ISSUANCE_PLANS}
                WHERE tenant = $1 AND project = $2 AND transaction_id = $3
                """,
                self.tenant, self.project, transaction_id,
            )
        return None if row is None else self._plan_row(row)

    @asynccontextmanager
    async def issuance_completion_section(self, transaction_id: str, *,
                                          claim_seconds: int = COMPLETION_CLAIM_SECONDS) -> AsyncIterator[Any]:
        """One completion of a transaction at a time, across processes: a short claim on its plan.

        The claim is taken, renewed and released by single committed
        statements: no connection is held while the completion makes its own
        database calls, so concurrent completions can never starve a shared
        pool. Another live claim refuses ``issuance_completion_busy`` at once;
        the caller reads the decision as it stands (pending until that
        completion ends). A claim whose holder died lapses after
        ``claim_seconds``. The section yields ``renew()``, which extends the
        claim and returns False once it was lost: the holder then decides
        nothing more. The owner token is per call, never process state.
        """
        if not _HEX64.fullmatch(str(transaction_id)):
            raise IssuanceStoreRefused("issuance_plan_unknown")
        owner = uuid.uuid4().hex
        seconds = max(1, int(claim_seconds))

        async def claim(*, renewal: bool) -> bool:
            condition = ("completing_owner = $4 AND completing_until > clock_timestamp()" if renewal else
                         "(completing_until IS NULL OR completing_until <= clock_timestamp() OR completing_owner = $4)")
            async with self._pool.acquire() as connection:
                return await connection.fetchval(
                    f"""
                    UPDATE {self.schema}.{TABLE_ISSUANCE_PLANS}
                       SET completing_owner = $4,
                           completing_until = clock_timestamp() + ($5 * interval '1 second')
                     WHERE tenant = $1 AND project = $2 AND transaction_id = $3 AND {condition}
                    RETURNING 1
                    """,
                    self.tenant, self.project, transaction_id, owner, seconds,
                ) is not None

        if not await claim(renewal=False):
            if await self.read_issuance_plan(transaction_id) is None:
                raise IssuanceStoreRefused("issuance_plan_unknown")
            raise IssuanceStoreRefused("issuance_completion_busy")

        async def renew() -> bool:
            return await claim(renewal=True)

        try:
            yield renew
        finally:
            async with self._pool.acquire() as connection:
                await connection.execute(
                    f"""
                    UPDATE {self.schema}.{TABLE_ISSUANCE_PLANS}
                       SET completing_owner = '', completing_until = NULL
                     WHERE tenant = $1 AND project = $2 AND transaction_id = $3 AND completing_owner = $4
                    """,
                    self.tenant, self.project, transaction_id, owner,
                )

    async def reserve_issued_credential(self, *, transaction_id: str, slot: str, token_sha256: str,
                                        record: Mapping[str, Any], ttl_seconds: int) -> str:
        """Reserve one minted credential under its decision's stored plan; idempotent on (transaction, slot).

        Every trusted field (Card id, subject, client, revision, cap and the
        reservation deadline) is copied from the stored plan inside the same
        statement; the caller supplies only the bearer digest, its non-secret
        record and its lifetime. Refused once the plan's ``reserved_until`` has
        passed (a retry never renews it), for an undeclared slot, and for a
        replay with another digest, record or lifetime.
        """
        if slot not in ISSUANCE_SLOTS:
            raise IssuanceStoreRefused("reservation_slot_undeclared")
        if not _HEX64.fullmatch(str(transaction_id)) or not _HEX64.fullmatch(str(token_sha256)):
            raise IssuanceStoreRefused("reservation_invalid")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= MAX_ISSUANCE_TTL_SECONDS:
            raise IssuanceStoreRefused("reservation_ttl_invalid")
        payload = dict(record)
        digest = reservation_digest(token_sha256, payload, ttl_seconds)
        reservation_id = f"ores_{uuid.uuid4().hex}"
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                plan_row = await connection.fetchrow(
                    f"""
                    SELECT plan, reserved_until > clock_timestamp() AS open
                    FROM {self.schema}.{TABLE_ISSUANCE_PLANS}
                    WHERE tenant = $1 AND project = $2 AND transaction_id = $3
                    FOR SHARE
                    """,
                    self.tenant, self.project, transaction_id,
                )
                if plan_row is None:
                    raise IssuanceStoreRefused("issuance_plan_unknown")
                plan = self._plan_row({"plan": plan_row["plan"], "transaction_id": transaction_id})["plan"]
                if slot not in plan["slots"]:
                    raise IssuanceStoreRefused("reservation_slot_undeclared")
                existing = await connection.fetchrow(
                    f"""
                    SELECT reservation_id, reservation_digest AS digest
                    FROM {self.schema}.{TABLE_ISSUANCE_RESERVATIONS}
                    WHERE tenant = $1 AND project = $2 AND transaction_id = $3 AND slot = $4
                    """,
                    self.tenant, self.project, transaction_id, slot,
                )
                if existing is not None:
                    if str(existing["digest"] or "") != digest:
                        raise IssuanceStoreRefused("reservation_replay_changed")
                    return str(existing["reservation_id"])
                if not plan_row["open"]:
                    raise IssuanceStoreRefused("reservation_window_closed")
                inserted = await connection.fetchval(
                    f"""
                    INSERT INTO {self.schema}.{TABLE_ISSUANCE_RESERVATIONS} (
                        reservation_id, tenant, project, transaction_id, slot, token_sha256, record,
                        registry_access_id, subject, client_id, card_revision, cap_expires_at,
                        ttl_seconds, original_input_digest, reservation_digest, reserved_until
                    )
                    SELECT $1, $2, $3, $4, $5, $6, ($7::text)::jsonb,
                           $8, $9, $10, $11, to_timestamp($12::bigint),
                           $13, plans.original_input_digest, $14, plans.reserved_until
                      FROM {self.schema}.{TABLE_ISSUANCE_PLANS} AS plans
                     WHERE plans.tenant = $2 AND plans.project = $3 AND plans.transaction_id = $4
                       AND plans.reserved_until > clock_timestamp()
                    ON CONFLICT DO NOTHING
                    RETURNING reservation_id
                    """,
                    reservation_id, self.tenant, self.project, transaction_id, slot, token_sha256,
                    _canonical(payload),
                    plan["access_id"], plan["grantor_subject"], plan["client_id"],
                    plan["candidate_revision"], plan["expires_at"], ttl_seconds, digest,
                )
        if inserted is None:
            # Lost the (transaction, slot) race, or a digest already reserved elsewhere.
            raise IssuanceStoreRefused("reservation_replay_changed")
        return str(inserted)

    async def bind_issued_credential(self, *, transaction_id: str, slot: str, effect_digest: str,
                                     access_id: str, card_revision: int) -> str:
        """STAGE: bind the open reservation of this slot to its effect; ``bound``, or a named refusal.

        Only a reservation still inside its window binds, and only for the
        Card id and revision the effect names; a replay with the same effect
        returns ``bound`` again.
        """
        if not _HEX64.fullmatch(str(effect_digest)):
            raise IssuanceStoreRefused("reservation_invalid")
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    f"""
                    SELECT state, pin, registry_access_id, card_revision,
                           reserved_until > clock_timestamp() AS open
                    FROM {self.schema}.{TABLE_ISSUANCE_RESERVATIONS}
                    WHERE tenant = $1 AND project = $2 AND transaction_id = $3 AND slot = $4
                    FOR UPDATE
                    """,
                    self.tenant, self.project, transaction_id, slot,
                )
                if row is None:
                    raise IssuanceStoreRefused("reservation_missing")
                if row["registry_access_id"] != access_id or int(row["card_revision"]) != card_revision:
                    raise IssuanceStoreRefused("reservation_binding_mismatch")
                if row["state"] == "bound" and row["pin"] == effect_digest:
                    return "bound"
                if row["state"] != "reserved" or row["pin"]:
                    raise IssuanceStoreRefused("reservation_binding_mismatch")
                if not row["open"]:
                    raise IssuanceStoreRefused("reservation_window_closed")
                await connection.execute(
                    f"""
                    UPDATE {self.schema}.{TABLE_ISSUANCE_RESERVATIONS}
                       SET state = 'bound', pin = $5, updated_at = clock_timestamp()
                     WHERE tenant = $1 AND project = $2 AND transaction_id = $3 AND slot = $4
                    """,
                    self.tenant, self.project, transaction_id, slot, effect_digest,
                )
        return "bound"

    async def activate_issued_credential(self, *, transaction_id: str, slot: str, effect_digest: str,
                                         card_live: bool) -> str:
        """COMMIT: make the bound reservation a usable credential; ``applied`` or ``superseded``.

        Runs under the Card's family lock. ``card_live`` is the authoritative
        Card fence the caller read under the Card's own section: False (a newer
        revision, an ended or expired Card) releases the reservation as
        ``superseded`` and creates nothing, even when the Card has no family.
        The pinned outcome is returned on every replay. Expiry is the
        reservation's own instant plus its lifetime, capped by the Card's
        absolute expiry: a late activation never extends a token.
        """
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                head = await connection.fetchrow(
                    f"""
                    SELECT registry_access_id FROM {self.schema}.{TABLE_ISSUANCE_RESERVATIONS}
                    WHERE tenant = $1 AND project = $2 AND transaction_id = $3 AND slot = $4
                    """,
                    self.tenant, self.project, transaction_id, slot,
                )
                if head is None:
                    raise IssuanceStoreRefused("reservation_missing")
                # The same order as rotation and revocation: the Card's families first.
                await self._lock_card_families(connection, str(head["registry_access_id"]))
                row = await connection.fetchrow(
                    f"""
                    SELECT * FROM {self.schema}.{TABLE_ISSUANCE_RESERVATIONS}
                    WHERE tenant = $1 AND project = $2 AND transaction_id = $3 AND slot = $4
                    FOR UPDATE
                    """,
                    self.tenant, self.project, transaction_id, slot,
                )
                if row["pin"] != effect_digest:
                    raise IssuanceStoreRefused("reservation_binding_mismatch")
                if row["state"] == "activated":
                    return "applied"
                if row["state"] == "released" and row["outcome"] == "superseded":
                    return "superseded"
                if row["state"] != "bound":
                    raise IssuanceStoreRefused("reservation_binding_mismatch")
                if not card_live:
                    await self._settle_reservation(connection, transaction_id, slot, "released", "superseded")
                    return "superseded"
                record = row["record"]
                record = json.loads(record) if isinstance(record, str) else dict(record)
                expiry = "LEAST(r.created_at + (r.ttl_seconds * interval '1 second'), r.cap_expires_at)"
                if slot == "refresh":
                    family_id = f"ofam_{uuid.uuid4().hex}"
                    generation_id = f"ogen_{uuid.uuid4().hex}"
                    await connection.execute(
                        f"""
                        INSERT INTO {self.schema}.{TABLE_FAMILIES} (
                            family_id, tenant, project, registry_access_id, card_kind, client_id, subject,
                            identity_scope, current_generation_id, state, expires_at, cap_expires_at,
                            card_revision
                        )
                        SELECT $1, r.tenant, r.project, r.registry_access_id, $5, r.client_id, r.subject,
                               $6, $7, 'active', {expiry}, r.cap_expires_at, r.card_revision
                          FROM {self.schema}.{TABLE_ISSUANCE_RESERVATIONS} AS r
                         WHERE r.tenant = $2 AND r.project = $3 AND r.transaction_id = $4 AND r.slot = 'refresh'
                        """,
                        family_id, self.tenant, self.project, transaction_id,
                        str(record.get("card_kind") or "").strip(),
                        str(record.get("identity_scope") or "").strip(), generation_id,
                    )
                    await connection.execute(
                        f"""
                        INSERT INTO {self.schema}.{TABLE_REFRESH_GENERATIONS} (
                            generation_id, family_id, token_sha256, record, state, expires_at
                        )
                        SELECT $1, $2, r.token_sha256, ($6::text)::jsonb, 'active', {expiry}
                          FROM {self.schema}.{TABLE_ISSUANCE_RESERVATIONS} AS r
                         WHERE r.tenant = $3 AND r.project = $4 AND r.transaction_id = $5 AND r.slot = 'refresh'
                        """,
                        generation_id, family_id, self.tenant, self.project, transaction_id, _canonical(record),
                    )
                else:
                    await connection.execute(
                        f"""
                        INSERT INTO {self.schema}.{TABLE_ACCESS_BINDINGS} (
                            token_sha256, tenant, project, registry_access_id, record, state, expires_at,
                            card_revision
                        )
                        SELECT r.token_sha256, r.tenant, r.project, r.registry_access_id, ($4::text)::jsonb,
                               'active', {expiry}, r.card_revision
                          FROM {self.schema}.{TABLE_ISSUANCE_RESERVATIONS} AS r
                         WHERE r.tenant = $1 AND r.project = $2 AND r.transaction_id = $3 AND r.slot = 'access'
                        """,
                        self.tenant, self.project, transaction_id, _canonical(record),
                    )
                await self._settle_reservation(connection, transaction_id, slot, "activated", "applied")
        return "applied"

    async def _settle_reservation(self, connection: Any, transaction_id: str, slot: str, state: str,
                                  outcome: str) -> None:
        await connection.execute(
            f"""
            UPDATE {self.schema}.{TABLE_ISSUANCE_RESERVATIONS}
               SET state = $5, outcome = $6, updated_at = clock_timestamp()
             WHERE tenant = $1 AND project = $2 AND transaction_id = $3 AND slot = $4
            """,
            self.tenant, self.project, transaction_id, slot, state, outcome,
        )

    async def release_issued_credential(self, *, transaction_id: str, slot: str, unbound_only: bool = False) -> str:
        """Release a reservation that will never be activated; ``released``, ``absent`` or a refusal.

        A recorded ABORT releases a reserved or bound reservation. The SDK's
        own release (``unbound_only``) releases only one STAGE never bound:
        once bound, only the decision settles it. An activated credential is
        never released here.
        """
        if slot not in ISSUANCE_SLOTS or not _HEX64.fullmatch(str(transaction_id)):
            raise IssuanceStoreRefused("reservation_invalid")
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                row = await connection.fetchrow(
                    f"""
                    SELECT state FROM {self.schema}.{TABLE_ISSUANCE_RESERVATIONS}
                    WHERE tenant = $1 AND project = $2 AND transaction_id = $3 AND slot = $4
                    FOR UPDATE
                    """,
                    self.tenant, self.project, transaction_id, slot,
                )
                if row is None:
                    return "absent"
                if row["state"] in ("released", "expired"):
                    return "released"
                if row["state"] == "activated":
                    raise IssuanceStoreRefused("reservation_activated")
                if row["state"] == "bound" and unbound_only:
                    raise IssuanceStoreRefused("reservation_bound")
                await self._settle_reservation(connection, transaction_id, slot, "released", "released")
        return "released"

    async def expire_issuance_reservations(self, *, limit: int = 100) -> int:
        """Mark reservations no STAGE bound before their deadline ``expired``; how many changed.

        A bound reservation is never expired here: its decision settles it
        (activated, superseded or released), however late.
        """
        async with self._pool.acquire() as connection:
            status = await connection.execute(
                f"""
                UPDATE {self.schema}.{TABLE_ISSUANCE_RESERVATIONS}
                   SET state = 'expired', outcome = 'expired', updated_at = clock_timestamp()
                 WHERE reservation_id IN (
                       SELECT reservation_id FROM {self.schema}.{TABLE_ISSUANCE_RESERVATIONS}
                        WHERE tenant = $1 AND project = $2 AND state = 'reserved'
                          AND reserved_until <= clock_timestamp()
                        ORDER BY reserved_until
                        LIMIT $3
                        FOR UPDATE SKIP LOCKED)
                """,
                self.tenant, self.project, max(1, min(int(limit), 1000)),
            )
        return int(str(status or "UPDATE 0").rsplit(" ", 1)[-1] or 0)

    async def issuance_reservations(self, transaction_id: str) -> dict[str, dict[str, str]]:
        """Each slot's reservation of one transaction: state, outcome, bearer digest and pin."""
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT slot, state, outcome, token_sha256, pin
                FROM {self.schema}.{TABLE_ISSUANCE_RESERVATIONS}
                WHERE tenant = $1 AND project = $2 AND transaction_id = $3
                """,
                self.tenant, self.project, transaction_id,
            )
        return {str(row["slot"]): {"state": str(row["state"]), "outcome": str(row["outcome"]),
                                   "token_sha256": str(row["token_sha256"]), "pin": str(row["pin"])}
                for row in rows}


__all__ = ["COMPLETION_CLAIM_SECONDS", "ISSUANCE_SLOTS", "IssuanceReservationStore", "IssuanceStoreRefused", "MAX_ISSUANCE_TTL_SECONDS",
           "reservation_digest"]
