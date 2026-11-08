"""Positive model-entry evidence and explicit, project-bound return checkpoints.

Notification probes and queue admission are deliberately not model entries.
This is execution guidance; it never parses a coordinator's prose into actions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from ..contract.errors import DomainError
from ..contract.refs import parse_ref
from .io import atomic_write_json, component, content_hash, exclusive_lock, new_id, parse_utc, read_json, utc_now


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

    def _read(self):
        value = read_json(self.path, required=False) or {}
        session = str(self.field.read_worker(self.worker).get("runtime_session_id") or "")
        if value and value.get("session_id") != session:
            # A stable worker cannot borrow a different native session's approval.
            return {"session_id": session, "episodes": {}, "previous_session": value.get("session_id")}
        return {"session_id": session, "episodes": {}, **value}

    def record_cause(self, cause: str, evidence: Mapping[str, Any]):
        if cause not in {"quota_hold", "session_suspension"}:
            raise ValueError("Only an observed quota hold or suspension is a known return cause")
        with exclusive_lock(self.lock):
            row = self._read()
            # Repeated observations of one hold retain one cause identity.
            token = content_hash([cause, dict(evidence)])
            row["cause"] = {"kind": cause, "evidence": dict(evidence), "token": token, "at": utc_now()}
            atomic_write_json(self.path, row)

    def _observe(self, row, *, now, threshold_seconds):
        worker = self.field.read_worker(self.worker)
        previous = row.get("model_entry") or {}
        cause = row.get("cause") or {}
        fresh_cause = bool(cause and cause.get("token") != row.get("consumed_cause"))
        gap = None
        if previous.get("at"):
            try:
                gap = (parse_utc(now) - parse_utc(previous["at"])).total_seconds()
            except (DomainError, ValueError, TypeError):
                pass
        elapsed = bool(threshold_seconds is not None and gap is not None and gap >= threshold_seconds)
        episodes = row["episodes"]
        if not fresh_cause and not elapsed and not episodes:
            return
        if not fresh_cause and not elapsed and all(e["state"] == "confirmed" for e in episodes.values()):
            changed = False
            for project, episode in episodes.items():
                try:
                    local = self.field.read_project_coordinator(parse_ref(project).object_id)
                    changed = changed or coordinator_binding(local) != episode.get("coordinator")
                except DomainError:
                    changed = True
            if not changed:
                return
        headers = self.field.pending_mail_headers(self.worker)
        projects = worker.get("attended_project_refs") or []
        for project in projects:
            episode = episodes.get(project)
            cohort = [h for h in headers if (h.get("project_ref") == project
                      or (not h.get("project_ref") and len(projects) == 1))
                      and h.get("kind") in ACTION_KINDS
                      and h.get("expected_reaction") not in {"acknowledge_only", "await_review"}
                      and not str(h.get("correlation_id") or "").startswith(PREFIX)]
            if not episode and cohort and (fresh_cause or elapsed):
                episode = {"episode_id": new_id("return"), "project_ref": project, "state": "pending",
                           "began_at": now, "prior_model_entry": dict(previous), "inventory": [],
                           "cause": dict(cause) if fresh_cause else {"kind": "observed_inactivity_gap"},
                           "threshold_seconds": threshold_seconds, "observed_gap_seconds": gap}
                episodes[project] = episode
            if not episode:
                continue
            # A later return is another episode; a coordinator change revokes
            # an earlier checkpoint even if the holder changed back again.
            if episode["state"] == "confirmed":
                local = self.field.read_project_coordinator(parse_ref(project).object_id)
                try:
                    changed = coordinator_binding(local) != episode.get("coordinator")
                except DomainError:
                    changed = True
                if (fresh_cause or elapsed) and cohort:
                    row.setdefault("history", []).append({k: v for k, v in episode.items() if k != "inventory"})
                    row["history"] = row["history"][-20:]
                    del episodes[project]
                    replacement = {"episode_id": new_id("return"), "project_ref": project,
                                   "state": "pending", "began_at": now, "prior_model_entry": dict(previous),
                                   "inventory": [], "cause": dict(cause) if fresh_cause else {"kind": "observed_inactivity_gap"},
                                   "threshold_seconds": threshold_seconds, "observed_gap_seconds": gap}
                    episodes[project] = episode = replacement
                elif changed:
                    episode.update(state="pending", request=None, coordinator_changed_at=now)
                else:
                    continue
            known = {h["message_ref"] for h in episode["inventory"]}
            added = [h for h in cohort if h["message_ref"] not in known]
            if added:
                episode["inventory"].extend(added)
                episode["inventory_hash"] = content_hash(episode["inventory"])
                episode["request"] = None
        if fresh_cause and any(e.get("state") == "pending" for e in episodes.values()):
            row["consumed_cause"] = cause["token"]

    def model_entry(self, via: str, *, now: str = "", threshold_seconds=INACTIVITY_THRESHOLD_SECONDS):
        if via not in {"receive", "wake-ack", "lease-read", "settle"}:
            raise ValueError("Not a model-facing handling boundary")
        now = now or utc_now()
        with exclusive_lock(self.lock):
            row = self._read()
            self._observe(row, now=now, threshold_seconds=threshold_seconds)
            row["model_entry"] = {"at": now, "via": via, "session_id": row["session_id"]}
            atomic_write_json(self.path, row)
            return self.signals(row)

    def prepare(self):
        """A notifier may latch evidence, but never advance the model watermark."""
        with exclusive_lock(self.lock):
            row = self._read()
            self._observe(row, now=utc_now(), threshold_seconds=INACTIVITY_THRESHOLD_SECONDS)
            atomic_write_json(self.path, row)
            return self.signals(row)

    def signals(self, row=None):
        row = self._read() if row is None else row
        return [{"kind": SIGNAL, "episode_id": e["episode_id"], "project_ref": p,
                 "began_at": e["began_at"], "last_model_handled_at": e["prior_model_entry"].get("at") or "unknown",
                 "handled_via": e["prior_model_entry"].get("via") or "unknown",
                 "cause": e["cause"], "policy_threshold_seconds": e.get("threshold_seconds") or "unconfigured",
                 "inventory_count": len(e["inventory"]), "inventory_hash": e.get("inventory_hash"),
                 "correlation_id": PREFIX + e["episode_id"],
                 "next": "Inspect the queued requests with worker inbox; ask the current coordinator for one current resume list; record per-item decisions, then explicitly return-confirm. Silence never confirms."}
                for p, e in row["episodes"].items() if e["state"] == "pending"]

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
        with exclusive_lock(self.lock):
            row = self._read()
            self._observe(row, now=utc_now(), threshold_seconds=INACTIVITY_THRESHOLD_SECONDS)
            episode = row["episodes"].get(project_ref)
            if not episode or correlation_id != PREFIX + episode["episode_id"]:
                raise DomainError("field_return_episode_mismatch", "The request must name the current return episode.", status=409)
            binding = coordinator_binding(coordinator)
            if recipient != binding["worker_name"]:
                raise DomainError("field_return_coordinator_mismatch", "Address the current coordinator.", status=409)
            atomic_write_json(self.path, row)
            return {"episode_id": episode["episode_id"], "inventory_hash": episode["inventory_hash"],
                    "coordinator": binding}

    def record_request(self, project_ref, context, message):
        with exclusive_lock(self.lock):
            row = self._read()
            episode = row["episodes"].get(project_ref) or {}
            if (episode.get("episode_id") != context["episode_id"]
                    or episode.get("inventory_hash") != context["inventory_hash"]):
                raise DomainError("field_return_inventory_changed", "Inspect the changed inventory and send a fresh reconciliation request.", status=409)
            ref = str(message.get("message_ref") or "")
            if not ref:
                raise DomainError("field_return_request_unrecorded", "No durable reconciliation request receipt was returned.", status=409)
            request = {**context, "message_ref": ref, "created_at": str(message.get("created_at") or utc_now())}
            if (episode.get("request") or {}).get("message_ref") == ref:
                return episode["request"]
            episode["request"] = request
            atomic_write_json(self.path, row)
            return request

    def confirm(self, project_ref, episode_id, decision, coordinator):
        """Explicit intent after the human-readable decisions have been recorded."""
        binding = coordinator_binding(coordinator)
        with exclusive_lock(self.lock):
            row = self._read()
            self._observe(row, now=utc_now(), threshold_seconds=INACTIVITY_THRESHOLD_SECONDS)
            atomic_write_json(self.path, row)
            episode = row["episodes"].get(project_ref) or {}
            request = episode.get("request") or {}
            if episode.get("episode_id") != episode_id:
                raise DomainError("field_return_episode_mismatch", "The current project episode does not match.", status=409)
            valid = (request and request.get("coordinator") == binding
                     and request.get("inventory_hash") == episode.get("inventory_hash")
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
            atomic_write_json(self.path, row)
            return {"schema": "problem-board.worker-return-confirm.v1", **{k: v for k, v in episode.items() if k != "inventory"}, "replayed": False}
