# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Translate an explicit stored-Control choice into standard Card authority."""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote, unquote

from .cards.model import NamedServiceSelection
from .descriptor_agent_card import DescriptorAgentCardRefused


def children(policy: Any, category: str, parent: str) -> set[str]:
    prefix = quote(parent, safe="-._~@") + "/"
    return {unquote(value[len(prefix):]) for value in policy.capabilities.get(category, ()) if value.startswith(prefix)}


async def validate_new_standard_authority(service: Any, user: Any, *, original: Any, resident: Any, active: Any) -> None:
    """A Control ceiling contributes no owner permissions to a new positive grant."""
    required: set[str] = set()
    for resource, grants in resident.resource_grants.items():
        held = set(original.resource_grants.get(resource, ())) if original is not None else set()
        required.update(set(grants) - held)
        operations = set(resident.resource_operations.get(resource, ()))
        old_operations = set(original.resource_operations.get(resource, ())) if original is not None else set()
        named = resident.named_service_operations.operations.get(resource, {})
        old_named = original.named_service_operations.operations.get(resource, {}) if original is not None else {}
        if operations - old_operations or any(set(values) - set(old_named.get(namespace, ())) for namespace, values in named.items()):
            required.update(grants)
    if not required:
        return
    config = await service._catalog_config(active, owner_subject=resident.grantor_subject)
    inventory = await service._available_inventory(user, requested_grants=sorted(required), config=config)
    if not required.issubset(set(inventory.grant_names())):
        raise DescriptorAgentCardRefused("agent_capability_grants_not_delegable", status=403)


