"""Positive model-entry evidence and explicit, project-bound return checkpoints.

Notification probes and queue admission are deliberately not model entries.
This is execution guidance; it never parses a coordinator's prose into actions.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from ..contract.errors import DomainError
from ..contract.refs import parse_ref
from .io import atomic_write_json, component, content_hash, exclusive_lock, new_id, parse_utc, read_json, utc_now
from . import return_policy as policy


# Product policy is intentionally unconfigured until explicitly decided.
INACTIVITY_THRESHOLD_SECONDS: int | None = None
SIGNAL = "worker.return.reconciliation_required"
PREFIX = "return-reconciliation:"
ACTION_KINDS = frozenset({"assign", "request", "question", "decision", "reply", "blocked", "delivery_failed"})


def coordinator_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    holder = value.get("holder") or {}
    name = str(holder.get("worker_name") or "")
    if not name or value.get("state") != "held" or not holder.get("attending", True):
        raise DomainError("field_return_coordinator_unknown", "A current attending coordinator is required.", status=409)
    return {"worker_name": name, "revision": int(value.get("revision") or 0),
            "since": str(value.get("since") or "")}


class ReturnReconciliation:
    def __init__(self, field, worker_name: str):
        self.field = field
        self.worker = component(worker_name, field="worker_name").lower()
        self.path = field.control / "return-reconciliation" / f"{self.worker}.json"
        self.lock = self.path.with_suffix(".lock")

    def _read(self, worker=None):
        value = read_json(self.path, required=False) or {}
        worker = worker if worker is not None else self.field.read_worker(self.worker)
        session = str(worker.get("runtime_session_id") or "")
        if value and value.get("session_id") != session:
            # A stable worker cannot borrow a different native session's approval.
            # Original mail remains in the inbox/history. Preserve the prior
            # evidence as audit metadata, never as an approval in this session.
            value = {"session_id": session, "episodes": {},
                     "previous_session": value.get("session_id"),
                     "previous_session_evidence": {
                         "model_entry": value.get("model_entry"),
                         "episode_ids": [e["episode_id"] for e in value.get("episodes", {}).values()],
                     }}
        projects = worker.get("attended_project_refs") or []
        return policy.migrate({"session_id": session, "episodes": {}, **value}, projects)

    def record_cause(self, cause: str, evidence: Mapping[str, Any]):
        if cause not in {"quota_hold", "session_suspension"}:
            raise ValueError("Only an observed quota hold or suspension is a known return cause")
        with exclusive_lock(self.lock):
            row = self._read()
            now = utc_now()
            # Only structured cause metadata is durable; never accept a native
            # transcript, arguments, credentials or arbitrary payload here.
            metadata = {k: str(v) for k, v in evidence.items()
                        if k in {"hold_id", "occurrence_id", "since", "until", "kind", "source"}}
            # Repeated observations of one hold retain one cause identity.
            occurrence = metadata.get("hold_id") or metadata.get("occurrence_id") or ""
            token = content_hash([cause, occurrence]) if occurrence else content_hash([cause, metadata])
            value = {"kind": cause, "evidence": metadata, "token": token, "at": now}
            if policy.fits(value):
                row["cause"] = value  # compatibility observation, not the ledger
                projects = self.field.read_worker(self.worker).get("attended_project_refs") or []
                policy.add_requirement(row, token, value, projects, now)
            else:
                policy.overflow(row, "cause_metadata", now)
            # Admit/invalidate the occurrence under the owner lock before a
            # potentially slow census. Other writers cannot approve R_A while
            # B is waiting for its outside-lock input capture.
            atomic_write_json(self.path, row)
        self._run(self._observe_and_write, now=now, threshold_seconds=INACTIVITY_THRESHOLD_SECONDS)

    def _coordinator(self, project):
        try:
            return coordinator_binding(self.field.read_project_coordinator(parse_ref(project).object_id))
        except DomainError:
            return None

    @staticmethod
    def _cohort(headers, project, projects):
        return [h for h in headers if (h.get("project_ref") == project
                or (not h.get("project_ref") and len(projects) == 1))
                and h.get("kind") in ACTION_KINDS
                and h.get("expected_reaction") not in {"acknowledge_only", "await_review"}
                and not str(h.get("correlation_id") or "").startswith(PREFIX)]

    def _state_context(self):
        worker = self.field.read_worker(self.worker)
        row = self._read(worker)
        projects = worker.get("attended_project_refs") or []
        # No mailbox or holder reads for the entirely inactive case. Existing
        # episodes still need holder-epoch checks even when policy is None.
        bindings = ({p: self._coordinator(p) for p in projects}
                    if row["episodes"] or row["pending_requirements"] or row.get("overflow") else {})
        context = {"session_id": row["session_id"], "projects": projects, "bindings": bindings}
        return row, context, content_hash([row, context])

    @staticmethod
    def _needs_census(row, now, threshold_seconds):
        if row.get("overflow"):
            return False  # approval already fenced; never rescan on recovery probes
        if row["pending_requirements"] or any(e["state"] == "pending" for e in row["episodes"].values()):
            return True
        if threshold_seconds is None:
            return False
        if isinstance(threshold_seconds, bool) or not isinstance(threshold_seconds, int) or threshold_seconds <= 0:
            raise ValueError("An elapsed-policy threshold must be a positive integer or None")
        gap = policy.gap_seconds(row["activity_baseline"], now)
        return gap is None or gap >= threshold_seconds

    def _run(self, action, *, now, threshold_seconds):
        """Capture bounded census input outside the owner lock, then revalidate.

        Full-census inspection remains the store's existing interface. This
        optimistic snapshot is not a second mailbox/authority. A racing state,
        attendance, session, holder or input stamp retries a bounded number of
        times; exhaustion preserves evidence behind a durable recovery fence.
        """
        for _attempt in range(policy.MAX_CENSUS_ATTEMPTS):
            with exclusive_lock(self.lock):
                row, context, expected = self._state_context()
                if not self._needs_census(row, now, threshold_seconds):
                    return action(row, context, [], now, threshold_seconds)
            snapshot = self.field.pending_return_census(
                self.worker, max_entries=policy.MAX_CENSUS_ENTRIES,
                max_bytes=policy.MAX_CENSUS_BYTES, max_record_bytes=policy.MAX_CENSUS_RECORD_BYTES,
                max_headers=policy.MAX_INVENTORY, max_projects=policy.MAX_EPISODES,
            )
            with exclusive_lock(self.lock):
                row, context, current = self._state_context()
                if (current != expected or not snapshot["stable"]
                        or not self.field.pending_return_census_current(self.worker, snapshot)):
                    continue
                if snapshot["overflow"]:
                    policy.overflow(row, snapshot["overflow"], now)
                # A new episode needs the current holder, including when the
                # original no-episode snapshot did not need a holder read.
                if not context["bindings"]:
                    context["bindings"] = {p: self._coordinator(p) for p in context["projects"]}
                return action(row, context, snapshot["headers"], now, threshold_seconds)
        with exclusive_lock(self.lock):
            row, context, _ = self._state_context()
            policy.overflow(row, "census_retry_exhausted", now)
            return action(row, context, [], now, threshold_seconds)

    def _observe_and_write(self, row, context, headers, now, threshold_seconds):
        self._observe(row, context, headers, now=now, threshold_seconds=threshold_seconds)
        atomic_write_json(self.path, row)
        return self.signals(row)

    def _observe(self, row, context, headers, *, now, threshold_seconds):
        projects = context["projects"]
        actionable = any(self._cohort(headers, p, projects) for p in projects)
        policy.latch_interval(row, now, threshold_seconds, projects, actionable=actionable)
        episodes = row["episodes"]
        for project in projects:
            episode = episodes.get(project)
            requirements = policy.pending_for(row, project)
            cohort = self._cohort(headers, project, projects)
            binding = context["bindings"].get(project)
            if episode and episode["state"] == "confirmed":
                if (requirements or row.get("overflow")) and cohort:
                    row.setdefault("history", []).append(
                        {k: deepcopy(v) for k, v in episode.items() if k != "inventory"}
                    )
                    del row["history"][:-policy.MAX_HISTORY]
                    del episodes[project]
                    episode = None
                elif binding != episode.get("coordinator"):
                    episode["state"] = "pending"
                    episode["authority_binding"] = binding
                    policy.invalidate(episode, "coordinator_changed", now)
                else:
                    continue
            if episode is None:
                if not cohort or not (requirements or row.get("overflow")):
                    continue
                if len(episodes) >= policy.MAX_EPISODES:
                    policy.overflow(row, "project_episodes", now)
                    continue
                first = requirements[0]["evidence"] if requirements else {"kind": "state_overflow"}
                previous = deepcopy(first.get("prior_model_entry") or row.get("activity_baseline") or {})
                episode = {
                    "episode_id": new_id("return"), "project_ref": project,
                    "state": "pending", "began_at": now, "prior_model_entry": previous,
                    "inventory": [], "cause": deepcopy(first),
                    "threshold_seconds": first.get("threshold_seconds", threshold_seconds),
                    "observed_gap_seconds": first.get("observed_gap_seconds", policy.gap_seconds(previous, now)),
                    "reconciliation_generation": 1,
                    "requirements": {r["requirement_id"]: deepcopy(r["evidence"]) for r in requirements},
                    "authority_binding": binding,
                }
                episodes[project] = episode
            else:
                policy.merge_requirements(row, episode, requirements, now)
                if episode.get("authority_binding") != binding:
                    episode["authority_binding"] = binding
                    policy.invalidate(episode, "coordinator_changed", now)
            known = {h["message_ref"] for h in episode["inventory"]}
            added = [h for h in cohort if h["message_ref"] not in known]
            if added:
                for header in added:
                    if len(episode["inventory"]) >= policy.MAX_INVENTORY or not policy.fits(header):
                        policy.overflow(row, "inventory_headers", now)
                        break
                    episode["inventory"].append(deepcopy(header))
                episode["inventory_hash"] = content_hash(episode["inventory"])
                # A fresh episode has no request to invalidate or generation
                # to advance merely for its original inventory population.
                if episode.get("request"):
                    policy.invalidate(episode, "inventory_changed", now)

    @staticmethod
    def _require_capacity(row):
        if row.get("overflow"):
            raise DomainError(
                "field_return_state_overflow",
                "The preserved return evidence reached its bound; reconciliation stays pending for explicit recovery.",
                status=409, details=dict(row["overflow"]),
            )

    @staticmethod
    def _context(row, episode, binding):
        return {"episode_id": episode["episode_id"],
                "inventory_hash": episode["inventory_hash"],
                "coordinator": binding, "session_id": row["session_id"],
                "reconciliation_generation": episode["reconciliation_generation"],
                "requirements_hash": policy.requirement_hash(episode)}

    def model_entry(self, via: str, *, now: str = "", threshold_seconds=INACTIVITY_THRESHOLD_SECONDS):
        if via not in {"receive", "wake-ack", "lease-read", "settle"}:
            raise ValueError("Not a model-facing handling boundary")
        now = now or utc_now()
        def apply(row, context, headers, now, threshold_seconds):
            self._observe(row, context, headers, now=now, threshold_seconds=threshold_seconds)
            if not row.get("overflow"):
                row["model_entry"] = {"at": now, "via": via, "session_id": row["session_id"]}
                # The preceding candidate and this future baseline are one
                # atomic update. Information/control entries cannot wash it.
                row["activity_baseline"] = dict(row["model_entry"])
            atomic_write_json(self.path, row)
            return self.signals(row)
        return self._run(apply, now=now, threshold_seconds=threshold_seconds)

    def observe_activity(self, point: Mapping[str, Any], *, now: str = "",
                         threshold_seconds=INACTIVITY_THRESHOLD_SECONDS):
        """Reduce an ordered metadata point; no native producer is wired here.

        This is the internal reducer seam for qualification fixtures and a
        future separately qualified producer. It asserts no active coverage,
        supported runtime version, tool progress or native cadence.
        """
        fields = {"session_id", "source_generation", "cursor", "turn_key", "occurred_at"}
        if set(point) != fields or any(not isinstance(point[k], str) or not point[k]
                                      for k in fields - {"cursor"}):
            raise DomainError("field_return_activity_invalid", "Expected metadata-only activity fields.")
        if (isinstance(point["cursor"], bool) or not isinstance(point["cursor"], int)
                or point["cursor"] < 0 or not policy.fits(point)):
            raise DomainError("field_return_activity_invalid", "The activity cursor or metadata is invalid.")
        now = now or utc_now()
        if parse_utc(point["occurred_at"]) > parse_utc(now):
            raise DomainError("field_return_activity_invalid", "Future activity is not handling evidence.")
        def apply(row, context, headers, now, threshold_seconds):
            if point["session_id"] != row["session_id"]:
                raise DomainError("field_return_activity_session_mismatch", "Activity belongs to another native session.")
            incoming = dict(point)
            cursor = row.get("activity_cursor") or {}
            if cursor.get("source_generation") == point["source_generation"]:
                if point["cursor"] <= cursor["cursor"]:
                    if incoming == cursor:
                        # A repeated read is not new activity. It can still
                        # expose the open tail since that original point.
                        self._observe(row, context, headers, now=now, threshold_seconds=threshold_seconds)
                        atomic_write_json(self.path, row)
                        return self.signals(row)
                    raise DomainError("field_return_activity_order", "Activity is not the next ordered point.")
            previous = row.get("activity_baseline") or {}
            if previous.get("at") and parse_utc(point["occurred_at"]) < parse_utc(previous["at"]):
                raise DomainError("field_return_activity_order", "Activity predates the committed handling baseline.")
            projects = context["projects"]
            actionable = any(self._cohort(headers, p, projects) for p in projects)
            policy.latch_interval(row, point["occurred_at"], threshold_seconds, projects, actionable=actionable)
            if not row.get("overflow"):
                row["activity_baseline"] = {
                    "at": point["occurred_at"], "via": "metadata_point",
                    "session_id": row["session_id"],
                    "event_id": content_hash([point["source_generation"], point["cursor"]]),
                }
                row["activity_cursor"] = incoming
            self._observe(row, context, headers, now=now, threshold_seconds=threshold_seconds)
            if row.get("overflow"):
                # The open tail can itself exceed a bound after the incoming
                # point was tentatively reduced. Commit its fence/evidence,
                # but never only an advanced baseline on overflow.
                row["activity_baseline"] = previous
                if cursor:
                    row["activity_cursor"] = cursor
                else:
                    row.pop("activity_cursor", None)
            atomic_write_json(self.path, row)
            return self.signals(row)
        return self._run(apply, now=now, threshold_seconds=threshold_seconds)

    def prepare(self):
        """A notifier may latch evidence, but never advance the model watermark."""
        return self._run(self._observe_and_write, now=utc_now(), threshold_seconds=INACTIVITY_THRESHOLD_SECONDS)

    def signals(self, row=None):
        row = self._read() if row is None else row
        signals = [{"kind": SIGNAL, "episode_id": e["episode_id"], "project_ref": p,
                 "began_at": e["began_at"], "last_model_handled_at": e["prior_model_entry"].get("at") or "unknown",
                 "handled_via": e["prior_model_entry"].get("via") or "unknown",
                 "cause": e["cause"], "policy_threshold_seconds": e.get("threshold_seconds") or "unconfigured",
                 "inventory_count": len(e["inventory"]), "inventory_hash": e.get("inventory_hash"),
                 "reconciliation_generation": e["reconciliation_generation"],
                 "requirements_hash": policy.requirement_hash(e),
                 "requirements": deepcopy(e["requirements"]),
                 "state_overflow": row.get("overflow"),
                 "correlation_id": PREFIX + e["episode_id"],
                 "next": "Inspect the queued requests with worker inbox; ask the current coordinator for one current resume list; record per-item decisions, then explicitly return-confirm. Silence never confirms."}
                for p, e in row["episodes"].items() if e["state"] == "pending"]
        if row.get("overflow"):
            projects = self.field.read_worker(self.worker).get("attended_project_refs") or []
            for project in projects:
                if project not in row["episodes"] or row["episodes"][project]["state"] != "pending":
                    episode_id = "overflow_" + content_hash([row["session_id"], project, row["overflow"]])[:32]
                    signals.append({"kind": SIGNAL, "project_ref": project, "episode_id": episode_id,
                                    "correlation_id": PREFIX + episode_id,
                                    "began_at": row["overflow"]["at"],
                                    "inventory_count": 0, "inventory_hash": "unknown",
                                    "last_model_handled_at": "unknown", "handled_via": "unknown",
                                    "cause": {"kind": "state_overflow"},
                                    "state_overflow": dict(row["overflow"]),
                                    "next": "Inspect preserved evidence and ask the current coordinator for explicit overflow recovery; queued execution stays gated."})
        return signals

    def census_headers(self):
        """Original request refs stay enumerable after receipt or settlement."""
        with exclusive_lock(self.lock):
            row = self._read()
            return [{**header, "return_episode_id": episode["episode_id"],
                     "census_original_request": True}
                    for episode in row["episodes"].values() if episode["state"] == "pending"
                    for header in episode["inventory"]]

    def request_context(self, project_ref, correlation_id, recipient, coordinator):
        """Validate a reconciliation request before publishing any mail."""
        def apply(row, context, headers, now, threshold_seconds):
            self._observe(row, context, headers, now=now, threshold_seconds=threshold_seconds)
            atomic_write_json(self.path, row)
            self._require_capacity(row)
            episode = row["episodes"].get(project_ref)
            if not episode or episode["state"] != "pending" or correlation_id != PREFIX + episode["episode_id"]:
                raise DomainError("field_return_episode_mismatch", "The request must name the current return episode.", status=409)
            binding = coordinator_binding(coordinator)
            if recipient != binding["worker_name"]:
                raise DomainError("field_return_coordinator_mismatch", "Address the current coordinator.", status=409)
            return self._context(row, episode, binding)
        return self._run(apply, now=utc_now(), threshold_seconds=INACTIVITY_THRESHOLD_SECONDS)

    def record_request(self, project_ref, context, message):
        def apply(row, captured, headers, now, threshold_seconds):
            self._observe(row, captured, headers, now=now, threshold_seconds=threshold_seconds)
            atomic_write_json(self.path, row)
            self._require_capacity(row)
            episode = row["episodes"].get(project_ref) or {}
            current = self._context(row, episode, captured["bindings"].get(project_ref)) if episode else {}
            if (episode.get("state") != "pending" or not current
                    or any(context.get(k) != v for k, v in current.items())):
                raise DomainError("field_return_inventory_changed", "Inspect the changed requirements, inventory and authority; send a fresh reconciliation request.", status=409)
            ref = str(message.get("message_ref") or "")
            if not ref:
                raise DomainError("field_return_request_unrecorded", "No durable reconciliation request receipt was returned.", status=409)
            request = {**context, "message_ref": ref, "created_at": str(message.get("created_at") or utc_now())}
            if (episode.get("request") or {}).get("message_ref") == ref:
                return episode["request"]
            episode["request"] = request
            atomic_write_json(self.path, row)
            return request
        return self._run(apply, now=utc_now(), threshold_seconds=INACTIVITY_THRESHOLD_SECONDS)

    def confirm(self, project_ref, episode_id, decision, coordinator):
        """Explicit intent after the human-readable decisions have been recorded."""
        binding = coordinator_binding(coordinator)
        def apply(row, context, headers, now, threshold_seconds):
            self._observe(row, context, headers, now=now, threshold_seconds=threshold_seconds)
            atomic_write_json(self.path, row)
            self._require_capacity(row)
            episode = row["episodes"].get(project_ref) or {}
            request = episode.get("request") or {}
            if episode.get("episode_id") != episode_id:
                raise DomainError("field_return_episode_mismatch", "The current project episode does not match.", status=409)
            current = self._context(row, episode, binding)
            valid = (request and all(request.get(k) == v for k, v in current.items())
                     and decision.get("project_ref") == project_ref
                     and decision.get("sender") == binding["worker_name"]
                     and decision.get("kind") == "reply"
                     and decision.get("correlation_id") == PREFIX + episode_id
                     and decision.get("reply_to") == request.get("message_ref")
                     and parse_utc(decision.get("created_at") or "") >= parse_utc(request["created_at"])
                     and parse_utc(request["created_at"]) >= parse_utc(episode["began_at"]))
            if not valid:
                raise DomainError("field_return_decision_mismatch", "A fresh exact-episode reply from the current coordinator is required; reconciliation stays pending.", status=409)
            if episode["state"] == "confirmed":
                if episode.get("decision_message_ref") != decision["message_ref"]:
                    raise DomainError("field_return_decision_mismatch", "This episode already has another checkpoint.", status=409)
                return {"schema": "problem-board.worker-return-confirm.v1", **{k: v for k, v in episode.items() if k != "inventory"}, "replayed": True}
            episode.update(state="confirmed", coordinator=binding,
                           decision_message_ref=decision["message_ref"], confirmed_at=utc_now())
            policy.consume(row, project_ref, episode)
            atomic_write_json(self.path, row)
            return {"schema": "problem-board.worker-return-confirm.v1", **{k: v for k, v in episode.items() if k != "inventory"}, "replayed": False}
        return self._run(apply, now=utc_now(), threshold_seconds=INACTIVITY_THRESHOLD_SECONDS)
