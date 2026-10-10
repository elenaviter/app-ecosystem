# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W661 piece 1: the Card version store as piece 2's ``CardVersionStore`` port sees it.

Piece 2 (``participant_card_version.CardVersionOperation``) binds this as ``persistence.card_versions``.
Every call goes through ``DelegatedCardService``, which holds every member Card's mutation lock (sorted
order) for the whole operation. The answers are links only: ``{card: {subject_hash, access_id}, version,
checksum}``, where ``version`` is the Card's ``card_revision`` and ``checksum`` its content hash.

``refused`` is piece 2's refusal class: a store refusal is raised as ``refused(code)`` with the contract
code, so neither package imports the other's module.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from .model import CardAuthority
from .service import CardConflict
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
}


class ServiceCardVersionStore:
    def __init__(self, service: Any, *, refused: Callable[[str], Exception]) -> None:
        self._service = service
        self._refused = refused

    async def _guard(self, awaitable: Any) -> Any:
        """A store refusal, or a lock conflict, answers as ``refused(contract code)``; anything else raises."""
        try:
            return await awaitable
        except CardTransactionRefused as exc:
            raise self._refused(_CONTRACT_CODES.get(exc.reason, exc.reason)) from exc
        except CardConflict as exc:
            raise self._refused(_CONTRACT_CODES.get(exc.reason, "storage_unavailable")) from exc

    async def stage(self, txn: str, *, request_id: str, request_digest: str, catalog: Mapping[str, Any],
                    actor_subject: str, actor_kind: str, members: Sequence[Mapping[str, Any]],
                    effects: Sequence[Mapping[str, Any]], prepare: Any, at: datetime) -> Mapping[str, Any]:
        """``at`` is the request's own time (a request field), never this host's clock: a retry then names
        the same version file (EMain 16:41Z: "STAGE takes 'at' from the request")."""
        try:
            rows = [(m["subject_hash"], m["access_id"], m["base_version"], CardAuthority.from_mapping(m["value"]))
                    for m in members]
        except Exception as exc:  # noqa: BLE001 - a malformed value is the caller's edit, never a store fault
            raise self._refused("edit_invalid") from exc
        answer = await self._guard(self._service.stage_card_version(
            txn=txn, request_digest=request_digest, catalog=dict(catalog), members=rows, now=at,
            effects=[dict(effect) for effect in effects], request_id=request_id,
            actor={"subject": actor_subject, "kind": actor_kind}, prepare=prepare))
        return {"members": _links(answer)}

    async def publish(self, txn: str, *, apply: Any) -> Mapping[str, Any]:
        answer = await self._guard(self._service.publish_card_version(txn=txn, run_effect=_port_effect(apply)))
        return {"members": _links(answer)}

    async def rollback(self, txn: str, *, apply: Any, release: Any) -> Mapping[str, Any]:
        state = await self._guard(self._service.rollback_card_version(txn=txn, run_effect=_port_effect(apply),
                                                                      release=_port_effect(release)))
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
