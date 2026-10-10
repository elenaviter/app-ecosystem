# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W661 piece 1: the Card version store as piece 2's ``CardVersionStore`` port sees it.

Piece 2 (``participant_card_version.CardVersionOperation``) binds this as ``persistence.card_versions``.
Every call goes through ``DelegatedCardService``, which holds every member Card's mutation lock (sorted
order) for the whole operation. The answers are links only: ``{card: {subject_hash, access_id}, version,
checksum}``, where ``version`` is the Card's ``card_revision`` and ``checksum`` its content hash.

``refused`` is piece 2's refusal class: a store refusal is raised as ``refused(code)`` with the contract
code, so neither package imports the other's module. ``scope`` and ``caller`` (the authenticated request
scope and service id) are recorded at STAGE and required on every replay, PUBLISH and ROLLBACK
(``txn_scope_mismatch``; Infra's W691 finding 2).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from .model import CardAuthority
from .service import CardConflict
from .store import CardStorageError
from .transaction_store import CardTransactionRefused

# Store-internal codes and the contract code each one answers as (contract v6.2, sections 3-5).
_CONTRACT_CODES = {
    "card_version_candidate_invalid": "edit_invalid",
    "card_version_base_invalid": "edit_invalid",
    "card_version_members_invalid": "edit_invalid",
    "card_version_request_invalid": "edit_invalid",
    "card_version_txn_invalid": "edit_invalid",
    "card_mutation_lock_timeout": "storage_unavailable",
    "card_version_members_moved": "storage_unavailable",
    # A ROLLBACK link that does not match the exact version its file holds: outcome unknown, PB keeps its rows.
    "card_version_link_mismatch": "storage_unavailable",
}


# Store-state refusals raised as CardStorageError (shared with the Hub-local writers) and their contract code.
_STORE_REFUSALS = {
    "card_version_effects_pending": "effects_pending",  # a predecessor's effects are not recorded yet (D3)
    "card_version_unresolved": "storage_unavailable",  # a partly published group (phase 2): fail closed
}


class ServiceCardVersionStore:
    """``catalog_store`` is the Hub's catalog store (``read_active()``); STAGE refuses without it (D1)."""

    def __init__(self, service: Any, *, refused: Callable[[str], Exception], catalog_store: Any = None) -> None:
        self._service = service
        self._refused = refused
        self._catalog_store = catalog_store

    async def _active_catalog(self) -> dict[str, str] | None:
        """D1: the ACTIVE catalog's link, read directly (under the Card locks, by STAGE)."""
        document = await self._catalog_store.read_active()
        return None if document is None else {"version": document.version, "content_hash": document.content_hash}

    async def _guard(self, awaitable: Any) -> Any:
        """A store refusal, or a lock conflict, answers as ``refused(contract code)``; anything else raises."""
        try:
            return await awaitable
        except CardTransactionRefused as exc:
            raise self._refused(_CONTRACT_CODES.get(exc.reason, exc.reason)) from exc
        except CardConflict as exc:
            raise self._refused(_CONTRACT_CODES.get(exc.reason, "storage_unavailable")) from exc
        except CardStorageError as exc:
            # Spark App M1: the D3 refusals of finalize_current_version reach PB with their contract code.
            if exc.reason in _STORE_REFUSALS:
                raise self._refused(_STORE_REFUSALS[exc.reason]) from exc
            raise

    async def stage(self, txn: str, *, request_id: str, request_digest: str, catalog: Mapping[str, Any],
                    actor_subject: str, actor_kind: str, members: Sequence[Mapping[str, Any]],
                    effects: Sequence[Mapping[str, Any]], prepare: Any, at: datetime, scope: str,
                    caller: str, reads: Sequence[Mapping[str, Any]] = ()) -> Mapping[str, Any]:
        """``at`` is the request's own time (a request field), never this host's clock: a retry then names
        the same version file (EMain 16:41Z: "STAGE takes 'at' from the request")."""
        try:
            rows = [(m["subject_hash"], m["access_id"], m["base_version"], CardAuthority.from_mapping(m["value"]))
                    for m in members]
            # Read members: {card: {subject_hash, access_id}, version}; locked and version-checked, never written.
            read_rows = [(r["card"]["subject_hash"], r["card"]["access_id"], r["version"]) for r in reads]
        except Exception as exc:  # noqa: BLE001 - a malformed value is the caller's edit, never a store fault
            raise self._refused("edit_invalid") from exc
        if self._catalog_store is None:
            raise self._refused("storage_unavailable")  # D1 needs the catalog store: fail closed
        answer = await self._guard(self._service.stage_card_version(
            txn=txn, request_digest=request_digest, catalog=dict(catalog), members=rows, now=at,
            effects=[dict(effect) for effect in effects], request_id=request_id,
            actor={"subject": actor_subject, "kind": actor_kind}, prepare=prepare,
            binding={"scope": scope, "caller": caller}, active_catalog=self._active_catalog, reads=read_rows))
        return {"members": _links(answer)}

    async def publish(self, txn: str, *, scope: str, caller: str, apply: Any) -> Mapping[str, Any]:
        answer = await self._guard(self._service.publish_card_version(
            txn=txn, run_effect=_port_effect(apply), binding={"scope": scope, "caller": caller}))
        return {"members": _links(answer)}

    async def rollback(self, txn: str, *, scope: str, caller: str, links: Sequence[Mapping[str, Any]] = (),
                       at: datetime | None = None, apply: Any, release: Any) -> Mapping[str, Any]:
        """``links`` are STAGE's answer and ``at`` the request's time: with the marker gone after PUBLISH (D2),
        they name the exact version files to read."""
        try:
            flat = [{"subject_hash": link["card"]["subject_hash"], "access_id": link["card"]["access_id"],
                     "version": link["version"], "checksum": link["checksum"]} for link in links]
        except (KeyError, TypeError) as exc:
            raise self._refused("edit_invalid") from exc
        state = await self._guard(self._service.rollback_card_version(
            txn=txn, run_effect=_port_effect(apply), release=_port_effect(release),
            binding={"scope": scope, "caller": caller}, links=flat, at=at))
        return {"state": state}

    async def read_current(self, subject_hash: str, access_id: str) -> Mapping[str, Any] | None:
        current = await self._service._store.read_current(subject_hash=subject_hash, access_id=access_id)
        if current is None:
            return None
        return {"version": current.card_revision, "checksum": current.content_hash}


def _links(answer: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [{"card": {"subject_hash": m["subject_hash"], "access_id": m["access_id"]}, "version": m["version"],
             "checksum": m["checksum"]} for m in answer]


def _port_view(marker: Mapping[str, Any]) -> dict[str, Any]:
    """The marker as the port documents it: txn plus members {card, base_version, version, checksum}."""
    return {"txn": marker["txn"], "members": [
        {"card": {"subject_hash": m["subject_hash"], "access_id": m["access_id"]}, "base_version": m["base_version"],
         "version": m["version"], "checksum": m["content_hash"]} for m in marker["members"]]}


def _port_effect(callback: Any) -> Any:
    if callback is None:
        return None

    async def run(effect: Mapping[str, Any], marker: Mapping[str, Any]) -> Any:
        return await callback(effect, _port_view(marker))
    return run


__all__ = ["ServiceCardVersionStore"]
