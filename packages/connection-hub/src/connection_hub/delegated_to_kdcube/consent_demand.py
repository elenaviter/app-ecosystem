# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Demand-driven consent bookkeeping.

Which tools a turn needs only becomes clear as the agent works, so consent
raises at the ATTEMPT: the tool invocation that hits an unmet claim records a
consent demand here and the chat renders the scoped banner. The record is one
small user-prop per user+bundle holding the LAST conversation's pending
demands — the next turn's transition check reads it, resolves ONLY those
tools' claims, and announces the ones the user satisfied
(``[CONNECTED ACCOUNTS UPDATE]``). Transition detection matters within one
conversation, so a new conversation simply overwrites the record.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Any, Callable, Mapping, Protocol, Sequence

from connection_hub.delegated_credentials.credential_view import (
    normalize_resource,
    resource_matches,
)

LOGGER = logging.getLogger("kdcube.connections.delegated_to_kdcube")


class UserPropertyStore(Protocol):
    async def get_user_prop(self, key: str, **kwargs: Any) -> Any: ...

    async def set_user_prop(self, key: str, value: Any, **kwargs: Any) -> None: ...

    async def delete_user_prop(self, key: str, **kwargs: Any) -> None: ...


EventSourceFactory = Callable[[Mapping[str, Any]], Any]
StoreFactory = Callable[..., Any]

# One record per user+bundle: {"conversation_id": …, "providers": [group…]}
# where a group is {provider_id, provider_label?, connector_app_id?, resource?,
# claims, tools, named_service_operations?} — the same shape the ANNOUNCE
# composer reads.
PENDING_CONSENT_KEY = "delegated_to_kdcube.blocked_snapshot"

# Hub-addressed registry (per user, under the Connection Hub bundle): every
# open demand with its FULL conversation address, so consent completion in the
# hub can author the granted event back into the right conversation lane.
# Value: {"demands": [{conversation_id, tenant, project, bundle_id, agent_id,
# provider_id, connector_app_id, resource, claims, tool_name, namespace, operation,
# recorded_at}]}.
PENDING_DEMANDS_REGISTRY_KEY = "delegated_to_kdcube.consent_demands"

# Semantic event type. The TRANSPORT lane kind is uniformly "external_event"
# (the shape the react timeline fold renders); the semantic type rides nested
# in payload.event.type, same as user followups/steers.
CONSENT_GRANTED_EVENT_KIND = "connections.consent.granted"
CONSENT_GRANTED_EVENT_TRANSPORT_KIND = "external_event"
CONSENT_GRANTED_EVENT_SOURCE_ID = "connection_hub.consent"


def _namespace(value: Any) -> str:
    return str(value or "").strip().lower().rstrip(":")


def _operation(value: Any) -> str:
    return str(value or "").strip()


def _operation_map(value: Any) -> dict[str, set[str]]:
    if not isinstance(value, Mapping):
        return {}
    normalized: dict[str, set[str]] = {}
    for raw_namespace, raw_operations in value.items():
        namespace = _namespace(raw_namespace)
        if not namespace:
            continue
        if isinstance(raw_operations, str):
            values = [raw_operations]
        elif isinstance(raw_operations, Sequence):
            values = list(raw_operations)
        else:
            values = []
        operations = {_operation(item) for item in values if _operation(item)}
        if operations:
            normalized[namespace] = operations
    return normalized


async def read_pending_consent(
    *,
    user_id: str,
    bundle_id: str,
    conversation_id: str,
    property_store: UserPropertyStore | None = None,
) -> list:
    """The conversation's pending consent-demand groups (empty otherwise).

    User properties use the shared asynchronous Postgres pool."""
    if not user_id or not bundle_id or not conversation_id:
        return []
    if property_store is None:
        return []
    try:
        raw = await property_store.get_user_prop(
            PENDING_CONSENT_KEY,
            user_id=user_id,
            bundle_id=bundle_id,
            default=None,
        )
    except Exception:
        return []
    if not isinstance(raw, dict) or str(raw.get("conversation_id") or "") != conversation_id:
        return []
    providers = raw.get("providers")
    return [g for g in providers if isinstance(g, dict)] if isinstance(providers, list) else []


