"""W606 pre-activation check: classify a RESTORED copy's Card credential-handle rows, read-only.

Root's decision (2026-10-09 00:08Z): before activation, count and classify the
rows on the restored copy; zero, or precise non-secret findings. The ONLY
predicate is W606's strict reader's own: ``PostgresCardCredentialHandleStore.read``
serves a row only when it is active and unexpired (``read_active``) and
``_validate_binding`` holds against the Card's current authority (access_id,
card_revision and expires_at all equal). No new rule is added here.

Classes, for each row the reader could serve (active and unexpired):

- ``readable``: the strict reader serves it;
- ``stale``: it refuses ``card_handle_revision_mismatch`` with the row BEHIND
  the Card (an older Card revision, no W606 re-bind): the pre-W606 case;
- ``card_absent`` / ``card_retired``: no current Card has the row's
  access_id, or that Card is revoked;
- ``malformed``: the row does not validate, the Card cannot be read, or the
  reader refuses for any other reason (a row AHEAD of the Card, an expiry
  that differs at the same revision); the reader's own code is reported.

Rows the reader never serves (not active, or expired) are counted as
``inert`` and not classified. The query selects no secret reference and no
fingerprint; findings carry only the table, access_id, the class, the code and
the two revision numbers. OAuth grant and refresh families carry no Card
revision binding and are never read through this predicate, so they are out of
its scope (reported as such, not as zero).
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from .credential_handles import CardCredentialHandleUnavailable, PostgresCardCredentialHandleStore
from .handle_metadata import HANDLE_STATE_ACTIVE, CardHandleMetadata
from .handle_schema import TABLE_CARD_HANDLE_METADATA
from .model import CARD_STATE_REVOKED, CardAuthority

REPORT_SCHEMA = "connection-hub.card-handle-classification.v1"
CLASSES = ("readable", "stale", "card_absent", "card_retired", "malformed")
BLOCKING = ("stale", "card_absent", "card_retired", "malformed")
OUT_OF_SCOPE = ("OAuth grant and refresh families: no Card revision binding; not read through the W606 predicate",)

# Identifiers, revision, state and expiry only: never the resident secret reference or its fingerprint.
_QUERY = """
    SELECT access_id, card_revision, state, EXTRACT(EPOCH FROM expires_at)::bigint AS expires_at
    FROM {schema}.{table}
    ORDER BY access_id
"""


def classify_row(row: Any, authority: CardAuthority | None | Exception, *, now: int) -> tuple[str, str]:
    """``(class, code)`` for one row against its Card's current authority (or the error reading it)."""
    try:
        metadata = CardHandleMetadata(access_id=str(row["access_id"] or ""), card_revision=int(row["card_revision"]),
                                      expires_at=int(row["expires_at"]), state=str(row["state"] or "")).validated()
    except Exception:  # noqa: BLE001 - any unparseable row is a finding, never a crash
        return "malformed", "card_handle_row_invalid"
    if metadata.state != HANDLE_STATE_ACTIVE or metadata.expires_at <= now:
        return "inert", ""
    if isinstance(authority, Exception):
        return "malformed", "card_unreadable"
    if authority is None:
        return "card_absent", "card_absent"
    if authority.state == CARD_STATE_REVOKED:
        return "card_retired", "card_revoked"
    try:
        PostgresCardCredentialHandleStore.validate_migration_binding(authority, metadata)  # the strict reader's
    except CardCredentialHandleUnavailable as exc:
        if exc.reason == "card_handle_revision_mismatch" and metadata.card_revision < authority.card_revision:
            return "stale", exc.reason
        return "malformed", exc.reason
    return "readable", ""


async def classify_restored_copy(connection: Any, *, schema: str, find_card: Callable[[str], Awaitable[Any]],
                                 now: int) -> dict[str, Any]:
    """Read every row once, in a READ ONLY transaction, and classify it; writes nothing anywhere.

    ``connection`` is an asyncpg connection to the RESTORED copy; ``find_card`` is the restored Hub Card
    store's ``find_current_authority``. ``activation_allowed`` is False when any blocking class is nonzero.
    """
    async with connection.transaction(readonly=True):
        rows = await connection.fetch(_QUERY.format(schema=schema, table=TABLE_CARD_HANDLE_METADATA))
    counts = {name: 0 for name in (*CLASSES, "inert")}
    findings = []
    for row in rows:
        authority: Any
        try:
            authority = await find_card(str(row["access_id"]))
        except Exception as exc:  # noqa: BLE001 - classified, by type only
            authority = exc
        kind, code = classify_row(row, authority, now=now)
        counts[kind] += 1
        if kind in BLOCKING:
            findings.append({"table": TABLE_CARD_HANDLE_METADATA, "access_id": str(row["access_id"] or ""),
                             "class": kind, "code": code, "row_card_revision": _int(row["card_revision"]),
                             "card_revision": getattr(authority, "card_revision", None)})
    return {"schema": REPORT_SCHEMA, "table": TABLE_CARD_HANDLE_METADATA, "checked_at": int(now), "rows": len(rows),
            "counts": counts, "findings": findings, "out_of_scope": list(OUT_OF_SCOPE),
            "activation_allowed": not any(counts[name] for name in BLOCKING)}


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = ["BLOCKING", "CLASSES", "REPORT_SCHEMA", "classify_restored_copy", "classify_row"]
