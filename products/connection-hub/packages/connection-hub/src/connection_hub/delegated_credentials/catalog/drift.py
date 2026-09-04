# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Backend-computed drift between a card's accepted state and current authority.

The comparison is transient: it explains a card, and never rewrites one. A
surface renders what this module returns instead of comparing catalogs itself.

Every card resource has its own descriptor authority: a static row follows the
deployment catalog version, a user-owned connector follows its own descriptor
revision. Drift is therefore judged per resource against what the card accepted
for that resource (``resource_acceptance``). The card-wide ``catalog_version``
still carries the baseline for removals and for inner named-service additions,
and remains the only evidence for a card written before per-resource acceptance
existed.

Removals are computed against the active catalog alone, so they survive a
missing baseline. Additions need a baseline, because "new" means "absent when
this card was last saved": the per-resource acceptance where the card has it,
otherwise the saved catalog version document. A resource that document never
described (an owner-overlay connector) reports no additions from it, so an
unrelated deployment catalog change cannot make an unchanged connector's grants
read as newly available.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from connection_hub.delegated_credentials.catalog.descriptors import (
    resource_descriptor_state,
)
from connection_hub.delegated_credentials.catalog.models import (
    CatalogDocument,
)
from connection_hub.delegated_credentials.named_service_policy import (
    configured_named_service_operations,
)
from connection_hub.delegated_credentials.oauth.config import (
    oauth_delegated_config_from_connections,
)
from connection_hub.delegated_credentials.resource_operations import (
    normalize_resource_operations,
)

DRIFT_CURRENT = "current"
DRIFT_CHANGED = "changed"
DRIFT_NO_RELEVANT_CHANGE = "no_relevant_change"
DRIFT_BASELINE_MISSING = "baseline_missing"
DRIFT_UNAVAILABLE = "unavailable"

EFFECT_DENIED = "denied_immediately"
# A selected operation whose descriptor changed is not denied by the catalog
# (the operation still exists); it is held back from use until the owner
# accepts the changed descriptor on the card.
EFFECT_SUSPENDED = "suspended_until_accepted"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _namespace(value: Any) -> str:
    return _clean(value).lower().rstrip(":")


class _CatalogView:
    """What one catalog generation offers, per card resource."""

    def __init__(self, document: CatalogDocument, *, config: Any = None) -> None:
        self.version = document.version
        self._config = config or oauth_delegated_config_from_connections(
            document.connections
        )

    def resource(self, resource: str) -> Any:
        # A card selector, so the all-resource row answers only a card that
        # holds it: otherwise a withdrawn door reads as still offered and its
        # claims are reported "already ineffective" while they still work.
        return self._config.card_selector_config(resource)

    def claims(self, resource: str) -> set[str] | None:
        """``None`` when the resource declares no claim ceiling."""
        cfg = self.resource(resource)
        if cfg is None:
            return set()
        configured = {_clean(grant) for grant in (cfg.grants or ()) if _clean(grant)}
        return configured or None

    def outer_operations(self, resource: str) -> set[str] | None:
        """What the resource publishes; ``None`` only for the all-resource row.

        The all-resource row takes its operations from endpoint policy, not from
        the catalog, so it carries no ceiling and nothing about it drifts. Every
        other row is bounded by what it publishes, and an emptied or deleted
        block publishes nothing — which the guard answers the same way.
        """
        cfg = self.resource(resource)
        if cfg is None:
            return set()
        if _clean(getattr(cfg, "resource", "")).rstrip("/") == "*":
            return None
        return {
            _clean(getattr(tool, "name", "")) for tool in (cfg.tools or ())
        } - {""}

    def named_service_operations(self, resource: str) -> dict[str, set[str]]:
        """What the resource publishes; empty when it publishes nothing.

        Unlike claims and outer operations this never reports "no ceiling":
        an absent or empty block offers no inner operation, which the guard
        answers the same way.
        """
        cfg = self.resource(resource)
        if cfg is None:
            return {}
        return configured_named_service_operations(getattr(cfg, "named_services", None))


def selected_named_service_operations(card: Any) -> dict[str, dict[str, set[str]]]:
    """``resource -> namespace -> operations`` the card selected.

    An exact selection says it directly. A wildcard or a pre-encoding card is
    read from the materialized boundary, which is that selection expanded under
    the catalog version the card was saved against.

    Derived, never authority. Drift computes against it, and the public card
    projection carries it so a surface can render a wildcard card's actual
    coverage instead of an empty picker.
    """
    selection = card.named_service_operations
    out: dict[str, dict[str, set[str]]] = {}
    if selection.is_none:
        return out
    if selection.is_exact:
        for resource, namespaces in selection.operations.items():
            per_resource = out.setdefault(_clean(resource), {})
            for namespace, operations in namespaces.items():
                per_resource[_namespace(namespace)] = {
                    _clean(op) for op in operations if _clean(op)
                }
        return out
    materialized = configured_named_service_operations(card.named_services)
    if not materialized:
        return out
    # The boundary is one tree for the whole card; attribute it to the resources
    # the card actually holds.
    for resource in card.resource_grants:
        out[_clean(resource)] = {
            namespace: set(operations) for namespace, operations in materialized.items()
        }
    return out


