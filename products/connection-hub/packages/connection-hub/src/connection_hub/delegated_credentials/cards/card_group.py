"""W578: the versioned Hub input for several Cards under ONE transaction decision.

Creating a person's project Control C, their My Card and claiming an
invitation's pending Card are one business change (CodeApp, 2026-10-06
22:59; Root 22:54: one-Card and multi-Card changes use the same prepare,
durable decision and finish protocol). The C1 shape (``hub_participant_input``)
stays the only shape for one Card; this module is the group shape beside it.

The kernel's participant projection is a closed 13-field shape, so a group
uses exactly those fields and carries its members in the candidate value
(``participant_candidates["connection-hub.card"]``), bound by
``candidate_digest`` the same way the one-Card candidate is:

- ``participant`` is ``connection-hub.card``: one aggregate receipt, never a
  per-Card pseudo-participant.
- ``binding_kind`` is ``connection-hub.card-group``; ``binding_ref`` is
  ``group:<sha256>`` over the sorted ``[subject_hash, access_id,
  original_revision]`` of every member. It names the group's targets at
  their base revisions; it is NOT unique per transaction (CodeApp 23:06): the
  same group replayed after an ABORT has the same ``binding_ref``. Separation
  and recovery are carried by the durable transaction id, its epoch and the
  immutable global intent digest, as for every participant. The aggregate's
  own revision fields are fixed: ``before_revision`` 0, ``candidate_revision``
  1, ``target_incarnation`` 1, ``action`` ``create`` (the kernel's create
  shape for a binding with no prior state of its own). What each member does
  is in the candidate, never in these aggregate fields.
- ``target_scope`` is the sha256 over the sorted unique member subject hashes.
- ``dependency_revisions`` are the group's unchanged Card, absence and catalog
  reads, with the C1 encoding. ``actor_subject``/``actor_kind`` are the one
  authenticated actor of the whole group. ``provisioning`` is ``{}``.

A member is ``{subject_hash, access_id, action, original_revision,
original_absent, candidate}``. An absent original (a newly minted id) is
explicit: ``original_absent`` true, ``original_revision`` 0, candidate
revision 1, action ``create``. It never means "an existing Card at revision 0"
and never recreates an old id: the store refuses an absent stage for any id
that has a history (enforced at staging).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from service_foundation.coordination.durable_decision_log import DecisionRefused
from service_foundation.coordination.durable_wire import WireRefused, canonical_json_bytes, sha256_hex

from .card_participant import PARTICIPANT, dependency_revisions, reads_from_dependencies
from .model import CardAuthority
from .store import subject_hash_for

GROUP_BINDING_KIND = "connection-hub.card-group"
GROUP_SCHEMA = "connection-hub.card-group.v1"
MAX_GROUP_MEMBERS = 8
MAX_GROUP_CANDIDATE_BYTES = 256 * 1024
_MEMBER_FIELDS = frozenset({"subject_hash", "access_id", "action", "original_revision", "original_absent",
                            "candidate"})
_ACTIONS = frozenset({"create", "update", "attach", "revoke"})


def _refuse(reason: str) -> DecisionRefused:
    return DecisionRefused(reason)


def group_member(*, original: CardAuthority | None, candidate: CardAuthority, action: str) -> dict[str, Any]:
    """One member entry; ``original`` None means a newly minted id with no history."""
    return {"subject_hash": subject_hash_for(candidate.grantor_subject), "access_id": candidate.access_id,
            "action": action, "original_revision": 0 if original is None else original.card_revision,
            "original_absent": original is None, "candidate": candidate.to_dict()}


def _member_key(member: Mapping[str, Any]) -> tuple[str, str]:
    return str(member.get("subject_hash")), str(member.get("access_id"))


def group_candidate_value(members: Sequence[Mapping[str, Any]],
                          effects: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """The canonical group candidate: members sorted by (subject_hash, access_id), effects in order."""
    return {"schema": GROUP_SCHEMA, "cards": sorted((dict(member) for member in members), key=_member_key),
            "effects": [dict(effect) for effect in effects]}


def group_binding_ref(value: Mapping[str, Any]) -> str:
    members = [[m["subject_hash"], m["access_id"], m["original_revision"]] for m in value["cards"]]
    return "group:" + sha256_hex(canonical_json_bytes({"members": members}))


def group_target_scope(value: Mapping[str, Any]) -> str:
    return sha256_hex(canonical_json_bytes({"scopes": sorted({m["subject_hash"] for m in value["cards"]})}))


def _validate_reads(reads: Any) -> list[dict[str, Any]]:
    """Every read is exactly {subject_hash (64 hex), access_id, revision (int >= 0)}; else a typed refusal."""
    if not isinstance(reads, (list, tuple)):
        raise _refuse("card_group_read_invalid")
    checked = []
    for read in reads:
        if (not isinstance(read, Mapping) or set(read) != {"subject_hash", "access_id", "revision"}
                or type(read["subject_hash"]) is not str or len(read["subject_hash"]) != 64
                or any(char not in "0123456789abcdef" for char in read["subject_hash"])
                or type(read["access_id"]) is not str or not read["access_id"]
                or type(read["revision"]) is not int or read["revision"] < 0):
            raise _refuse("card_group_read_invalid")
        checked.append(dict(read))
    return checked


def _validate_member(member: Any) -> None:
    if not isinstance(member, Mapping) or set(member) != _MEMBER_FIELDS:
        raise _refuse("card_group_member_invalid")
    try:
        candidate = CardAuthority.from_mapping(member["candidate"])
    except Exception as exc:  # noqa: BLE001 - any malformed candidate is the same named refusal
        raise _refuse("card_group_member_invalid") from exc
    if (candidate.to_dict() != member["candidate"] or candidate.access_id != member["access_id"]
            or subject_hash_for(candidate.grantor_subject) != member["subject_hash"]
            or member["action"] not in _ACTIONS or type(member["original_absent"]) is not bool
            or type(member["original_revision"]) is not int):
        raise _refuse("card_group_member_invalid")
    if member["original_absent"]:
        if member["original_revision"] != 0 or candidate.card_revision != 1 or member["action"] != "create":
            raise _refuse("card_group_absent_original_invalid")
    elif (member["original_revision"] < 1 or member["action"] == "create"
            or candidate.card_revision != member["original_revision"] + 1):
        raise _refuse("card_group_member_revision_invalid")


def validate_group_candidate(value: Any, *, reads: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """The canonical group candidate, or a named refusal; ``reads`` are the group's dependency reads."""
    if not isinstance(value, Mapping) or set(value) != {"schema", "cards", "effects"} \
            or value["schema"] != GROUP_SCHEMA:
        raise _refuse("card_group_invalid")
    members = value["cards"]
    if type(members) is not list or not members:
        raise _refuse("card_group_empty")
    if len(members) > MAX_GROUP_MEMBERS:
        raise _refuse("card_group_too_large")
    if type(value["effects"]) is not list:
        raise _refuse("card_group_invalid")
    try:
        size = len(canonical_json_bytes(dict(value)))
    except WireRefused as exc:
        raise _refuse("card_group_invalid") from exc
    if size > MAX_GROUP_CANDIDATE_BYTES:
        raise _refuse("card_group_too_large")
    for member in members:
        _validate_member(member)
    keys = [_member_key(member) for member in members]
    if len(set(keys)) != len(keys):
        raise _refuse("card_group_member_duplicate")
    if keys != sorted(keys):
        raise _refuse("card_group_not_canonical")
    reads = _validate_reads(reads)
    read_keys = [(read["subject_hash"], read["access_id"]) for read in reads]
    if len(set(read_keys)) != len(read_keys):
        # card:<x> and card-absent:<x> together contradict each other.
        raise _refuse("card_group_dependency_contradiction")
    if set(read_keys) & set(keys):
        raise _refuse("card_group_read_overlaps_target")
    present = {(read["subject_hash"], read["access_id"]) for read in reads if read["revision"] >= 1}
    _require_parent_reads(members, set(keys), present)
    return dict(value)


