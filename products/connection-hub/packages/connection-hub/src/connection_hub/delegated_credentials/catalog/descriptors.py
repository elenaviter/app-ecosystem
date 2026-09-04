# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Per-resource accepted descriptor state.

A card grants operations on several resources, and each resource has its own
authority over what those operations mean:

    static catalog row          the deployment catalog version and the row's
                                published tools
    user-owned MCP connector    the connector's descriptor revision and the
                                discovered tool descriptors
    future gateway provider     its own declared revision contract

The card therefore records, per resource, what it accepted: the resource kind,
the revision and digest of the descriptor it was saved against, the claims it
saw, and one digest per operation the resource offered. Drift is then judged
resource by resource. An unrelated change elsewhere in the deployment catalog
cannot make an unchanged connector's grants look new, a changed selected
operation is suspended until the owner accepts exactly that change, and a tool
the server started advertising stays ungranted.

Rows arriving through an owner overlay (remote MCP connectors) carry their own
descriptor evidence as attributes; a plain catalog row is digested here from
its published projection. Nothing in this module reads a store.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

RESOURCE_KIND_CATALOG = "catalog"
RESOURCE_KIND_REMOTE_MCP = "remote_mcp"

# Optional attributes an overlay row may carry to declare its own descriptor
# authority. Absent attributes mean "a static catalog row".
ROW_ATTR_KIND = "descriptor_kind"
ROW_ATTR_PROVIDER = "descriptor_provider"
ROW_ATTR_REVISION = "descriptor_revision"
ROW_ATTR_DIGEST = "descriptor_digest"
ROW_ATTR_OPERATION_DIGESTS = "operation_digests"


class ResourceAcceptanceError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _strings(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = values.replace(",", " ").split()
    return tuple(sorted({_clean(item) for item in values if _clean(item)}))


def canonical_digest(value: Any) -> str:
    """Full lowercase SHA-256 of the canonical JSON encoding of ``value``."""
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def operation_descriptor_digest(tool: Any) -> str:
    """Digest of one published outer operation: what a grantor reads when they
    tick it. A connector tool carries its own digest over its schemas; a
    catalog tool is digested from its declared projection."""
    own = _clean(getattr(tool, "descriptor_digest", ""))
    if own:
        return own.lower()
    return canonical_digest(
        {
            "name": _clean(getattr(tool, "name", "")),
            "label": _clean(getattr(tool, "label", "")),
            "description": _clean(getattr(tool, "description", "")),
            "grants": list(_strings(getattr(tool, "grants", ()))),
        }
    )


def resource_row_digest(row: Any) -> str:
    """Digest of one catalog row's published projection.

    Covers the claim ceiling, the outer tools, the named-service tree, and the
    identity facts a grantor accepts with the row. Deliberately excludes the
    catalog version: the version changes whenever anything anywhere changes,
    the row digest only when this row does.
    """
    own = _clean(getattr(row, ROW_ATTR_DIGEST, ""))
    if own:
        return own.lower()
    tools = sorted(
        (
            {
                "name": _clean(getattr(tool, "name", "")),
                "label": _clean(getattr(tool, "label", "")),
                "description": _clean(getattr(tool, "description", "")),
                "grants": list(_strings(getattr(tool, "grants", ()))),
            }
            for tool in (getattr(row, "tools", ()) or ())
            if _clean(getattr(tool, "name", ""))
        ),
        key=lambda item: item["name"],
    )
    named_services = getattr(row, "named_services", None)
    return canonical_digest(
        {
            "resource": _clean(getattr(row, "resource", "")),
            "label": _clean(getattr(row, "label", "")),
            "identity_scope": _clean(getattr(row, "identity_scope", "")),
            "admin_only": bool(getattr(row, "admin_only", False)),
            "grants": list(_strings(getattr(row, "grants", ()))),
            "tools": tools,
            "named_services": copy.deepcopy(dict(named_services))
            if isinstance(named_services, Mapping)
            else {},
        }
    )


@dataclass(frozen=True)
class ResourceAcceptance:
    """What a card accepted for one resource when it was last saved."""

    kind: str
    revision: str
    digest: str
    grants: tuple[str, ...] = ()
    operations: Mapping[str, str] = field(default_factory=dict)
    provider: str = ""

    def __post_init__(self) -> None:
        kind = _clean(self.kind) or RESOURCE_KIND_CATALOG
        digest = _clean(self.digest).lower()
        if not digest:
            raise ResourceAcceptanceError("resource_acceptance_digest_missing")
        operations = {
            _clean(name): _clean(value).lower()
            for name, value in dict(self.operations or {}).items()
            if _clean(name) and _clean(value)
        }
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "revision", _clean(self.revision))
        object.__setattr__(self, "digest", digest)
        object.__setattr__(self, "grants", _strings(self.grants))
        object.__setattr__(self, "operations", operations)
        object.__setattr__(self, "provider", _clean(self.provider))

    @classmethod
    def from_mapping(cls, value: Any) -> "ResourceAcceptance":
        if not isinstance(value, Mapping):
            raise ResourceAcceptanceError("resource_acceptance_not_object")
        operations = value.get("operations")
        if operations is not None and not isinstance(operations, Mapping):
            raise ResourceAcceptanceError("resource_acceptance_operations_invalid")
        return cls(
            kind=_clean(value.get("kind")),
            revision=_clean(value.get("revision")),
            digest=_clean(value.get("digest")),
            grants=_strings(value.get("grants")),
            operations=dict(operations or {}),
            provider=_clean(value.get("provider")),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "revision": self.revision,
            "digest": self.digest,
            "grants": list(self.grants),
            "operations": dict(sorted(self.operations.items())),
        }
        if self.provider:
            payload["provider"] = self.provider
        return payload