async def write_pending_consent(
    *,
    user_id: str,
    bundle_id: str,
    conversation_id: str,
    providers: list,
    property_store: UserPropertyStore | None = None,
) -> None:
    if not user_id or not bundle_id or not conversation_id:
        return
    if property_store is None:
        return
    try:
        if providers:
            await property_store.set_user_prop(
                PENDING_CONSENT_KEY,
                {"conversation_id": conversation_id, "providers": providers},
                user_id=user_id,
                bundle_id=bundle_id,
            )
        else:
            await property_store.delete_user_prop(
                PENDING_CONSENT_KEY,
                user_id=user_id,
                bundle_id=bundle_id,
            )
    except Exception:
        LOGGER.debug("pending-consent write unavailable", exc_info=True)


async def record_consent_demand(
    *,
    user_id: str,
    bundle_id: str,
    conversation_id: str,
    provider_id: str,
    connector_app_id: str = "",
    provider_label: str = "",
    claims: list | tuple = (),
    tool_name: str = "",
    resource: str = "",
    namespace: str = "",
    operation: str = "",
    tenant: str = "",
    project: str = "",
    agent_id: str = "",
    connection_hub_bundle_id: str = "",
    property_store: UserPropertyStore | None = None,
) -> bool:
    """Record one attempted tool's consent demand.

    Returns True when this (provider, resource, claims, tool, operation) is NEW for the
    conversation — the caller emits the chat consent event exactly once per
    demand; retries of the same tool in the same conversation stay quiet
    server-side (the banner reducer's signature dedupe covers client replays).

    A demand is a CONVERSATION fact: it exists to raise the banner there and
    to author the granted event back into that lane. A caller with no full
    conversation address (an external MCP attempt is turn-less and
    conversation-less) records nothing — that client's consent loop is the
    structured response + Connection Hub link + retry.
    """
    provider_key = str(provider_id or "").strip()
    tool_key = str(tool_name or "").strip()
    claim_list = [str(c).strip() for c in (claims or []) if str(c or "").strip()]
    resource_key = normalize_resource(resource)
    namespace_key = _namespace(namespace)
    operation_key = _operation(operation) if namespace_key else ""
    if not provider_key or not tool_key:
        return False
    if not str(user_id or "").strip() or not str(bundle_id or "").strip() or not str(conversation_id or "").strip():
        return False
    pending = await read_pending_consent(
        user_id=user_id,
        bundle_id=bundle_id,
        conversation_id=conversation_id,
        property_store=property_store,
    )
    for group in pending:
        if str(group.get("provider_id") or "") != provider_key:
            continue
        if normalize_resource(group.get("resource")) != resource_key:
            continue
        known_tools = {str(t) for t in (group.get("tools") or [])}
        known_claims = {str(c) for c in (group.get("claims") or [])}
        known_operations = _operation_map(group.get("named_service_operations"))
        operation_known = (
            not operation_key
            or operation_key in known_operations.get(namespace_key, set())
        )
        if (
            tool_key in known_tools
            and set(claim_list) <= known_claims
            and operation_known
        ):
            return False
        group["tools"] = sorted(known_tools | {tool_key})
        group["claims"] = sorted(known_claims | set(claim_list))
        if operation_key:
            known_operations.setdefault(namespace_key, set()).add(operation_key)
            group["named_service_operations"] = {
                key: sorted(values) for key, values in known_operations.items()
            }
        if provider_label and not group.get("provider_label"):
            group["provider_label"] = provider_label
        await write_pending_consent(
            user_id=user_id,
            bundle_id=bundle_id,
            conversation_id=conversation_id,
            providers=pending,
            property_store=property_store,
        )
        await _register_demand_address(
            user_id=user_id,
            bundle_id=bundle_id,
            conversation_id=conversation_id,
            provider_id=provider_key,
            connector_app_id=str(connector_app_id or "").strip(),
            claims=claim_list,
            tool_name=tool_key,
            resource=resource_key,
            namespace=namespace_key,
            operation=operation_key,
            tenant=tenant,
            project=project,
            agent_id=agent_id,
            connection_hub_bundle_id=connection_hub_bundle_id,
            property_store=property_store,
        )
        return True
    group = {
        "provider_id": provider_key,
        "provider_label": str(provider_label or "").strip(),
        "connector_app_id": str(connector_app_id or "").strip(),
        "claims": sorted(set(claim_list)),
        "tools": [tool_key],
    }
    if resource_key:
        group["resource"] = resource_key
    if operation_key:
        group["named_service_operations"] = {namespace_key: [operation_key]}
    pending.append(group)
    await write_pending_consent(
        user_id=user_id,
        bundle_id=bundle_id,
        conversation_id=conversation_id,
        providers=pending,
        property_store=property_store,
    )
    await _register_demand_address(
        user_id=user_id,
        bundle_id=bundle_id,
        conversation_id=conversation_id,
        provider_id=provider_key,
        connector_app_id=str(connector_app_id or "").strip(),
        claims=claim_list,
        tool_name=tool_key,
        resource=resource_key,
        namespace=namespace_key,
        operation=operation_key,
        tenant=tenant,
        project=project,
        agent_id=agent_id,
        connection_hub_bundle_id=connection_hub_bundle_id,
        property_store=property_store,
    )
    return True