def _require_parent_reads(members: Sequence[Mapping[str, Any]], targets: set, reads: set) -> None:
    """Every Control a member binds is a member or a held read: the chain the decision checked stays put.

    For a person's project Control C bound under the project's Control P,
    P (``card:<sha256(creator)>:<P id>``) must be read PRESENT (revision >= 1;
    an absence read of the parent does not count); for the My Card bound under
    C, C is normally a member of the same group.
    """
    for member in members:
        binding = member["candidate"].get("control_card")
        if not isinstance(binding, Mapping):
            continue
        holder = str(binding.get("holder_subject") or "") or member["candidate"]["grantor_subject"]
        parent = (subject_hash_for(holder), str(binding.get("control_id") or ""))
        if parent not in targets and parent not in reads:
            raise _refuse("card_group_control_read_missing")


def hub_group_participant_input(*, members: Sequence[Mapping[str, Any]], actor_subject: str, actor_kind: str,
                                effects: Sequence[Mapping[str, Any]] = (),
                                reads: Sequence[Mapping[str, Any]] = (),
                                catalog_version_digest: str = "") -> dict[str, Any]:
    """The Hub's group ``participant_inputs[PARTICIPANT]``; refuses a group the Hub would never stage."""
    if (actor_kind not in ("caller", "grantor") or type(actor_subject) is not str or not actor_subject.strip()
            or actor_subject != actor_subject.strip()):
        raise _refuse("card_group_actor_invalid")
    value = validate_group_candidate(group_candidate_value(members, effects), reads=reads)
    return {
        "participant": PARTICIPANT, "binding_kind": GROUP_BINDING_KIND, "binding_ref": group_binding_ref(value),
        "target_scope": group_target_scope(value), "target_incarnation": 1, "action": "create",
        "before_revision": 0, "candidate_revision": 1,
        "candidate_digest": sha256_hex(canonical_json_bytes(value)),
        "dependency_revisions": dependency_revisions(reads, catalog_version_digest=catalog_version_digest),
        "actor_subject": actor_subject, "actor_kind": actor_kind, "provisioning": {},
    }