def row_acceptance(row: Any, *, catalog_version: str) -> ResourceAcceptance:
    """The acceptance a save would record for ``row`` right now."""
    kind = _clean(getattr(row, ROW_ATTR_KIND, "")) or RESOURCE_KIND_CATALOG
    revision = _clean(getattr(row, ROW_ATTR_REVISION, "")) or _clean(catalog_version)
    declared = getattr(row, ROW_ATTR_OPERATION_DIGESTS, None)
    if isinstance(declared, Mapping) and declared:
        operations = {_clean(name): _clean(value) for name, value in declared.items()}
    else:
        operations = {
            _clean(getattr(tool, "name", "")): operation_descriptor_digest(tool)
            for tool in (getattr(row, "tools", ()) or ())
            if _clean(getattr(tool, "name", ""))
        }
    return ResourceAcceptance(
        kind=kind,
        revision=revision,
        digest=resource_row_digest(row),
        grants=_strings(getattr(row, "grants", ())),
        operations=operations,
        provider=_clean(getattr(row, ROW_ATTR_PROVIDER, "")),
    )


def parse_resource_acceptance(value: Any) -> dict[str, ResourceAcceptance]:
    """``resource -> ResourceAcceptance`` from a stored or submitted mapping."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ResourceAcceptanceError("resource_acceptance_invalid")
    out: dict[str, ResourceAcceptance] = {}
    for resource, entry in value.items():
        key = _clean(resource)
        if not key:
            continue
        out[key] = ResourceAcceptance.from_mapping(entry)
    return out


def next_resource_acceptance(
    *,
    resources: Iterable[str],
    row_for: Any,
    catalog_version: str,
    selected_operations: Mapping[str, Iterable[str]],
    previous: Mapping[str, ResourceAcceptance] | None = None,
    accepted_operations: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, ResourceAcceptance]:
    """The acceptance a save writes for each resource it keeps.

    ``row_for(resource)`` returns the governing row or ``None``. A resource the
    card had not accepted before, or whose kind changed, is accepted as the
    current row shows it. A resource accepted before keeps the digest it
    accepted for every SELECTED operation whose descriptor has since changed,
    unless that operation is named in ``accepted_operations``: an ordinary save
    (a rename, a claim edit) does not silently accept a changed tool.
    Operations not selected carry the current digest, so a tool the resource
    starts advertising is recorded as seen and stops reading as new after this
    save, while remaining ungranted.
    """
    previous = dict(previous or {})
    accepted = {
        _clean(resource): {_clean(op) for op in ops if _clean(op)}
        for resource, ops in dict(accepted_operations or {}).items()
    }
    out: dict[str, ResourceAcceptance] = {}
    for raw_resource in resources:
        resource = _clean(raw_resource)
        if not resource:
            continue
        row = row_for(resource)
        prior = previous.get(resource)
        if row is None:
            # Withdrawn from the catalog: the reconciliation that runs before
            # this prunes it; keep the prior evidence if it is still keyed.
            if prior is not None:
                out[resource] = prior
            continue
        current = row_acceptance(row, catalog_version=catalog_version)
        if prior is None or prior.kind != current.kind:
            out[resource] = current
            continue
        selected = {_clean(op) for op in (selected_operations.get(resource) or ()) if _clean(op)}
        operations: dict[str, str] = {}
        for name, current_digest in current.operations.items():
            old_digest = prior.operations.get(name)
            if (
                name in selected
                and old_digest
                and old_digest != current_digest
                and name not in accepted.get(resource, set())
            ):
                operations[name] = old_digest
            else:
                operations[name] = current_digest
        suspended = any(
            operations[name] != current.operations[name] for name in operations
        )
        out[resource] = ResourceAcceptance(
            kind=current.kind,
            # The row-level evidence advances only when nothing selected is
            # left suspended, so the resource keeps reading as changed until
            # the owner has reviewed every changed selected operation.
            revision=current.revision if not suspended else prior.revision,
            digest=current.digest if not suspended else prior.digest,
            grants=current.grants,
            operations=operations,
            provider=current.provider,
        )
    return out


def resource_descriptor_state(
    *,
    accepted: ResourceAcceptance | None,
    row: Any,
    catalog_version: str,
    selected_operations: Iterable[str],
    selected_grants: Iterable[str] = (),
) -> dict[str, Any]:
    """Compare one resource's accepted state with what its authority shows now.

    Statuses:

        current           accepted evidence matches the current descriptor
        changed           a selected operation's descriptor changed (it is
                          suspended until accepted), a selected operation or
                          claim was withdrawn, or the resource now advertises
                          operations or claims it did not before
        removed           the resource is no longer offered
        unknown           the card carries no acceptance for it (written before
                          this evidence existed)
    """
    selected = [_clean(op) for op in selected_operations if _clean(op)]
    held_claims = _strings(selected_grants)
    if row is None:
        return {
            "status": "removed",
            "kind": accepted.kind if accepted is not None else "",
            "accepted_revision": accepted.revision if accepted is not None else "",
            "accepted_digest": accepted.digest if accepted is not None else "",
            "current_revision": "",
            "current_digest": "",
            "changed_operations": [],
            "removed_operations": list(selected),
            "added_operations": [],
            "removed_claims": list(held_claims),
            "added_claims": [],
        }
    current = row_acceptance(row, catalog_version=catalog_version)
    if accepted is None:
        return {
            "status": "unknown",
            "kind": current.kind,
            "accepted_revision": "",
            "accepted_digest": "",
            "current_revision": current.revision,
            "current_digest": current.digest,
            "changed_operations": [],
            "removed_operations": sorted(
                op for op in selected if op not in current.operations
            ),
            "added_operations": [],
            "removed_claims": sorted(set(held_claims) - set(current.grants))
            if current.grants
            else [],
            "added_claims": [],
        }
    changed = sorted(
        op
        for op in selected
        if op in current.operations
        and accepted.operations.get(op)
        and accepted.operations[op] != current.operations[op]
    )
    removed_ops = sorted(op for op in selected if op not in current.operations)
    added_ops = sorted(op for op in current.operations if op not in accepted.operations)
    removed_claims = (
        sorted(set(held_claims) - set(current.grants)) if current.grants else []
    )
    added_claims = sorted(set(current.grants) - set(accepted.grants))
    status = (
        "changed"
        if changed or removed_ops or added_ops or removed_claims or added_claims
        or accepted.digest != current.digest
        else "current"
    )
    return {
        "status": status,
        "kind": current.kind,
        "accepted_revision": accepted.revision,
        "accepted_digest": accepted.digest,
        "current_revision": current.revision,
        "current_digest": current.digest,
        "changed_operations": changed,
        "removed_operations": removed_ops,
        "added_operations": added_ops,
        "removed_claims": removed_claims,
        "added_claims": added_claims,
    }


__all__ = [
    "RESOURCE_KIND_CATALOG",
    "RESOURCE_KIND_REMOTE_MCP",
    "ROW_ATTR_DIGEST",
    "ROW_ATTR_KIND",
    "ROW_ATTR_OPERATION_DIGESTS",
    "ROW_ATTR_PROVIDER",
    "ROW_ATTR_REVISION",
    "ResourceAcceptance",
    "ResourceAcceptanceError",
    "canonical_digest",
    "next_resource_acceptance",
    "operation_descriptor_digest",
    "parse_resource_acceptance",
    "resource_descriptor_state",
    "resource_row_digest",
    "row_acceptance",
]
