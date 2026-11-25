"""Hub-owned custody of the exact Card versions for one bounded write group."""

from __future__ import annotations

import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Mapping

from service_foundation.coordination.durable_decision_log import DecisionRefused
from service_foundation.coordination.durable_wire import (
    canonical_json_bytes, parse_canonical_json_bytes, participant_projection, sha256_hex,
)

from ..durable_io import DurableStorageError, list_child_names, path_is_file, read_json_or_none, write_json_atomic
from .card_group import GROUP_BINDING_KIND, group_candidate_value, verify_group_projection
from .model import CardAuthority, CardRecordError
from .store import CardStorageError
from .version_link import (
    discard_hidden_version, is_version_link, load_version, planned_link, pointer_link,
    staging_tag, unlink_guarded_any, write_hidden_version,
)

ROOT_SCHEMA = "connection-hub.card-candidate-set-root.v1"
REF_SCHEMA = "connection-hub.card-candidate-set-ref.v1"
INTENT_FIELD = "participant_candidate_sets"
MAX_CARDS = 6
MAX_CARD_BYTES = 64 * 1024
MAX_SEAL_SECONDS = 300
SEAL_CLEANUP_GRACE_SECONDS = 15 * 60
_ID = re.compile(r"[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_REF_FIELDS = {"schema", "set_id", "count", "root"}
_ROOT_FIELDS = {"schema", "set_id", "scope", "request", "deadline", "count", "leaves", "logical_candidate_digest"}
_MEMBER_FIELDS = ("subject_hash", "access_id", "action", "original_revision", "original_absent")
_LINK_FIELDS = ("card_revision", "revision_name", "content_hash")
_LEAF_FIELDS = {*_MEMBER_FIELDS, *_LINK_FIELDS, "original", "candidate_digest"}
_SCOPE_FIELDS = {"tenant", "app", "peer", "actor", "project", "incarnation", "target_scope"}
_REQUEST_FIELDS = {"id", "approval_basis"}


def _digest(value: Any) -> str:
    return sha256_hex(canonical_json_bytes(value))


def _hex(value: Any, pattern: re.Pattern[str] = _HASH) -> bool:
    return type(value) is str and bool(pattern.fullmatch(value))


def _text(value: Any) -> bool:
    return type(value) is str and 0 < len(value) <= 512 and value.isprintable()


def validate_candidate_ref(value: Any) -> dict[str, Any]:
    if (not isinstance(value, Mapping) or set(value) != _REF_FIELDS or value["schema"] != REF_SCHEMA
            or not _hex(value["set_id"], _ID) or not _hex(value["root"])
            or type(value["count"]) is not int or not 1 <= value["count"] <= MAX_CARDS):
        raise DecisionRefused("candidate_custody_reference_invalid")
    return dict(value)


def validate_compact_projection(projection: Mapping[str, Any]) -> None:
    """Check the group envelope before opening the exact candidates."""
    binding = projection.get("binding_ref")
    if (projection.get("binding_kind") != GROUP_BINDING_KIND
            or type(binding) is not str or not binding.startswith("group:") or not _hex(binding[6:])
            or not _hex(projection.get("target_scope")) or not _hex(projection.get("candidate_digest"))
            or any(type(projection.get(key)) is not int or projection[key] != value for key, value in (
                ("target_incarnation", 1), ("before_revision", 0), ("candidate_revision", 1)))
            or projection.get("action") != "create" or projection.get("provisioning") != {}
            or projection.get("actor_kind") not in ("caller", "grantor")):
        raise DecisionRefused("authority_candidate_invalid")


def candidate_set_id(scope: Mapping[str, Any], request: Mapping[str, Any], deadline: int) -> str:
    if (set(scope) != _SCOPE_FIELDS or not all(_text(scope[key]) for key in _SCOPE_FIELDS)
            or not _hex(scope["target_scope"]) or set(request) != _REQUEST_FIELDS
            or not _text(request["id"]) or not _hex(request["approval_basis"])
            or type(deadline) is not int or deadline < 1):
        raise DecisionRefused("candidate_custody_binding_invalid")
    return _digest({"schema": ROOT_SCHEMA, "scope": dict(scope), "request_id": request["id"],
                    "deadline": deadline})[:32]


def _root(manifest: Mapping[str, Any]) -> str:
    return _digest({key: manifest[key] for key in _ROOT_FIELDS})


def _ref(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {"schema": REF_SCHEMA, "set_id": manifest["set_id"], "count": manifest["count"],
            "root": manifest["root"]}


def _key(leaf: Mapping[str, Any]) -> tuple[str, str]:
    return leaf["subject_hash"], leaf["access_id"]


def _link(leaf: Mapping[str, Any]) -> dict[str, Any]:
    return {key: leaf[key] for key in _LINK_FIELDS}


def _member_digest(member: Mapping[str, Any], effects: Any) -> str:
    return _digest({**dict(member), "effects": effects})


def _owner(set_id: str) -> str:
    return staging_tag("card-candidate-set", set_id)


def candidate_set_lock_path(store: Any, set_id: str):
    if not _hex(set_id, _ID):
        raise DecisionRefused("candidate_custody_reference_invalid")
    return store.root / "card-candidate-set-locks" / f"{set_id[:2]}.lock"


def _header_path(store: Any, set_id: str):
    if not _hex(set_id, _ID):
        raise DecisionRefused("candidate_custody_reference_invalid")
    return store.root / "card-candidate-sets" / f"{set_id}.json"


def _validate_manifest(raw: Any, set_id: str) -> dict[str, Any]:
    required = _ROOT_FIELDS | {"root", "state", "at", "effects", "collection", "transaction"}
    if (not isinstance(raw, Mapping) or set(raw) != required or raw["schema"] != ROOT_SCHEMA
            or raw["set_id"] != set_id or not isinstance(raw["scope"], Mapping)
            or not isinstance(raw["request"], Mapping)
            or candidate_set_id(raw["scope"], raw["request"], raw["deadline"]) != set_id
            or not _hex(raw["logical_candidate_digest"]) or not _hex(raw["root"])
            or type(raw["at"]) is not int or not 0 < raw["at"] < raw["deadline"]
            or type(raw["count"]) is not int or not 1 <= raw["count"] <= MAX_CARDS
            or type(raw["leaves"]) is not list or len(raw["leaves"]) != raw["count"]
            or type(raw["effects"]) is not list or (raw["collection"] is not None and type(raw["collection"]) is not dict)
            or raw["state"] not in ("staging", "ready", "adopting", "adopted")):
        raise DecisionRefused("candidate_custody_mismatch")
    transaction = raw["transaction"]
    if raw["state"] in ("adopting", "adopted"):
        if (not isinstance(transaction, Mapping) or set(transaction) != {"id", "digest"}
                or not _text(transaction["id"]) or not _hex(transaction["digest"])):
            raise DecisionRefused("candidate_custody_mismatch")
    elif transaction is not None:
        raise DecisionRefused("candidate_custody_mismatch")
    for leaf in raw["leaves"]:
        if (not isinstance(leaf, Mapping) or set(leaf) != _LEAF_FIELDS
                or not _hex(leaf["subject_hash"]) or not _text(leaf["access_id"]) or not _text(leaf["action"])
                or type(leaf["original_absent"]) is not bool or type(leaf["original_revision"]) is not int
                or not is_version_link(_link(leaf)) or not _hex(leaf["candidate_digest"])):
            raise DecisionRefused("candidate_custody_mismatch")
        if leaf["original_absent"]:
            if leaf["original"] is not None or leaf["original_revision"] != 0:
                raise DecisionRefused("candidate_custody_mismatch")
        elif not is_version_link(leaf["original"]) or leaf["original"]["card_revision"] != leaf["original_revision"]:
            raise DecisionRefused("candidate_custody_mismatch")
    keys = [_key(leaf) for leaf in raw["leaves"]]
    if keys != sorted(set(keys)) or _root(raw) != raw["root"]:
        raise DecisionRefused("candidate_custody_mismatch")
    return dict(raw)


class CandidateCustody:
    """The set section is always outermost, including for adoption and cleanup."""

    def __init__(self, *, store: Any, service: Any, clock=time.time) -> None:
        self.store, self.service, self.clock = store, service, clock

    async def _read(self, set_id: str) -> dict[str, Any] | None:
        path = _header_path(self.store, set_id)
        try:
            raw = await read_json_or_none(path)
            if raw is None and not await path_is_file(path):
                return None
            return _validate_manifest(raw, set_id)
        except (OSError, DurableStorageError, CardStorageError) as exc:
            raise DecisionRefused("candidate_custody_unavailable") from exc
        except (TypeError, ValueError) as exc:
            raise DecisionRefused("candidate_custody_mismatch") from exc

    def _now(self) -> int:
        return int(self.clock())

    def _check_deadline(self, deadline: int) -> int:
        now = self._now()
        if not now < deadline <= now + MAX_SEAL_SECONDS:
            raise DecisionRefused("candidate_custody_expired")
        return now

    async def seal(self, *, scope: Mapping[str, Any], request: Mapping[str, Any], deadline: int,
                   projection: Mapping[str, Any], candidate: Mapping[str, Any]) -> dict[str, Any]:
        # Capture values before the first await. The caller supplies a trusted, admitted plan.
        scope, request, projection, candidate = parse_canonical_json_bytes(
            canonical_json_bytes([dict(scope), dict(request), dict(projection), dict(candidate)])
        )
        set_id = candidate_set_id(scope, request, deadline)
        checked = verify_group_projection(projection, candidate)
        members = checked["cards"]
        if (len(members) > MAX_CARDS or any(len(canonical_json_bytes(row["candidate"])) > MAX_CARD_BYTES
                                             for row in members)):
            raise DecisionRefused("candidate_custody_too_large")
        if (projection["actor_subject"] != scope["actor"] or projection["target_scope"] != scope["target_scope"]):
            raise DecisionRefused("candidate_custody_binding_invalid")
        path = _header_path(self.store, set_id)
        async with self.service.candidate_set_section(set_id):
            now = self._check_deadline(deadline)
            async with self.service._card_version_sections([_key(member) for member in members]):
                existing = await self._read(set_id)
                if existing is not None:
                    if (existing["scope"] != scope or existing["request"] != request
                            or existing["logical_candidate_digest"] != projection["candidate_digest"]
                            or existing["effects"] != checked["effects"]
                            or existing["collection"] != checked.get("collection")):
                        raise DecisionRefused("candidate_custody_conflict")
                    if existing["state"] != "staging":
                        self._check_deadline(deadline)
                        return _ref(existing)
                    manifest = existing
                else:
                    manifest = await self._plan_manifest(set_id, scope, request, deadline, now, checked, projection)
                    self._check_deadline(deadline)
                    await write_json_atomic(path, manifest)
                at = datetime.fromtimestamp(manifest["at"], timezone.utc)
                try:
                    for member, leaf in zip(members, manifest["leaves"], strict=True):
                        authority = CardAuthority.from_mapping(member["candidate"])
                        if (_member_digest(member, checked["effects"]) != leaf["candidate_digest"]
                                or planned_link(authority=authority, at=at, tag=_owner(set_id)) != _link(leaf)):
                            raise DecisionRefused("candidate_custody_mismatch")
                        self._check_deadline(deadline)
                        link = await write_hidden_version(self.store, subject_hash=leaf["subject_hash"],
                                                          authority=authority, at=at, tag=_owner(set_id))
                        if link != _link(leaf):
                            raise DecisionRefused("candidate_custody_mismatch")
                        await load_version(self.store, subject_hash=leaf["subject_hash"], access_id=leaf["access_id"],
                                           link=link, marker=_owner(set_id))
                except CardRecordError as exc:
                    raise DecisionRefused("candidate_custody_mismatch") from exc
                self._check_deadline(deadline)
                await write_json_atomic(path, {**manifest, "state": "ready"})
                self._check_deadline(deadline)
                return _ref(manifest)

    async def _plan_manifest(self, set_id, scope, request, deadline, now, checked, projection):
        leaves = []
        at = datetime.fromtimestamp(now, timezone.utc)
        for member in checked["cards"]:
            present = await self.store.read_current_authority(
                subject_hash=member["subject_hash"], access_id=member["access_id"])
            if (member["original_absent"] != (present is None)
                    or (present is not None and present[1].card_revision != member["original_revision"])):
                raise DecisionRefused("candidate_custody_base_moved")
            authority = CardAuthority.from_mapping(member["candidate"])
            leaves.append({**{key: member[key] for key in _MEMBER_FIELDS},
                           **planned_link(authority=authority, at=at, tag=_owner(set_id)),
                           "original": pointer_link(present[0]) if present else None,
                           "candidate_digest": _member_digest(member, checked["effects"])})
        manifest = {"schema": ROOT_SCHEMA, "set_id": set_id, "scope": scope, "request": request,
                    "deadline": deadline, "count": len(leaves), "leaves": leaves,
                    "logical_candidate_digest": projection["candidate_digest"], "state": "staging", "at": now,
                    "effects": checked["effects"], "collection": checked.get("collection"), "transaction": None}
        return {**manifest, "root": _root(manifest)}

    @asynccontextmanager
    async def adopt(self, *, reference: Mapping[str, Any], verified: Any, peer: str, project: str):
        """Bind to this verified transaction before letting the intent recorder move any marker."""
        from .transaction_store import member_transaction_id

        ref = validate_candidate_ref(reference)
        set_id = ref["set_id"]
        transaction = {"id": verified.intent.transaction_id, "digest": verified.intent.digest}
        async with self.service.candidate_set_section(set_id):
            manifest = await self._read(set_id)
            if manifest is None or manifest["state"] == "staging":
                raise DecisionRefused("candidate_custody_missing")
            scope = manifest["scope"]
            projection = verified.projection
            project_input = participant_projection(verified.intent, "pb")
            if (_ref(manifest) != ref or manifest["request"]["id"] != verified.intent.request_id
                    or verified.intent.expires_at > manifest["deadline"] or scope["peer"] != peer
                    or scope["project"] != project or scope["actor"] != projection["actor_subject"]
                    or scope["incarnation"] != project_input["target_incarnation"]
                    or scope["target_scope"] != projection["target_scope"]
                    or manifest["logical_candidate_digest"] != projection["candidate_digest"]
                    or manifest["transaction"] not in (None, transaction)):
                raise DecisionRefused("candidate_custody_mismatch")
            if verified.decision != "undecided" or self._now() >= verified.intent.expires_at:
                raise DecisionRefused("candidate_custody_expired")
            leaves = manifest["leaves"]
            members = []
            async with self.service._card_version_sections([_key(leaf) for leaf in leaves]):
                for index, leaf in enumerate(leaves):
                    owners = {_owner(set_id)}
                    if manifest["transaction"] is not None:
                        owners.add(member_transaction_id(transaction["id"], index))
                    try:
                        authority = await load_version(self.store, subject_hash=leaf["subject_hash"],
                                                       access_id=leaf["access_id"], link=_link(leaf), owners=owners)
                        current = await self.store.read_current_authority(subject_hash=leaf["subject_hash"],
                                                                         access_id=leaf["access_id"])
                        if (pointer_link(current[0]) if current else None) != leaf["original"]:
                            raise DecisionRefused("candidate_custody_base_moved")
                    except CardRecordError as exc:
                        code = "candidate_custody_missing" if str(exc) == "version_link_missing" else "candidate_custody_mismatch"
                        raise DecisionRefused(code) from exc
                    except (OSError, DurableStorageError, CardStorageError) as exc:
                        raise DecisionRefused("candidate_custody_unavailable") from exc
                    member = {**{key: leaf[key] for key in _MEMBER_FIELDS}, "candidate": authority.to_dict()}
                    if _member_digest(member, manifest["effects"]) != leaf["candidate_digest"]:
                        raise DecisionRefused("candidate_custody_mismatch")
                    members.append(member)
                candidate = group_candidate_value(members, manifest["effects"], collection=manifest["collection"])
                try:
                    verify_group_projection(projection, candidate)
                except DecisionRefused as exc:
                    raise DecisionRefused("candidate_custody_mismatch") from exc
            path = _header_path(self.store, set_id)
            if manifest["transaction"] is None:
                if self._now() >= verified.intent.expires_at:
                    raise DecisionRefused("candidate_custody_expired")
                manifest = {**manifest, "state": "adopting", "transaction": transaction}
                await write_json_atomic(path, manifest)
            # The recorder reacquires the member sections in the same order.
            # We keep the set lock until its immutable adoption record is ready.
            yield candidate, [_link(leaf) for leaf in leaves], _owner(set_id)
            if manifest["state"] != "adopted":
                await write_json_atomic(path, {**manifest, "state": "adopted"})

    async def discard_unadopted(self, set_id: str) -> bool:
        async with self.service.candidate_set_section(set_id):
            manifest = await self._read(set_id)
            if (manifest is None or manifest["transaction"] is not None
                    or self._now() < manifest["deadline"] + SEAL_CLEANUP_GRACE_SECONDS):
                return False
            await self._discard_staging(manifest)
            await unlink_guarded_any(_header_path(self.store, set_id))
            return True

    async def _discard_staging(self, manifest: Mapping[str, Any]) -> None:
        async with self.service._card_version_sections([_key(leaf) for leaf in manifest["leaves"]]):
            for leaf in manifest["leaves"]:
                await discard_hidden_version(self.store, subject_hash=leaf["subject_hash"],
                                             access_id=leaf["access_id"], link=_link(leaf),
                                             tag=_owner(manifest["set_id"]))

    async def retire_after_finish(self, reference: Mapping[str, Any], record: Any) -> None:
        """Retire an adopted manifest only after the existing FINISH produced its receipt."""
        ref = validate_candidate_ref(reference)
        set_id = ref["set_id"]
        async with self.service.candidate_set_section(set_id):
            manifest = await self._read(set_id)
            if manifest is None or manifest["transaction"] is None:
                return  # the deadline sweep owns never-adopted staging
            expected = {"id": record.intent.transaction_id, "digest": record.intent.digest}
            if manifest["transaction"] != expected or _ref(manifest) != ref or not record.terminal:
                raise DecisionRefused("candidate_custody_mismatch")
            if record.state == "aborted":
                await self._discard_staging(manifest)
            await unlink_guarded_any(_header_path(self.store, set_id))

    async def sweep_unadopted(self, *, limit: int = 8) -> dict[str, int]:
        """Read a bounded page. A stale cursor cannot authorize disposal."""
        directory = self.store.root / "card-candidate-sets"
        cursor = directory / "sweep.json"
        names = sorted(name[:-5] for name in await list_child_names(directory)
                       if name.endswith(".json") and _hex(name[:-5], _ID))
        if not names:
            return {"deleted": 0, "skipped": 0}
        try:
            saved = await read_json_or_none(cursor)
        except ValueError:
            saved = None
        after = saved.get("after", "") if isinstance(saved, Mapping) else ""
        after = after if _hex(after, _ID) else ""
        limit = max(1, min(limit, 50))
        size = 4 * limit + 64
        page = [name for name in names if name > after][:size]
        report = {"deleted": 0, "skipped": 0}
        next_after = ""
        for set_id in page:
            next_after = set_id
            try:
                report["deleted"] += int(await self.discard_unadopted(set_id))
            except (DecisionRefused, CardRecordError, CardStorageError, DurableStorageError, OSError, TimeoutError):
                report["skipped"] += 1
            if report["deleted"] >= limit:
                break
        exhausted = not page or (next_after == page[-1] and len(page) < size)
        await write_json_atomic(cursor, {"after": "" if exhausted else next_after})
        return report
