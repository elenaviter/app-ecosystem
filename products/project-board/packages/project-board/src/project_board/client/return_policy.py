"""Bounded, pure transitions used by the existing return-reconciliation owner.

No native reader, timer, filesystem owner or execution authority lives here.
The candidates are test inputs, not production defaults or qualified cadence.
"""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Mapping

from .io import content_hash, parse_utc
from ..contract.errors import DomainError

STATE_VERSION = 2
POLICY_REVISION = "ordered-return-v2"
CANDIDATE_INACTIVITY_SECONDS = 3600
CANDIDATE_ACTIVE_FRESHNESS_SECONDS = 60
MAX_REQUIREMENTS = 64
MAX_EPISODES = 16
MAX_INVENTORY = 128
MAX_RECORD_BYTES = 2048
MAX_REQUEST_HISTORY = 8
MAX_HISTORY = 20
MAX_SEEN = 256
# Input-work budgets, separate from persisted inventory bounds. A reader may
# inspect one extra directory entry/byte only to establish that a cap was hit.
MAX_CENSUS_ENTRIES = 256
MAX_CENSUS_BYTES = 1024 * 1024
MAX_CENSUS_RECORD_BYTES = 128 * 1024
MAX_CENSUS_ATTEMPTS = 2


def fits(value: Any) -> bool:
    return len(json.dumps(value, ensure_ascii=True, sort_keys=True).encode()) <= MAX_RECORD_BYTES


def invalidate(episode, reason: str, now: str, *, advance: bool = True):
    request = episode.get("request")
    if request:
        history = episode.setdefault("invalidated_requests", [])
        history.append({**request, "invalidated_at": now, "reason": reason})
        del history[:-MAX_REQUEST_HISTORY]
    episode["request"] = None
    if advance:
        episode["reconciliation_generation"] = int(episode.get("reconciliation_generation") or 1) + 1


def overflow(row, reason: str, now: str):
    """Keep the original prefix and a durable fence; never prune pending work."""
    if not row.get("overflow"):
        row["overflow"] = {"reason": reason, "at": now}
        for episode in row.get("episodes", {}).values():
            if episode.get("state") == "pending":
                invalidate(episode, "state_overflow", now)


def requirement_hash(episode) -> str:
    return content_hash(episode.get("requirements") or {})


def merge_requirements(row, episode, requirements, now: str):
    current = episode.setdefault("requirements", {})
    additions = [r for r in requirements if r["requirement_id"] not in current]
    if not additions:
        return
    if len(current) + len(additions) > MAX_REQUIREMENTS:
        overflow(row, "episode_requirements", now)
        return
    for requirement in additions:
        current[requirement["requirement_id"]] = deepcopy(requirement["evidence"])
    invalidate(episode, "requirements_changed", now)


def add_requirement(row, requirement_id: str, evidence: Mapping[str, Any], projects, now: str):
    """Deduplicate occurrences, not their mutable retry-time observations."""
    pending = row.setdefault("pending_requirements", {})
    if requirement_id in pending or requirement_id in row.setdefault("seen_requirements", []):
        return
    # A pending episode's first evidence remains an identity fence even after
    # the bounded cache of already-consumed global occurrences has rotated.
    if any(requirement_id in e.get("requirements", {}) for e in row["episodes"].values()):
        return
    if len(pending) >= MAX_REQUIREMENTS or not fits(evidence):
        overflow(row, "pending_requirements", now)
        row["overflow"].setdefault("blocked_requirement_id", requirement_id)
        row["overflow"].setdefault("blocked_evidence_hash", content_hash(evidence))
        return
    # Attendance is already normalized by its owner. Check cardinality before
    # copying/deduplicating an unsupported scope into the bounded ledger.
    if len(projects) > MAX_EPISODES:
        overflow(row, "attended_projects", now)
        row["overflow"].setdefault("blocked_requirement_id", requirement_id)
        row["overflow"].setdefault("blocked_evidence_hash", content_hash(evidence))
        return
    targets = list(dict.fromkeys(projects))
    record = {"requirement_id": requirement_id, "evidence": deepcopy(dict(evidence)),
              "projects": targets, "consumed_by": []}
    pending[requirement_id] = record
    for project, episode in row["episodes"].items():
        if project in targets and episode.get("state") == "pending":
            merge_requirements(row, episode, [record], now)


def pending_for(row, project):
    return [r for r in row.get("pending_requirements", {}).values()
            if project in r["projects"] and project not in r["consumed_by"]]


def consume(row, project, episode):
    pending = row["pending_requirements"]
    for key in episode.get("requirements", {}):
        record = pending.get(key)
        if not record:
            continue
        if project not in record["consumed_by"]:
            record["consumed_by"].append(project)
        if set(record["projects"]) <= set(record["consumed_by"]):
            del pending[key]
            row["seen_requirements"].append(key)
    del row["seen_requirements"][:-MAX_SEEN]


def gap_seconds(previous: Mapping[str, Any], now: str):
    if not previous.get("at"):
        return None
    try:
        gap = (parse_utc(now) - parse_utc(previous["at"])).total_seconds()
        return gap if gap >= 0 else None
    except (DomainError, ValueError, TypeError):
        return None