def selected_standard_maps(*, resident: Any, control: Any, authority: Any, selected: Any,
                           config: Any, allow_additions: bool = True) -> tuple[dict[str, list[str]], dict[str, list[str]], Any]:
    """Preserve hidden choices and unrelated resources; replace the visible standard choices.

    The Control and provider-owned catalog are the only authority inputs.
    These maps still pass the ordinary owner's delegability checks and the
    stored Control addition checks before a Card decision can commit them.
    """
    from .agent_capability_sync import _named_operation_grants, _named_service_outer_operations, _operation_matches

    grants = {resource: set(values) for resource, values in resident.resource_grants.items()}
    operations = {resource: set(values) for resource, values in resident.resource_operations.items()}
    namespaces = set(authority.capabilities.get("named_services", ()))
    namespaces.update(unquote(value.split("/", 1)[0])
                      for value in authority.capabilities.get("named_service_operations", ()) if "/" in value)
    raw = resident.named_service_operations
    if raw.is_unknown:
        raise DescriptorAgentCardRefused("agent_capability_named_selection_review_required")
    named = {resource: {namespace: set(values) for namespace, values in rows.items()}
             for resource, rows in raw.operations.items()} if raw.is_exact else {}
    ceiling_named = control.named_service_operations
    for configured in config.resources:
        resource = configured.resource
        if not isinstance(configured.named_services, Mapping):
            continue
        from .named_service_policy import configured_named_service_operations

        offered = configured_named_service_operations(configured.named_services)
        rows = named.setdefault(resource, {})
        if raw.is_all:
            rows.update({name: set(values) for name, values in offered.items()})
        removed_grants: set[str] = set()
        for namespace in namespaces & set(offered):
            offered_ops = set(offered[namespace])
            visible = children(authority, "named_service_operations", namespace)
            if "named_service_operations" not in authority.capabilities:
                visible = offered_ops
            visible = {operation for operation in offered_ops if any(_operation_matches(want, operation) for want in visible)}
            allowed = (offered_ops if ceiling_named.is_all else
                       set(ceiling_named.operations.get(resource, {}).get(namespace, ())))
            wanted = children(selected, "named_service_operations", namespace)
            if "named_service_operations" not in selected.capabilities:
                wanted = visible
            chosen = {operation for operation in visible & allowed
                      if namespace in selected.capabilities.get("named_services", ())
                      and any(_operation_matches(want, operation) for want in wanted)}
            rows[namespace] = (rows.get(namespace, set()) - visible) | chosen
            by_operation = _named_operation_grants(configured.named_services, namespace)
            for operation in visible:
                removed_grants.update(by_operation.get(operation, ()))
        needed_grants: set[str] = set()
        all_operations: set[str] = set()
        for namespace, chosen in rows.items():
            by_operation = _named_operation_grants(configured.named_services, namespace)
            for operation in chosen:
                needed_grants.update(by_operation.get(operation, ()))
            all_operations.update(chosen)
        grants[resource] = (grants.get(resource, set()) - removed_grants) | needed_grants
        outer = {tool.name for tool in configured.tools or () if tool.name.startswith("named_services_")}
        operations[resource] = (operations.get(resource, set()) - outer) | set(
            _named_service_outer_operations(all_operations, (tool.name for tool in configured.tools or ())))

    from .agent_capability_policy import AGENT_CAPABILITY_METADATA_PROPERTY, AGENT_CAPABILITY_SELECTION_PROPERTY, AgentCapabilityPolicy

    choices: dict[str, tuple[set[str], set[str], bool]] = {}
    for resource in authority.capabilities.get("resources", ()):
        visible = children(authority, "resource_operations", resource)
        chosen = children(selected, "resource_operations", resource)
        if "resource_operations" not in authority.capabilities:
            visible = set(control.resource_operations.get(resource, ()))
        if "resource_operations" not in selected.capabilities:
            chosen = visible
        choices[resource] = (visible, chosen, resource in selected.capabilities.get("resources", ()))
    metadata = dict(control.properties or {}).get(AGENT_CAPABILITY_METADATA_PROPERTY) or {}
    entries = metadata.get("entries") if isinstance(metadata, Mapping) else None
    servers = entries.get("mcp_servers") if isinstance(entries, Mapping) else None
    servers = servers if isinstance(servers, Mapping) else {}
    current = AgentCapabilityPolicy.from_property(resident.properties[AGENT_CAPABILITY_SELECTION_PROPERTY])
    for server in authority.capabilities.get("mcp_servers", ()):
        entry = servers.get(server)
        entry = entry if isinstance(entry, Mapping) else {}
        if entry.get("delegated") is False:
            continue  # no standard Card resource exists for this MCP connection
        resource = str(entry.get("resource") or "")
        if not resource:
            if (server in current.capabilities.get("mcp_servers", ())) != (server in selected.capabilities.get("mcp_servers", ())) \
                    or children(current, "mcp_tools", server) != children(selected, "mcp_tools", server):
                raise DescriptorAgentCardRefused("agent_capability_standard_mapping_refresh_required", retryable=True)
            continue
        visible = children(authority, "mcp_tools", server)
        chosen = children(selected, "mcp_tools", server)
        if "mcp_tools" not in authority.capabilities:
            visible = set(control.resource_operations.get(resource, ()))
        if "mcp_tools" not in selected.capabilities:
            chosen = visible
        enabled = server in selected.capabilities.get("mcp_servers", ())
        prior_visible, prior_chosen, prior_enabled = choices.get(resource, (set(), set(), False))
        choices[resource] = (prior_visible | visible, prior_chosen | (chosen if enabled else set()), prior_enabled or enabled)

    for resource, (visible, chosen, enabled) in choices.items():
        if not enabled:
            grants.pop(resource, None)
            operations.pop(resource, None)
            named.pop(resource, None)
            continue
        configured = config.card_selector_config(resource)
        if configured is None:
            raise DescriptorAgentCardRefused("agent_capability_standard_resource_unavailable")
        allowed = set(control.resource_operations.get(resource, ()))
        chosen &= visible & allowed
        before = operations.get(resource, set())
        operations[resource] = (before - visible) | chosen
        removed = {grant for tool in configured.tools or () if tool.name in visible for grant in tool.grants or ()}
        needed = {grant for tool in configured.tools or () if tool.name in operations[resource] for grant in tool.grants or ()}
        grants[resource] = (grants.get(resource, set()) - removed) | needed

    if not allow_additions:
        # Passive sync carries the owner's stored standard restrictions. A
        # saved inner choice cannot recreate a bridge or claim the owner
        # removed. Creation and an explicit selection save still resolve new
        # authority, subject to the ordinary delegability and Control checks.
        grants = {resource: values & set(resident.resource_grants.get(resource, ()))
                  for resource, values in grants.items()}
        operations = {resource: values & set(resident.resource_operations.get(resource, ()))
                      for resource, values in operations.items()}
        if not raw.is_all:
            named = {resource: {namespace: values & set(raw.operations.get(resource, {}).get(namespace, ()))
                                for namespace, values in rows.items()}
                     for resource, rows in named.items()}

    def ordered(values, held):
        # These dimensions are sets, but the durable Card preserves order.
        # A refresh must not write a revision just to sort an unchanged set.
        return [value for value in held if value in values] + sorted(values - set(held))

    return ({resource: ordered(values, resident.resource_grants.get(resource, ()))
             for resource, values in grants.items() if values},
            {resource: ordered(values, resident.resource_operations.get(resource, ()))
             for resource, values in operations.items() if resource in grants and grants[resource]},
            NamedServiceSelection.exact({resource: {namespace: ordered(values, raw.operations.get(resource, {}).get(namespace, ()))
                                                   for namespace, values in rows.items() if values}
                for resource, rows in named.items() if any(rows.values())}))
