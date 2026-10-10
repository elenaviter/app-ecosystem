"""Durable Card intents hold exact version links, never another authority body.

The links-only staging manifest is written at the SAME intent path before
any hidden version. It names the complete write set and freezes filenames
and time, so a crash can resume or abort without a directory scan.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
import re
from types import SimpleNamespace
from typing import Any, Mapping

from service_foundation.coordination.durable_decision_log import DecisionRefused

from ..durable_io import drain_writes_before_release, path_is_file, read_json_or_none, unlink_guarded, write_json_atomic
from .model import CardRecordError, card_revision_name
from .store import CardStorageError
from .transaction_store import member_transaction_id, read_receipt, tombstone_path
from .version_link import adopt_hidden_version, is_version_link, load_version, pointer_link, version_link, write_hidden_version

SINGLE_SCHEMA = "connection-hub.card-intent.v2"
GROUP_SCHEMA = "connection-hub.card-group-intent.v2"
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def is_link_record(raw: Any) -> bool:
    return isinstance(raw, Mapping) and raw.get("schema") in (SINGLE_SCHEMA, GROUP_SCHEMA)


def validate_link_record(raw: Any) -> None:
    if not is_link_record(raw):
        raise DecisionRefused("card_intent_invalid")
    common = {"schema", "transaction_id", "intent_digest", "effects", "actor_subject", "actor_kind", "reads",
              "authority", "scope", "catalog", "status", "record_digest", "at"}
    member_fields = {"subject_hash", "access_id", "action", "original", "candidate", "candidate_staging_tag", "owner"}
    expected = common | ({"members"} if raw.get("schema") == GROUP_SCHEMA else member_fields)
    optional = {"collection"} if raw.get("schema") == GROUP_SCHEMA else set()
    if (not is_link_record(raw) or not expected <= set(raw) or not (set(raw) - expected) <= optional
            or raw.get("status") not in ("staging", "ready")):
        raise DecisionRefused("card_intent_invalid")
    members = raw.get("members") if raw["schema"] == GROUP_SCHEMA else [raw]
    if not isinstance(members, list) or not 1 <= len(members) <= 8:
        raise DecisionRefused("card_intent_invalid")
    if raw["schema"] == GROUP_SCHEMA and any(not isinstance(member, Mapping) or set(member) != member_fields for member in members):
        raise DecisionRefused("card_intent_invalid")
    for field in ("transaction_id", "intent_digest", "record_digest"):
        if type(raw[field]) is not str or not _HEX64.fullmatch(raw[field]):
            raise DecisionRefused("card_intent_invalid")
    if (any(type(raw[field]) is not str for field in ("actor_subject", "actor_kind", "authority", "scope", "catalog", "at"))
            or any(not isinstance(raw[field], list) or any(not isinstance(value, Mapping) for value in raw[field])
                   for field in ("effects", "reads"))):
        raise DecisionRefused("card_intent_invalid")
    try:
        if datetime.fromisoformat(raw["at"]).tzinfo is None:
            raise ValueError()
    except ValueError as exc:
        raise DecisionRefused("card_intent_invalid") from exc
    keys = []
    for index, member in enumerate(members):
        owner = member_transaction_id(raw["transaction_id"], index) if raw["schema"] == GROUP_SCHEMA else raw["transaction_id"]
        if (type(member["subject_hash"]) is not str or not _HEX64.fullmatch(member["subject_hash"])
                or type(member["access_id"]) is not str or not member["access_id"]
                or type(member["action"]) is not str or not member["action"]
                or type(member["candidate_staging_tag"]) is not str or member["owner"] != owner
                or not is_version_link(member["candidate"])
                or (member["original"] is None and raw["schema"] != GROUP_SCHEMA)
                or (member["original"] is not None and not is_version_link(member["original"]))):
            raise DecisionRefused("card_intent_invalid")
        keys.append((member["subject_hash"], member["access_id"]))
    if len(set(keys)) != len(keys) or keys != sorted(keys):
        raise DecisionRefused("card_intent_invalid")


def validate_finished_binding(raw: Mapping[str, Any]) -> None:
    binding = raw.get("intent_binding")
    if (not isinstance(binding, Mapping) or set(binding) != {"authority", "scope", "record_digest"}
            or type(binding["authority"]) is not str or type(binding["scope"]) is not str
            or type(binding["record_digest"]) is not str or not _HEX64.fullmatch(binding["record_digest"])
            or raw.get("intent_finished") is not True):
        raise DecisionRefused("card_intent_invalid")


def intent_record_digest(intent: Any) -> str:
    from .card_participant import receipt_digest

    return receipt_digest(intent.to_dict())


def members_of(intent: Any) -> list[Any]:
    from .card_participant import CardGroupIntent

    return list(intent.members) if isinstance(intent, CardGroupIntent) else [intent]


@asynccontextmanager
async def sections(source: Any, keys: Any):
    # The service carries the hosting composition's mutation lock (including
    # its cluster layer). There is deliberately no private fallback lock.
    service = source._service or getattr(source._store, "_card_intent_service", None)
    if keys and service is None:
        raise DecisionRefused("card_intent_recorder_unavailable")
    if service is not None:
        async with service._card_version_sections(keys):
            yield
    else:
        async with drain_writes_before_release():
            yield


async def terminal(source: Any, transaction_id: str) -> dict[str, Any] | None:
    receipt = await read_receipt(source._store, transaction_id)
    if receipt is not None:
        return receipt if receipt["state"] in ("committed", "aborted") else None
    raw = await read_json_or_none(tombstone_path(source._store, transaction_id))
    if raw is None and await path_is_file(tombstone_path(source._store, transaction_id)):
        raise DecisionRefused("card_intent_invalid")
    if raw is not None:
        if (not isinstance(raw, dict) or raw.get("transaction_id") != transaction_id or raw.get("state") != "aborted"
                or not set(raw) <= {"transaction_id", "state", "intent_digest", "intent_binding", "intent_finished"}):
            raise DecisionRefused("card_intent_invalid")
        if "intent_binding" in raw or "intent_finished" in raw:
            validate_finished_binding(raw)
            if type(raw.get("intent_digest")) is not str or not _HEX64.fullmatch(raw["intent_digest"]):
                raise DecisionRefused("card_intent_invalid")
        return raw
    return None


async def read_binding(source: Any, transaction_id: str) -> Any:
    raw = await read_json_or_none(source._path(transaction_id))
    if raw is None and await path_is_file(source._path(transaction_id)):
        raise DecisionRefused("card_intent_invalid")
    if raw is not None and (not isinstance(raw, Mapping) or raw.get("transaction_id") != transaction_id):
        raise DecisionRefused("card_intent_invalid")
    if is_link_record(raw):
        validate_link_record(raw)
    if raw is None:
        receipt = await terminal(source, transaction_id)
        raw = (receipt or {}).get("intent_binding")
        if raw is None:
            return None
    if not isinstance(raw, Mapping) or type(raw.get("authority", "")) is not str or type(raw.get("scope", "")) is not str:
        raise DecisionRefused("card_intent_invalid")
    return SimpleNamespace(authority=raw.get("authority", ""), scope=raw.get("scope", ""))


async def record_links(source: Any, intent: Any) -> None:
    from .card_participant import CardGroupIntent, CardIntent, HubCardParticipant

    path = source._path(intent.transaction_id)
    digest = intent_record_digest(intent)
    changing = isinstance(intent, (CardIntent, CardGroupIntent))
    members = members_of(intent) if changing else []
    keys = [(member.subject_hash, member.candidate.access_id) for member in members]
    if len(set(keys)) != len(keys) or (isinstance(intent, CardGroupIntent) and keys != sorted(keys)):
        raise DecisionRefused("card_intent_invalid")
    # In production the coordinator's projection binds the target set BEFORE
    # writes. A malformed competing record cannot choose a disjoint lock set.
    bound = None
    if source._decisions is not None:
        bound = await source._decisions.read(intent.transaction_id)
        HubCardParticipant._check_bound(intent, bound)
    async with sections(source, keys):
        existing = await read_json_or_none(path)
        if existing is None and await path_is_file(path):
            raise DecisionRefused("card_intent_invalid")  # JSON null is not confirmed absence
        if existing is not None and not isinstance(existing, Mapping):
            raise DecisionRefused("card_intent_invalid")
        ended = await terminal(source, intent.transaction_id)
        if ended is not None:
            binding = ended.get("intent_binding")
            if binding is not None and binding.get("record_digest") == digest:
                return  # exact terminal replay, no write or re-hiding marker
            if is_link_record(existing) and existing.get("record_digest") == digest:
                return  # terminal duties/cleanup pending: replay never resurrects anything
            if existing is not None and not is_link_record(existing) and existing == intent.to_dict():
                return  # unfinished legacy cleanup may not yet have run
            raise DecisionRefused("card_intent_conflict")
        if bound is not None and bound.terminal:
            if is_link_record(existing) and existing.get("status") == "ready" and existing.get("record_digest") == digest:
                return
            raise DecisionRefused("card_transaction_late_stage")
        if existing is not None:
            if not is_link_record(existing):
                if existing != intent.to_dict():
                    raise DecisionRefused("card_intent_conflict")
                return
            validate_link_record(existing)
            if existing.get("record_digest") != digest:
                raise DecisionRefused("card_intent_conflict")
            if existing.get("status") == "ready":
                await load_links(source, existing)  # verify immutable files on replay
                return
            if existing.get("status") != "staging":
                raise DecisionRefused("card_intent_invalid")
            manifest = existing
        elif not changing:
            await write_json_atomic(path, intent.to_dict())
            return
        else:
            at = source._now()
            raw_members = []
            for index, member in enumerate(members):
                try:
                    current = await source._store.read_current_authority(
                        subject_hash=member.subject_hash, access_id=member.candidate.access_id)
                except CardStorageError as exc:
                    raise DecisionRefused(str(exc)) from exc
                if member.original is None:
                    if current is not None:
                        raise DecisionRefused("card_transaction_base_moved")
                    original = None
                else:
                    if current is None or current[1].to_dict() != member.original.to_dict():
                        raise DecisionRefused("card_transaction_revision_moved")
                    original = pointer_link(current[0])
                owner = member_transaction_id(intent.transaction_id, index) if isinstance(intent, CardGroupIntent) else intent.transaction_id
                link = member.candidate_link
                if link is None:
                    link = version_link(card_revision=member.candidate.card_revision,
                                        content_hash=member.candidate.content_hash(),
                                        revision_name=card_revision_name(card_revision=member.candidate.card_revision,
                                                                        content_hash=member.candidate.content_hash(),
                                                                        updated_at=at, txn=owner))
                # A caller-supplied plan link must name THIS in-memory candidate.
                if (not is_version_link(link) or link["card_revision"] != member.candidate.card_revision
                        or link["content_hash"] != member.candidate.content_hash()
                        or (member.candidate_link is not None and not member.candidate_staging_tag)):
                    raise DecisionRefused("card_intent_invalid")
                raw_members.append({"subject_hash": member.subject_hash, "access_id": member.candidate.access_id,
                                    "action": member.action, "original": original, "candidate": dict(link),
                                    "candidate_staging_tag": member.candidate_staging_tag, "owner": owner})
            manifest = intent.to_dict()
            manifest.update(schema=GROUP_SCHEMA if isinstance(intent, CardGroupIntent) else SINGLE_SCHEMA,
                            status="staging", record_digest=digest, at=at.isoformat())
            if isinstance(intent, CardGroupIntent):
                manifest["members"] = raw_members
            else:
                manifest.update(raw_members[0])
            await write_json_atomic(path, manifest)
        raw_members = manifest["members"] if manifest["schema"] == GROUP_SCHEMA else [manifest]
        if len(raw_members) != len(members):
            raise DecisionRefused("card_intent_conflict")
        at = datetime.fromisoformat(manifest["at"])
        try:
            for member, raw in zip(members, raw_members):
                if raw["candidate_staging_tag"]:
                    await adopt_hidden_version(source._store, subject_hash=raw["subject_hash"],
                                               access_id=raw["access_id"], link=raw["candidate"],
                                               from_tag=raw["candidate_staging_tag"], to_transaction_id=raw["owner"])
                else:
                    written = await write_hidden_version(source._store, subject_hash=raw["subject_hash"],
                                                         authority=member.candidate, at=at, tag=raw["owner"])
                    if written != raw["candidate"]:
                        raise DecisionRefused("card_intent_conflict")
        except CardRecordError as exc:
            raise DecisionRefused(str(exc)) from exc
        await write_json_atomic(path, {**manifest, "status": "ready"})


async def load_links(source: Any, raw: Any) -> Any:
    from .card_participant import CardGroupIntent, CardGroupMemberIntent, CardIntent

    validate_link_record(raw)
    if raw.get("status") != "ready":
        raise DecisionRefused("card_intent_incomplete")
    members = raw.get("members") if raw["schema"] == GROUP_SCHEMA else [raw]
    if not isinstance(members, list) or not members:
        raise DecisionRefused("card_intent_invalid")
    result = []
    try:
        for index, member in enumerate(members):
            original = await load_version(source._store, subject_hash=member["subject_hash"],
                                          access_id=member["access_id"], link=member["original"]) if member["original"] is not None else None
            owner = member_transaction_id(raw["transaction_id"], index) if raw["schema"] == GROUP_SCHEMA else raw["transaction_id"]
            if member["owner"] != owner:
                raise DecisionRefused("card_intent_invalid")
            receipt = await read_receipt(source._store, owner)
            committed = (receipt is not None and receipt["state"] == "committed"
                         and receipt["intent_digest"] == raw["intent_digest"]
                         and pointer_link_from_mapping(receipt["after"]) == member["candidate"])
            candidate = await load_version(source._store, subject_hash=member["subject_hash"],
                                           access_id=member["access_id"], link=member["candidate"],
                                           owners=(owner,), allow_unmarked=committed)
            result.append(CardGroupMemberIntent(member["subject_hash"], original, candidate, member["action"],
                                                candidate_link=dict(member["candidate"])))
        common = dict(transaction_id=raw["transaction_id"], intent_digest=raw["intent_digest"],
                      effects=tuple(raw.get("effects") or ()), actor_subject=raw.get("actor_subject", ""),
                      actor_kind=raw.get("actor_kind", ""), reads=tuple(raw.get("reads") or ()),
                      authority=raw.get("authority", ""), scope=raw.get("scope", ""), catalog=raw.get("catalog", ""))
        if raw["schema"] == GROUP_SCHEMA:
            intent = CardGroupIntent(members=tuple(result), collection=raw.get("collection"), **common)
        else:
            member = result[0]
            if member.original is None:
                raise DecisionRefused("card_intent_invalid")
            intent = CardIntent(subject_hash=member.subject_hash, original=member.original, candidate=member.candidate,
                                action=member.action, candidate_link=member.candidate_link, **common)
        if intent_record_digest(intent) != raw["record_digest"]:
            raise DecisionRefused("card_intent_conflict")
        return intent
    except (KeyError, TypeError, ValueError, CardRecordError) as exc:
        raise DecisionRefused("card_intent_invalid" if not isinstance(exc, CardRecordError) else str(exc)) from exc


def pointer_link_from_mapping(raw: Any) -> dict[str, Any]:
    from .model import CardCurrentPointer

    return pointer_link(CardCurrentPointer.from_mapping(raw))


async def retire_intent(source: Any, transaction_id: str, decision: str) -> dict[str, Any]:
    """Called ONLY after all service FINISH duties succeed. No scans or body reads.

    The existing receipt/tombstone records routing and completed service duties
    before deletion starts. A failed cleanup retains the intent; its next
    FINISH retries exact names without re-reading missing candidate bodies.
    """
    from .card_participant import intent_from_mapping
    from .transaction_store import receipt_path, revision_marker_path

    raw = await read_json_or_none(source._path(transaction_id))
    if raw is None and await path_is_file(source._path(transaction_id)):
        raise DecisionRefused("card_intent_invalid")
    ended = await terminal(source, transaction_id)
    if ended is None or ended["state"] != decision:
        raise DecisionRefused("card_intent_not_bound")
    if raw is None:
        if ended.get("intent_finished") is not True or not ended.get("intent_binding"):
            raise DecisionRefused("card_intent_unknown")
        return ended
    linked = is_link_record(raw)
    if linked:
        validate_link_record(raw)
        members = raw["members"] if raw["schema"] == GROUP_SCHEMA else [raw]
        digest = raw["record_digest"]
        keys = [(member["subject_hash"], member["access_id"]) for member in members]
    else:
        intent = intent_from_mapping(raw)
        digest = intent_record_digest(intent)
        from .card_participant import CardIntent, CardGroupIntent
        values = members_of(intent) if isinstance(intent, (CardIntent, CardGroupIntent)) else []
        keys = [(member.subject_hash, member.candidate.access_id) for member in values]
        members = []  # legacy staged bodies are already owned by their receipts
    binding = {"authority": raw.get("authority", ""), "scope": raw.get("scope", ""), "record_digest": digest}
    async with sections(source, keys):
        current = await read_json_or_none(source._path(transaction_id))
        if current is None:
            return await retire_intent(source, transaction_id, decision)
        if current != raw:
            raise DecisionRefused("card_intent_conflict")
        ended = await terminal(source, transaction_id)
        if ended is None or ended["state"] != decision:
            raise DecisionRefused("card_intent_not_bound")
        if ended.get("intent_binding") is not None and ended["intent_binding"] != binding:
            raise DecisionRefused("card_intent_conflict")
        finished = {**ended, "intent_binding": binding, "intent_finished": True}
        if "schema" not in ended:
            finished["intent_digest"] = raw["intent_digest"]
        target = receipt_path(source._store, transaction_id) if "schema" in ended else tombstone_path(source._store, transaction_id)
        if ended != finished:
            await write_json_atomic(target, finished)
        if decision == "aborted":
            for member in members:
                marker_path = revision_marker_path(source._store, subject_hash=member["subject_hash"],
                                                   access_id=member["access_id"], revision_name=member["candidate"]["revision_name"])
                file_path = source._store.revision_path(subject_hash=member["subject_hash"], access_id=member["access_id"],
                                                       revision_name=member["candidate"]["revision_name"])
                marker = await read_json_or_none(marker_path)
                if marker is None:
                    if await path_is_file(marker_path) or await path_is_file(file_path):
                        raise DecisionRefused("card_intent_cleanup_owner_unknown")
                    continue
                owners = {member["owner"]}
                if member["candidate_staging_tag"]:
                    owners.add(member["candidate_staging_tag"])
                if not isinstance(marker, Mapping) or set(marker) != {"transaction_id"} or marker["transaction_id"] not in owners:
                    raise DecisionRefused("card_intent_cleanup_owner_conflict")
                pointer = await read_json_or_none(source._store.current_path(subject_hash=member["subject_hash"], access_id=member["access_id"]))
                if pointer is not None and not isinstance(pointer, Mapping):
                    raise DecisionRefused("card_intent_invalid")
                if pointer is not None and (pointer.get("revision_name") == member["candidate"]["revision_name"]
                                            or (pointer.get("after") or {}).get("revision_name") == member["candidate"]["revision_name"]):
                    raise DecisionRefused("card_intent_cleanup_still_current")
                unlink_guarded(file_path)
                unlink_guarded(marker_path)
        unlink_guarded(source._path(transaction_id))
        return finished