def _hub_bundle_id(value: str = "") -> str:
    if str(value or "").strip():
        return str(value).strip()
    from connection_hub.delegated_to_kdcube.models import (
        CONNECTION_HUB_BUNDLE_ID,
    )

    return CONNECTION_HUB_BUNDLE_ID


async def _register_demand_address(
    *,
    user_id: str,
    bundle_id: str,
    conversation_id: str,
    provider_id: str,
    connector_app_id: str,
    claims: list,
    tool_name: str,
    resource: str,
    namespace: str,
    operation: str,
    tenant: str,
    project: str,
    agent_id: str,
    connection_hub_bundle_id: str,
    property_store: UserPropertyStore | None = None,
) -> None:
    """Append this demand (with its full conversation address) to the
    hub-addressed registry, so consent completion can author the granted
    event back into the conversation. One entry per (conversation, provider,
    resource, tool, operation). Best-effort. Only a FULL address registers:
    without a conversation there is no lane to author the granted event into."""
    if not str(user_id or "").strip() or not str(conversation_id or "").strip():
        return
    if property_store is None:
        return
    try:
        import time

        hub_bundle = _hub_bundle_id(connection_hub_bundle_id)
        raw = await property_store.get_user_prop(
            PENDING_DEMANDS_REGISTRY_KEY,
            user_id=user_id,
            bundle_id=hub_bundle,
            default=None,
        )
        demands = list(raw.get("demands") or []) if isinstance(raw, dict) else []
        for entry in demands:
            if (
                isinstance(entry, dict)
                and str(entry.get("conversation_id") or "") == conversation_id
                and str(entry.get("provider_id") or "") == provider_id
                and str(entry.get("tool_name") or "") == tool_name
                and normalize_resource(entry.get("resource")) == resource
                and _namespace(entry.get("namespace")) == namespace
                and _operation(entry.get("operation")) == operation
            ):
                entry["claims"] = sorted({*(entry.get("claims") or []), *claims})
                break
        else:
            demands.append({
                "conversation_id": conversation_id,
                "tenant": str(tenant or "").strip(),
                "project": str(project or "").strip(),
                "bundle_id": bundle_id,
                "agent_id": str(agent_id or "").strip(),
                "provider_id": provider_id,
                "connector_app_id": connector_app_id,
                "claims": sorted(set(claims)),
                "tool_name": tool_name,
                "resource": resource,
                "namespace": namespace,
                "operation": operation,
                "recorded_at": time.time(),
            })
        await property_store.set_user_prop(
            PENDING_DEMANDS_REGISTRY_KEY,
            {"demands": demands},
            user_id=user_id,
            bundle_id=hub_bundle,
        )
    except Exception:
        LOGGER.debug("consent demand address registration unavailable", exc_info=True)


def consent_granted_event_text(
    *,
    provider_label: str,
    claims: list,
    tools: list,
    namespace: str = "",
    operation: str = "",
) -> str:
    """The timeline-facing sentence of the granted event."""
    claim_text = ", ".join(claims)
    tool_text = ", ".join(sorted({str(t).rsplit(".", 1)[-1] for t in tools if str(t or "").strip()}))
    if namespace and operation and claim_text:
        approval = (
            f"The user approved {provider_label} access for operation "
            f"{operation} in {namespace} and claims ({claim_text})."
        )
    elif namespace and operation:
        approval = (
            f"The user approved {provider_label} access for operation "
            f"{operation} in {namespace}."
        )
    else:
        approval = f"The user approved {provider_label} access ({claim_text})."
    return (
        f"{approval} "
        f"The tools that needed it ({tool_text}) are usable now. "
        "The call this approval unblocked has NOT run — approving never "
        "re-runs it. Run it again only if it is still in the user's focus, "
        "and confirm its own result before reporting the outcome; otherwise "
        "ask before re-firing."
    )


