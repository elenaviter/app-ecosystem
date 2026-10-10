# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W661 scope B (c): the one-off purge of FINISHED v1 Card intents (the cutover-window step).

A v1 intent (``card-transactions/intents/<txn>.json``) stores full Card bodies: ``original`` and
``candidate`` for one Card, or the same per member of a group. Intent v2 stores links only and is
deleted when its transaction finishes (lane 3). The v1 files written before that are deleted ONCE,
in the approved cutover window: this is a one-off migration step, never a hot-path scan.

A file is deleted only when it is v1 (it holds a Card body) AND its decision is terminal AND every
participant of that decision has finished. Everything else is kept and counted: an in-flight v1
intent stays readable until its own transaction finishes; a v2 intent is lane 3's to delete. Nothing
in the result carries a Card body, only transaction ids and counts. ``apply=False`` (the default) is a
dry run that reports what it would delete; ``apply=True`` deletes through the guarded unlink (``unlink_guarded_async`` under R),
so the store's writer guard refuses it exactly as it refuses any other write.
"""

from __future__ import annotations

from typing import Any, Mapping

from connection_hub.delegated_credentials.durable_io import list_child_names, read_json_or_none
from .version_link import is_version_link, unlink_guarded_any

INTENTS_DIR = ("card-transactions", "intents")


def _holds_body(value: Any) -> bool:
    return isinstance(value, Mapping) and not is_version_link(value)


def is_v1_intent(raw: Any) -> bool:
    """Whether an intent file stores a Card body anywhere (one Card, or any group member)."""
    if not isinstance(raw, Mapping):
        return False
    if "members" in raw:
        return any(isinstance(member, Mapping)
                   and (_holds_body(member.get("original")) or _holds_body(member.get("candidate")))
                   for member in raw["members"] or ())
    return _holds_body(raw.get("original")) or _holds_body(raw.get("candidate"))


def _finished(record: Any) -> bool:
    if record is None or not record.terminal:
        return False
    participants = set(getattr(record.intent, "participants", ()) or ())
    return bool(participants) and participants <= set(record.finished)


async def purge_finished_v1_intents(store: Any, decisions: Any, *, apply: bool = False) -> dict[str, Any]:
    """Delete (``apply=True``) or list (dry run) the finished v1 intents; return ids and counts only."""
    directory = store.root.joinpath(*INTENTS_DIR)
    deleted, kept_in_flight, kept_v2, unreadable = [], [], 0, []
    for name in sorted(await list_child_names(directory)):
        if not name.endswith(".json"):
            continue
        transaction_id = name[:-len(".json")]
        path = directory / name
        raw = await read_json_or_none(path)
        if raw is None:
            continue
        if not isinstance(raw, Mapping):
            unreadable.append(transaction_id)
            continue
        if not is_v1_intent(raw):
            kept_v2 += 1
            continue
        if not _finished(await decisions.read(transaction_id)):
            kept_in_flight.append(transaction_id)
            continue
        if apply:
            await unlink_guarded_any(path)  # a deletion is a write: the writer guard (and R's owner) decide first
        deleted.append(transaction_id)
    return {"applied": bool(apply), "deleted": deleted, "kept_in_flight": kept_in_flight,
            "kept_v2": kept_v2, "unreadable": unreadable}


__all__ = ["INTENTS_DIR", "is_v1_intent", "purge_finished_v1_intents"]