def latch_interval(row, now: str, threshold_seconds, projects, *, actionable: bool):
    """Evaluate *before* an incoming point advances the future baseline.

    This restricted slice has no qualified native continuity producer. A
    point never fills its preceding interval and never clears a latched gap.
    """
    if threshold_seconds is None:
        return
    if isinstance(threshold_seconds, bool) or not isinstance(threshold_seconds, int) or threshold_seconds <= 0:
        raise ValueError("An elapsed-policy threshold must be a positive integer or None")
    previous = row.get("activity_baseline") or {}
    gap = gap_seconds(previous, now)
    if gap is not None and gap < threshold_seconds:
        return
    # Empty first attendance establishes a baseline, not a latent demand to
    # reconcile every future fresh request. A real preceding gap is retained
    # even while its eventual actionable cohort is not yet present.
    if gap is None and not actionable:
        return
    kind = "observed_inactivity_gap" if gap is not None else "activity_interval_unknown"
    key = content_hash([POLICY_REVISION, row["session_id"], kind, previous])
    add_requirement(row, key, {
        "kind": kind, "at": now, "prior_model_entry": deepcopy(previous),
        "observed_gap_seconds": gap, "threshold_seconds": threshold_seconds,
        "policy_revision": POLICY_REVISION, "activity_coverage": "unknown",
    }, projects, now)


def migrate(row, projects):
    """Add fences without borrowing a legacy in-flight request's approval.

    Already-confirmed receipts remain replayable; pending legacy requests
    need a new census. Known-but-not-consumed legacy causes remain latched.
    """
    row.setdefault("episodes", {})
    row.setdefault("pending_requirements", {})
    row.setdefault("seen_requirements", [])
    row.setdefault("activity_baseline", deepcopy(row.get("model_entry") or {}))
    if row.get("state_version") == STATE_VERSION:
        return row
    now = str((row.get("model_entry") or {}).get("at") or "")
    cause = row.get("cause") or {}
    cause_key = str(cause.get("token") or "")
    # Seed each original episode before admitting the latest global cause.
    # Otherwise cause B could populate the new map first and hide episode A's
    # original requirement during migration of an already-pending episode.
    for project, episode in row["episodes"].items():
        episode.setdefault("reconciliation_generation", 1)
        episode.setdefault("requirements", {})
        if not episode["requirements"]:
            original = deepcopy(episode.get("cause") or {"kind": "legacy_return"})
            key = str(original.get("token") or content_hash(["legacy", episode["episode_id"]]))
            if fits(original):
                episode["requirements"][key] = original
            else:
                overflow(row, "legacy_requirement", now)
        if episode.get("state") == "pending":
            # The ledger is newly introduced. A request which never bound it
            # cannot be upgraded into a current request by filling fields in.
            invalidate(episode, "legacy_request_requires_fresh_census", now, advance=False)
            for key, evidence in episode["requirements"].items():
                if key not in row["pending_requirements"]:
                    if len(row["pending_requirements"]) >= MAX_REQUIREMENTS:
                        overflow(row, "legacy_pending_requirements", now)
                        break
                    row["pending_requirements"][key] = {
                        "requirement_id": key, "evidence": deepcopy(evidence),
                        "projects": [project], "consumed_by": [],
                    }
                elif project not in row["pending_requirements"][key]["projects"]:
                    row["pending_requirements"][key]["projects"].append(project)
        elif episode.get("request"):
            # Only a durable prior confirmation may replay its old receipt;
            # this does not authorize a new request or consume later causes.
            episode["request"].update(
                reconciliation_generation=episode["reconciliation_generation"],
                requirements_hash=requirement_hash(episode), session_id=row["session_id"],
            )
        if (len(episode.get("inventory", [])) > MAX_INVENTORY
                or any(not fits(h) for h in episode.get("inventory", []))
                or len(episode["requirements"]) > MAX_REQUIREMENTS
                or any(not fits(e) for e in episode["requirements"].values())):
            overflow(row, "legacy_state_limits", now)
    unconfirmed = any(e.get("state") == "pending" for e in row["episodes"].values())
    if cause_key and (unconfirmed or cause_key != row.get("consumed_cause")):
        if cause_key in row["pending_requirements"]:
            record = row["pending_requirements"][cause_key]
            if len(projects) > MAX_EPISODES:
                overflow(row, "legacy_attended_projects", now)
            else:
                record["projects"] = list(dict.fromkeys([*record["projects"], *projects]))
                if len(record["projects"]) > MAX_EPISODES:
                    overflow(row, "legacy_attended_projects", now)
        else:
            add_requirement(row, cause_key, cause, projects, now)
    elif cause_key:
        row["seen_requirements"].append(cause_key)
    for project, episode in row["episodes"].items():
        if episode.get("state") == "pending":
            merge_requirements(row, episode, pending_for(row, project), now)
        elif episode.get("request"):
            # A legacy global observation checkpoint cannot clear Q merely
            # because P confirmed. Only durable per-project confirmations
            # consume their original requirements. Clock ordering alone is
            # not proof that an old confirmation covered a later occurrence.
            consume(row, project, episode)
    if len(row["episodes"]) > MAX_EPISODES or len(row.get("history", [])) > MAX_HISTORY:
        overflow(row, "legacy_episode_limits", now)
    row["state_version"] = STATE_VERSION
    return row
