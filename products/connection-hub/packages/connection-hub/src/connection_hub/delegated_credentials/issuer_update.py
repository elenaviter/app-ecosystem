"""Exact, widening-only issuer updates; no issuer-specific policy vocabulary.

The query supplies coordinates and one resource's proposed final selection,
not an actor, approval, decision, candidate Card or credential. The host binds
the actor. The issuer decides the server-built complete candidate. Storage
owns durable replay and holds its production fence through publication.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Any, Mapping

from .cards.lifecycle import LifecycleRefused, LifecycleTarget
from .cards.model import CardAuthority
from .issuer_gate import IssuerRequest, IssuerWriteRefused, change_digest, issuer_write_refusal


class IssuerUpdateRefused(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _text(value: Any, limit: int = 2048) -> bool:
    return type(value) is str and bool(value.strip()) and value == value.strip() and len(value.encode()) <= limit


def _selection(value: Any) -> tuple[str, ...]:
    if (type(value) is not list or len(value) > 512 or not all(_text(v, 256) for v in value)
            or value != sorted(set(value))):
        raise IssuerUpdateRefused("issuer_update_selection_invalid")
    return tuple(value)


@dataclasses.dataclass(frozen=True)
class IssuerUpdateQuery:
    context_ref: str
    request_id: str
    target: LifecycleTarget
    resource: str
    operations: tuple[str, ...]
    grants: tuple[str, ...]

    @classmethod
    def from_mapping(cls, raw: Any) -> IssuerUpdateQuery:
        if (not isinstance(raw, Mapping) or set(raw) != {"context_ref", "request_id", "target", "delta"}
                or not _text(raw["context_ref"], 8192) or not _text(raw["request_id"], 512)):
            raise IssuerUpdateRefused("issuer_update_query_invalid")
        delta = raw["delta"]
        if (not isinstance(delta, Mapping) or set(delta) != {"resource", "operations", "grants"}
                or not _text(delta["resource"])):
            raise IssuerUpdateRefused("issuer_update_delta_invalid")
        try:
            target = LifecycleTarget.from_mapping(raw["target"])
        except (LifecycleRefused, TypeError, ValueError) as exc:
            raise IssuerUpdateRefused("issuer_update_target_invalid") from exc
        result = cls(raw["context_ref"], raw["request_id"], target, delta["resource"],
                     _selection(delta["operations"]), _selection(delta["grants"]))
        if result.to_dict() != raw:
            raise IssuerUpdateRefused("issuer_update_query_invalid")
        return result

    def to_dict(self) -> dict[str, Any]:
        return {"context_ref": self.context_ref, "request_id": self.request_id,
                "target": dataclasses.asdict(self.target),
                "delta": {"resource": self.resource, "operations": list(self.operations), "grants": list(self.grants)}}

    def binding(self, actor_subject: str) -> dict[str, Any]:
        if not _text(actor_subject) or actor_subject == "anonymous" or actor_subject.startswith(("integration:", "telegram_")):
            raise IssuerUpdateRefused("issuer_update_requires_platform_human")
        return {"actor_subject": actor_subject, "request": self.to_dict()}

    def transaction_id(self, actor_subject: str) -> str:
        self.binding(actor_subject)
        # Intentionally exclude the delta/target: a changed body under the same
        # actor, context and request id must conflict with the original receipt.
        return change_digest({"actor_subject": actor_subject, "context_ref": self.context_ref,
                              "request_id": self.request_id})


def build_candidate(original: CardAuthority, query: IssuerUpdateQuery) -> CardAuthority:
    try:
        query.target.assert_authority(original)
    except LifecycleRefused as exc:
        raise IssuerUpdateRefused(exc.reason.replace("issuer_lifecycle_", "issuer_update_", 1)) from exc
    if original.expires_at and datetime.now(timezone.utc).timestamp() >= original.expires_at:
        raise IssuerUpdateRefused("issuer_update_target_expired")
    from .issuer_snapshot import full_authority, IssuerSnapshotRefused
    try:
        full_authority(original)
    except IssuerSnapshotRefused as exc:
        raise IssuerUpdateRefused("issuer_update_credential_material") from exc
    held = set(original.resource_operations.get(query.resource, ()))
    granted = set(original.resource_grants.get(query.resource, ()))
    if not held <= set(query.operations) or not granted <= set(query.grants):
        raise IssuerUpdateRefused("issuer_update_narrowing_unsupported")
    if held == set(query.operations) and granted == set(query.grants):
        raise IssuerUpdateRefused("issuer_update_delta_empty")
    operations = dict(original.resource_operations)
    grants = dict(original.resource_grants)
    operations[query.resource], grants[query.resource] = query.operations, query.grants
    candidate = dataclasses.replace(original, card_revision=original.card_revision + 1,
                                    resource_operations=operations, resource_grants=grants)
    # An explicit preservation invariant, including new model fields. Changing
    # the single selection also changes its derived flat operation union.
    before, after = original.to_dict(), candidate.to_dict()
    allowed = {"card_revision", "operations", "resource_operations", "resource_grants"}
    if any(before.get(k) != after.get(k) for k in set(before) | set(after) if k not in allowed):
        raise IssuerUpdateRefused("issuer_update_preservation_failed")
    return candidate


def issuer_request(query: IssuerUpdateQuery, actor_subject: str, candidate_digest: str) -> IssuerRequest:
    return IssuerRequest(actor_subject, query.request_id, "update", query.target.access_id,
                         query.target.expected_card_revision, query.target.issuer_kind, query.target.issuer_ref,
                         candidate_digest, query.context_ref)


async def issuer_managed_card_update(raw: Mapping[str, Any], *, actor_subject: str,
        registry: Any, persistence: Any, host_is_current: Any) -> dict[str, Any]:
    """Internal host operation; every new mutation requires a fresh sealed issuer.

    Terminal replay recovers the exact prior result, never performs another
    mutation. Raw authority remains in request-bound server orchestration.
    """
    from .issuer_gate import IssuerRegistry
    from .cards.service import CardConflict
    query = None
    try:
        query = IssuerUpdateQuery.from_mapping(raw)
        query.binding(actor_subject)
        if type(registry) is not IssuerRegistry or not callable(host_is_current) or not host_is_current():
            raise IssuerUpdateRefused("issuer_update_host_unavailable")
        apply = getattr(persistence, "update_issuer", None)
        read_authority = getattr(persistence, "read_issuer_update_authority", None)
        if not callable(apply) or not callable(read_authority):
            raise IssuerUpdateRefused("issuer_update_port_unavailable")
        initial = None

        async def gate(original, candidate):
            nonlocal initial
            if not host_is_current():
                raise IssuerWriteRefused("issuer_update_context_changed")
            if original.expires_at and datetime.now(timezone.utc).timestamp() >= original.expires_at:
                raise IssuerWriteRefused("issuer_update_target_expired")
            request = issuer_request(query, actor_subject, change_digest(candidate.to_dict()))
            if initial is None:
                initial = await registry.decide(request)
                decision = initial
            else:
                decision = await registry.revalidate(request, initial)
            if not host_is_current():
                raise IssuerWriteRefused("issuer_update_context_changed")
            refusal = issuer_write_refusal(original, request, decision, now=datetime.now(timezone.utc))
            if refusal is not None:
                raise IssuerWriteRefused(refusal["reason"])
            if original.expires_at:
                return min(decision.valid_until, datetime.fromtimestamp(original.expires_at, timezone.utc))
            return decision.valid_until

        receipt = await apply(query, actor_subject=actor_subject, before_commit=gate)
        committed = receipt["state"] == "committed"
        pending = receipt["state"] == "prepared"
        complete = receipt["serving_state"] in ("complete", "not_required")
        confirmed = False
        if receipt["change_digest"] and not pending:
            request = issuer_request(query, actor_subject, receipt["change_digest"])
            confirmed = await registry.finalize(request, state=receipt["state"],
                card_revision=query.target.expected_card_revision + (1 if committed else 0))
        result = {"ok": committed and complete and confirmed,
            "status": 200 if committed and complete and confirmed else 202 if committed or pending else 409,
            "state": receipt["state"], "serving_state": receipt["serving_state"],
            "transaction_id": receipt["transaction_id"], "request": query.to_dict(),
            "change_digest": receipt["change_digest"], "reason": receipt["reason"],
            "issuer_outcome_confirmed": confirmed, "retryable": pending or committed and not (complete and confirmed),
            "requires_new_request_id": receipt["state"] == "refused"}
        if not host_is_current():
            # The commit cannot be undone or relabelled. Withhold personal data
            # if the host/scope changed across finalization or storage awaits.
            return {**result, "ok": False, "status": 202 if committed or pending else 409,
                    "reason": "issuer_update_context_changed", "retryable": committed or pending}
        if committed and complete:
            authority = await read_authority(receipt)
            if not host_is_current():
                return {**result, "ok": False, "status": 202, "reason": "issuer_update_context_changed", "retryable": True}
            if authority is None:
                return {**result, "ok": False, "status": 202, "reason": "issuer_update_result_unavailable", "retryable": True}
            return {**result, "authority": authority.to_dict(), "authority_fingerprint": authority.content_hash()}
        return result
    except (IssuerUpdateRefused, IssuerWriteRefused) as exc:
        return {"ok": False, "status": 403, "error": exc.reason, "requires_new_request_id": True}
    except CardConflict as exc:
        return {"ok": False, "status": 409, "error": exc.reason, "retryable": True}
    except Exception:
        # A failed transport/file operation is not proof of no durable write.
        return {"ok": False, "status": 503, "error": "issuer_update_outcome_unavailable", "retryable": True,
                "outcome_unknown": query is not None}