async def author_consent_granted_events(
    *,
    redis: Any,
    user_id: str,
    provider_id: str,
    granted_claims: list | tuple,
    granted_named_service_operations: Mapping[str, Sequence[str]] | str | None = None,
    granted_resource: str = "",
    connector_app_id: str = "",
    account_id: str = "",
    connection_hub_bundle_id: str = "",
    source_factory: EventSourceFactory | None = None,
    property_store: UserPropertyStore | None = None,
) -> int:
    """Author `connections.consent.granted` conversation events for every
    pending demand this grant satisfies — the closing symmetry of
    demand-driven consent (the ask was an event; the grant is one too).

    Authored through the INGRESS EVENT MECHANISM: event inception writes a
    ConversationExternalEvent into the per-conversation event lane via
    `RedisConversationExternalEventSource.publish` — the same primitive
    `ingress_core._publish_external_event_batch` uses for authored events
    (the hub process holds the same redis lane client, so the write needs no
    HTTP hop). The external-event timeline protocol handles the rest: a LIVE
    turn folds the event through the lane watcher; with no live turn it
    resides in the lane as passive context the next user-initiated turn folds
    in. It is NOT a reactive event and is never modeled as one: published
    WITHOUT a task payload, the promoter permanently acks it
    (`claim_next_promotable` skips `task_payload is None`), so it can never
    start anything resembling a turn and the `@on_reactive_event` chat-handler
    surface plays no role.

    One event per demand entry: authored entries leave the registry AND their
    tools leave the conversation's pending snapshot (so the turn-start
    announce stays silent — the event is the stronger, factual record).
    Entries whose publish fails stay recorded; the announce covers them as
    the fallback. Returns the number of events authored."""
    provider_key = str(provider_id or "").strip()
    granted = {str(c).strip() for c in (granted_claims or []) if str(c or "").strip()}
    all_operations_granted = (
        isinstance(granted_named_service_operations, str)
        and granted_named_service_operations.strip() == "*"
    )
    granted_operations = _operation_map(granted_named_service_operations)
    granted_resource_key = normalize_resource(granted_resource)
    clean_user = str(user_id or "").strip()
    if (
        not provider_key
        or not clean_user
        or (not granted and not all_operations_granted and not granted_operations)
    ):
        return 0
    if property_store is None or source_factory is None:
        LOGGER.warning(
            "[delegated.consent] granted event not authored: persistence or event-source port is not configured"
        )
        return 0
    try:
        hub_bundle = _hub_bundle_id(connection_hub_bundle_id)
        raw = await property_store.get_user_prop(
            PENDING_DEMANDS_REGISTRY_KEY,
            user_id=clean_user,
            bundle_id=hub_bundle,
            default=None,
        )
        demands = list(raw.get("demands") or []) if isinstance(raw, dict) else []
        if not demands:
            return 0
        remaining: list = []
        published_entries: list[dict] = []
        authored = 0
        for entry in demands:
            if not isinstance(entry, dict):
                continue
            entry_claims = {str(c) for c in (entry.get("claims") or [])}
            entry_resource = normalize_resource(entry.get("resource"))
            entry_namespace = _namespace(entry.get("namespace"))
            entry_operation = _operation(entry.get("operation"))
            has_requirement = bool(entry_claims or (entry_namespace and entry_operation))
            claims_match = not entry_claims or entry_claims <= granted
            operation_match = (
                not entry_operation
                or (
                    bool(entry_namespace)
                    and (
                        all_operations_granted
                        or entry_operation
                        in granted_operations.get(entry_namespace, set())
                    )
                )
            )
            resource_match = (
                not entry_resource
                or (
                    bool(granted_resource_key)
                    and resource_matches(granted_resource_key, entry_resource)
                )
            )
            # Connector narrows the match when both sides name one. For a
            # per-agent grant the connector slot carries the agent's
            # `kdcube-agent:<app>:<agent>` client id, so granting one agent
            # never closes another agent's demand on the same claims.
            entry_connector = str(entry.get("connector_app_id") or "").strip()
            caller_connector = str(connector_app_id or "").strip()
            matches = (
                str(entry.get("provider_id") or "") == provider_key
                and has_requirement
                and resource_match
                and claims_match
                and operation_match
                and (
                    not entry_connector
                    or not caller_connector
                    or entry_connector == caller_connector
                )
            )
            if not matches:
                remaining.append(entry)
                continue
            conversation_id = str(entry.get("conversation_id") or "")
            tenant = str(entry.get("tenant") or "")
            project = str(entry.get("project") or "")
            tool_name = str(entry.get("tool_name") or "")
            if not conversation_id.strip():
                # No conversation address means no lane to author into — the
                # grant is complete without an event (the caller's consent
                # loop was response + link + retry). Drop the entry.
                LOGGER.info(
                    "[delegated.consent] demand without conversation address dropped on grant: provider=%s tool=%s user=%s",
                    provider_key, tool_name, clean_user,
                )
                continue
            try:
                source = source_factory(entry)
                provider_label = (
                    "KDCube"
                    if provider_key == "kdcube"
                    else (provider_key[:1].upper() + provider_key[1:])
                )
                claims_list = sorted(entry_claims)
                event_text = consent_granted_event_text(
                    provider_label=provider_label,
                    claims=claims_list,
                    tools=[tool_name],
                    namespace=entry_namespace,
                    operation=entry_operation,
                )
                grant_facts = {
                    "provider_id": provider_key,
                    "connector_app_id": str(entry.get("connector_app_id") or connector_app_id or ""),
                    "claims": claims_list,
                    "account_id": str(account_id or ""),
                    "tools": [tool_name],
                }
                if entry_resource:
                    grant_facts["resource"] = entry_resource
                if entry_namespace and entry_operation:
                    grant_facts.update({
                        "namespace": entry_namespace,
                        "operation": entry_operation,
                        "named_service_operations": {
                            entry_namespace: [entry_operation]
                        },
                    })
                event_ts = (
                    _dt.datetime.now(_dt.timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
                await source.publish(
                    # Transport kind is uniformly "external_event"; the react
                    # timeline fold renders exactly this shape into a visible
                    # block (a user followup is the reference behavior). The
                    # semantic type rides in payload.event.type.
                    kind=CONSENT_GRANTED_EVENT_TRANSPORT_KIND,
                    source="connection_hub",
                    event_source_id=CONSENT_GRANTED_EVENT_SOURCE_ID,
                    text=event_text,
                    payload={
                        "text": event_text,
                        "event": {
                            "type": CONSENT_GRANTED_EVENT_KIND,
                            "event_source_id": CONSENT_GRANTED_EVENT_SOURCE_ID,
                            "reactive": False,
                            "timestamp": event_ts,
                            # The nested payload.event carries the model-facing
                            # sentence and the grant facts; the timeline fold
                            # surfaces it as the event block's `ret` body.
                            "payload": {
                                "mime": "text/markdown",
                                "event": {
                                    "text": event_text,
                                    **grant_facts,
                                },
                            },
                        },
                        **grant_facts,
                    },
                    # Passive by construction: no task payload means the
                    # promoter acks the event; it can never start a turn.
                    task_payload=None,
                )
                authored += 1
                published_entries.append(entry)
                LOGGER.info(
                    "[delegated.consent] granted event authored: conversation=%s provider=%s claims=%s tool=%s user=%s",
                    conversation_id, provider_key, ",".join(claims_list), tool_name, clean_user,
                )
            except Exception:
                LOGGER.warning(
                    "[delegated.consent] granted event publish failed (announce fallback keeps the record): conversation=%s provider=%s",
                    conversation_id, provider_key, exc_info=True,
                )
                remaining.append(entry)
        dropped_snapshot_tools: set[tuple[str, str, str, str]] = set()
        for entry in published_entries:
            drop_key = (
                str(entry.get("bundle_id") or ""),
                str(entry.get("conversation_id") or ""),
                str(entry.get("provider_id") or ""),
                str(entry.get("tool_name") or ""),
            )
            if drop_key in dropped_snapshot_tools:
                continue
            if any(
                isinstance(other, dict)
                and (
                    str(other.get("bundle_id") or ""),
                    str(other.get("conversation_id") or ""),
                    str(other.get("provider_id") or ""),
                    str(other.get("tool_name") or ""),
                ) == drop_key
                for other in remaining
            ):
                continue
            dropped_snapshot_tools.add(drop_key)
            await _drop_tools_from_snapshot(
                user_id=clean_user,
                bundle_id=drop_key[0],
                conversation_id=drop_key[1],
                provider_id=drop_key[2],
                tools=[drop_key[3]],
                property_store=property_store,
            )
        if remaining:
            await property_store.set_user_prop(
                PENDING_DEMANDS_REGISTRY_KEY,
                {"demands": remaining},
                user_id=clean_user,
                bundle_id=hub_bundle,
            )
        else:
            await property_store.delete_user_prop(
                PENDING_DEMANDS_REGISTRY_KEY,
                user_id=clean_user,
                bundle_id=hub_bundle,
            )
        return authored
    except Exception:
        LOGGER.debug("consent granted authoring unavailable", exc_info=True)
        return 0


async def _drop_tools_from_snapshot(
    *,
    user_id: str,
    bundle_id: str,
    conversation_id: str,
    provider_id: str,
    tools: list,
    property_store: UserPropertyStore | None = None,
) -> None:
    """Remove event-covered tools from the conversation's pending snapshot so
    the turn-start announce stays silent for them (the authored event is the
    stronger record)."""
    try:
        pending = await read_pending_consent(
            user_id=user_id,
            bundle_id=bundle_id,
            conversation_id=conversation_id,
            property_store=property_store,
        )
        if not pending:
            return
        drop = {str(t) for t in tools}
        next_groups: list = []
        for group in pending:
            if str(group.get("provider_id") or "") != provider_id:
                next_groups.append(group)
                continue
            kept = [t for t in (group.get("tools") or []) if str(t) not in drop]
            if kept:
                next_groups.append({**group, "tools": kept})
        await write_pending_consent(
            user_id=user_id,
            bundle_id=bundle_id,
            conversation_id=conversation_id,
            providers=next_groups,
            property_store=property_store,
        )
    except Exception:
        LOGGER.debug("pending snapshot trim unavailable", exc_info=True)


async def announce_consent_demand(
    *,
    comm: Any = None,
    payload: Any,
    provider_id: str,
    connector_app_id: str = "",
    claims: list | tuple = (),
    tool_name: str = "",
    resource: str = "",
    namespace: str = "",
    operation: str = "",
    identity: Any = None,
    connection_hub_bundle_id: str = "",
    identity_provider: Callable[[], Mapping[str, Any] | None] | None = None,
    communicator_provider: Callable[[], Any] | None = None,
    agent_id: str = "",
    property_store: UserPropertyStore | None = None,
) -> bool:
    """Record one attempted tool's consent demand and emit the scoped chat
    consent event ONCE per (provider, resource, claims, tool, operation) demand
    per conversation.

    ``tool_name`` is the entry as the user sees it in the composer menu
    (``alias.tool`` for python tools, the bare namespace for named-service
    tools) — the banner's turn-off spotlight targets exactly that. Identity
    (user / bundle / conversation) comes from the bound request context;
    ``comm`` defaults to the context communicator. Best-effort: never raises.
    """
    try:
        if identity is None:
            identity = identity_provider() if identity_provider is not None else {}
        identity = identity or {}
        user_id = str(identity.get("user_id") or "").strip()
        bundle_id = str(identity.get("bundle_id") or "").strip()
        conversation_id = str(identity.get("conversation_id") or "").strip()
        provider_key = str(provider_id or "").strip()
        if not user_id or not bundle_id or not conversation_id:
            LOGGER.warning(
                "[delegated.consent] demand has NO full conversation address "
                "(user=%r bundle=%r conversation=%r provider=%s tool=%s) — "
                "nothing recorded; only a direct event emit can reach the chat",
                user_id, bundle_id, conversation_id, provider_key, tool_name,
            )
        newly_recorded = await record_consent_demand(
            user_id=user_id,
            bundle_id=bundle_id,
            conversation_id=conversation_id,
            provider_id=provider_key,
            connector_app_id=str(connector_app_id or "").strip(),
            provider_label=(provider_key[:1].upper() + provider_key[1:]) if provider_key else "",
            claims=list(claims or []),
            tool_name=str(tool_name or "").strip(),
            resource=normalize_resource(resource),
            namespace=_namespace(namespace),
            operation=_operation(operation),
            tenant=str(identity.get("tenant_id") or "").strip(),
            project=str(identity.get("project_id") or "").strip(),
            agent_id=agent_id,
            connection_hub_bundle_id=connection_hub_bundle_id,
            property_store=property_store,
        )
        if not newly_recorded:
            return False
        communicator = (
            comm
            if comm is not None
            else (communicator_provider() if communicator_provider is not None else None)
        )
        event = getattr(communicator, "event", None) if communicator is not None else None
        if not callable(event):
            return False
        result = event(
            agent="connection-hub",
            type="chat.step",
            route="chat.step",
            title="Account consent needed",
            step="delegated_to_kdcube.consent",
            data=dict(payload or {}),
            status="completed",
            broadcast=False,
        )
        if hasattr(result, "__await__"):
            await result
        return True
    except Exception:
        LOGGER.debug("consent demand announce unavailable", exc_info=True)
        return False


async def claim_coverage_for_policies(
    *,
    user_id: str,
    policies: list,
    connection_hub_bundle_id: str = "",
    store_factory: StoreFactory | None = None,
) -> dict:
    """READ-ONLY per-tool claim coverage for a picker UI.

    Answers "which of this tool's declared claims does the user's connected
    account set already hold" from the account records alone — zero credential
    reads, zero consent events, zero health probes (a menu render must ask
    nothing). A claim counts covered when ANY connected account of the
    requirement's provider (and connector app, when named) holds it; the
    consent plan and the tool attempt stay the authorities on health and
    account choice.

    Returns {tool_name: {provider_id, connector_app_id, claims, unmet,
    covered}} for every policy carrying connected-account requirements.
    """
    from connection_hub.delegated_to_kdcube.models import (
        CONNECTION_HUB_BUNDLE_ID,
    )
    clean_user = str(user_id or "").strip()
    coverage: dict = {}
    if not clean_user or store_factory is None:
        return coverage
    store = store_factory(
        user_id=clean_user,
        bundle_id=str(connection_hub_bundle_id or "").strip() or CONNECTION_HUB_BUNDLE_ID,
    )
    try:
        # Await natively: the store offloads its synchronous storage client to
        # a worker thread itself (never a nested event loop over shared
        # clients).
        accounts = await store.list_accounts()
    except Exception:
        LOGGER.debug("claim coverage account read unavailable", exc_info=True)
        return coverage
    for policy in policies or []:
        tool_name = str(getattr(policy, "tool_name", "") or "").strip()
        requirements = list(getattr(policy, "connected_accounts", ()) or ())
        if not tool_name or not requirements:
            continue
        declared: list = []
        unmet: list = []
        provider_id = ""
        connector_app_id = ""
        for requirement in requirements:
            provider_id = provider_id or str(getattr(requirement, "provider_id", "") or "")
            connector_app_id = connector_app_id or str(getattr(requirement, "connector_app_id", "") or "")
            req_provider = str(getattr(requirement, "provider_id", "") or "")
            req_connector = str(getattr(requirement, "connector_app_id", "") or "")
            eligible = [
                account for account in accounts
                if account.provider_id == req_provider
                and account.connected
                and (not req_connector or not account.connector_app_id or account.connector_app_id == req_connector)
            ]
            for claim in getattr(requirement, "claims", ()) or ():
                claim_key = str(claim or "").strip()
                if not claim_key or claim_key in declared:
                    continue
                declared.append(claim_key)
                if not any(account.allows(claim_key) for account in eligible):
                    unmet.append(claim_key)
        coverage[tool_name] = {
            "provider_id": provider_id,
            "connector_app_id": connector_app_id,
            "claims": declared,
            "unmet": unmet,
            "covered": not unmet,
        }
    return coverage


def pending_consent_delta(previous: list, current: list) -> list:
    """Provider groups whose tools left the pending set (consent satisfied)."""
    pending_now: dict = {}
    for group in current or []:
        if isinstance(group, dict):
            key = str(group.get("provider_id") or "")
            pending_now.setdefault(key, set()).update(
                str(t) for t in (group.get("tools") or []) if str(t or "").strip()
            )
    satisfied: list = []
    for group in previous or []:
        if not isinstance(group, dict):
            continue
        key = str(group.get("provider_id") or "")
        freed = [
            str(t) for t in (group.get("tools") or [])
            if str(t or "").strip() and str(t) not in pending_now.get(key, set())
        ]
        if freed:
            satisfied.append({**group, "tools": freed})
    return satisfied