def _acceptance_of(card: Any) -> Mapping[str, Any]:
    value = getattr(card, "resource_acceptance", None)
    return value if isinstance(value, Mapping) else {}


def _removed(
    *, card: Any, active: _CatalogView
) -> dict[str, list[dict[str, Any]]]:
    resources: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    outer_operations: list[dict[str, Any]] = []
    named_service_operations: list[dict[str, Any]] = []

    selected_inner = selected_named_service_operations(card)
    selected_outer = normalize_resource_operations(
        getattr(card, "resource_operations", {})
    )

    for raw_resource, grants in card.resource_grants.items():
        resource = _clean(raw_resource)
        if active.resource(resource) is None:
            resources.append(
                {"resource": resource, "was_selected": True, "effect": EFFECT_DENIED}
            )
            continue

        ceiling = active.claims(resource)
        if ceiling is not None:
            for claim in sorted({_clean(g) for g in (grants or ()) if _clean(g)} - ceiling):
                claims.append(
                    {
                        "resource": resource,
                        "claim": claim,
                        "was_selected": True,
                        "effect": EFFECT_DENIED,
                    }
                )

        offered_outer = active.outer_operations(resource)
        if offered_outer is not None:
            for operation in sorted(
                set(selected_outer.get(resource, ())) - offered_outer
            ):
                outer_operations.append(
                    {
                        "resource": resource,
                        "operation": operation,
                        "was_selected": True,
                        "effect": EFFECT_DENIED,
                    }
                )

        offered_inner = active.named_service_operations(resource)
        for namespace, operations in sorted(selected_inner.get(resource, {}).items()):
            for operation in sorted(operations - offered_inner.get(namespace, set())):
                named_service_operations.append(
                    {
                        "resource": resource,
                        "namespace": namespace,
                        "operation": operation,
                        "was_selected": True,
                        "effect": EFFECT_DENIED,
                    }
                )

    return {
        "resources": resources,
        "claims": claims,
        "outer_operations": outer_operations,
        "named_service_operations": named_service_operations,
    }


