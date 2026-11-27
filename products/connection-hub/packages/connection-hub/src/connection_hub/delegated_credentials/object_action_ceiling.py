"""One ceiling rule for named-service ``object.action``.

| descriptor for a namespace | action ceiling |
| --- | --- |
| ``object.action`` only | every action of the namespace in the Connection Hub catalog, later ones included |
| ``object.action`` and a list | every action in the catalog; the list changes nothing |
| a list only | exactly the listed actions |
| an administrator override or Control edit | its explicit list, inside the rows above |

A descriptor Control holds the actions twice: in its standard part
(``named_service_operations``) and in its capability ceiling
(``kdcube.agent_capability_authority``). Both read ``object.action`` by this
rule, so the Card editor, the consent review, the base, the picker and the sync
agree.
"""

from __future__ import annotations

from typing import Any, Mapping
from urllib.parse import quote, unquote

from connection_hub.delegated_credentials.agent_capability_policy import AgentCapabilityPolicy
from connection_hub.delegated_credentials.named_service_policy import configured_named_service_operations

_OBJECT_ACTION = "object.action"
_NAMED_SERVICE_OPERATIONS_CATEGORY = "named_service_operations"


def _capability_member(parent: str, child: str) -> str:
    return f"{quote(parent, safe='-._~@')}/{quote(child, safe='-._~@')}"


def expand_object_action_ceiling(
    policy: AgentCapabilityPolicy | None,
    catalog_config: Any,
) -> AgentCapabilityPolicy | None:
    """Apply the one ceiling rule for named-service actions to a capability policy.

    A parent ``<namespace>/object.action`` names every action the active
    Connection Hub catalog offers for that namespace, today's and later ones,
    with or without an explicit list beside it. Without the parent, the listed
    ``object.action.<name>`` keys are the ceiling as they stand. The standard
    part of a descriptor Control is resolved by the same rule
    (``_descriptor_standard_maps``), so both parts of the Control agree and an
    exact-key comparison (picker states, a picker save, the base after a Card
    Save) holds against the concrete action keys.
    """

    if policy is None:
        return None
    values = tuple(policy.capabilities.get(_NAMED_SERVICE_OPERATIONS_CATEGORY, ()))
    parents: set[str] = set()
    for value in values:
        namespace, separator, operation = str(value or "").partition("/")
        if separator and unquote(operation) == _OBJECT_ACTION:
            parents.add(unquote(namespace).strip().lower().rstrip(":"))
    if not parents:
        return policy
    expanded = set(values)
    for configured in getattr(catalog_config, "resources", ()) or ():
        named_services = getattr(configured, "named_services", None)
        if not isinstance(named_services, Mapping):
            continue
        for namespace, operations in configured_named_service_operations(named_services).items():
            if namespace not in parents:
                continue
            expanded.update(
                _capability_member(namespace, operation)
                for operation in operations
                if str(operation).startswith(f"{_OBJECT_ACTION}.")
            )
    if expanded == set(values):
        return policy
    return AgentCapabilityPolicy(
        resource=policy.resource,
        capabilities={
            **dict(policy.capabilities),
            _NAMED_SERVICE_OPERATIONS_CATEGORY: tuple(sorted(expanded)),
        },
    )


def selection_matches(selected: set[str] | frozenset[str]):
    """How a user's chosen operations match offered ones in one namespace.

    Beside a listed ``object.action.<name>`` the bare parent matches only the
    action call itself; without a listed action it covers every action.
    """

    lists_actions = any(str(value).startswith(f"{_OBJECT_ACTION}.") for value in selected)

    def matches(wanted: str, operation: str) -> bool:
        if wanted == "*" or operation == wanted:
            return True
        if lists_actions and wanted == _OBJECT_ACTION:
            return False
        return operation.startswith(f"{wanted}.")

    return matches


