"""W578: the versioned Hub input for a transaction that changes NO Card but holds Cards and the catalog.

An ordinary Problem Board change with no Card change (CodeApp 23:19,
confirmed 23:27) still needs its actor and target Cards, absences and the
active catalog held unchanged until its one decision. C1 needs a revision
bump and the card group refuses an empty member list, so this is a third
binding on the SAME participant ``connection-hub.card`` and the same
prepare, Decision and finish:

- ``binding_kind`` ``connection-hub.card-read-set``; ``binding_ref``
  ``reads:<sha256(canonical candidate_value)>``; ``target_scope`` the sha256
  over the sorted unique read scopes; ``target_incarnation`` 1; ``action``
  ``read``; ``before_revision`` 1 and ``candidate_revision`` 1 (the kernel
  allows a non-create action at before >= 1; the Hub refuses any other pair);
  ``candidate_digest`` sha256 of the candidate value; ``dependency_revisions``
  EXACTLY the candidate's reads and catalog in the C1 encoding; one actor;
  ``provisioning`` ``{}``.
- The candidate value is ``{"schema": "connection-hub.card-read-set.v1",
  "reads": [...], "catalog": "<64 hex or empty>"}``: reads sorted and unique
  by (subject_hash, access_id), at least one read or a catalog, at most
  ``MAX_READ_SET_READS`` reads and ``MAX_READ_SET_BYTES`` canonical bytes; a read is ``{subject_hash, access_id, revision}``, revision >= 1 a
  present Card and 0 an absence. No effects and no Card writes.

An optional empty catalog in the generic wire is not permission for a caller
to drop a catalog dependency its own check needs (CodeApp 23:27).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from service_foundation.coordination.durable_decision_log import DecisionRefused
from service_foundation.coordination.durable_wire import WireRefused, canonical_json_bytes, sha256_hex

from .census_read import MAX_ANSWER_BYTES as MAX_CENSUS_ANSWER_BYTES, MAX_PERSONS
from .card_participant import (
    PARTICIPANT, catalog_reservation_from_dependencies, dependency_revisions, reads_from_dependencies,
)

READ_SET_BINDING_KIND = "connection-hub.card-read-set"
READ_SET_SCHEMA = "connection-hub.card-read-set.v1"
# The read set holds every Card a transaction depends on. A project-wide step (the
# zero cutover) reads the unique union of every person's My Card, Control Card and
# Control chain Cards (CodeApp: card_business_hub_census read_reservations); a
# shared chain (the project Control and its ancestors) counts once. So the count is
# 2 per person plus the DISTINCT chain Cards, sized here for MAX_PERSONS people with
# up to MAX_READ_SET_CHAIN_READS distinct chain Cards. A larger set, or one past the
# census answer's own byte bound, is refused by name (card_read_set_too_large): no
# proof is dropped and no member count is imposed. The 64 this replaced was sized
# for W578's ordinary actor/target/witness read and refused a 33-member zero
# cutover (66 reads).
MAX_READ_SET_CHAIN_READS = 24
MAX_READ_SET_READS = 2 * MAX_PERSONS + MAX_READ_SET_CHAIN_READS
# The candidate's canonical bytes stay within the census answer's own bound.
MAX_READ_SET_BYTES = MAX_CENSUS_ANSWER_BYTES
_HEX = frozenset("0123456789abcdef")


def _refuse(reason: str) -> DecisionRefused:
    return DecisionRefused(reason)


def _exact_int(value: Any, expected: int = 1) -> bool:
    return type(value) is int and value == expected


def _is_hex64(value: Any) -> bool:
    return type(value) is str and len(value) == 64 and set(value) <= _HEX


def read_set_candidate_value(reads: Sequence[Mapping[str, Any]], catalog: str = "") -> dict[str, Any]:
    return {"schema": READ_SET_SCHEMA, "catalog": catalog,
            "reads": sorted((dict(read) for read in reads),
                            key=lambda read: (str(read.get("subject_hash")), str(read.get("access_id"))))}


def validate_read_set_candidate(value: Any) -> dict[str, Any]:
    """The canonical read-set candidate, or a named refusal."""
    if (not isinstance(value, Mapping) or set(value) != {"schema", "reads", "catalog"}
            or value["schema"] != READ_SET_SCHEMA or type(value["reads"]) is not list
            or type(value["catalog"]) is not str):
        raise _refuse("card_read_set_invalid")
    if value["catalog"] and not _is_hex64(value["catalog"]):
        raise _refuse("card_read_set_invalid")
    reads = value["reads"]
    if not reads and not value["catalog"]:
        raise _refuse("card_read_set_empty")
    if len(reads) > MAX_READ_SET_READS:
        raise _refuse("card_read_set_too_large")
    keys = []
    for read in reads:
        if (not isinstance(read, Mapping) or set(read) != {"subject_hash", "access_id", "revision"}
                or not _is_hex64(read["subject_hash"]) or type(read["access_id"]) is not str
                or not read["access_id"] or type(read["revision"]) is not int or read["revision"] < 0):
            raise _refuse("card_read_set_read_invalid")
        keys.append((read["subject_hash"], read["access_id"]))
    if len(set(keys)) != len(keys):
        # card:<x> with card-absent:<x>, or the same Card twice, contradict each other.
        raise _refuse("card_read_set_dependency_contradiction")
    if keys != sorted(keys):
        raise _refuse("card_read_set_not_canonical")
    try:
        encoded = canonical_json_bytes(dict(value))
    except WireRefused as exc:
        raise _refuse("card_read_set_invalid") from exc
    if len(encoded) > MAX_READ_SET_BYTES:
        raise _refuse("card_read_set_too_large")
    return dict(value)


def _aggregate(value: Mapping[str, Any]) -> dict[str, Any]:
    return {"binding_ref": "reads:" + sha256_hex(canonical_json_bytes(dict(value))),
            "target_scope": sha256_hex(canonical_json_bytes(
                {"scopes": sorted({read["subject_hash"] for read in value["reads"]})})),
            "candidate_digest": sha256_hex(canonical_json_bytes(dict(value)))}


def hub_read_set_participant_input(*, reads: Sequence[Mapping[str, Any]], catalog_version_digest: str = "",
                                   actor_subject: str, actor_kind: str) -> dict[str, Any]:
    """The Hub's read-set ``participant_inputs[PARTICIPANT]``; refuses a read set the Hub would never prepare."""
    if (actor_kind not in ("caller", "grantor") or type(actor_subject) is not str or not actor_subject.strip()
            or actor_subject != actor_subject.strip()):
        raise _refuse("card_read_set_actor_invalid")
    value = validate_read_set_candidate(read_set_candidate_value(reads, catalog_version_digest))
    return {"participant": PARTICIPANT, "binding_kind": READ_SET_BINDING_KIND, **_aggregate(value),
            "target_incarnation": 1, "action": "read", "before_revision": 1, "candidate_revision": 1,
            "dependency_revisions": dependency_revisions(value["reads"], catalog_version_digest=value["catalog"]),
            "actor_subject": actor_subject, "actor_kind": actor_kind, "provisioning": {}}