def verify_group_projection(projection: Mapping[str, Any], value: Any) -> dict[str, Any]:
    """The group candidate a verified projection names, or a named refusal (every field compared)."""
    if projection.get("binding_kind") != GROUP_BINDING_KIND:
        raise _refuse("card_group_binding_invalid")
    reads = reads_from_dependencies(projection.get("dependency_revisions"))
    checked = validate_group_candidate(value, reads=reads)
    if (projection.get("participant") != PARTICIPANT
            or projection.get("binding_ref") != group_binding_ref(checked)
            or projection.get("target_scope") != group_target_scope(checked)
            or projection.get("target_incarnation") != 1 or projection.get("action") != "create"
            or projection.get("before_revision") != 0 or projection.get("candidate_revision") != 1
            or projection.get("candidate_digest") != sha256_hex(canonical_json_bytes(checked))
            or projection.get("provisioning") != {}
            or projection.get("actor_kind") not in ("caller", "grantor")
            or type(projection.get("actor_subject")) is not str or not projection["actor_subject"].strip()
            or projection["actor_subject"] != projection["actor_subject"].strip()):
        raise _refuse("card_group_not_bound")
    return checked


__all__ = ["GROUP_BINDING_KIND", "GROUP_SCHEMA", "MAX_GROUP_CANDIDATE_BYTES", "MAX_GROUP_MEMBERS",
           "group_binding_ref", "group_candidate_value", "group_member", "group_target_scope",
           "hub_group_participant_input", "validate_group_candidate", "verify_group_projection"]
