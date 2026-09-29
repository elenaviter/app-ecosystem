"""One explicit recovery of a Codex wake whose automatic retry ran out (W405).

The relay gives a consumed wake one retry and then stops (W198): a session
that took two wakes without receiving is not helped by an endless third. When
mail is still pending after that, the session is stranded until someone acts.
On 2026-09-29 a Spark session sat that way for four hours with thirteen
messages pending, and the coordinator recovered it by hand with one native
queue prompt naming the existing wake.

This module is that recovery as a supported command:

- it names the exact session and the exact outstanding wake, so a stale view
  cannot recover the wrong one;
- it reserves the recovery under the worker's lock before the native queue
  call, so a crash leaves an inspectable "outcome unknown" record and never
  invites a second submission;
- it sends the same prompt the relay sends, with the existing wake id, so the
  worker's ``pb worker receive --wake-id`` acknowledges exactly that wake;
- only that receive resolves it. Queue admission is recorded as ``submitted``
  and is not proof that the model received anything.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from ..contract.errors import DomainError
from .host_config import HostRelayConfig
from .session_delivery import CODEX_QUEUE_ADAPTER, delivery_adapter, notify_agent_session
from .store import SharedFieldStore

# Native queue results that prove nothing was queued: another recovery of the
# same wake is then allowed. Anything else unclear is an unknown outcome.
DEFINITE_FAILURES = frozenset({"codex_command_not_found", "codex_queue_failed"})


def recover_worker_wake(
    config: HostRelayConfig,
    *,
    worker_name: str,
    wake_id: str,
    requested_by: str,
    notifier: Callable[..., Mapping[str, Any]] = notify_agent_session,
) -> dict[str, Any]:
    """Submit one recovery prompt for the named session's exhausted wake."""

    channel = next(
        (candidate for candidate in config.workers if candidate.worker_name == worker_name),
        None,
    )
    if channel is None:
        raise DomainError(
            "field_worker_wake_recovery_not_on_host",
            "That worker has no channel on this host; run the recovery on the "
            "host where its session runs.",
            status=404,
            details={"worker_name": worker_name, "host_id": config.host_id},
        )
    if delivery_adapter(channel.runtime_kind) != CODEX_QUEUE_ADAPTER:
        raise DomainError(
            "field_worker_wake_recovery_unsupported",
            "Only a session woken through its native queue (Codex) has a wake "
            "to recover; a Claude Code session is reached by its own watch.",
            status=409,
            details={"worker_name": worker_name, "runtime_kind": channel.runtime_kind},
        )
    field = SharedFieldStore(config.field_root)
    recovery = field.reserve_wake_recovery(
        worker_name, wake_id=wake_id, requested_by=requested_by
    )
    session = field.worker_listener_session(worker_name) or {}
    subscription = dict(session.get("subscription") or {})
    provenance = {
        **dict(subscription.get("wake_provenance") or {}),
        "attempt": "recovery",
        "attempted_at": recovery["requested_at"],
    }
    # An exception here leaves the reservation in place: the call may or may
    # not have reached the queue, so it reads as an unknown outcome and holds
    # off a second submission.
    result = dict(
        notifier(
            channel,
            event_kind="input.available",
            wake_id=wake_id,
            wake_provenance=provenance,
        )
    )
    reason = str(result.get("reason") or "")
    if bool(result.get("delivered")):
        state = "submitted"
    elif reason in DEFINITE_FAILURES:
        state = "failed"
    else:
        state = "outcome_unknown"
    recorded = field.record_wake_recovery(
        worker_name,
        wake_id=wake_id,
        state=state,
        submission_id=str(result.get("queued_submission_id") or ""),
        reason=reason,
    )
    return {
        "worker_name": worker_name,
        "wake_id": wake_id,
        "recovery": recorded or {**recovery, "state": "resolved_before_recording"},
        "queue_result": {
            key: result.get(key)
            for key in ("adapter", "state", "delivered", "reason", "queued_submission_id")
            if key in result
        },
        "next": (
            "Queue admission is not model handling. The recovery is resolved "
            "only when the worker runs pb worker receive for this wake; read "
            "pb worker list for that, and do not submit again while it reads "
            "submitted or outcome_unknown."
        ),
    }


__all__ = ["DEFINITE_FAILURES", "recover_worker_wake"]