def verify_read_set_projection(projection: Mapping[str, Any], value: Any) -> dict[str, Any]:
    """The read-set candidate a verified projection names, or a named refusal (every field compared)."""
    if projection.get("binding_kind") != READ_SET_BINDING_KIND:
        raise _refuse("card_read_set_binding_invalid")
    checked = validate_read_set_candidate(value)
    expected = _aggregate(checked)
    dependencies = projection.get("dependency_revisions")
    if (projection.get("participant") != PARTICIPANT
            or any(projection.get(name) != expected[name] for name in expected)
            # Exact integers: True == 1 in Python, so the type is checked too (CodeApp 23:45).
            or not _exact_int(projection.get("target_incarnation")) or projection.get("action") != "read"
            or not _exact_int(projection.get("before_revision"))
            or not _exact_int(projection.get("candidate_revision"))
            or projection.get("provisioning") != {}
            or projection.get("actor_kind") not in ("caller", "grantor")
            or type(projection.get("actor_subject")) is not str or not projection["actor_subject"].strip()
            or projection["actor_subject"] != projection["actor_subject"].strip()
            or dependencies != dependency_revisions(checked["reads"], catalog_version_digest=checked["catalog"])
            or reads_from_dependencies(dependencies) != checked["reads"]
            or catalog_reservation_from_dependencies(dependencies) != checked["catalog"]):
        raise _refuse("card_read_set_not_bound")
    return checked


__all__ = ["MAX_READ_SET_BYTES", "MAX_READ_SET_READS", "READ_SET_BINDING_KIND", "READ_SET_SCHEMA", "hub_read_set_participant_input",
           "read_set_candidate_value", "validate_read_set_candidate", "verify_read_set_projection"]