def expand_selected_object_action(
    policy: AgentCapabilityPolicy | None,
    catalog_config: Any,
) -> AgentCapabilityPolicy | None:
    """Read ``object.action`` in a user's selection.

    A selection lists the actions it chose. Beside a listed action the bare
    parent only keeps the action call open; it never brings back an action the
    user left out. Only a namespace whose selection lists no action at all
    reads the parent as every action (a descriptor that declares only the
    parent offers no list to choose from).
    """

    if policy is None:
        return None
    listed: set[str] = set()
    for value in policy.capabilities.get(_NAMED_SERVICE_OPERATIONS_CATEGORY, ()):
        namespace, separator, operation = str(value or "").partition("/")
        if separator and unquote(operation).startswith(f"{_OBJECT_ACTION}."):
            listed.add(unquote(namespace).strip().lower().rstrip(":"))
    if not listed:
        return expand_object_action_ceiling(policy, catalog_config)
    kept = tuple(
        value for value in policy.capabilities.get(_NAMED_SERVICE_OPERATIONS_CATEGORY, ())
        if not (unquote(str(value).partition("/")[2]) == _OBJECT_ACTION
                and unquote(str(value).partition("/")[0]).strip().lower().rstrip(":") in listed)
    )
    narrowed = AgentCapabilityPolicy(resource=policy.resource, capabilities={
        **dict(policy.capabilities), _NAMED_SERVICE_OPERATIONS_CATEGORY: kept})
    expanded = expand_object_action_ceiling(narrowed, catalog_config)
    if expanded is None:
        return policy
    return AgentCapabilityPolicy(resource=policy.resource, capabilities={
        **dict(expanded.capabilities),
        _NAMED_SERVICE_OPERATIONS_CATEGORY: tuple(sorted(
            set(expanded.capabilities.get(_NAMED_SERVICE_OPERATIONS_CATEGORY, ()))
            | set(policy.capabilities.get(_NAMED_SERVICE_OPERATIONS_CATEGORY, ())))),
    })


def _is_object_action_member(value: str) -> bool:
    _, separator, operation = str(value or "").partition("/")
    operation = unquote(operation)
    return bool(separator) and (
        operation == _OBJECT_ACTION or operation.startswith(f"{_OBJECT_ACTION}.")
    )


def bound_ceiling_to_standard_part(
    policy: AgentCapabilityPolicy | None,
    named_service_operations: Any,
) -> AgentCapabilityPolicy | None:
    """Keep the capability ceiling's named-service operations inside the standard part.

    Both parts of a descriptor Control describe one ceiling. Without an
    administrator override they already agree once ``object.action`` is
    expanded; with one, the override is an explicit list that narrows the
    standard part, and this narrows the capability ceiling to the same list.
    Only ``object.action`` and ``object.action.<name>`` keys are narrowed; any
    other operation stays as the descriptor allows it.
    """

    if policy is None or _NAMED_SERVICE_OPERATIONS_CATEGORY not in policy.capabilities:
        return policy
    if not getattr(named_service_operations, "is_exact", False):
        return policy
    allowed: set[str] = set()
    for namespaces in named_service_operations.operations.values():
        for namespace, operations in namespaces.items():
            name = str(namespace).strip().lower().rstrip(":")
            for operation in operations:
                allowed.add(_capability_member(name, str(operation)))
                # The parent stays while the namespace keeps any action: the SDK
                # reads it as the switch for the action call itself.
                if str(operation).startswith(f"{_OBJECT_ACTION}."):
                    allowed.add(_capability_member(name, _OBJECT_ACTION))
    values = tuple(policy.capabilities.get(_NAMED_SERVICE_OPERATIONS_CATEGORY, ()))
    # Only object.action keys follow this rule. Other operations a descriptor
    # allows may be served outside the Connection Hub catalog (the SDK's
    # object.host_file, for one) and never appear in the standard part.
    kept = tuple(
        value for value in values
        if not _is_object_action_member(value) or value in allowed
    )
    if kept == values:
        return policy
    return AgentCapabilityPolicy(
        resource=policy.resource,
        capabilities={**dict(policy.capabilities), _NAMED_SERVICE_OPERATIONS_CATEGORY: kept},
    )


__all__ = ["bound_ceiling_to_standard_part", "expand_object_action_ceiling"]