def _changed_operations(
    *, card: Any, resource_states: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Selected operations whose descriptor changed since the card accepted it."""
    rows: list[dict[str, Any]] = []
    for resource, state in sorted(resource_states.items()):
        for operation in state.get("changed_operations") or ():
            rows.append(
                {
                    "resource": resource,
                    "operation": operation,
                    "was_selected": True,
                    "effect": EFFECT_SUSPENDED,
                    "accepted_digest": state.get("accepted_digest", ""),
                    "current_digest": state.get("current_digest", ""),
                }
            )
    return rows


def _added(
    *,
    card: Any,
    active: _CatalogView,
    baseline: _CatalogView | None,
    resource_states: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """What the card's resources offer now and did not when the card was last
    saved. Never selected: an addition is an option, not a grant.

    Per resource, the evidence is the card's own acceptance when it has one;
    otherwise the saved catalog version document. A resource that document
    never described yields no additions: without a baseline for it, "new"
    cannot be distinguished from "already there, left unchecked".
    """
    claims: list[dict[str, Any]] = []
    outer_operations: list[dict[str, Any]] = []
    named_service_operations: list[dict[str, Any]] = []

    for raw_resource in card.resource_grants:
        resource = _clean(raw_resource)
        if active.resource(resource) is None:
            continue
        state = resource_states.get(resource)
        accepted_known = bool(state) and state.get("status") not in ("unknown", "removed")
        if accepted_known:
            for claim in state.get("added_claims") or ():
                claims.append({"resource": resource, "claim": claim, "selected": False})
            for operation in state.get("added_operations") or ():
                outer_operations.append(
                    {"resource": resource, "operation": operation, "selected": False}
                )
        elif baseline is not None and baseline.resource(resource) is not None:
            for claim in sorted(
                (active.claims(resource) or set()) - (baseline.claims(resource) or set())
            ):
                claims.append({"resource": resource, "claim": claim, "selected": False})
            for operation in sorted(
                (active.outer_operations(resource) or set())
                - (baseline.outer_operations(resource) or set())
            ):
                outer_operations.append(
                    {"resource": resource, "operation": operation, "selected": False}
                )

        # Inner operations are itemized only by the catalog document; a
        # resource the baseline never described contributes none.
        if baseline is None or baseline.resource(resource) is None:
            continue
        offered = active.named_service_operations(resource) or {}
        known = baseline.named_service_operations(resource) or {}
        for namespace in sorted(offered):
            for operation in sorted(offered[namespace] - known.get(namespace, set())):
                named_service_operations.append(
                    {
                        "resource": resource,
                        "namespace": namespace,
                        "operation": operation,
                        "selected": False,
                    }
                )

    return {
        "claims": claims,
        "outer_operations": outer_operations,
        "named_service_operations": named_service_operations,
    }


def resource_states(
    *, card: Any, active: _CatalogView
) -> dict[str, dict[str, Any]]:
    """Per-resource descriptor state of a card against the active view."""
    acceptance = _acceptance_of(card)
    selected_outer = normalize_resource_operations(
        getattr(card, "resource_operations", {})
    )
    out: dict[str, dict[str, Any]] = {}
    for raw_resource, grants in card.resource_grants.items():
        resource = _clean(raw_resource)
        if not resource:
            continue
        out[resource] = resource_descriptor_state(
            accepted=acceptance.get(resource),
            row=active.resource(resource),
            catalog_version=active.version,
            selected_operations=selected_outer.get(resource, ()),
            selected_grants=grants or (),
        )
    return out


def card_resource_states(
    *, card: Any, active: CatalogDocument, active_config: Any = None
) -> dict[str, dict[str, Any]]:
    """Per-resource descriptor state against the active document, read with
    the grantor's delegable config (owner overlay included) when given."""
    return resource_states(card=card, active=_CatalogView(active, config=active_config))


def _any(block: Mapping[str, Iterable[Any]]) -> bool:
    return any(bool(rows) for rows in block.values())


def _empty_added() -> dict[str, list[dict[str, Any]]]:
    return {"claims": [], "outer_operations": [], "named_service_operations": []}


def card_drift(
    *,
    card: Any,
    active: CatalogDocument,
    baseline: CatalogDocument | None,
    baseline_confirmed_absent: bool = False,
    active_config: Any = None,
) -> dict[str, Any]:
    """The drift block for one card.

    ``baseline`` is the card's saved version document; pass ``None`` with
    ``baseline_confirmed_absent`` when durable absence was confirmed.
    ``active_config`` is the delegable config the active document resolves to
    for this grantor, including owner-overlay rows; without it the document
    alone is read.
    """
    saved_version = _clean(getattr(card, "catalog_version", ""))
    active_view = _CatalogView(active, config=active_config)
    removed = _removed(card=card, active=active_view)
    states = resource_states(card=card, active=active_view)
    changed_operations = _changed_operations(card=card, resource_states=states)
    resource_changed = any(
        state.get("status") in ("changed", "removed") for state in states.values()
    )
    version_current = bool(saved_version) and saved_version == active.version

    if version_current and not _any(removed) and not resource_changed:
        return {
            "status": DRIFT_CURRENT,
            "saved_version": saved_version,
            "current_version": active.version,
            "resources": states,
        }

    baseline_view = _CatalogView(baseline) if baseline is not None else None
    if version_current:
        # Same catalog generation: a static row cannot have gained anything,
        # so additions come only from resources with their own authority.
        added = _added(card=card, active=active_view, baseline=None, resource_states=states)
        return {
            "status": DRIFT_CHANGED,
            "saved_version": saved_version,
            "current_version": active.version,
            "removed": removed,
            "changed": {"outer_operations": changed_operations},
            "added": added,
            "resources": states,
        }

    added = _added(
        card=card, active=active_view, baseline=baseline_view, resource_states=states
    )
    if baseline is None:
        # Removals still hold against the verified current catalog; additions
        # since the last save can be established only for resources the card
        # accepted individually.
        return {
            "status": DRIFT_BASELINE_MISSING
            if not (resource_changed or _any(removed) or _any(added))
            else DRIFT_CHANGED,
            "saved_version": saved_version,
            "current_version": active.version,
            "removed": removed,
            "changed": {"outer_operations": changed_operations},
            "added": added,
            "baseline_confirmed_absent": bool(baseline_confirmed_absent),
            "resources": states,
        }

    changed = _any(removed) or _any(added) or resource_changed
    return {
        "status": DRIFT_CHANGED if changed else DRIFT_NO_RELEVANT_CHANGE,
        "saved_version": saved_version,
        "current_version": active.version,
        "removed": removed,
        "changed": {"outer_operations": changed_operations},
        "added": added,
        "resources": states,
    }


def drift_unavailable(reason: str = "") -> dict[str, Any]:
    """Editing authority is disabled: the comparison could not be made."""
    return {
        "status": DRIFT_UNAVAILABLE,
        "reason": _clean(reason) or "catalog_unavailable",
    }


__all__ = [
    "DRIFT_BASELINE_MISSING",
    "DRIFT_CHANGED",
    "DRIFT_CURRENT",
    "DRIFT_NO_RELEVANT_CHANGE",
    "DRIFT_UNAVAILABLE",
    "EFFECT_DENIED",
    "EFFECT_SUSPENDED",
    "card_drift",
    "card_resource_states",
    "drift_unavailable",
    "resource_states",
    "selected_named_service_operations",
]
