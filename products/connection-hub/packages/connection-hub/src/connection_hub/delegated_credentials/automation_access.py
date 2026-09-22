# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""User-created delegated access credentials for automations.

This module is the SDK-owned backend for the Connection Hub "Delegated Access"
surface. It deliberately reuses the delegated-client credential model used by
OAuth/MCP connectors:

- the approving platform subject remains the grantor;
- the issued bearer belongs to an ``integration:automation:*`` subject;
- grants are narrowed through the platform authority inventory;
- token metadata is bound in ``GrantStore`` so managed surfaces can enforce the
  selected grants/operations.

The Connection Hub bundle should only adapt UI operations to this service.
"""

from __future__ import annotations

import dataclasses

import copy
import hashlib
import json

import secrets
import time
from dataclasses import dataclass, field
from dataclasses import replace as replace_fields
from typing import Any, Awaitable, Callable, Iterable, Mapping

from connection_hub.authority_inventory import (
    AuthorityGrantInventory,
    PlatformAuthorityInventoryProvider,
    platform_identity_from_user,
    selected_delegation_edge,
)
from connection_hub.authority_projection import (
    authority_has_platform_privilege,
)
from connection_hub.delegated_credentials.oauth.authority import (
    build_delegated_client_credential,
)
from connection_hub.delegated_credentials.oauth.config import (
    OAuthDelegatedClientConfig,
    oauth_delegated_config_from_connections,
)
from connection_hub.delegated_credentials.oauth.grants import (
    ACCESS_TOKEN_TTL_SECONDS,
    integration_subject,
    mint_delegated_client_access_token,
)
from connection_hub.delegated_credentials.oauth.clients import (
    client_uses_full_card_catalog,
    normalize_public_client_metadata,
)
from connection_hub.delegated_credentials.oauth.store import (
    GrantStore,
)
from connection_hub.delegated_credentials.named_service_policy import (
    as_string_list,
    boundary_permits_operation,
    clean_text,
    configured_named_service_operations,
    merge_named_service_configs,
    named_service_policy_for_resource,
    narrow_named_service_config,
    operation_grants as _named_service_operation_grants,
)
from connection_hub.delegated_credentials.application_operation_policy import (
    APPLICATION_API_RESOURCE,
    APPLICATION_OPERATIONS_PROPERTY,
    APPLICATION_OPERATIONS_SCHEMA_V2,
    ApplicationOperationPolicyError,
    ApplicationOperationRolePolicy,
    application_operation_role_policy,
    platform_role_allowed_by,
    validate_application_operation_role_policy,
)
from connection_hub.delegated_credentials.agent_capability_control import (
    preserve_descriptor_acceptance,
)
from connection_hub.delegated_credentials.resource_operations import (
    normalize_resource_operations,
    operation_union,
    project_legacy_operations,
    resolve_declared_resource,
    resolve_declared_resource_keys,
)
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CREDENTIALLESS_CARD_SOURCE,
    CONTROL_COMPOSITION_AND,
    CONTROL_COMPOSITIONS,
    NAMED_SERVICE_OPERATIONS_ALL,
    CardAuthority,
    CardCredentialHandles,
    CardRecordError,
    ControlCardBinding,
    NamedServiceSelection,
    authority_is_credentialless,
)
from connection_hub.delegated_credentials.controls.effective import (
    ControlCardMismatch,
    effective_card_authority,
)
from connection_hub.delegated_credentials.conversation_target_policy import (
    conversation_targets,
)
from connection_hub.delegated_credentials.controls.model import (
    ControlCardError,
    control_card_id_for_issuer,
    new_credentialless_card,
)
from connection_hub.delegated_credentials.controls.snapshot import (
    CONTROL_SNAPSHOT_PROPERTY,
    control_snapshot_is_exact,
    control_snapshot_refusal,
    fail_closed_control_snapshot,
    materialize_control_snapshot,
    reviewed_control_snapshot_properties,
)
from connection_hub.delegated_credentials.cards.identity import (
    CARD_KINDS,
    CARD_KIND_AGENT,
    CARD_KIND_AUTOMATION,
    CARD_KIND_CONNECTOR,
    CARD_KIND_CONTROL,
    ResidentCallerProfile,
    is_resident_client_id,
    legacy_resident_access_id,
    stable_automation_access_id,
    stable_card_access_id,
    stable_connector_access_id,
    stable_resident_access_id,
)
from connection_hub.delegated_credentials.cards.read_model import (
    DelegatedCardView,
    build_card_view,
    compatible_resource_offers,
)
from connection_hub.delegated_credentials.secret_resources import (
    SecretResource,
    SecretResourceError,
    validate_secret_card_resource,
)
from connection_hub.delegated_credentials.catalog.descriptors import (
    RESOURCE_KIND_CATALOG,
    ROW_ATTR_KIND,
    ROW_ATTR_PROVIDER,
    ResourceAcceptance,
    ResourceAcceptanceError,
    next_resource_acceptance,
    parse_resource_acceptance,
)
from connection_hub.delegated_credentials.cards.cache import (
    DelegatedCardRuntimeCache,
)
from connection_hub.delegated_credentials.cards.store import (
    subject_hash_for,
)
from connection_hub.delegated_credentials.cards.resolver import (
    CardUnavailable,
)
from connection_hub.delegated_credentials.cards.service import (
    CardCommitFailed,
    CardConflict,
    CardServingUnavailable,
)
from connection_hub.delegated_credentials.catalog.resolver import (
    CatalogUnavailable,
)
from connection_hub.delegated_credentials.catalog.drift import (
    card_drift,
    card_resource_states,
    drift_unavailable,
    selected_named_service_operations,
)
from connection_hub.delegated_credentials.catalog.reconcile import (
    reconcile_selection,
)
from connection_hub.delegated_credentials.cards.migration import (
    MIGRATION_CONFIRMATION_REQUIRED,
    pre_migration_ambiguity,
)
from connection_hub.delegated_credentials.catalog.authorization import (
    CAPABILITY_NAMED_SERVICE_OPERATION,
    CAPABILITY_RESOURCE_CLAIM,
    ActiveCatalogCapabilities,
    CapabilityRequest,
    CardProvenance,
    authorize_current_capability,
    capability_denial,
    card_boundary_denial,
)
from connection_hub.named_service_boundary import (
    NamedServiceBoundaryCatalog,
)
AUTOMATION_ACCESS_SCHEMA = "connection_hub.automation_access.v1"
AUTOMATION_CLIENT_PREFIX = "automation"
AUTOMATION_ACCESS_DEFAULT_TTL_SECONDS = ACCESS_TOKEN_TTL_SECONDS
ALL_RESOURCES_RESOURCE = "*"
DELEGATED_SESSION_MAX_TTL_SECONDS = 7 * 24 * 3600

AuthorityFactory = Callable[..., Any]
NamedServiceDiscoveryFactory = Callable[..., Any]
RelayFactory = Callable[[], Any]
ResourceOverlayProvider = Callable[[str], Awaitable[Iterable[Any]]]

# Live delivery: registry mutations (an OAuth consent lands a grant, a manual
# token is created, anything is revoked) are pushed to the user's OPEN
# Connection Hub widgets over the Data Bus. The widget registers its federated
# data-bus session at claim time; mutations fan out to every live session of
# the grantor. Event type consumed by the widget:
DELEGATED_ACCESS_CHANGED_EVENT = "connection_hub.delegated_access.changed"
# A resident profile's legacy cards disagree; nothing was folded.
RESIDENT_MIGRATION_CONFLICT = "resident_profile_migration_conflict"

_LOGGER = __import__("logging").getLogger("connection_hub.delegated_access")


def _live_sessions_key(tenant: str, project: str, grantor_subject: str) -> str:
    return (
        f"{_clean(tenant)}:{_clean(project)}:kdcube:delegated-access:"
        f"live-sessions:{_subject_key(_clean(grantor_subject))}"
    )


async def register_delegated_access_live_session(
    redis: Any,
    *,
    tenant: str,
    project: str,
    grantor_subject: str,
    session_id: str,
    expires_at: int | float | None = None,
) -> None:
    """Remember a user's live Connection Hub data-bus session so registry
    mutations can be pushed to it. Members expire with the session token."""
    subject = _clean(grantor_subject)
    sid = _clean(session_id)
    if not subject or not sid:
        return
    now = int(time.time())
    score = int(expires_at or 0) or (now + 3600)
    key = _live_sessions_key(tenant, project, subject)
    await redis.zadd(key, {sid: score})
    await redis.zremrangebyscore(key, "-inf", now)
    await redis.expire(key, DELEGATED_SESSION_MAX_TTL_SECONDS)


async def notify_delegated_access_changed(
    redis: Any,
    *,
    tenant: str,
    project: str,
    grantor_subject: str,
    action: str,
    access: Mapping[str, Any] | None = None,
    access_id: str = "",
    relay: Any = None,
    relay_factory: RelayFactory | None = None,
) -> None:
    """Fan a registry mutation out to the grantor's live hub sessions.

    Fire-and-forget by contract: a delivery failure must never fail the
    mutation that triggered it.
    """
    subject = _clean(grantor_subject)
    if not subject:
        return
    try:
        key = _live_sessions_key(tenant, project, subject)
        now = int(time.time())
        await redis.zremrangebyscore(key, "-inf", now)
        session_ids = [
            sid.decode("utf-8") if isinstance(sid, (bytes, bytearray)) else str(sid)
            for sid in await redis.zrange(key, 0, -1)
        ]
        if not session_ids:
            return
        if relay is None:
            if relay_factory is None:
                _LOGGER.warning(
                    "[connection-hub.delegated_access] live notify skipped: "
                    "relay factory is not configured"
                )
                return
            relay = relay_factory()
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for sid in session_ids:
            payload = {
                "type": DELEGATED_ACCESS_CHANGED_EVENT,
                "timestamp": timestamp,
                "service": {
                    "request_id": f"delegated-access-{_clean(access_id) or action}",
                    "tenant": _clean(tenant),
                    "project": _clean(project),
                    "user": subject,
                },
                "conversation": {"session_id": sid, "conversation_id": sid, "turn_id": ""},
                "event": {
                    "agent": "connection-hub",
                    "title": "Delegated Access Changed",
                    "status": "completed",
                    "step": "connection.delegated_access",
                },
                "data": {
                    "action": action,
                    "access_id": _clean(access_id) or str((access or {}).get("access_id") or ""),
                    "access": dict(access or {}),
                },
                "route": "chat_service",
            }
            await relay.emit(
                event="chat_service",
                data=payload,
                tenant=tenant,
                project=project,
                session_id=sid,
            )
    except Exception:
        _LOGGER.exception(
            "[connection-hub.delegated_access] live notify failed action=%s", action
        )


# Shared with the managed guard, which re-derives the same narrowing from the
# live card on every call.
_clean = clean_text
_as_list = as_string_list


def normalize_account_scope(value: Any) -> dict[str, dict[str, tuple[str, ...]]]:
    """Nested per-account claim binding: ``{provider: {account_id: (claims...)}}``.

    For each provider the agent may reach, this names the exact connected
    account(s) it may use AND, per account, the claims it may use on that
    account — so "read+write from account 1, read-only from account 2" is
    expressible independent of what each account is itself capable of.

    - account key ``"*"`` = any account; a claim entry ``"*"`` (or an empty
      claim list) = any claim the account supports.
    - Accepts the legacy list form ``{provider: [account_ids]}`` and migrates
      each account to ``("*",)`` (bound to those accounts for every claim), so
      existing grants keep working unchanged.
    - An absent provider key remains absent. Enforcement interprets that shape
      together with the caller identity: it is default-closed for a delegated
      caller and unrestricted only for the user's own non-delegated turn.
    """
    out: dict[str, dict[str, tuple[str, ...]]] = {}
    for provider, entry in dict(value or {}).items():
        pkey = _clean(provider)
        if not pkey:
            continue
        accounts: dict[str, tuple[str, ...]] = {}
        if isinstance(entry, Mapping):
            for account_id, claims in entry.items():
                akey = _clean(account_id)
                if not akey:
                    continue
                cl = tuple(_as_list(claims))
                accounts[akey] = cl or ("*",)
        else:
            for account_id in _as_list(entry):
                akey = _clean(account_id)
                if akey:
                    accounts[akey] = ("*",)
        if accounts:
            out[pkey] = accounts
    return out


def _subject_from_user(user: Mapping[str, Any]) -> str:
    for key in ("user_id", "sub", "id"):
        value = _clean(user.get(key))
        if value and value != "anonymous":
            return value
    return ""


def _delegate_mutation_refusal(user: Mapping[str, Any]) -> dict[str, Any] | None:
    """Card authority is changed by the grantor, never by the delegate.

    A delegated caller authenticates as ``integration:<client>:<grantor>``. It
    would find no card under a grantor-keyed index anyway; this states the rule
    instead of relying on that.
    """
    subject = _subject_from_user(user)
    if subject.startswith("integration:"):
        return {
            "ok": False,
            "error": "delegated_access_requires_grantor",
            "message": (
                "A delegated credential cannot change the authority of the card that "
                "issued it. Sign in as the granting user in Connection Hub."
            ),
        }
    return None


def _grants_delegable(grants: Iterable[str], delegable: set[str]) -> bool:
    """An entry is offered only when EVERY claim it costs is delegable: ticking
    it makes the panel demand all of them."""
    return all(str(grant) in delegable for grant in (grants or ()))


def _delegable_named_service_options(
    namespaces: list[dict[str, Any]], delegable: set[str]
) -> list[dict[str, Any]]:
    """Drop operations whose claims this grantor cannot delegate, then tools and
    namespaces left with nothing."""
    kept: list[dict[str, Any]] = []
    for namespace in namespaces:
        tools = namespace.get("tools")
        if not isinstance(tools, Mapping):
            kept.append(namespace)
            continue
        kept_tools: dict[str, Any] = {}
        for tool_name, raw in tools.items():
            tool = dict(raw) if isinstance(raw, Mapping) else {}
            operations = tool.get("operations")
            if isinstance(operations, Mapping) and operations:
                kept_ops = {
                    op: policy
                    for op, policy in operations.items()
                    if _grants_delegable(
                        (policy or {}).get("grants") if isinstance(policy, Mapping) else (),
                        delegable,
                    )
                }
                if not kept_ops:
                    continue
                tool["operations"] = kept_ops
            elif not _grants_delegable(tool.get("grants") or (), delegable):
                continue
            kept_tools[tool_name] = tool
        if not kept_tools:
            continue
        narrowed = dict(namespace)
        narrowed["tools"] = kept_tools
        kept.append(narrowed)
    return kept


def automation_record_key(tenant: str, project: str, access_id: str) -> str:
    """The pre-durable card key format. Nothing reads or writes it; cards live
    in durable storage and are cached under ``…:delegated-access:card:<id>``."""
    return f"{tenant}:{project}:kdcube:delegated-access:automation:{access_id}"


def oauth_access_id(grantor_subject: str, client_id: str, resource: str = "") -> str:
    """Compatibility helper for pre-kind callers.

    A resource names a connector Card. An empty resource names a multi-resource
    automation Card. New code should call ``stable_card_access_id`` with an
    explicit persisted kind.
    """
    if _clean(resource):
        return stable_connector_access_id(grantor_subject, client_id, resource)
    return stable_automation_access_id(grantor_subject, client_id)


def _subject_key(subject: str) -> str:
    return subject_hash_for(subject)


def _is_platform_admin(user: Mapping[str, Any]) -> bool:
    return authority_has_platform_privilege(_as_list(user.get("roles")))


def _bounded_ttl(value: Any) -> int:
    try:
        ttl = int(value or AUTOMATION_ACCESS_DEFAULT_TTL_SECONDS)
    except Exception:
        ttl = AUTOMATION_ACCESS_DEFAULT_TTL_SECONDS
    return max(60, min(ttl, DELEGATED_SESSION_MAX_TTL_SECONDS))


def _grantor_authority(
    user: Mapping[str, Any],
    *,
    grants: Iterable[str],
    inventory: AuthorityGrantInventory,
) -> dict[str, Any]:
    roles = sorted(set(_as_list(user.get("roles"))))
    has_privilege = authority_has_platform_privilege(roles)
    edge = selected_delegation_edge(
        inventory,
        grants,
        economics_budget_bypass=has_privilege,
    )
    edges = [edge.to_dict()] if edge is not None else []
    permissions = sorted(set(edge.permissions if edge is not None else ()))
    out: dict[str, Any] = {
        "schema": "connection_hub.grantor_authority.v1",
        "economics_budget_bypass": has_privilege,
    }
    if roles:
        out["grantor_roles"] = roles
    if permissions:
        out["grantor_permissions"] = permissions
    if edges:
        out["delegation_edges"] = edges
    return out


def _application_role_policy(
    *,
    properties: Mapping[str, Any],
    resource_grants: Mapping[str, Any],
    resource_operations: Mapping[str, Any],
    delegable_roles: Iterable[str],
    allowed_roles: Iterable[str],
) -> ApplicationOperationRolePolicy | None:
    if APPLICATION_API_RESOURCE not in resource_grants:
        return None
    policy = application_operation_role_policy(
        properties,
        resource_grants=resource_grants,
    )
    if policy is None:
        return None
    if policy.schema == APPLICATION_OPERATIONS_SCHEMA_V2:
        stored_defaults = {
            _clean(value)
            for value in _as_list(resource_grants.get(APPLICATION_API_RESOURCE))
            if _clean(value)
        }
        if stored_defaults != {policy.default_role}:
            raise ApplicationOperationPolicyError(
                "application_default_role_mismatch"
            )
    validate_application_operation_role_policy(
        policy,
        selected_operations=resource_operations.get(APPLICATION_API_RESOURCE, ()),
        delegable_roles=delegable_roles,
        allowed_roles=allowed_roles,
    )
    return policy


def _application_policy_refusal(
    exc: ApplicationOperationPolicyError,
) -> dict[str, Any]:
    return {
        "ok": False,
        "error": exc.reason,
        "status": 400,
        "message": (
            "The application API role policy is not a valid subset of the "
            "selected operations and delegable platform roles."
        ),
    }


ACCESS_SOURCE_MANUAL = "manual"
ACCESS_SOURCE_OAUTH = "oauth"
# A per-agent delegated grant: the consenting user grants a hosted agent
# (a "Delegated By KDCube" entity, keyed by a deterministic client_id) access to
# a resource. Unlike a MANUAL automation (which mints its own random client), the
# client_id is caller-supplied and stable, so re-consent updates one record.
ACCESS_SOURCE_AGENT = "agent"
ACCESS_SOURCE_CONTROL = CREDENTIALLESS_CARD_SOURCE


def oauth_card_kind(
    client_metadata: Mapping[str, Any] | None,
    entry_resource: str = "",
) -> str:
    """Kind asserted at OAuth registration and then persisted on the Card.

    A request without an RFC 8707 resource names the client's general profile,
    which is multi-resource and therefore keyed by user and client. A connector
    kind is valid only when there is a concrete entry door to include in its
    identity.
    """
    return (
        CARD_KIND_AUTOMATION
        if client_uses_full_card_catalog(client_metadata)
        or not _clean(entry_resource)
        else CARD_KIND_CONNECTOR
    )


def _whole_card_consent(
    *,
    client_metadata: Mapping[str, Any] | None,
    identity: Mapping[str, Any],
) -> bool:
    """Whether an OAuth consent without an entry resource may proceed.

    An agent or automation Card is keyed by user and client, so its consent
    names no entry door. The evidence is the client's registration (a
    full-catalog client) or an existing agent/automation Card of this user and
    client, never the mere absence of a resource: a connector Card belongs to
    one fixed gate and still needs it.
    """
    if _clean(identity.get("card_kind")) not in {CARD_KIND_AGENT, CARD_KIND_AUTOMATION}:
        return False
    return client_uses_full_card_catalog(client_metadata) or bool(identity.get("existing"))


def _legacy_record_card_kind(
    *,
    source: str,
    client_id: str,
    client_metadata: Mapping[str, Any] | None,
    entry_resource: str = "",
) -> str:
    if source == ACCESS_SOURCE_CONTROL:
        return CARD_KIND_CONTROL
    if source == ACCESS_SOURCE_AGENT or is_resident_client_id(client_id):
        return CARD_KIND_AGENT
    if source == ACCESS_SOURCE_OAUTH:
        return oauth_card_kind(client_metadata, entry_resource)
    return CARD_KIND_AUTOMATION


def _record_is_credentialless(record: "AutomationAccessRecord") -> bool:
    """The one material difference between a linked Card and a caller Card."""
    return (
        record.source == ACCESS_SOURCE_CONTROL
        and not record.delegate_subject
        and record.expires_at == 0
    )


def agent_grant_access_id(grantor_subject: str, client_id: str, resources: Iterable[str]) -> str:
    """LEGACY: the resource-dependent record id of a per-agent grant, one record
    per (grantor, client, selected resource set). Nothing is written under it
    any more; a resident profile's card is ``stable_resident_access_id`` and is
    independent of its resources. Kept so reads can still find, and migration
    can fold, the records that were written under this formula."""
    return legacy_resident_access_id(grantor_subject, client_id, resources)


def resident_access_ids(grantor_subject: str, client_id: str, resources: Iterable[str]) -> list[str]:
    """The stable resident Card id, then its legacy resource-dependent id."""
    grantor = _clean(grantor_subject)
    client = _clean(client_id)
    if not grantor or not is_resident_client_id(client):
        return []
    ids = [
        stable_resident_access_id(grantor, client),
        legacy_resident_access_id(grantor, client, resources),
    ]
    return list(dict.fromkeys(ids))


def _record_holds_resources(record: Any, resources: Iterable[str]) -> bool:
    """Whether a card covers every requested resource (by the guard's match)."""
    wanted = [_clean(item) for item in (resources or ()) if _clean(item)]
    if not wanted:
        return True
    return all(_card_holds_resource(record, resource) for resource in wanted)


async def read_agent_grant_record(
    redis: Any,
    *,
    tenant: str,
    project: str,
    grantor_subject: str,
    client_id: str,
    resources: Iterable[str],
) -> "AutomationAccessRecord | None":
    """Read-only probe of a per-agent grant record: the parsed, unexpired record,
    or ``None`` while consent is pending. Needs only Redis + the scope — no
    delegated config — so a picker/menu enrichment can show given/pending without
    constructing the full service. The token stays server-side with the caller."""
    grantor = _clean(grantor_subject)
    client = _clean(client_id)
    if not grantor or not client:
        return None
    cache = DelegatedCardRuntimeCache(redis, tenant=_clean(tenant), project=_clean(project))
    # The stable profile card first, then the legacy resource-dependent record a
    # not-yet-migrated profile may still live under.
    for access_id in resident_access_ids(grantor, client, resources) or [
        agent_grant_access_id(grantor, client, resources)
    ]:
        try:
            # A rolled-back projection is not shown as given while the current
            # Redis run is unswept (cards/reconcile.py): the probe reads pending.
            in_run, entry = await cache.read_in_current_run(access_id)
        except Exception:
            # A probe enriches a picker; it never denies on its own.
            return None
        if not in_run:
            return None
        if entry is None or not entry.is_card or entry.authority is None:
            continue
        record = record_from_card(entry.authority)
        if record.source != ACCESS_SOURCE_AGENT:
            continue
        if record.expires_at and record.expires_at <= int(time.time()):
            continue
        if not _record_holds_resources(record, resources):
            continue
        return record
    return None


def _serving_state_unavailable(exc: CardServingUnavailable) -> dict[str, Any]:
    """The revision is committed; its serving state is not.

    Names the card so a caller that minted credential material it never
    received can point at it. Retrying creates a second card when the id is
    random, so the answer says which one already exists.
    """
    return {
        "ok": False,
        "error": "delegated_card_serving_state_unavailable",
        "reason": exc.reason,
        "access_id": exc.access_id,
        "retryable": True,
        "status": 503,
    }


def _selection_policy_argument(
    selection: NamedServiceSelection,
) -> dict[str, dict[str, list[str]]] | None:
    """The narrowing argument ``named_service_policy_for_resource`` expects.

    ``None`` keeps the full descriptor policy, an empty map narrows every
    resource to nothing, and an exact map narrows to its entries. Only an
    explicit ``"*"`` keeps the full policy; a pre-encoding selection reaching
    here unresolved narrows to nothing rather than widening.
    """
    if selection.is_all:
        return None
    if selection.is_none or selection.is_unknown:
        return {}
    return {
        resource: {namespace: list(values) for namespace, values in namespaces.items()}
        for resource, namespaces in selection.operations.items()
    }


@dataclass(frozen=True)
class ResolvedCardAuthority:
    """What a save writes, or why it cannot.

    ``revoke`` is the reconciliation outcome where nothing survived the active
    catalog: an empty card is revoked, never persisted.
    """

    resource_grants: dict[str, list[str]] = field(default_factory=dict)
    resource_operations: dict[str, list[str]] = field(default_factory=dict)
    operations: list[str] = field(default_factory=list)
    named_service_operations: NamedServiceSelection = field(
        default_factory=NamedServiceSelection.none
    )
    named_services: dict[str, Any] = field(default_factory=dict)
    account_scope: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    identity_scope: str = "grantor"
    properties: dict[str, Any] = field(default_factory=dict)
    reconciled: Any = None
    revoke: bool = False
    error: dict[str, Any] | None = None


def _inherited_selection(
    existing: "AutomationAccessRecord",
    grants_by_resource: Mapping[str, Iterable[str]] | None = None,
) -> NamedServiceSelection:
    """The selection an edit that never mentioned it carries forward.

    A wildcard is bound to the catalog version the card was saved against, so
    an unrelated edit must not re-pin it to the current one: it is frozen into
    the exact set it already means. The materialized boundary is that expansion
    by construction, so freezing needs no historical catalog document and works
    even when the referenced version is gone.

    A pre-encoding record names no operations either, and the design resolves
    it the same way: "derive the prior exact set from stored named_services".

    An explicitly submitted ``"*"`` does not come through here — that one is
    consent to everything the current catalog shows.
    """
    selection = existing.named_service_operations
    if not (selection.is_all or selection.is_unknown):
        return selection
    # Filtered by the claims THIS save persists, not the ones the record used to
    # hold: a wildcard boundary is the descriptor tree unfiltered, an exact
    # selection may only name what the card can actually invoke, and an edit
    # that widens claims must not lose the namespaces those claims just opened.
    held = dict(grants_by_resource) if grants_by_resource is not None else dict(existing.resource_grants)
    frozen: dict[str, dict[str, list[str]]] = {}
    for resource, grants in held.items():
        offered = configured_named_service_operations(
            existing.named_services, grants=list(grants or ())
        )
        per_namespace = {
            namespace: sorted(operations)
            for namespace, operations in offered.items()
            if operations
        }
        if per_namespace:
            frozen[resource] = per_namespace
    if not frozen:
        return NamedServiceSelection.none()
    return NamedServiceSelection.exact(frozen)


def _materialized_has_namespaces(named_services: Any) -> bool:
    if not isinstance(named_services, Mapping):
        return False
    namespaces = named_services.get("namespaces")
    return isinstance(namespaces, Mapping) and bool(namespaces)


def _parse_named_service_selection(value: Mapping[str, Any]) -> NamedServiceSelection:
    """Read the stored selection.

    A stored ``{}`` is an explicit empty policy only when the materialized
    boundary carries no namespaces; otherwise the record predates the encoding.
    """
    try:
        selection = NamedServiceSelection.from_stored(
            value.get("named_service_operations"),
            present="named_service_operations" in value,
        )
    except CardRecordError:
        return NamedServiceSelection.unknown()
    if selection.is_none and _materialized_has_namespaces(value.get("named_services")):
        return NamedServiceSelection.unknown()
    return selection


def _parse_resource_acceptance_field(value: Any) -> dict[str, ResourceAcceptance]:
    """The stored per-resource acceptance, or empty when absent or unreadable.
    Unreadable evidence is dropped rather than trusted: the next save stamps it
    again from the current authority."""
    try:
        return parse_resource_acceptance(value)
    except ResourceAcceptanceError:
        return {}


@dataclass(frozen=True)
class AutomationAccessRecord:
    access_id: str
    label: str
    client_id: str
    grantor_subject: str
    delegate_subject: str
    card_kind: str
    operations: tuple[str, ...]
    resource_grants: Mapping[str, tuple[str, ...]]
    resource_operations: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # Exact user-visible selection in one of four states. A newly written card
    # always carries "*", {}, or an exact map; `unknown` is a legacy-record
    # condition only.
    named_service_operations: NamedServiceSelection = field(
        default_factory=NamedServiceSelection.unknown
    )
    # Boundary tree for the named-service bridge, narrowed from the descriptor
    # by `named_service_operations`. Empty on cards written before this field;
    # the guard then keeps the bound snapshot.
    named_services: Mapping[str, Any] = field(default_factory=dict)
    # Per-agent, per-account claim binding:
    # {provider_id: {account_id: (claims...)}}. For a provider, which connected
    # account(s) this client may use AND, per account, the exact claims it may
    # use there. account "*" = any account; claim "*" (or empty) = any claim.
    # An absent provider key is default-closed for delegated callers. The
    # request boundary carries the delegated identity alongside this map so
    # enforcement can distinguish it from a non-delegated user turn.
    account_scope: Mapping[str, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)
    identity_scope: str = ""
    # The catalog generation this card was last saved against. "*" and every
    # exact selection are defined relative to it.
    catalog_version: str = ""
    # Monotonic per-card revision; rejects concurrent lost updates.
    card_revision: int = 0
    session_id: str = ""
    created_at: int = 0
    expires_at: int = 0
    last_four: str = ""
    source: str = ACCESS_SOURCE_MANUAL
    # OAuth-flow grants keep their live token material so revoke can kill the
    # refresh token and the current access-grant binding. Never public.
    refresh_token: str = ""
    access_token: str = ""
    # Last token issuance (initial consent or refresh rotation) — staleness
    # signal: a card whose last_issued_at is old is likely an orphan (the
    # client disconnected without revoking).
    last_issued_at: int = 0
    # Per resource: the descriptor authority the card accepted at its last
    # save (kind, revision, digest, claims, one digest per offered operation).
    # Drift is judged against it resource by resource. Empty on records written
    # before the field existed; the next save stamps it.
    resource_acceptance: Mapping[str, ResourceAcceptance] = field(default_factory=dict)
    # Non-secret lineage written by the resident-profile migration: the legacy
    # records folded into this card and when. Empty otherwise.
    provenance: Mapping[str, Any] = field(default_factory=dict)
    # The protected resource where an OAuth client began authorization. It is
    # the card's entry door for display and reconnect identity; the owner may
    # grant other compatible resources on the same card.
    entry_resource: str = ""
    # Public, client-asserted identification retained for operator review and
    # search. It never participates in an authority decision.
    client_metadata: Mapping[str, Any] = field(default_factory=dict)
    # One optional Control Card may adjust this Card. It is resolved only by
    # the live authorization path.
    control_card: ControlCardBinding | None = None
    issuer_ref: str = ""
    issuer_kind: str = ""
    issuer_label: str = ""
    manage_url: str = ""
    composition_mode: str = ""
    properties: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        kind = _clean(self.card_kind)
        if kind not in CARD_KINDS:
            raise CardRecordError("card_kind_invalid")
        object.__setattr__(self, "card_kind", kind)
        normalized = normalize_resource_operations(self.resource_operations)
        if not normalized and self.operations:
            normalized = project_legacy_operations(
                self.resource_grants, self.operations
            )
        object.__setattr__(self, "resource_operations", normalized)
        object.__setattr__(self, "operations", operation_union(normalized))
        object.__setattr__(
            self,
            "client_metadata",
            normalize_public_client_metadata(self.client_metadata),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AutomationAccessRecord":
        source = _clean(value.get("source")) or ACCESS_SOURCE_MANUAL
        client_id = _clean(value.get("client_id"))
        metadata = (
            dict(value.get("client_metadata"))
            if isinstance(value.get("client_metadata"), Mapping)
            else {}
        )
        return cls(
            access_id=_clean(value.get("access_id")),
            label=_clean(value.get("label")),
            client_id=client_id,
            grantor_subject=_clean(value.get("grantor_subject")),
            delegate_subject=_clean(value.get("delegate_subject")),
            card_kind=(
                _clean(value.get("card_kind"))
                or _legacy_record_card_kind(
                    source=source,
                    client_id=client_id,
                    client_metadata=metadata,
                    entry_resource=_clean(value.get("entry_resource")),
                )
            ),
            operations=tuple(_as_list(value.get("operations"))),
            resource_grants={
                _clean(key): tuple(_as_list(grants))
                for key, grants in dict(value.get("resource_grants") or {}).items()
                if _clean(key)
            },
            resource_operations=(
                normalize_resource_operations(value.get("resource_operations"))
                if "resource_operations" in value
                else project_legacy_operations(
                    dict(value.get("resource_grants") or {}),
                    _as_list(value.get("operations")),
                )
            ),
            named_service_operations=_parse_named_service_selection(value),
            named_services=(
                dict(value.get("named_services"))
                if isinstance(value.get("named_services"), Mapping)
                else {}
            ),
            account_scope=normalize_account_scope(value.get("account_scope")),
            identity_scope=_clean(value.get("identity_scope")),
            catalog_version=_clean(value.get("catalog_version")),
            card_revision=int(value.get("card_revision") or 0),
            session_id=_clean(value.get("session_id")),
            created_at=int(value.get("created_at") or 0),
            expires_at=int(value.get("expires_at") or 0),
            last_four=_clean(value.get("last_four")),
            source=source,
            refresh_token=_clean(value.get("refresh_token")),
            access_token=_clean(value.get("access_token")),
            last_issued_at=int(value.get("last_issued_at") or 0),
            resource_acceptance=_parse_resource_acceptance_field(value.get("resource_acceptance")),
            provenance=(
                dict(value.get("provenance"))
                if isinstance(value.get("provenance"), Mapping)
                else {}
            ),
            entry_resource=_clean(value.get("entry_resource")),
            client_metadata=metadata,
            control_card=(
                ControlCardBinding.from_mapping(value.get("control_card"))
                if value.get("control_card") is not None
                else None
            ),
            issuer_ref=_clean(value.get("issuer_ref")),
            issuer_kind=_clean(value.get("issuer_kind")),
            issuer_label=_clean(value.get("issuer_label")),
            manage_url=_clean(value.get("manage_url")),
            composition_mode=_clean(value.get("composition_mode")).lower(),
            properties=(
                copy.deepcopy(dict(value.get("properties")))
                if isinstance(value.get("properties"), Mapping)
                else {}
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": AUTOMATION_ACCESS_SCHEMA,
            "access_id": self.access_id,
            "label": self.label,
            "client_id": self.client_id,
            "grantor_subject": self.grantor_subject,
            "delegate_subject": self.delegate_subject,
            "card_kind": self.card_kind,
            "operations": list(self.operations),
            "resource_grants": {key: list(value) for key, value in self.resource_grants.items()},
            "resource_operations": {
                key: list(value) for key, value in self.resource_operations.items()
            },
            "named_services": dict(self.named_services or {}),
            "account_scope": {
                provider: {account_id: list(claims) for account_id, claims in accounts.items()}
                for provider, accounts in self.account_scope.items()
            },
            "identity_scope": self.identity_scope,
            "catalog_version": self.catalog_version,
            "card_revision": self.card_revision,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "last_four": self.last_four,
            "source": self.source,
            "refresh_token": self.refresh_token,
            "access_token": self.access_token,
            "last_issued_at": self.last_issued_at,
            "resource_acceptance": {
                resource: acceptance.to_dict()
                for resource, acceptance in sorted(self.resource_acceptance.items())
            },
            "provenance": dict(self.provenance or {}),
            "entry_resource": self.entry_resource,
            "client_metadata": copy.deepcopy(dict(self.client_metadata or {})),
            "issuer_ref": self.issuer_ref,
            "issuer_kind": self.issuer_kind,
            "issuer_label": self.issuer_label,
            "manage_url": self.manage_url,
            "composition_mode": self.composition_mode,
            "properties": copy.deepcopy(dict(self.properties or {})),
        }
        stored_selection = self.named_service_operations.to_stored()
        if stored_selection is not None:
            payload["named_service_operations"] = stored_selection
        if self.control_card is not None:
            payload["control_card"] = self.control_card.to_dict()
        return payload

    def to_public_dict(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload.pop("session_id", None)
        payload.pop("refresh_token", None)
        payload.pop("access_token", None)
        # Derived from the descriptor and only consumed by the guard; the
        # selection (`named_service_operations`) is what surfaces render.
        payload.pop("named_services", None)
        # The selection is retained verbatim: an explicit {} must not render as
        # unrestricted, and "*" must survive a refetch.
        selection = payload.pop("named_service_operations", None)
        public = {key: value for key, value in payload.items() if value not in ("", [], {})}
        if selection is not None:
            public["named_service_operations"] = selection
        # Derived, never authority: the selection expanded under the version the
        # card was saved against. "*" and a pre-encoding record name no
        # operations, so without this a surface can only render them as an empty
        # picker — indistinguishable from an explicit {}.
        effective = {
            resource: {
                namespace: sorted(operations)
                for namespace, operations in namespaces.items()
                if operations
            }
            for resource, namespaces in selected_named_service_operations(self).items()
        }
        effective = {resource: rows for resource, rows in effective.items() if rows}
        if effective:
            public["effective_named_service_operations"] = effective
        # These are separate facts. ``source`` says how the credential reached
        # the caller; reach says whether the caller is bound to one entry
        # resource or may present the credential to several selected resources.
        public["credential_delivery"] = {
            ACCESS_SOURCE_AGENT: "hosted",
            ACCESS_SOURCE_OAUTH: "oauth",
            ACCESS_SOURCE_MANUAL: "issued_token",
            ACCESS_SOURCE_CONTROL: "credentialless",
        }.get(self.source, self.source or "issued_token")
        public["credential_reach"] = (
            "multi_resource"
            if self.source
            in {ACCESS_SOURCE_AGENT, ACCESS_SOURCE_MANUAL, ACCESS_SOURCE_CONTROL}
            or client_uses_full_card_catalog(self.client_metadata)
            else "single_resource"
        )
        return public


def card_authority_from_record(record: AutomationAccessRecord) -> CardAuthority:
    """The record's non-secret authorization decision."""
    return CardAuthority(
        access_id=record.access_id,
        client_id=record.client_id,
        grantor_subject=record.grantor_subject,
        delegate_subject=record.delegate_subject,
        source=record.source,
        card_kind=record.card_kind,
        label=record.label,
        card_revision=record.card_revision,
        catalog_version=record.catalog_version,
        state=CARD_STATE_ACTIVE,
        operations=tuple(record.operations),
        resource_grants={key: tuple(value) for key, value in record.resource_grants.items()},
        resource_operations={
            key: tuple(value) for key, value in record.resource_operations.items()
        },
        named_service_operations=record.named_service_operations,
        named_services=copy.deepcopy(dict(record.named_services or {})),
        account_scope={
            provider: {account_id: tuple(claims) for account_id, claims in accounts.items()}
            for provider, accounts in record.account_scope.items()
        },
        identity_scope=record.identity_scope,
        created_at=record.created_at,
        expires_at=record.expires_at,
        last_issued_at=record.last_issued_at,
        last_four=record.last_four,
        resource_acceptance=dict(record.resource_acceptance or {}),
        provenance=copy.deepcopy(dict(record.provenance or {})),
        entry_resource=record.entry_resource,
        client_metadata=copy.deepcopy(dict(record.client_metadata or {})),
        control_card=record.control_card,
        issuer_ref=record.issuer_ref,
        issuer_kind=record.issuer_kind,
        issuer_label=record.issuer_label,
        manage_url=record.manage_url,
        composition_mode=record.composition_mode,
        properties=copy.deepcopy(dict(record.properties or {})),
    )


def card_handles_from_record(record: AutomationAccessRecord) -> CardCredentialHandles:
    """The record's live, reusable credential material."""
    return CardCredentialHandles(
        access_id=record.access_id,
        access_token=record.access_token,
        refresh_token=record.refresh_token,
        session_id=record.session_id,
    )


def record_from_card(
    authority: CardAuthority,
    handles: CardCredentialHandles | None = None,
) -> AutomationAccessRecord:
    """Recombine authority and handles into the record shape callers render."""
    held = handles or CardCredentialHandles(access_id=authority.access_id)
    return AutomationAccessRecord(
        access_id=authority.access_id,
        label=authority.label,
        client_id=authority.client_id,
        grantor_subject=authority.grantor_subject,
        delegate_subject=authority.delegate_subject,
        card_kind=authority.card_kind,
        operations=tuple(authority.operations),
        resource_grants={key: tuple(value) for key, value in authority.resource_grants.items()},
        resource_operations={
            key: tuple(value) for key, value in authority.resource_operations.items()
        },
        named_service_operations=authority.named_service_operations,
        named_services=copy.deepcopy(dict(authority.named_services or {})),
        account_scope={
            provider: {account_id: tuple(claims) for account_id, claims in accounts.items()}
            for provider, accounts in authority.account_scope.items()
        },
        identity_scope=authority.identity_scope,
        catalog_version=authority.catalog_version,
        card_revision=authority.card_revision,
        session_id=held.session_id,
        created_at=authority.created_at,
        expires_at=authority.expires_at,
        last_four=authority.last_four,
        source=authority.source,
        refresh_token=held.refresh_token,
        access_token=held.access_token,
        last_issued_at=authority.last_issued_at,
        resource_acceptance=dict(authority.resource_acceptance or {}),
        provenance=copy.deepcopy(dict(authority.provenance or {})),
        entry_resource=authority.entry_resource,
        client_metadata=copy.deepcopy(dict(authority.client_metadata or {})),
        control_card=authority.control_card,
        issuer_ref=authority.issuer_ref,
        issuer_kind=authority.issuer_kind,
        issuer_label=authority.issuer_label,
        manage_url=authority.manage_url,
        composition_mode=authority.composition_mode,
        properties=copy.deepcopy(dict(authority.properties or {})),
    )


def _card_holds_resource(record: Any, configured_resource: str) -> bool:
    """Whether a card's stored resources reach a configured row."""
    grants = getattr(record, "resource_grants", None) or {}
    if configured_resource in grants:
        return True
    from connection_hub.delegated_credentials.credential_view import (
        resource_matches,
    )

    return any(
        resource_matches(configured_resource, str(stored or ""))
        or resource_matches(str(stored or ""), configured_resource)
        for stored in grants
    )


def _card_claims_for_resource(record: Any, configured_resource: str) -> set[str]:
    """The card's claims that apply to a configured resource row.

    A card keys `resource_grants` by what its issuance saw: an agent or manual
    card by the configured selector, an OAuth card by the concrete request URL.
    An exact lookup by the row's selector therefore reads empty for the URL
    shape and reports every required claim as missing. Matching is the same
    wildcard match the guard uses to decide which row governs the request.
    """
    from connection_hub.delegated_credentials.credential_view import (
        resource_matches,
    )

    grants = getattr(record, "resource_grants", None) or {}
    exact = grants.get(configured_resource)
    if exact is not None:
        return {_clean(item) for item in exact if _clean(item)}
    out: set[str] = set()
    for stored, held in grants.items():
        if resource_matches(configured_resource, str(stored or "")) or resource_matches(
            str(stored or ""), configured_resource
        ):
            out.update(_clean(item) for item in (held or ()) if _clean(item))
    return out


def _account_scope_claims(
    account_scope: Mapping[str, Any] | None,
    *,
    required: Iterable[str],
) -> set[str]:
    """Provider claims held through at least one bound connected account.

    Named-service admission treats those claims as effective authority without
    copying them into ``resource_grants``.  This mirrors the managed MCP bridge:
    explicit claims are globally named and therefore carry as written, while a
    legacy ``"*"`` expands only to currently required claims in that provider's
    own claim namespace.  The connected-account broker still enforces the exact
    account when the provider call executes.
    """
    required_claims = {_clean(item) for item in required if _clean(item)}
    held: set[str] = set()
    for provider, accounts in normalize_account_scope(account_scope).items():
        provider_prefix = f"{provider}:"
        for claims in accounts.values():
            normalized = {_clean(item) for item in claims if _clean(item)}
            if "*" in normalized:
                held.update(
                    claim
                    for claim in required_claims
                    if claim.startswith(provider_prefix)
                )
            held.update(claim for claim in normalized if claim != "*")
    return held


def _account_scope_claims_for_requirements(
    record: Any,
    *,
    required: Iterable[str],
) -> set[str]:
    return _account_scope_claims(
        getattr(record, "account_scope", None),
        required=required,
    )


def _effective_named_service_grants(
    grants: Iterable[str],
    *,
    account_scope: Mapping[str, Any] | None,
    required: Iterable[str],
) -> list[str]:
    """Claims that may materialize a named-service operation boundary."""
    effective = _as_list(list(grants))
    for claim in sorted(_account_scope_claims(account_scope, required=required)):
        if claim not in effective:
            effective.append(claim)
    return effective


class AutomationAccessService:
    """Create/list/revoke user-created delegated automation credentials."""

    def __init__(
        self,
        *,
        redis: Any,
        tenant: str,
        project: str,
        config: OAuthDelegatedClientConfig,
        grant_store: GrantStore | None = None,
        authority: Any | None = None,
        catalog_resolver: Any | None = None,
        card_persistence: Any | None = None,
        minter: Any | None = None,
        named_service_discovery: Any | None = None,
        authority_factory: AuthorityFactory | None = None,
        named_service_discovery_factory: NamedServiceDiscoveryFactory | None = None,
        relay_factory: RelayFactory | None = None,
        resource_overlay_provider: ResourceOverlayProvider | None = None,
        invocation_policy_service: Any | None = None,
    ) -> None:
        self._redis = redis
        self._tenant = _clean(tenant)
        self._project = _clean(project)
        self._config = config
        self._store = grant_store or GrantStore(redis, self._tenant, self._project)
        self._authority = authority
        self._authority_factory = authority_factory
        self._minter = minter
        self._named_service_discovery = named_service_discovery
        self._named_service_discovery_factory = named_service_discovery_factory
        self._relay_factory = relay_factory
        self._resource_overlay_provider = resource_overlay_provider
        # Required by the operations that stamp a card. Read-only and
        # credential-lifecycle operations do not need it.
        self._catalog_resolver = catalog_resolver
        # Policy depends on the persistence contract, not on how a card is
        # stored. Composition of the durable implementation belongs to the
        # caller that owns storage.
        self._persistence = card_persistence
        # Optional: lets the resident-profile migration carry once/always
        # policies to the stable card and the read model report them. Without
        # it, migration refuses to fold a record whose policies it cannot see.
        self._invocation_policies = invocation_policy_service

    # -- card persistence -----------------------------------------------------
    #
    # Durable revisions are the source of truth; Redis holds the live
    # projection and the bounded credential handles. Every raw record access
    # goes through these three operations.

    def _cards(self) -> Any:
        if self._persistence is None:
            raise CardUnavailable("card_persistence_not_configured")
        return self._persistence

    async def _ensure_control_snapshot(
        self,
        record: AutomationAccessRecord,
    ) -> AutomationAccessRecord:
        """Migrate one legacy Control Card from its first durable revision.

        The first immutable revision is the only defensible historical
        boundary. Expanding a wildcard against the active catalog would grant
        capabilities that did not exist when the project owner created the
        Card. Missing historical evidence becomes an exact empty selection and
        is surfaced for review.
        """

        current = card_authority_from_record(record)
        if not authority_is_credentialless(current) or control_snapshot_is_exact(
            current
        ):
            return record

        load_initial = getattr(self._cards(), "load_initial", None)
        initial = (
            await load_initial(
                current.access_id,
                subject_hash=_subject_key(current.grantor_subject),
            )
            if callable(load_initial)
            else None
        )
        history_is_trusted = bool(
            initial is not None
            and authority_is_credentialless(initial)
            and initial.access_id == current.access_id
            and initial.grantor_subject == current.grantor_subject
            and initial.catalog_version
        )

        if not history_is_trusted:
            migrated = dataclasses.replace(
                fail_closed_control_snapshot(
                    current,
                    basis_catalog_version=current.catalog_version,
                    reason="historical_boundary_unavailable",
                ),
                card_revision=current.card_revision + 1,
            )
        else:
            assert initial is not None

            properties = copy.deepcopy(dict(current.properties or {}))
            properties.pop(CONTROL_SNAPSHOT_PROPERTY, None)
            properties.pop(APPLICATION_OPERATIONS_PROPERTY, None)
            initial_application_policy = dict(initial.properties or {}).get(
                APPLICATION_OPERATIONS_PROPERTY
            )
            if isinstance(initial_application_policy, Mapping):
                properties[APPLICATION_OPERATIONS_PROPERTY] = copy.deepcopy(
                    dict(initial_application_policy)
                )

            historical = dataclasses.replace(
                current,
                catalog_version=initial.catalog_version,
                resource_grants=copy.deepcopy(dict(initial.resource_grants)),
                resource_operations=copy.deepcopy(dict(initial.resource_operations)),
                named_service_operations=initial.named_service_operations,
                named_services=copy.deepcopy(dict(initial.named_services or {})),
                account_scope=copy.deepcopy(dict(initial.account_scope or {})),
                identity_scope=initial.identity_scope,
                resource_acceptance=copy.deepcopy(
                    dict(initial.resource_acceptance or {})
                ),
                properties=properties,
            )
            migrated = dataclasses.replace(
                materialize_control_snapshot(
                    historical,
                    basis_catalog_version=initial.catalog_version,
                    origin="legacy_wildcard",
                    source_card_revision=initial.card_revision,
                ),
                card_revision=current.card_revision + 1,
            )
        try:
            await self._persist_record(
                record_from_card(migrated),
                expected_revision=current.card_revision,
            )
        except CardConflict:
            reloaded = await self._load_record(
                current.access_id,
                grantor_subject=current.grantor_subject,
            )
            if reloaded is not None and control_snapshot_is_exact(
                card_authority_from_record(reloaded)
            ):
                return reloaded
            raise CardUnavailable("control_card_snapshot_migration_conflict")
        except (CardCommitFailed, CardServingUnavailable) as exc:
            raise CardUnavailable(
                getattr(exc, "reason", "control_card_snapshot_migration_failed")
            ) from exc
        return record_from_card(migrated)

    async def _resolve_control_record(
        self,
        control_id: str,
        *,
        grantor_subject: str,
    ) -> AutomationAccessRecord | None:
        """Resolve a regular Control Card from its durable revision.

        Legacy project Control Cards live only in Redis, with no durable
        revision a Redis rollback could be checked against, so they are not
        authority here: admission, attachment and effective composition all
        resolve through this method. A Card still bound to one resolves no
        control and fails closed.
        """

        record = await self._load_record(
            control_id,
            grantor_subject=grantor_subject,
        )
        if record is None:
            return None
        return await self._ensure_control_snapshot(record)

    async def _effective_control_view(
        self,
        record: AutomationAccessRecord,
    ) -> dict[str, Any]:
        """Owner-facing explanation of the authority currently in force."""

        binding = record.control_card
        if binding is None:
            return {"state": "not_controlled"}
        try:
            control = await self._resolve_control_record(
                binding.control_id,
                grantor_subject=record.grantor_subject,
            )
        except CardUnavailable as exc:
            return {
                "state": "unavailable",
                "reason": exc.reason,
                "fail_closed": True,
                "binding": binding.to_dict(),
            }
        except Exception:
            return {
                "state": "unavailable",
                "reason": "control_card_lookup_unavailable",
                "fail_closed": True,
                "binding": binding.to_dict(),
            }
        if control is None:
            return {
                "state": "unavailable",
                "reason": "control_card_unresolvable",
                "fail_closed": True,
                "binding": binding.to_dict(),
            }
        if not _record_is_credentialless(control):
            return {
                "state": "unavailable",
                "reason": "control_card_has_credential",
                "fail_closed": True,
                "binding": binding.to_dict(),
            }
        try:
            effective = effective_card_authority(
                card_authority_from_record(record),
                card_authority_from_record(control),
            )
        except ControlCardMismatch as exc:
            return {
                "state": "unavailable",
                "reason": exc.reason,
                "fail_closed": True,
                "binding": binding.to_dict(),
            }
        return {
            "state": "active",
            "fail_closed": False,
            "binding": effective.control_card.to_dict()
            if effective.control_card is not None
            else binding.to_dict(),
            # Owner-visible and credentialless. The saved effective authority
            # below is intentionally insufficient for previewing an unsaved
            # caller edit: an intersection cannot reveal Control Card access
            # that the saved caller did not select.
            "control_authority": control.to_public_dict(),
            "authority": record_from_card(effective).to_public_dict(),
            "resolution": {
                "participant_card_revision": record.card_revision,
                "participant_catalog_version": record.catalog_version,
                "control_card_revision": control.card_revision,
                "control_catalog_version": control.catalog_version,
            },
            "composition_mode": control.composition_mode,
            "properties": copy.deepcopy(dict(control.properties or {})),
        }

    async def _load_record(
        self, access_id: str, *, grantor_subject: str
    ) -> AutomationAccessRecord | None:
        loaded = await self._cards().load(
            access_id, subject_hash=_subject_key(grantor_subject)
        )
        if loaded is None:
            return None
        authority, handles = loaded
        return record_from_card(authority, handles)

    async def _load_record_any_state(
        self, access_id: str, *, grantor_subject: str
    ) -> tuple[AutomationAccessRecord, str] | None:
        """The owner's card in any state, expired included, with its state.
        Owner-facing only (listing, renewal); never authority for a call."""
        loaded = await self._cards().load_current(
            access_id, subject_hash=_subject_key(grantor_subject)
        )
        if loaded is None:
            return None
        authority, handles = loaded
        return record_from_card(authority, handles), str(authority.state)

    async def _list_owner_records(self, grantor_subject: str) -> list[AutomationAccessRecord]:
        """Every card the grantor still owns: active ones and expired ones,
        which stay listed for renewal until revoked. Guards and pickers keep
        reading the active list."""
        authorities = await self._cards().list_current(
            subject_hash=_subject_key(grantor_subject)
        )
        return [
            record_from_card(authority)
            for authority in authorities
            if not authority_is_credentialless(authority)
        ]

    async def _list_all_owner_records(
        self,
        grantor_subject: str,
    ) -> list[AutomationAccessRecord]:
        """Every current revision used only for tuple identity lookup.

        A revoked pre-migration Card still owns its tuple. Re-consent may write
        its next revision, but it must not create a second Card at the new
        canonical id merely because the old one is not active authority.
        """
        cards = self._cards()
        list_all = getattr(cards, "list_all_current", None)
        list_current = getattr(cards, "list_current", None)
        if callable(list_all):
            authorities = await list_all(subject_hash=_subject_key(grantor_subject))
        elif callable(list_current):
            authorities = await list_current(subject_hash=_subject_key(grantor_subject))
        else:
            # Compatibility for injected persistence ports written before the
            # all-current identity lookup was added. Production persistence
            # implements the full method; this fallback cannot see revoked
            # history and therefore must not be used by migration tooling.
            authorities = await cards.list_active(
                subject_hash=_subject_key(grantor_subject),
                now=int(time.time()),
            )
        return [
            record_from_card(authority)
            for authority in authorities
            if not authority_is_credentialless(authority)
        ]

    async def resolve_card_identity(
        self,
        *,
        grantor_subject: str,
        client_id: str,
        card_kind: str,
        entry_resource: str = "",
    ) -> dict[str, Any]:
        """Find a Card by its persisted identity tuple before deriving an id.

        This is the create/consent guard during and after migration. Existing
        non-canonical Cards keep being addressed by their stored tuple until
        the migration moves them. Multiple pair Cards are an explicit
        collision; no runtime path merges their authority.
        """
        grantor = _clean(grantor_subject)
        client = _clean(client_id)
        kind = _clean(card_kind)
        entry = _clean(entry_resource)
        if not grantor or not client or kind not in {
            CARD_KIND_AGENT,
            CARD_KIND_AUTOMATION,
            CARD_KIND_CONNECTOR,
        }:
            return {"ok": False, "error": "card_identity_incomplete", "status": 400}
        if kind == CARD_KIND_CONNECTOR and not entry:
            return {"ok": False, "error": "card_identity_entry_resource_missing", "status": 400}
        try:
            records = await self._list_all_owner_records(grantor)
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_cards_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        same_client = [record for record in records if record.client_id == client]
        same_kind = [record for record in same_client if record.card_kind == kind]
        candidates = (
            [record for record in same_kind if _clean(record.entry_resource) == entry]
            if kind == CARD_KIND_CONNECTOR
            else same_kind
        )
        if len(candidates) > 1:
            return {
                "ok": False,
                "error": "card_identity_collision",
                "status": 409,
                "card_kind": kind,
                "grantor_subject": grantor,
                "client_id": client,
                "entry_resource": entry if kind == CARD_KIND_CONNECTOR else "",
                "access_ids": sorted(record.access_id for record in candidates),
            }
        if candidates:
            record = candidates[0]
            return {
                "ok": True,
                "access_id": record.access_id,
                "card_kind": kind,
                "existing": True,
                "canonical_access_id": stable_card_access_id(
                    card_kind=kind,
                    grantor_subject=grantor,
                    client_id=client,
                    entry_resource=entry,
                ),
            }
        pair_kinds = {CARD_KIND_AGENT, CARD_KIND_AUTOMATION}
        incompatible = (
            [
                record
                for record in same_client
                if record.card_kind in pair_kinds and record.card_kind != kind
            ]
            if kind in pair_kinds
            else []
        )
        if incompatible:
            return {
                "ok": False,
                "error": "card_identity_kind_mismatch",
                "status": 409,
                "card_kind": kind,
                "client_id": client,
                "existing": [
                    {"access_id": record.access_id, "card_kind": record.card_kind}
                    for record in sorted(incompatible, key=lambda item: item.access_id)
                ],
            }
        access_id = stable_card_access_id(
            card_kind=kind,
            grantor_subject=grantor,
            client_id=client,
            entry_resource=entry,
        )
        return {
            "ok": True,
            "access_id": access_id,
            "card_kind": kind,
            "existing": False,
            "canonical_access_id": access_id,
        }

    async def resolve_oauth_card_identity(
        self,
        *,
        grantor_subject: str,
        client_id: str,
        entry_resource: str,
        client_metadata: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Resolve the identity carried from consent through token issuance."""
        metadata = normalize_public_client_metadata(client_metadata)
        if not metadata:
            grantor = _clean(grantor_subject)
            client = _clean(client_id)
            entry = _clean(entry_resource)
            try:
                candidates = [
                    record
                    for record in await self._list_all_owner_records(grantor)
                    if record.client_id == client
                    and (
                        record.card_kind == CARD_KIND_AUTOMATION
                        or (
                            record.card_kind == CARD_KIND_CONNECTOR
                            and _clean(record.entry_resource) == entry
                        )
                    )
                ]
            except CardUnavailable as exc:
                return {
                    "ok": False,
                    "error": "delegated_cards_unavailable",
                    "reason": exc.reason,
                    "retryable": True,
                    "status": 503,
                }
            if len(candidates) > 1:
                return {
                    "ok": False,
                    "error": "card_identity_collision",
                    "status": 409,
                    "client_id": client,
                    "access_ids": sorted(record.access_id for record in candidates),
                }
            if candidates:
                record = candidates[0]
                return {
                    "ok": True,
                    "access_id": record.access_id,
                    "card_kind": record.card_kind,
                    "existing": True,
                    "canonical_access_id": stable_card_access_id(
                        card_kind=record.card_kind,
                        grantor_subject=grantor,
                        client_id=client,
                        entry_resource=entry,
                    ),
                }
        return await self.resolve_card_identity(
            grantor_subject=grantor_subject,
            client_id=client_id,
            card_kind=oauth_card_kind(metadata, entry_resource),
            entry_resource=entry_resource,
        )

    async def _committed_revision(self, access_id: str, *, grantor_subject: str) -> int:
        """The write precondition for this id. A revoked or expired card is not
        live authority but still owns the counter, so this is not derived from
        the loaded record."""
        return await self._cards().current_revision(
            access_id, subject_hash=_subject_key(grantor_subject)
        )

    async def _persist_record(
        self, record: AutomationAccessRecord, *, expected_revision: int
    ) -> None:
        await self._cards().persist(
            card_authority_from_record(record),
            card_handles_from_record(record),
            subject_hash=_subject_key(record.grantor_subject),
            expected_revision=expected_revision,
        )

    async def _forget_record(self, record: AutomationAccessRecord) -> None:
        await self._cards().forget(
            card_authority_from_record(record),
            subject_hash=_subject_key(record.grantor_subject),
        )

    async def _list_active_records(
        self, grantor_subject: str, *, now: int | None = None
    ) -> list[AutomationAccessRecord]:
        authorities = await self._cards().list_active(
            subject_hash=_subject_key(grantor_subject), now=now
        )
        return [
            record_from_card(authority)
            for authority in authorities
            if not authority_is_credentialless(authority)
        ]

    async def _save_precondition_conflict(
        self,
        *,
        existing: AutomationAccessRecord,
        active: Any,
        expected_card_revision: int | None,
        expected_catalog_version: str | None,
    ) -> dict[str, Any] | None:
        """``409`` with a refreshed projection when the editor's inputs moved.

        Both preconditions are optional; a caller that sends neither keeps the
        previous last-writer-wins behaviour.
        """
        mismatched: dict[str, Any] = {}
        if expected_card_revision is not None and int(expected_card_revision) != int(
            existing.card_revision
        ):
            mismatched["card_revision"] = {
                "expected": int(expected_card_revision),
                "actual": int(existing.card_revision),
            }
        expected_version = _clean(expected_catalog_version)
        if expected_version and expected_version != _clean(getattr(active, "version", "")):
            mismatched["catalog_version"] = {
                "expected": expected_version,
                "actual": _clean(getattr(active, "version", "")),
            }
        if not mismatched:
            return None

        refreshed = existing.to_public_dict()
        try:
            baseline, reason = await self._baseline_document(_clean(existing.catalog_version))
            active_config = await self._catalog_config(
                active, owner_subject=existing.grantor_subject
            )
            refreshed["catalog_drift"] = (
                drift_unavailable(reason)
                if reason
                else card_drift(
                    card=existing,
                    active=active,
                    baseline=baseline,
                    baseline_confirmed_absent=baseline is None,
                    active_config=active_config,
                )
            )
        except Exception:
            _LOGGER.warning(
                "[automation-access] drift for conflict response failed card=%s",
                existing.access_id,
                exc_info=True,
            )
            refreshed["catalog_drift"] = drift_unavailable("catalog_unavailable")
        return {
            "ok": False,
            "error": "delegated_access_precondition_failed",
            "status": 409,
            "mismatched": mismatched,
            "access": refreshed,
        }

    async def _catalog_drift(
        self,
        records: Iterable[AutomationAccessRecord],
        *,
        owner_subject: str = "",
    ) -> dict[str, dict[str, Any]]:
        """Drift per card: one active read, and one read per distinct baseline.

        Listing explains cards; it never rewrites them, and an unreadable
        comparison disables editing rather than hiding the card.
        """
        rows = list(records)
        try:
            active = await self._active_catalog()
        except CatalogUnavailable as exc:
            return {record.access_id: drift_unavailable(exc.reason) for record in rows}
        except Exception:
            _LOGGER.warning("[automation-access] active catalog unreadable", exc_info=True)
            return {
                record.access_id: drift_unavailable("catalog_unavailable") for record in rows
            }

        active_config = await self._catalog_config(
            active, owner_subject=owner_subject
        )
        baselines: dict[str, tuple[Any, str]] = {}
        out: dict[str, dict[str, Any]] = {}
        for record in rows:
            version = _clean(record.catalog_version)
            if version and version == active.version:
                out[record.access_id] = card_drift(
                    card=record,
                    active=active,
                    baseline=active,
                    active_config=active_config,
                )
                continue
            if version not in baselines:
                baselines[version] = await self._baseline_document(version)
            document, reason = baselines[version]
            if reason:
                out[record.access_id] = drift_unavailable(reason)
                continue
            out[record.access_id] = card_drift(
                card=record,
                active=active,
                baseline=document,
                baseline_confirmed_absent=document is None,
                active_config=active_config,
            )
        return out

    async def _baseline_document(self, version: str) -> tuple[Any, str]:
        """``(document, reason)``. No document and no reason is confirmed absence."""
        if not version:
            return None, ""
        try:
            return await self._catalog_resolver.resolve_version(version), ""
        except CatalogUnavailable as exc:
            return None, (exc.reason or "catalog_unavailable")
        except Exception:
            _LOGGER.warning(
                "[automation-access] baseline unreadable version=%s", version, exc_info=True
            )
            return None, "catalog_unavailable"

    async def _active_catalog(self):
        """The registered catalog a governed decision is taken against."""
        if self._catalog_resolver is None:
            raise CatalogUnavailable("catalog_resolver_not_configured")
        return await self._catalog_resolver.resolve_active()

    async def _catalog_config(
        self, active: Any, *, owner_subject: str = ""
    ) -> Any:
        """The delegable config a save decides against: the registered catalog,
        read with the same parser request paths use, plus resources owned by
        this grantor in host registries such as remote MCP connectors."""
        config = oauth_delegated_config_from_connections(
            getattr(active, "connections", None) or {}
        )
        provider = self._resource_overlay_provider
        owner = _clean(owner_subject)
        if provider is None or not owner:
            return config
        rows = tuple(await provider(owner) or ())
        if not rows:
            return config
        existing = {_clean(getattr(item, "resource", "")) for item in config.resources}
        additions = tuple(
            item
            for item in rows
            if _clean(getattr(item, "resource", ""))
            and _clean(getattr(item, "resource", "")) not in existing
        )
        return replace_fields(config, resources=tuple(config.resources) + additions)

    @staticmethod
    def _version_of(active: Any) -> str:
        version = _clean(getattr(active, "version", ""))
        if not version:
            raise CatalogUnavailable("active_catalog_version_missing")
        return version

    async def _active_catalog_version(self) -> str:
        """The generation a save is stamped with.

        A card may not be written without naming the catalog its selection is
        defined against, so an absent resolver fails closed rather than
        producing an unstamped record.
        """
        if self._catalog_resolver is None:
            raise CatalogUnavailable("catalog_resolver_not_configured")
        active = await self._catalog_resolver.resolve_active()
        version = _clean(getattr(active, "version", ""))
        if not version:
            raise CatalogUnavailable("active_catalog_version_missing")
        return version

    async def active_catalog_version(self) -> str:
        """Return the catalog revision against which a new card write resolves."""

        return await self._active_catalog_version()

    async def _available_inventory(
        self,
        user: Mapping[str, Any],
        *,
        requested_grants: Iterable[str] = (),
        config: Any = None,
    ) -> AuthorityGrantInventory:
        provider = PlatformAuthorityInventoryProvider((config or self._config).capabilities)
        return await provider.list_delegable_grants(
            platform_identity_from_user(user),
            requested_grants=requested_grants,
        )

    async def _offer_config(self, *, owner_subject: str = "") -> Any | None:
        """The catalog a form may offer from, or ``None`` when it cannot be
        established. Offering from effective props would promise what a save,
        which reads the catalog, then refuses."""
        try:
            return await self._catalog_config(
                await self._active_catalog(), owner_subject=owner_subject
            )
        except CatalogUnavailable:
            return None

    async def oauth_consent_config(self, *, grantor_subject: str) -> Any | None:
        """The registered catalog plus resources owned by this grantor.

        OAuth discovery remains anonymous and descriptor-only. The browser
        consent step calls this after platform login, when owner-scoped
        resources such as external MCP connectors may be shown safely.
        """

        return await self._offer_config(owner_subject=grantor_subject)

    async def grant_options(self, user: Mapping[str, Any]) -> list[dict[str, Any]]:
        offer = await self._offer_config(owner_subject=_subject_from_user(user))
        if offer is None:
            return []
        inventory = await self._available_inventory(user, config=offer)
        return [item.to_dict() for item in inventory.grants]

    async def _named_service_options(
        self,
        config: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Project the configured namespace boundary and provider requirements.

        The namespace/tool tree comes directly from the same descriptor-backed
        catalog used by OAuth consent. Connected-account requirements are
        copied verbatim from each live provider's discovery metadata. On
        credential creation, the selected operation subset narrows this same
        ``named_services`` policy object; no parallel policy model is created.
        """

        namespaces = NamedServiceBoundaryCatalog(config).list_public()
        if not namespaces:
            return []

        discovery = self._named_service_discovery
        if discovery is None and self._named_service_discovery_factory is not None:
            discovery = self._named_service_discovery_factory(
                redis=self._redis,
                tenant=self._tenant,
                project=self._project,
            )
        if discovery is None:
            _LOGGER.warning(
                "[connection-hub.delegated_access] named-service account "
                "requirements unavailable: discovery is not configured"
            )
            return namespaces
        for namespace in namespaces:
            namespace_name = _clean(namespace.get("namespace"))
            if not namespace_name:
                continue
            try:
                entries = await discovery.entries_for_namespace(namespace_name)
            except Exception:
                _LOGGER.debug(
                    "[connection-hub.delegated_access] named-service provider requirements unavailable namespace=%s",
                    namespace_name,
                    exc_info=True,
                )
                continue

            requirements: list[dict[str, Any]] = []
            seen: set[str] = set()
            for entry in entries or ():
                spec = getattr(entry, "spec", None)
                metadata = getattr(spec, "metadata", None)
                raw_requirements = (
                    metadata.get("connected_accounts")
                    if isinstance(metadata, Mapping)
                    else None
                )
                if not isinstance(raw_requirements, (list, tuple)):
                    continue
                for raw_requirement in raw_requirements:
                    if not isinstance(raw_requirement, Mapping):
                        continue
                    requirement = copy.deepcopy(dict(raw_requirement))
                    signature = json.dumps(
                        requirement,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                    if signature in seen:
                        continue
                    seen.add(signature)
                    requirements.append(requirement)
            if requirements:
                namespace["connected_accounts"] = requirements
        return namespaces

    async def resource_options(self, user: Mapping[str, Any]) -> list[dict[str, Any]]:
        """The catalog projected through what THIS grantor may delegate.

        A card can never carry more than its grantor holds, so a claim outside
        their delegable set is not offered — the same rule the admin-only row
        above already applies to whole resources. The rows come from the
        registered catalog, which is what a save decides against; an
        unestablished catalog offers nothing rather than offering props.
        """
        offer = await self._offer_config(owner_subject=_subject_from_user(user))
        if offer is None:
            return []
        platform_admin = _is_platform_admin(user)
        delegable = set(
            (await self._available_inventory(user, config=offer)).grant_names()
        )
        out: list[dict[str, Any]] = []
        for resource in offer.resources:
            if resource.admin_only and not platform_admin:
                continue
            option = {
                "resource": resource.resource,
                "label": resource.label or resource.resource,
                "kind": (
                    _clean(getattr(resource, ROW_ATTR_KIND, ""))
                    or RESOURCE_KIND_CATALOG
                ),
                "provider_id": _clean(
                    getattr(resource, ROW_ATTR_PROVIDER, "")
                ),
                "identity_scope": resource.identity_scope,
                "grants": [
                    grant for grant in resource.grants if grant in delegable
                ],
                "admin_only": bool(resource.admin_only),
                "operations": [
                    {
                        "name": tool.name,
                        "label": tool.label,
                        "description": tool.description,
                        "grants": list(tool.grants),
                    }
                    for tool in resource.tools
                    if _grants_delegable(tool.grants, delegable)
                ],
            }
            selector_type = str(
                getattr(resource, "selector_type", "") or ""
            ).strip()
            if selector_type:
                option["selector_type"] = selector_type
                option["selector_context"] = {
                    "tenant": self._tenant,
                    "project": self._project,
                }
            if bool(getattr(resource, "resource_selection", False)):
                from connection_hub.delegated_credentials.oauth.consent import (
                    resource_selection_rows,
                )

                selectable = resource_selection_rows(
                    tuple(option["grants"]),
                    config=offer,
                    resource=resource.resource,
                )
                option["resource_selection"] = True
                option["selectable_resources"] = [
                    row["resource"]
                    for row in selectable
                    if _clean(row.get("resource"))
                ]
            if isinstance(resource.named_services, Mapping):
                named_services = _delegable_named_service_options(
                    await self._named_service_options(resource.named_services),
                    delegable,
                )
                if named_services:
                    option["named_services"] = named_services
            out.append(option)
        return out

    def _entry_resource_for(self, record: Any, *, config: Any = None) -> str:
        """The door an OAuth card's client connected to. The stored value when
        the consent wrote it; for an OAuth card written before the field
        existed, the card resource whose row is a selection door (a proxy), or
        failing that its first catalog-row resource. Empty for manual and
        resident cards: those hold any set of doors by construction."""
        if _clean(getattr(record, "source", "")) != ACCESS_SOURCE_OAUTH:
            return ""
        stored = _clean(getattr(record, "entry_resource", ""))
        if stored:
            return stored
        resources = [
            _clean(resource)
            for resource in dict(getattr(record, "resource_grants", None) or {})
            if _clean(resource) and _clean(resource) != "*"
        ]
        rows = [(resource, self._configured_resource(resource, config=config)) for resource in resources]
        for resource, row in rows:
            if row is not None and bool(getattr(row, "resource_selection", False)):
                return resource
        for resource, row in rows:
            kind = _clean(getattr(row, ROW_ATTR_KIND, "")) if row is not None else ""
            if row is not None and (not kind or kind == RESOURCE_KIND_CATALOG):
                return resource
        return resources[0] if resources else ""

    def _reachable_through_door(
        self, entry_resource: str, *, config: Any = None
    ) -> set[str]:
        """Resources an MCP endpoint explicitly makes selectable.

        A normal MCP client knows only its entry endpoint. A selection endpoint,
        such as the remote-MCP proxy, may expose child resources through that
        endpoint; every other service remains unreachable to that client.
        """

        door = _clean(entry_resource)
        if not door:
            return set()
        catalog = config or self._config
        row = self._configured_resource(door, config=catalog)
        if row is None or not bool(getattr(row, "resource_selection", False)):
            return set()
        from connection_hub.delegated_credentials.oauth.consent import (
            resource_selection_rows,
        )

        try:
            rows = resource_selection_rows(
                tuple(getattr(row, "grants", ()) or ()),
                config=catalog,
                resource=door,
            )
        except Exception:  # noqa: BLE001 - an unreadable catalog offers nothing
            return set()
        return {
            _clean(item.get("resource"))
            for item in rows
            if _clean(item.get("resource"))
        }

    def _oauth_allowed_resources(
        self,
        *,
        entry_resource: str,
        client_metadata: Mapping[str, Any] | None,
        config: Any,
    ) -> set[str] | None:
        """``None`` for multi-resource delivery, else exact MCP reachability."""

        if client_uses_full_card_catalog(client_metadata):
            return None
        entry = _clean(entry_resource)
        entry_key = entry
        if callable(getattr(config, "card_selector_config", None)):
            entry_key, _literal = resolve_declared_resource(config, entry)
        allowed = {entry_key}
        allowed.update(self._reachable_through_door(entry, config=config))
        return allowed

    def _configured_resource(
        self, resource: str, *, config: Any = None
    ) -> Any | None:
        """The catalog row governing a card resource. Matched, not compared:
        a card key is the row's pattern or a concrete URL. ``config`` defaults
        to the process descriptor; a save passes the registered catalog."""
        text = _clean(resource)
        if not text:
            return None
        try:
            validate_secret_card_resource(
                text,
                tenant=self._tenant,
                project=self._project,
            )
        except SecretResourceError:
            return None
        return (config or self._config).card_selector_config(text)

    def _card_resource_keys(
        self, resources: Iterable[str], *, config: Any = None
    ) -> tuple[str, ...]:
        """Card resources expressed as the catalog keys that govern them.

        OAuth Cards now store declared selectors. Cards written before that
        rule may still hold the concrete URL they connected to. Comparing that
        URL with its catalog pattern as plain strings made the editor offer a
        service the Card already held. Match legacy keys through the catalog;
        keep a resource whose row has left the catalog literal so the guard can
        report that drift.
        """

        keys: list[str] = []
        for resource in resources or ():
            text = _clean(resource)
            if not text:
                continue
            row = self._configured_resource(text, config=config)
            keys.append(_clean(getattr(row, "resource", "")) or text)
        return tuple(keys)

    def _configured_resource_pairs(
        self, resources: Iterable[str], *, config: Any = None
    ) -> tuple[tuple[str, Any], ...]:
        """``(card resource, governing row)``. Grants and the selection are
        keyed by the card resource, the descriptor subtree by the row."""
        selected = _as_list(list(resources))
        pairs: list[tuple[str, Any]] = []
        missing: list[str] = []
        for resource in selected:
            cfg = self._configured_resource(resource, config=config)
            if cfg is None:
                missing.append(resource)
            else:
                pairs.append((resource, cfg))
        if missing:
            raise ValueError("unknown delegated resource(s): " + ", ".join(missing))
        return tuple(pairs)

    def _configured_resources(self, resources: Iterable[str]) -> tuple[Any, ...]:
        return tuple(cfg for _, cfg in self._configured_resource_pairs(resources))

    def _resource_grants(self, resource_grants: Mapping[str, Any]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for resource, grants in dict(resource_grants or {}).items():
            resource_value = _clean(resource)
            selected = _as_list(grants)
            if resource_value and selected:
                out[resource_value] = selected
        return out

    def _declared_resource_keys(
        self,
        config: OAuthDelegatedClientConfig,
        resource_grants: Mapping[str, list[str]],
    ) -> tuple[dict[str, list[str]], dict[str, str]]:
        return resolve_declared_resource_keys(config, resource_grants)

    def _declared_named_service_selection(
        self,
        config: OAuthDelegatedClientConfig,
        selection: NamedServiceSelection,
    ) -> NamedServiceSelection:
        """Express an exact named-service choice under declared resource keys."""

        if not selection.is_exact:
            return selection
        resolved: dict[str, dict[str, list[str]]] = {}
        for resource, namespaces in selection.operations.items():
            key, _literal = resolve_declared_resource(config, resource)
            target = resolved.setdefault(key, {})
            for namespace, operations in namespaces.items():
                held = target.setdefault(namespace, [])
                for operation in operations:
                    if operation not in held:
                        held.append(operation)
        return NamedServiceSelection.exact(resolved)

    def _canonical_oauth_record(
        self,
        record: "AutomationAccessRecord",
        *,
        config: OAuthDelegatedClientConfig,
    ) -> "AutomationAccessRecord":
        """Project legacy host-pinned authority onto its declared selectors.

        The concrete OAuth resource remains ``entry_resource`` and therefore
        remains part of the Card's stable identity. Only authority maps are
        canonicalized. Unknown resources stay literal; ``*`` is never selected
        as a substitute for one concrete resource.
        """

        grants, _rewritten_grants = resolve_declared_resource_keys(
            config,
            record.resource_grants,
        )
        operations, _rewritten_operations = resolve_declared_resource_keys(
            config,
            record.resource_operations,
        )
        acceptance: dict[str, ResourceAcceptance] = {}
        for resource, accepted in record.resource_acceptance.items():
            key, _literal = resolve_declared_resource(config, resource)
            # Prefer evidence already stored under the canonical key when a
            # legacy concrete key and its declared selector both exist.
            if key not in acceptance or resource == key:
                acceptance[key] = accepted
        return replace_fields(
            record,
            resource_grants={key: tuple(values) for key, values in grants.items()},
            resource_operations={
                key: tuple(values) for key, values in operations.items()
            },
            operations=operation_union(operations),
            named_service_operations=self._declared_named_service_selection(
                config,
                record.named_service_operations,
            ),
            resource_acceptance=acceptance,
        )

    def _named_service_operation_selection(
        self,
        value: Any,
    ) -> NamedServiceSelection | None:
        """Parse a submitted selection. ``None`` means the field was omitted."""
        if value is None:
            return None
        if isinstance(value, str):
            if _clean(value) != NAMED_SERVICE_OPERATIONS_ALL:
                raise ValueError("named_service_operations must be '*' or an object")
            return NamedServiceSelection.all()
        if not isinstance(value, Mapping):
            raise ValueError("named_service_operations must be an object")
        for resource, raw_namespaces in value.items():
            if _clean(resource) and not isinstance(raw_namespaces, Mapping):
                raise ValueError(
                    f"named_service_operations[{_clean(resource)!r}] must be an object"
                )
        try:
            return NamedServiceSelection.exact(value)
        except CardRecordError as exc:
            raise ValueError(str(exc)) from exc

    # Delegators; the managed guard calls the same functions per request.
    @staticmethod
    def _operation_grants(policy: Mapping[str, Any], fallback: Mapping[str, Any]) -> set[str]:
        return _named_service_operation_grants(policy, fallback)

    def _narrow_named_service_config(
        self,
        *,
        config: Mapping[str, Any],
        selected: Mapping[str, list[str]],
        grants: Iterable[str],
        resource: str,
    ) -> dict[str, Any]:
        return narrow_named_service_config(
            config=config, selected=selected, grants=grants, resource=resource,
        )

    @staticmethod
    def _merge_named_service_configs(
        target: dict[str, Any],
        source: Mapping[str, Any],
    ) -> dict[str, Any]:
        return merge_named_service_configs(target, source)

    async def list_access(self, user: Mapping[str, Any]) -> dict[str, Any]:
        grantor_subject = _subject_from_user(user)
        if not grantor_subject:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}

        now = int(time.time())
        try:
            records_found = await self._list_owner_records(grantor_subject)
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_cards_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        drift_by_card = await self._catalog_drift(
            records_found, owner_subject=grantor_subject
        )
        try:
            active = await self._active_catalog()
        except CatalogUnavailable:
            active = None
        # Without a catalog a card cannot be explained, only listed and revoked.
        listing_config = (
            await self._catalog_config(active, owner_subject=grantor_subject)
            if active is not None
            else None
        )
        resource_option_rows = await self.resource_options(user)
        platform_admin = _is_platform_admin(user)
        records = []
        for record in records_found:
            item = record.to_public_dict()
            effective_control = await self._effective_control_view(record)
            item["control_card"] = effective_control
            item["project_control"] = effective_control
            # Expired cards stay listed so their grants can be renewed; the
            # flag is the server's word on it, read against its own clock.
            item["expired"] = bool(record.expires_at and record.expires_at <= now)
            item["catalog_drift"] = drift_by_card[record.access_id]
            # The resident profile behind an agent card, and whether this card
            # already lives under the profile's stable id. A legacy card reads
            # ``stable_identity: false`` until its profile's next grant folds it.
            profile = ResidentCallerProfile.parse(
                record.grantor_subject,
                record.client_id,
            )
            if profile is not None:
                item["caller_profile"] = profile.to_dict()
                item["stable_identity"] = record.access_id == profile.access_id
            # Which owner-visible delegable resources may join this card, and
            # why the others may not. The editor renders the picker from this;
            # a resident ceiling (Projection) narrows it further downstream.
            # A multi-resource client receives a general card through OAuth.
            # An ordinary OAuth MCP client knows only the endpoint it connected
            # to and any child resources explicitly exposed through that door.
            entry_resource = self._entry_resource_for(record, config=listing_config)
            if entry_resource:
                item["entry_resource"] = entry_resource
            item["resource_offers"] = compatible_resource_offers(
                card_resources=self._card_resource_keys(
                    record.resource_grants, config=listing_config
                ),
                card_identity_scope=record.identity_scope,
                options=resource_option_rows,
                platform_admin=platform_admin,
                entry_resource=entry_resource,
                reachable=(
                    self._oauth_allowed_resources(
                        entry_resource=entry_resource,
                        client_metadata=record.client_metadata,
                        config=listing_config,
                    )
                    if record.source == ACCESS_SOURCE_OAUTH
                    and listing_config is not None
                    else None
                ),
            )
            # Reported, never applied: listing does not rewrite a record.
            ambiguity = pre_migration_ambiguity(
                record,
                named_service_resources=[
                    resource
                    for resource in record.resource_grants
                    if isinstance(
                        getattr(
                            self._configured_resource(resource, config=listing_config),
                            "named_services",
                            None,
                        ),
                        Mapping,
                    )
                ],
                offered=(
                    self._catalog_named_service_operations(active, record.resource_grants)
                    if active is not None
                    else None
                ),
            )
            if ambiguity is not None:
                item["migration"] = ambiguity
            # Card resource -> the catalog row it resolves to. A surface must
            # not re-derive this: the match is the resolver's, not a comparison.
            rows = {}
            for resource in record.resource_grants:
                cfg = self._configured_resource(resource, config=listing_config)
                if cfg is not None:
                    rows[resource] = str(getattr(cfg, "resource", "") or "")
            if rows:
                item["catalog_row_by_resource"] = rows
            records.append(item)

        records.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return {
            "ok": True,
            "platform_user_id": grantor_subject,
            "grant_options": await self.grant_options(user),
            "resources": resource_option_rows,
            "items": records,
        }

    def _resolve_resource_operations(
        self,
        *,
        resource_grants: Mapping[str, Iterable[str]],
        resource_operations: Mapping[str, Any] | None,
        legacy_operations: Iterable[str] = (),
        default_all: bool = False,
        prune_unknown: bool = False,
        config: Any = None,
    ) -> dict[str, list[str]]:
        source = config or self._config
        available: dict[str, set[str]] = {}
        accepts_endpoint_operations: dict[str, bool] = {}
        for resource, grants in resource_grants.items():
            selector = getattr(source, "card_selector_config", None)
            configured = selector(resource) if callable(selector) else None
            accepts_endpoint_operations[resource] = (
                _clean(getattr(configured, "resource", "")).rstrip("/") == "*"
            )
            available[resource] = {
                _clean(getattr(operation, "name", ""))
                for operation in source.tools_for_scopes(
                    list(grants or ()), resource=resource or None
                )
                if _clean(getattr(operation, "name", ""))
            }

        if resource_operations is not None:
            selected = normalize_resource_operations(resource_operations)
            unknown_resources = sorted(set(selected) - set(resource_grants))
            if unknown_resources:
                raise ValueError(
                    "operation selection names unknown resource(s): "
                    + ", ".join(unknown_resources)
                )
            resolved: dict[str, list[str]] = {}
            for resource in resource_grants:
                requested = set(selected.get(resource, ()))
                if accepts_endpoint_operations.get(resource):
                    # The all-application row is an authority container for
                    # operation identities published by application catalogs.
                    # Its operations are not statically enumerated in the
                    # Connection Hub descriptor, so preserve the exact refs
                    # selected from that catalog. Active-catalog and runtime
                    # boundaries still fail closed when an operation is stale
                    # or absent from the live application.
                    resolved[resource] = sorted(requested)
                    continue
                unknown = sorted(requested - available.get(resource, set()))
                if unknown and not prune_unknown:
                    raise ValueError(
                        f"unknown or unauthorized operation(s) for {resource}: "
                        + ", ".join(unknown)
                    )
                resolved[resource] = sorted(
                    requested & available.get(resource, set())
                )
            return resolved

        legacy = {_clean(operation) for operation in legacy_operations if _clean(operation)}
        if legacy:
            offered_any = set().union(*available.values()) if available else set()
            unknown = sorted(legacy - offered_any)
            if unknown:
                raise ValueError(
                    "unknown or unauthorized operation(s): " + ", ".join(unknown)
                )
            return {
                resource: sorted(legacy & offered)
                for resource, offered in available.items()
            }
        if default_all:
            return {
                resource: sorted(offered) for resource, offered in available.items()
            }
        return {resource: [] for resource in resource_grants}

    async def create_access(
        self,
        user: Mapping[str, Any],
        *,
        label: str,
        resource_grants: Mapping[str, Any],
        operations: Iterable[str] = (),
        resource_operations: Mapping[str, Any] | None = None,
        named_service_operations: Mapping[str, Any] | str | None = None,
        account_scope: Mapping[str, Any] | None = None,
        properties: Mapping[str, Any] | None = None,
        ttl_seconds: Any = None,
        client_id: str | None = None,
        merge_existing: bool = True,
    ) -> dict[str, Any]:
        """Create a delegated-access grant the current user grants to a client.

        ``client_id`` is normally omitted — a fresh random ``automation:…`` client
        is minted per grant. When a caller passes a DETERMINISTIC client_id (a
        hosted agent's ``kdcube-agent:<app>:<agent>`` identity), the grant is keyed
        to it and DEDUPLICATED: one record per (grantor, client, resources).
        With ``merge_existing`` (the default) a re-grant MERGES claims and
        narrowing into the record — sequential one-click grants accumulate.
        ``merge_existing=False`` is the EDIT semantics: the submitted selection
        REPLACES the record exactly (the user unchecked something). The
        credential is built + bound identically either way, so the minted token
        passes the @mcp guard the same as any Delegated-By-KDCube grant."""
        grantor_subject = _subject_from_user(user)
        if not grantor_subject:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}
        refusal = _delegate_mutation_refusal(user)
        if refusal is not None:
            return refusal

        # Resolved before anything is decided: a card is written against the
        # registered catalog, never against effective props.
        try:
            active = await self._active_catalog()
            catalog_version = self._version_of(active)
        except CatalogUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_catalog_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        catalog_config = await self._catalog_config(
            active, owner_subject=grantor_subject
        )

        selected_resource_grants = self._resource_grants(resource_grants)
        selected_resource_grants, host_pinned = self._declared_resource_keys(
            catalog_config, selected_resource_grants
        )
        if host_pinned:
            _LOGGER.info(
                "delegated access resolved host-pinned resources to declared doors: %s",
                host_pinned,
            )
        if _clean(client_id) and merge_existing:
            # An incremental agent demand may add only an exact operation or
            # account binding because the card already holds its door claims.
            # Keep the submitted door key long enough to derive that existing
            # deterministic card id; the post-merge non-empty check below still
            # prevents creation of a claim-less card.
            for raw_resource in dict(resource_grants or {}):
                resource_value = _clean(raw_resource)
                if not resource_value:
                    continue
                # The declared door, for the same reason as above. Re-adding the
                # submitted URL here would put the host-pinned key straight back
                # onto the card the resolution above just took it off.
                selected_resource_grants.setdefault(
                    host_pinned.get(resource_value, resource_value), []
                )
        try:
            selected_named_service_operations = self._named_service_operation_selection(
                named_service_operations
            )
        except ValueError as exc:
            return {
                "ok": False,
                "error": "invalid_named_service_operation_selection",
                "message": str(exc),
            }
        selected_resources = list(selected_resource_grants)
        if catalog_config.resources and not selected_resources:
            return {"ok": False, "error": "delegated_access_requires_resource_grants"}

        selected_grants = _as_list([
            grant
            for grants_for_resource in selected_resource_grants.values()
            for grant in grants_for_resource
        ])

        # The resource FIRST: a claim for an endpoint this deployment never put
        # in the hub's catalog is not "a claim you may not delegate" — it is an
        # endpoint nobody here has decided to expose, and the two need different
        # answers. The hub's catalog is a SUBSET of what apps declare: an app
        # states what its surface can delegate, this deployment decides how much
        # of that may be asked for here, and until it says so the answer is no.
        try:
            resource_pairs = (
                self._configured_resource_pairs(selected_resources, config=catalog_config)
                if catalog_config.resources else ()
            )
        except ValueError:
            return {
                "ok": False,
                "error": "delegated_access_unknown_resources",
                "resources": selected_resources,
                "message": (
                    "This deployment has not made that endpoint delegable. Add it to "
                    "connection-hub@1-0 `connections.delegated_credentials.oauth.resources`, "
                    "with the tools it may grant, before access to it can be asked for."
                ),
            }
        resource_configs = tuple(cfg for _, cfg in resource_pairs)

        inventory = await self._available_inventory(
            user, requested_grants=selected_grants, config=catalog_config
        )
        available = set(inventory.grant_names())
        denied = [grant for grant in selected_grants if grant not in available]
        if denied:
            return {
                "ok": False,
                "error": "delegated_access_grants_not_delegable",
                "grants": denied,
                "message": (
                    "These permissions are not yours to delegate. A card never carries "
                    "more than its grantor holds. Who may delegate each permission is "
                    "declared in connection-hub@1-0 "
                    "`connections.delegated_credentials.oauth.capabilities`, as "
                    "`delegable_roles` and `delegable_permissions`."
                ),
            }
        admin_required = [cfg.resource for cfg in resource_configs if cfg.admin_only]
        if admin_required and not _is_platform_admin(user):
            return {
                "ok": False,
                "error": "delegated_access_resource_requires_admin",
                "resources": admin_required,
            }
        cfg_by_resource = dict(resource_pairs)
        if selected_named_service_operations is not None and selected_named_service_operations.is_exact:
            # Dropped, not rejected: see the matching note on the update path. A
            # card pinned to an older catalog version keeps entries for services
            # withdrawn since, and rejecting on them leaves the card unsavable
            # forever. Dropping only narrows the card.
            stale_selection_resources = sorted(
                set(selected_named_service_operations.operations) - set(selected_resources)
            )
            if stale_selection_resources:
                _LOGGER.info(
                    "[automation-access] dropping named-service operations for "
                    "resources this request does not select: %s",
                    stale_selection_resources,
                )
                selected_named_service_operations = NamedServiceSelection.exact({
                    resource: namespaces
                    for resource, namespaces in selected_named_service_operations.operations.items()
                    if resource in set(selected_resources)
                })
        for resource_value, grants_for_resource in selected_resource_grants.items():
            cfg = cfg_by_resource.get(resource_value)
            if cfg is None:
                continue
            allowed_for_resource = set(catalog_config.supported_scopes(resource_value))
            disallowed = [
                grant
                for grant in grants_for_resource
                if grant not in allowed_for_resource
                and not (
                    resource_value == APPLICATION_API_RESOURCE
                    and platform_role_allowed_by(grant, allowed_for_resource)
                )
            ]
            if disallowed:
                return {
                    "ok": False,
                    "error": "delegated_access_grants_not_allowed_for_resources",
                    "grants": disallowed,
                    "resource": resource_value,
                }
        identity_scopes = {
            _clean(getattr(cfg, "identity_scope", "") or "grantor")
            for cfg in resource_configs
        }
        if len(identity_scopes) > 1:
            return {
                "ok": False,
                "error": "delegated_access_resources_have_conflicting_identity_scopes",
                "resources": selected_resources,
                "identity_scopes": sorted(identity_scopes),
                "message": (
                    "A resident agent has one Card, and its current credential can "
                    "carry resources under one acting identity. These resources use "
                    "different identity scopes: " + ", ".join(sorted(identity_scopes))
                    + ". Connection Hub refused the request without creating another "
                    "Card for the same agent."
                ),
            }
        identity_scope = next(iter(identity_scopes), "grantor")

        # Per-agent, per-account claim binding: {provider: {account_id: [claims]}}.
        account_scope_provided = account_scope is not None
        selected_account_scope: dict[str, dict[str, list[str]]] = {
            provider: {account_id: list(claims) for account_id, claims in accounts.items()}
            for provider, accounts in normalize_account_scope(account_scope).items()
        }

        requested_client_id = _clean(client_id)
        access_source = ACCESS_SOURCE_MANUAL
        created_at_override: int | None = None
        existing: AutomationAccessRecord | None = None
        if requested_client_id:
            # A deterministic client (a resident agent above all) has ONE stable
            # card, independent of the resources it holds (cards/identity.py).
            # Re-consent MERGES into it: sequential
            # one-click grants (memories today, slack tomorrow) accumulate on
            # the same card; a replace would silently revoke the earlier
            # consent. Records written under the older resource-dependent id
            # are folded into the stable card first, or the fold reports why it
            # cannot be done without widening authority.
            client_id = requested_client_id
            access_source = ACCESS_SOURCE_AGENT
            identity = await self.resolve_card_identity(
                grantor_subject=grantor_subject,
                client_id=client_id,
                card_kind=CARD_KIND_AGENT,
            )
            if identity.get("ok") is not True:
                return identity
            access_id = _clean(identity.get("access_id"))
            try:
                existing = await self._load_record(
                    access_id, grantor_subject=grantor_subject
                )
            except CardUnavailable as exc:
                return {
                    "ok": False,
                    "error": "delegated_cards_unavailable",
                    "reason": exc.reason,
                    "retryable": True,
                    "status": 503,
                }
            if existing is not None:
                existing_scope = _clean(existing.identity_scope) or "grantor"
                if existing_scope != identity_scope:
                    return {
                        "ok": False,
                        "error": "delegated_access_resources_have_conflicting_identity_scopes",
                        "resources": sorted(
                            set(existing.resource_grants) | set(selected_resources)
                        ),
                        "identity_scopes": sorted({existing_scope, identity_scope}),
                        "message": (
                            "This resident agent already has one Card whose resources "
                            f"act as {existing_scope}. The requested resource acts as "
                            f"{identity_scope}; Connection Hub refused the change rather "
                            "than creating a second Card for the same agent."
                        ),
                    }
                created_at_override = existing.created_at or None
                if not merge_existing and not account_scope_provided:
                    # Replace only dimensions the caller actually submitted.
                    # Omitted account_scope preserves the current binding;
                    # an explicit {} below clears it.
                    selected_account_scope = {
                        provider: {
                            account_id: list(claims)
                            for account_id, claims in accounts.items()
                        }
                        for provider, accounts in existing.account_scope.items()
                    }
                if not merge_existing and selected_named_service_operations is None:
                    # Same rule for the namespace narrowing: omitting it keeps
                    # the record's own state, including an explicit empty one.
                    selected_named_service_operations = _inherited_selection(
                        existing, selected_resource_grants
                    )
            if existing is not None and merge_existing:
                for resource_key, held in existing.resource_grants.items():
                    merged = list(selected_resource_grants.get(resource_key, []))
                    for grant in held:
                        if grant not in merged:
                            merged.append(grant)
                    selected_resource_grants[resource_key] = merged
                selected_grants = _as_list([
                    grant
                    for grants_for_resource in selected_resource_grants.values()
                    for grant in grants_for_resource
                ])
                # The card's own side is frozen before it is merged into: a
                # consent screen names a door and its claims, never the inner
                # namespaces, so it is not the reviewed explicit "*" that may
                # re-pin. Computed after the claim merge above, because the
                # freeze is filtered by the claims THIS save persists.
                inherited = _inherited_selection(existing, selected_resource_grants)
                if selected_named_service_operations is None:
                    selected_named_service_operations = inherited
                else:
                    # A one-click extension accumulates: the merged boundary is
                    # the wider of what the card holds and what was submitted.
                    selected_named_service_operations = (
                        selected_named_service_operations.union(inherited)
                    )
                # Merge the account binding per provider AND per account: union
                # the claim lists (a one-click grant accumulates; a REPLACE edit
                # sends the full desired scope and overwrites, same as
                # resource_grants).
                for provider, held_accounts in existing.account_scope.items():
                    target_accounts = selected_account_scope.setdefault(provider, {})
                    for account_id, held_claims in held_accounts.items():
                        merged_claims = list(target_accounts.get(account_id, []))
                        for claim in held_claims:
                            if claim not in merged_claims:
                                merged_claims.append(claim)
                        target_accounts[account_id] = merged_claims
        else:
            client_id = (
                f"{AUTOMATION_CLIENT_PREFIX}:"
                f"{secrets.token_urlsafe(10)}"
            )
            access_id = stable_automation_access_id(grantor_subject, client_id)

        # An exact operation-only demand is a valid incremental update for an
        # existing deterministic agent card: the merge above restores that
        # card's already-granted door claims before the new operation is
        # materialized. It may never create a claim-less card, and replace
        # semantics may never preserve claims the caller omitted.
        if not selected_grants:
            return {"ok": False, "error": "delegated_access_requires_resource_grants"}

        # A create call that names no selection selects nothing. `"*"` is stored
        # only when the user chose every operation the current catalog offers;
        # claims do not select operations on their own.
        if selected_named_service_operations is None:
            selected_named_service_operations = NamedServiceSelection.none()
        elif selected_named_service_operations.is_unknown:
            selected_named_service_operations = (
                _inherited_selection(existing, selected_resource_grants)
                if existing is not None
                else NamedServiceSelection.none()
            )
        try:
            committed_revision = await self._committed_revision(
                access_id, grantor_subject=grantor_subject
            )
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_cards_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }

        named_services: dict[str, Any] = {}
        for resource_value, cfg in resource_pairs:
            if isinstance(cfg.named_services, Mapping):
                try:
                    selected_policy = named_service_policy_for_resource(
                        named_services=cfg.named_services,
                        resource=resource_value,
                        selection=_selection_policy_argument(selected_named_service_operations),
                        grants=_effective_named_service_grants(
                            selected_resource_grants.get(resource_value, []),
                            account_scope=selected_account_scope,
                            required=catalog_config.supported_scopes(resource_value),
                        ),
                    )
                except ValueError as exc:
                    return {
                        "ok": False,
                        "error": "invalid_named_service_operation_selection",
                        "message": str(exc),
                    }
                named_services = self._merge_named_service_configs(
                    named_services,
                    selected_policy,
                )
        legacy_operations = _as_list(list(operations))
        operation_selection = (
            normalize_resource_operations(resource_operations)
            if resource_operations is not None
            else None
        )
        default_all = existing is None and operation_selection is None and not legacy_operations
        if existing is not None:
            existing_operations = self._resolve_resource_operations(
                resource_grants=selected_resource_grants,
                resource_operations={
                    resource: tuple(selected)
                    for resource, selected in existing.resource_operations.items()
                    if resource in selected_resource_grants
                },
                prune_unknown=True,
                config=catalog_config,
            )
            if operation_selection is None and not legacy_operations:
                operation_selection = existing_operations
                # A merge may add a new resource. Preserve reviewed authority on
                # retained resources and initialize only the new resource from
                # its own catalog.
                missing = {
                    resource: selected_resource_grants[resource]
                    for resource in selected_resource_grants
                    if resource not in operation_selection
                }
                if merge_existing and missing:
                    defaults = self._resolve_resource_operations(
                        resource_grants=missing,
                        resource_operations=None,
                        default_all=True,
                        config=catalog_config,
                    )
                    operation_selection = {**operation_selection, **defaults}
            elif merge_existing and operation_selection is not None:
                merged_operations = {
                    resource: list(values)
                    for resource, values in existing_operations.items()
                }
                for resource, values in operation_selection.items():
                    held = merged_operations.setdefault(resource, [])
                    for operation in values:
                        if operation not in held:
                            held.append(operation)
                operation_selection = merged_operations
            elif merge_existing and legacy_operations:
                legacy_selection = self._resolve_resource_operations(
                    resource_grants=selected_resource_grants,
                    resource_operations=None,
                    legacy_operations=legacy_operations,
                    config=catalog_config,
                )
                merged_operations = {
                    resource: list(values)
                    for resource, values in existing_operations.items()
                }
                for resource, values in legacy_selection.items():
                    held = merged_operations.setdefault(resource, [])
                    for operation in values:
                        if operation not in held:
                            held.append(operation)
                operation_selection = merged_operations
                legacy_operations = ()
        selected_resource_operations = self._resolve_resource_operations(
            resource_grants=selected_resource_grants,
            resource_operations=operation_selection,
            legacy_operations=legacy_operations,
            default_all=default_all,
            config=catalog_config,
        )
        selected_operations = list(operation_union(selected_resource_operations))
        selected_properties = (
            copy.deepcopy(dict(existing.properties or {}))
            if existing is not None
            else {}
        )
        if properties is not None:
            selected_properties.update(copy.deepcopy(dict(properties)))
        try:
            submitted_application_policy = (
                application_operation_role_policy(
                    selected_properties,
                    resource_grants=selected_resource_grants,
                )
                if APPLICATION_API_RESOURCE in selected_resource_grants
                else None
            )
            authority_grants = list(selected_grants)
            if submitted_application_policy is not None:
                authority_grants.extend(
                    [
                        submitted_application_policy.default_role,
                        *submitted_application_policy.operation_roles.values(),
                    ]
                )
            authority_grants = _as_list(authority_grants)
            inventory = await self._available_inventory(
                user,
                requested_grants=authority_grants,
                config=catalog_config,
            )
            _application_role_policy(
                properties=selected_properties,
                resource_grants=selected_resource_grants,
                resource_operations=selected_resource_operations,
                delegable_roles=inventory.grant_names(),
                allowed_roles=catalog_config.supported_scopes(
                    APPLICATION_API_RESOURCE
                ),
            )
        except ApplicationOperationPolicyError as exc:
            return _application_policy_refusal(exc)

        ttl = _bounded_ttl(ttl_seconds)
        now = int(time.time())
        created_at = created_at_override or now
        credential = build_delegated_client_credential(
            grantor_subject=grantor_subject,
            client_id=client_id,
            scopes=selected_grants,
            operations=selected_operations,
            resource_operations=selected_resource_operations,
            tenant=self._tenant,
            project=self._project,
            resource_grants=selected_resource_grants,
            account_scope=selected_account_scope,
            identity_scope=identity_scope,
            expires_in=ttl,
            issued_at=now,
        )
        minter = self._minter or mint_delegated_client_access_token
        authority = self._authority
        if authority is None:
            if self._authority_factory is None:
                raise RuntimeError("session authority is not configured")
            authority = self._authority_factory(
                tenant=self._tenant,
                project=self._project,
            )
        minted = await minter(
            grantor_subject,
            selected_grants,
            authority=authority,
            client_id=client_id,
            operations=selected_operations,
            credential=credential.to_dict(),
            ttl_seconds=ttl,
        )
        access_token = _clean(minted.get("access_token"))
        expires_in = int(minted.get("expires_in") or ttl)
        expires_at = now + expires_in
        session_id = _clean(minted.get("session_id"))

        grantor_authority = _grantor_authority(
            user,
            grants=authority_grants,
            inventory=inventory,
        )
        delegation_edges = list(grantor_authority.get("delegation_edges") or [])
        await self._store.bind_access_grant(
            access_token,
            selected_operations,
            expires_in,
            credential=credential.to_dict(),
            resource_operations=selected_resource_operations,
            grantor_authority=grantor_authority,
            delegation_edges=delegation_edges,
            named_services=named_services,
            # The card is the authority: this binding is a POINTER onto it, so
            # the guard resolves the card live (grants, resource_grants,
            # account_scope) and an edit applies to the reused agent bearer on
            # its very next call — not only after a re-mint. Same mechanism
            # OAuth clients use; makes card-authority universal.
            registry_access_id=access_id,
        )

        record = AutomationAccessRecord(
            access_id=access_id,
            label=_clean(label) or "Automation access",
            client_id=client_id,
            grantor_subject=grantor_subject,
            delegate_subject=integration_subject(grantor_subject, client_id=client_id),
            card_kind=(
                CARD_KIND_AGENT
                if access_source == ACCESS_SOURCE_AGENT
                else CARD_KIND_AUTOMATION
            ),
            operations=tuple(selected_operations),
            resource_grants={key: tuple(value) for key, value in selected_resource_grants.items()},
            resource_operations={
                key: tuple(value)
                for key, value in selected_resource_operations.items()
            },
            named_service_operations=selected_named_service_operations,
            named_services=copy.deepcopy(named_services),
            account_scope={
                provider: {account_id: tuple(claims) for account_id, claims in accounts.items()}
                for provider, accounts in selected_account_scope.items()
            },
            identity_scope=identity_scope,
            catalog_version=catalog_version,
            card_revision=committed_revision + 1,
            session_id=session_id,
            created_at=created_at,
            expires_at=expires_at,
            last_four=access_token[-4:] if access_token else "",
            source=access_source,
            # A per-agent grant persists its token so each turn REUSES the
            # consented bearer (looked up by the resolver) rather than minting an
            # unbound one; a manual automation keeps the token client-side only.
            access_token=access_token if access_source == ACCESS_SOURCE_AGENT else "",
            # What each resource's authority showed when this save accepted it.
            # A resource the card already held keeps the digests it accepted for
            # selected operations whose descriptor changed since: a consent
            # merge is not a review of unrelated changes.
            resource_acceptance=preserve_descriptor_acceptance(
                existing.resource_acceptance if existing is not None else None,
                next_resource_acceptance(
                    resources=selected_resource_grants,
                    row_for=lambda resource: self._configured_resource(
                        resource,
                        config=catalog_config,
                    ),
                    catalog_version=catalog_version,
                    selected_operations=selected_resource_operations,
                    previous=(
                        existing.resource_acceptance
                        if existing is not None
                        else None
                    ),
                ),
            ),
            provenance=(
                copy.deepcopy(dict(existing.provenance or {}))
                if existing is not None
                else {}
            ),
            control_card=(
                existing.control_card if existing is not None else None
            ),
            properties=selected_properties,
        )
        try:
            await self._persist_record(record, expected_revision=committed_revision)
        except CardServingUnavailable as exc:
            return _serving_state_unavailable(exc)
        except (CardUnavailable, CardConflict, CardCommitFailed) as exc:
            return {
                "ok": False,
                "error": "delegated_card_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
            }
        await self.notify_change(grantor_subject, action="created", access=record.to_public_dict())

        return {
            "ok": True,
            "access": record.to_public_dict(),
            "access_token": access_token,
            "authorization_header": f"Bearer {access_token}" if access_token else "",
        }

    async def _resolve_card_authority(
        self,
        *,
        user: Mapping[str, Any],
        existing: "AutomationAccessRecord",
        active: Any,
        resource_grants: Mapping[str, Any],
        resource_operations: Mapping[str, Any] | None,
        operations: Iterable[str],
        named_service_operations: Mapping[str, Any] | str | None,
        account_scope: Mapping[str, Any] | None,
        properties: Mapping[str, Any] | None,
    ) -> "ResolvedCardAuthority":
        """The authority a save writes, resolved once for every entrance.

        Owns the decisions, not the record: selection resolution, catalog
        reconciliation, the delegability checks, the materialized boundary, and
        the account binding. Callers own identity, preconditions, and the
        credential fields their family carries.
        """
        selected_resource_grants = self._resource_grants(resource_grants)
        selected_properties = copy.deepcopy(
            dict(existing.properties if properties is None else properties)
        )
        # Every decision below reads the registered catalog. Effective props are
        # an input to publication, not to a card write.
        catalog_config = await self._catalog_config(
            active, owner_subject=_subject_from_user(user)
        )
        # A save carries the same risk issuance does. The editor normally
        # submits declared doors, so this is a no-op for it, and it is here so
        # that a caller which submits a concrete URL cannot pin the card to a
        # hostname through the back entrance.
        selected_resource_grants, host_pinned = self._declared_resource_keys(
            catalog_config, selected_resource_grants
        )
        if host_pinned:
            _LOGGER.info(
                "delegated access save resolved host-pinned resources to declared doors: %s",
                host_pinned,
            )
        try:
            selected_named_service_operations = self._named_service_operation_selection(
                named_service_operations
            )
        except ValueError as exc:
            return ResolvedCardAuthority(error={
                "ok": False,
                "error": "invalid_named_service_operation_selection",
                "message": str(exc),
            })
        if selected_named_service_operations is not None:
            selected_named_service_operations = self._declared_named_service_selection(
                catalog_config,
                selected_named_service_operations,
            )
        if selected_named_service_operations is None:
            ambiguity = pre_migration_ambiguity(
                existing,
                named_service_resources=[
                    resource
                    for resource in selected_resource_grants
                    if isinstance(
                        getattr(
                            self._configured_resource(resource, config=catalog_config),
                            "named_services",
                            None,
                        ),
                        Mapping,
                    )
                ],
                offered=self._catalog_named_service_operations(active, selected_resource_grants),
            )
            if ambiguity is not None:
                return ResolvedCardAuthority(error={
                    "ok": False,
                    "error": MIGRATION_CONFIRMATION_REQUIRED,
                    "message": (
                        "This card predates the explicit named-service encoding and its "
                        "stored evidence cannot be read without guessing. Review the "
                        "operations and save an explicit selection."
                    ),
                    **ambiguity,
                })
        # Resolved before pruning: pruning acts on the selection about to be
        # persisted. Omitted keeps the record's own state; a pre-encoding record
        # resolves to what it materialized.
        if (
            selected_named_service_operations is None
            or selected_named_service_operations.is_unknown
        ):
            selected_named_service_operations = _inherited_selection(
                existing, selected_resource_grants
            )
        selected_named_service_operations = self._declared_named_service_selection(
            catalog_config,
            selected_named_service_operations,
        )

        # Submitting nothing is a client error; pruning to nothing is a revoke.
        if not any(selected_resource_grants.values()):
            return ResolvedCardAuthority(error={
                "ok": False, "error": "delegated_access_requires_resource_grants",
            })

        legacy_operations = _as_list(list(operations))
        if resource_operations is not None:
            try:
                selected_resource_operations = normalize_resource_operations(
                    resource_operations
                )
                selected_resource_operations, _rewritten_operations = (
                    resolve_declared_resource_keys(
                        catalog_config,
                        selected_resource_operations,
                    )
                )
            except ValueError as exc:
                return ResolvedCardAuthority(error={
                    "ok": False,
                    "error": "invalid_resource_operation_selection",
                    "message": str(exc),
                })
        elif legacy_operations:
            try:
                selected_resource_operations = self._resolve_resource_operations(
                    resource_grants=selected_resource_grants,
                    resource_operations=None,
                    legacy_operations=legacy_operations,
                    config=catalog_config,
                )
            except ValueError as exc:
                return ResolvedCardAuthority(error={
                    "ok": False,
                    "error": "invalid_resource_operation_selection",
                    "message": str(exc),
                })
        else:
            # An edit that omits this dimension preserves it. In particular, a
            # label or account edit must not select new operations from a newer
            # catalog generation.
            existing_resource_operations, _rewritten_operations = (
                resolve_declared_resource_keys(
                    catalog_config,
                    existing.resource_operations,
                )
            )
            selected_resource_operations = {
                resource: list(existing_resource_operations.get(resource, ()))
                for resource in selected_resource_grants
            }

        # Values absent from the active catalog are pruned, not rejected.
        reconciled = reconcile_selection(
            resource_grants=selected_resource_grants,
            resource_operations=selected_resource_operations,
            named_service_operations=selected_named_service_operations,
            active=active,
            config=catalog_config,
        )
        if reconciled.empty:
            return ResolvedCardAuthority(reconciled=reconciled, revoke=True)
        selected_resource_grants = reconciled.resource_grants
        selected_resource_operations = reconciled.resource_operations
        selected_named_service_operations = reconciled.named_service_operations
        selected_resources = list(selected_resource_grants)
        if catalog_config.resources and not selected_resources:
            return ResolvedCardAuthority(error={
                "ok": False, "error": "delegated_access_requires_resource_grants",
            })
        selected_grants = _as_list([
            grant
            for grants_for_resource in selected_resource_grants.values()
            for grant in grants_for_resource
        ])
        if not selected_grants:
            # Removing everything is a revoke, not an edit.
            return ResolvedCardAuthority(error={
                "ok": False, "error": "delegated_access_requires_resource_grants",
            })
        # Same order as create_access: an endpoint this deployment never made
        # delegable is a different answer from a permission it will not delegate.
        try:
            resource_pairs = (
                self._configured_resource_pairs(selected_resources, config=catalog_config)
                if catalog_config.resources else ()
            )
        except ValueError:
            return ResolvedCardAuthority(error={
                "ok": False,
                "error": "delegated_access_unknown_resources",
                "resources": selected_resources,
                "message": (
                    "This deployment has not made that endpoint delegable. Add it to "
                    "connection-hub@1-0 `connections.delegated_credentials.oauth.resources`, "
                    "with the tools it may grant, before access to it can be asked for."
                ),
            })
        resource_configs = tuple(cfg for _, cfg in resource_pairs)
        try:
            submitted_application_policy = (
                application_operation_role_policy(
                    selected_properties,
                    resource_grants=selected_resource_grants,
                )
                if APPLICATION_API_RESOURCE in selected_resource_grants
                else None
            )
        except ApplicationOperationPolicyError as exc:
            return ResolvedCardAuthority(error=_application_policy_refusal(exc))
        authority_grants = list(selected_grants)
        if submitted_application_policy is not None:
            authority_grants.extend(
                [
                    submitted_application_policy.default_role,
                    *submitted_application_policy.operation_roles.values(),
                ]
            )
        authority_grants = _as_list(authority_grants)
        inventory = await self._available_inventory(
            user, requested_grants=authority_grants, config=catalog_config
        )
        denied = [grant for grant in selected_grants if grant not in set(inventory.grant_names())]
        if denied:
            return ResolvedCardAuthority(error={
                "ok": False,
                "error": "delegated_access_grants_not_delegable",
                "grants": denied,
                "message": (
                    "These permissions are not yours to delegate. A card never carries "
                    "more than its grantor holds. Who may delegate each permission is "
                    "declared in connection-hub@1-0 "
                    "`connections.delegated_credentials.oauth.capabilities`, as "
                    "`delegable_roles` and `delegable_permissions`."
                ),
            })
        try:
            _application_role_policy(
                properties=selected_properties,
                resource_grants=selected_resource_grants,
                resource_operations=selected_resource_operations,
                delegable_roles=inventory.grant_names(),
                allowed_roles=catalog_config.supported_scopes(
                    APPLICATION_API_RESOURCE
                ),
            )
        except ApplicationOperationPolicyError as exc:
            return ResolvedCardAuthority(error=_application_policy_refusal(exc))
        admin_required = [cfg.resource for cfg in resource_configs if cfg.admin_only]
        if admin_required and not _is_platform_admin(user):
            return ResolvedCardAuthority(error={
                "ok": False,
                "error": "delegated_access_resource_requires_admin",
                "resources": admin_required,
            })
        cfg_by_resource = dict(resource_pairs)
        if selected_named_service_operations is not None and selected_named_service_operations.is_exact:
            # Named-service operations for a resource this save does not select
            # are dropped, not rejected. A card is pinned to the catalog version
            # it was written against, so a service withdrawn since then leaves
            # entries behind; rejecting on them makes the card permanently
            # unsavable, and a person cannot edit their way out of a change the
            # catalog made underneath them. Card management has to survive the
            # catalog moving.
            #
            # Dropping is safe in every case: an operation on a resource the
            # card does not grant confers nothing, so this can only narrow the
            # card, never widen it. The reconciliation above states the same
            # rule ("values absent from the active catalog are pruned, not
            # rejected") and this check used to contradict it.
            stale = sorted(
                set(selected_named_service_operations.operations) - set(selected_resources)
            )
            if stale:
                # Recorded, never silently dropped. The reconciliation above
                # already reports what it pruned, and the update result carries
                # that document back to the caller; anything removed here has to
                # appear in the same place or the grantor is told a save
                # succeeded while part of their card quietly disappeared.
                keep = set(selected_resources)
                for resource in stale:
                    for namespace, operations in (
                        selected_named_service_operations.operations.get(resource) or {}
                    ).items():
                        for operation in operations:
                            reconciled.pruned_named_service_operations.append(
                                {
                                    "resource": resource,
                                    "namespace": str(namespace),
                                    "operation": str(operation),
                                    # Withdrawn from the catalog since this card
                                    # was written, rather than never valid: the
                                    # card was pinned to a version that offered
                                    # it. The two are different facts and a
                                    # person reading this should see which.
                                    "reason": "resource_not_selected",
                                }
                            )
                _LOGGER.info(
                    "[automation-access] pruning named-service operations for "
                    "resources this save does not select: %s",
                    stale,
                )
                selected_named_service_operations = NamedServiceSelection.exact({
                    resource: namespaces
                    for resource, namespaces in selected_named_service_operations.operations.items()
                    if resource in keep
                })
        for resource_value, grants_for_resource in selected_resource_grants.items():
            cfg = cfg_by_resource.get(resource_value)
            if cfg is None:
                continue
            allowed_for_resource = set(catalog_config.supported_scopes(resource_value))
            disallowed = [
                grant
                for grant in grants_for_resource
                if grant not in allowed_for_resource
                and not (
                    resource_value == APPLICATION_API_RESOURCE
                    and platform_role_allowed_by(grant, allowed_for_resource)
                )
            ]
            if disallowed:
                return ResolvedCardAuthority(error={
                    "ok": False,
                    "error": "delegated_access_grants_not_allowed_for_resources",
                    "grants": disallowed,
                    "resource": resource_value,
                })
        identity_scopes = {
            _clean(getattr(cfg, "identity_scope", "") or "grantor") for cfg in resource_configs
        }
        if len(identity_scopes) > 1:
            return ResolvedCardAuthority(error={
                "ok": False,
                "error": "delegated_access_resources_have_conflicting_identity_scopes",
                "resources": selected_resources,
                "identity_scopes": sorted(identity_scopes),
                "message": (
                    "A card issues one credential, so every endpoint on it must run "
                    "under the same identity. These do not: " + ", ".join(sorted(identity_scopes))
                    + ". The value is `identity_scope` in connection-hub@1-0 "
                    "`connections.delegated_credentials.oauth.resources`. Select "
                    "resources with one acting identity; the existing card is unchanged."
                ),
            })
        if account_scope is None:
            selected_account_scope = {
                provider: {account_id: list(claims) for account_id, claims in accounts.items()}
                for provider, accounts in existing.account_scope.items()
            }
        else:
            selected_account_scope = {
                provider: {account_id: list(claims) for account_id, claims in accounts.items()}
                for provider, accounts in normalize_account_scope(account_scope).items()
            }
        # Materialize the boundary tree from the same active catalog the rows
        # came from, so the stored tree and the version stamped on the card
        # describe one generation.
        named_services: dict[str, Any] = {}
        for resource_value, cfg in resource_pairs:
            if not isinstance(cfg.named_services, Mapping):
                continue
            try:
                selected_policy = named_service_policy_for_resource(
                    named_services=cfg.named_services,
                    resource=resource_value,
                    # Same value the record stores, so the tree and the
                    # selection it was derived from cannot disagree.
                    selection=_selection_policy_argument(selected_named_service_operations),
                    grants=_effective_named_service_grants(
                        selected_resource_grants.get(resource_value, []),
                        account_scope=selected_account_scope,
                        required=catalog_config.supported_scopes(resource_value),
                    ),
                )
            except ValueError as exc:
                return ResolvedCardAuthority(error={
                    "ok": False,
                    "error": "invalid_named_service_operation_selection",
                    "message": str(exc),
                })
            named_services = self._merge_named_service_configs(named_services, selected_policy)
        return ResolvedCardAuthority(
            resource_grants=selected_resource_grants,
            resource_operations=selected_resource_operations,
            operations=list(operation_union(selected_resource_operations)),
            named_service_operations=selected_named_service_operations,
            named_services=named_services,
            account_scope=selected_account_scope,
            identity_scope=next(iter(identity_scopes), existing.identity_scope or "grantor"),
            properties=selected_properties,
            reconciled=reconciled,
        )

    async def update_access(
        self,
        user: Mapping[str, Any],
        *,
        access_id: str,
        resource_grants: Mapping[str, Any],
        resource_operations: Mapping[str, Any] | None = None,
        operations: Iterable[str] = (),
        named_service_operations: Mapping[str, Any] | str | None = None,
        account_scope: Mapping[str, Any] | None = None,
        label: str | None = None,
        expected_card_revision: int | None = None,
        expected_catalog_version: str | None = None,
        accepted_operations: Mapping[str, Iterable[str]] | None = None,
        properties: Mapping[str, Any] | None = None,
        composition_mode: str | None = None,
    ) -> dict[str, Any]:
        """Edit a card's authority IN PLACE, whatever family issued it.

        ``accepted_operations`` names, per resource, the selected operations
        whose CHANGED descriptor the grantor reviewed and accepts with this
        save. Every other changed selected operation keeps the digest the card
        accepted before and stays suspended; an ordinary edit never accepts a
        changed tool as a side effect.

        The card keeps its access_id and its credential material; the card is
        the guard's authority (resolved live via resolve_live_grant_card), so
        the change applies to the client's existing bearer on its very next call
        — no re-mint, no re-authorization. The submitted selection REPLACES the
        record exactly."""
        grantor_subject = _subject_from_user(user)
        if not grantor_subject:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}
        refusal = _delegate_mutation_refusal(user)
        if refusal is not None:
            return refusal
        access_id = _clean(access_id)
        if not access_id:
            return {"ok": False, "error": "delegated_access_requires_access_id"}
        try:
            existing = await self._load_record(access_id, grantor_subject=grantor_subject)
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_cards_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        if existing is None:
            return {"ok": False, "error": "delegated_access_not_found"}
        if existing.grantor_subject != grantor_subject:
            return {"ok": False, "error": "delegated_access_not_owned"}
        if _record_is_credentialless(existing):
            try:
                existing = await self._ensure_control_snapshot(existing)
            except CardUnavailable as exc:
                return {
                    "ok": False,
                    "error": "control_card_snapshot_unavailable",
                    "reason": exc.reason,
                    "retryable": True,
                    "status": 503,
                }
        selected_composition_mode = (
            _clean(composition_mode).lower()
            if composition_mode is not None
            else existing.composition_mode
        )
        if _record_is_credentialless(existing):
            selected_composition_mode = (
                selected_composition_mode or CONTROL_COMPOSITION_AND
            )
        if (
            selected_composition_mode
            and selected_composition_mode not in CONTROL_COMPOSITIONS
        ):
            return {
                "ok": False,
                "error": "control_card_composition_mode_invalid",
                "status": 400,
            }
        # Every family edits here. The source records how the credential is
        # managed; it does not decide whether the grantor may change authority.

        try:
            active = await self._active_catalog()
        except CatalogUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_catalog_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        catalog_version = _clean(getattr(active, "version", ""))
        if not catalog_version:
            return {
                "ok": False,
                "error": "delegated_catalog_unavailable",
                "reason": "active_catalog_version_missing",
                "retryable": True,
                "status": 503,
            }
        catalog_config = await self._catalog_config(
            active,
            owner_subject=grantor_subject,
        )
        if existing.source == ACCESS_SOURCE_OAUTH:
            entry_resource = self._entry_resource_for(
                existing,
                config=catalog_config,
            )
            allowed_resources = self._oauth_allowed_resources(
                entry_resource=entry_resource,
                client_metadata=existing.client_metadata,
                config=catalog_config,
            )
            outside_entry = sorted(
                _clean(resource)
                for resource in resource_grants
                if _clean(resource)
                and allowed_resources is not None
                and _clean(resource) not in allowed_resources
            )
            if outside_entry:
                return {
                    "ok": False,
                    "error": "oauth_client_resource_unreachable",
                    "resources": outside_entry,
                    "entry_resource": entry_resource,
                }
        conflict = await self._save_precondition_conflict(
            existing=existing,
            active=active,
            expected_card_revision=expected_card_revision,
            expected_catalog_version=expected_catalog_version,
        )
        if conflict is not None:
            return conflict

        resolved = await self._resolve_card_authority(
            user=user,
            existing=existing,
            active=active,
            resource_grants=resource_grants,
            resource_operations=resource_operations,
            operations=operations,
            named_service_operations=named_service_operations,
            account_scope=account_scope,
            properties=properties,
        )
        if resolved.error is not None:
            return resolved.error
        reconciled = resolved.reconciled
        if resolved.revoke:
            # An edit that leaves nothing recognised is REFUSED, never revoked.
            #
            # This used to revoke the card. On 2026-09-12 an operator removed a
            # single service from a card whose other selection had been
            # withdrawn from the catalog by a rename, and lost the card, the
            # credential, and the agent's access, in one click, with no
            # confirmation and no warning that removal could do that. Recovery
            # was a full re-consent.
            #
            # Destroying an authorization is a deliberate act with its own
            # command. It must never be the side effect of an ordinary save,
            # and least of all when what emptied the card was a catalog change
            # the person did not make. Refusing leaves the card exactly as it
            # was, which is always recoverable; revoking is not.
            return {
                "ok": False,
                "error": "delegated_access_requires_resource_grants",
                "pruned": reconciled.to_public_dict(),
                "message": (
                    "This save would leave the card with nothing the service catalog "
                    "still offers, so it was not applied and the card is unchanged. "
                    "Select at least one current service, or revoke the card "
                    "deliberately if that is what you intend."
                ),
            }
        selected_resource_grants = resolved.resource_grants
        selected_resource_operations = resolved.resource_operations
        selected_named_service_operations = resolved.named_service_operations
        named_services = resolved.named_services
        selected_operations = resolved.operations
        selected_account_scope = resolved.account_scope
        selected_properties = resolved.properties
        if _record_is_credentialless(existing):
            candidate = dataclasses.replace(
                card_authority_from_record(existing),
                catalog_version=catalog_version,
                operations=tuple(selected_operations),
                resource_grants={
                    key: tuple(value)
                    for key, value in selected_resource_grants.items()
                },
                resource_operations={
                    key: tuple(value)
                    for key, value in selected_resource_operations.items()
                },
                named_service_operations=selected_named_service_operations,
                named_services=copy.deepcopy(named_services),
                account_scope={
                    provider: {
                        account_id: tuple(claims)
                        for account_id, claims in accounts.items()
                    }
                    for provider, accounts in selected_account_scope.items()
                },
                properties=selected_properties,
            )
            refusal = control_snapshot_refusal(candidate)
            if refusal is not None:
                return refusal
            selected_properties = reviewed_control_snapshot_properties(
                selected_properties,
                basis_catalog_version=catalog_version,
            )
        now = int(time.time())
        # Editing is about the grants, expiry about the credential. An expired
        # card is edited like any other; its credential comes back by renewal.
        remaining = max(1, int(existing.expires_at) - now)
        updated = AutomationAccessRecord(
            access_id=existing.access_id,
            label=_clean(label) if label else existing.label,
            client_id=existing.client_id,
            grantor_subject=existing.grantor_subject,
            delegate_subject=existing.delegate_subject,
            card_kind=existing.card_kind,
            operations=tuple(selected_operations),
            resource_grants={key: tuple(value) for key, value in selected_resource_grants.items()},
            resource_operations={
                key: tuple(value)
                for key, value in selected_resource_operations.items()
            },
            named_service_operations=selected_named_service_operations,
            named_services=copy.deepcopy(named_services),
            account_scope={
                provider: {account_id: tuple(claims) for account_id, claims in accounts.items()}
                for provider, accounts in selected_account_scope.items()
            },
            identity_scope=resolved.identity_scope,
            catalog_version=catalog_version,
            card_revision=existing.card_revision + 1,
            session_id=existing.session_id,
            created_at=existing.created_at,
            expires_at=existing.expires_at,
            last_four=existing.last_four,
            source=existing.source,
            # Credential material is the card family's lifecycle, not its
            # authority: an agent's reusable bearer and an OAuth client's token
            # handles survive an authority edit untouched. A manual card holds
            # none — its token is client-side only.
            refresh_token=existing.refresh_token,
            access_token=existing.access_token,
            last_issued_at=existing.last_issued_at,
            resource_acceptance=next_resource_acceptance(
                resources=selected_resource_grants,
                row_for=lambda resource: self._configured_resource(resource, config=catalog_config),
                catalog_version=catalog_version,
                selected_operations=selected_resource_operations,
                previous=existing.resource_acceptance,
                accepted_operations=accepted_operations,
            ),
            provenance=copy.deepcopy(dict(existing.provenance or {})),
            client_metadata=copy.deepcopy(dict(existing.client_metadata or {})),
            control_card=existing.control_card,
            issuer_ref=existing.issuer_ref,
            issuer_kind=existing.issuer_kind,
            issuer_label=existing.issuer_label,
            manage_url=existing.manage_url,
            composition_mode=selected_composition_mode,
            properties=selected_properties,
        )
        del remaining
        try:
            await self._persist_record(updated, expected_revision=existing.card_revision)
        except CardServingUnavailable as exc:
            return _serving_state_unavailable(exc)
        except (CardUnavailable, CardConflict, CardCommitFailed) as exc:
            return {
                "ok": False,
                "error": "delegated_card_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
            }
        await self.notify_change(grantor_subject, action="updated", access=updated.to_public_dict())
        saved = updated.to_public_dict()
        saved["catalog_drift"] = card_drift(card=updated, active=active, baseline=active)
        return {"ok": True, "access": saved, "pruned": reconciled.to_public_dict()}

    # -- resident caller profile: stable card, legacy fold, read model --------

    async def _resident_card_for_resources(
        self,
        *,
        grantor_subject: str,
        client_id: str,
        resources: Iterable[str],
    ) -> AutomationAccessRecord | None:
        """The agent card that covers ``resources`` for this client.

        The stable profile card is tried first, then the
        legacy resource-dependent record a not-yet-folded profile may still
        live under. A card is returned only when it holds every requested
        resource, so a caller never receives a bearer for a door the card does
        not open.
        """
        grantor = _clean(grantor_subject)
        client = _clean(client_id)
        if not grantor or not client:
            return None
        candidates = resident_access_ids(grantor, client, resources) or [
            stable_resident_access_id(grantor, client),
            agent_grant_access_id(grantor, client, resources),
        ]
        for access_id in dict.fromkeys(candidates):
            try:
                record = await self._load_record(access_id, grantor_subject=grantor)
            except CardUnavailable:
                raise
            if record is None or record.source != ACCESS_SOURCE_AGENT:
                continue
            if _clean(record.client_id) != client:
                continue
            if not _record_holds_resources(record, resources):
                continue
            return record
        return None

    async def _legacy_resident_records(
        self,
        *,
        grantor_subject: str,
        client_id: str,
        exclude: Iterable[str] = (),
    ) -> list[AutomationAccessRecord]:
        """Active agent cards of this client that do not live under the stable
        id. Scoped to the grantor's own cards and an exact client id, so no
        other profile's record can be a candidate."""
        excluded = {_clean(item) for item in exclude if _clean(item)}
        out: list[AutomationAccessRecord] = []
        for record in await self._list_active_records(_clean(grantor_subject)):
            if record.source != ACCESS_SOURCE_AGENT:
                continue
            if _clean(record.client_id) != _clean(client_id):
                continue
            if record.access_id in excluded:
                continue
            out.append(record)
        out.sort(key=lambda item: (item.created_at, item.access_id))
        return out

    @staticmethod
    def _migration_conflict(
        *, reason: str, target_access_id: str, candidates: Iterable[AutomationAccessRecord], evidence: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "error": RESIDENT_MIGRATION_CONFLICT,
            "status": 409,
            "reason": reason,
            "target_access_id": target_access_id,
            "candidates": [
                {
                    "access_id": item.access_id,
                    "card_revision": item.card_revision,
                    "resources": sorted(item.resource_grants),
                }
                for item in candidates
            ],
            "evidence": dict(evidence),
            "message": (
                "This agent holds several delegated cards written under the earlier "
                "resource-dependent identity, and they disagree on authority. Nothing "
                "was changed: review those cards in Connection Hub, revoke or edit the "
                "ones that should not carry over, then grant again."
            ),
            "recovery": {
                "action": "review_legacy_resident_cards",
                "retry_same_request": False,
            },
        }

    async def _fold_legacy_resident_records(
        self,
        user: Mapping[str, Any],
        *,
        client_id: str,
        target_access_id: str,
    ) -> dict[str, Any] | None:
        """Fold the legacy resource-dependent cards of one resident profile into
        its stable card.

        ``None`` when there is nothing to fold. Otherwise the result of the
        migration: ``ok`` with the folded ids, or a conflict that changed
        nothing. The rules, each of them fail-closed:

        - candidates are the grantor's own active agent cards with this exact
          client id, excluding the target;
        - source cards must agree on acting identity scope; disagreement is a
          conflict, never another stable card;
        - two records holding the same resource must agree on its claims and
          operations, else conflict;
        - non-empty account bindings must be identical across every record
          folded (including the target), else conflict: a binding one card made
          for its resource is not extended to another card's resource;
        - the folded card expires when the earliest of its sources would;
        - invocation policies move with their operation: ``always`` and an
          unconsumed ``once`` are re-declared on the stable card, a consumed
          ``once`` drops the operation from the folded card so a spent permit
          cannot come back to life. Without a policy service the fold cannot
          see policies and refuses;
        - the target keeps its own resources, policies, and lineage; the
          legacy cards are revoked after the target committed, which also
          invalidates their bearers;
        - replay is a no-op: a second pass finds no candidates.
        """
        grantor_subject = _subject_from_user(user)
        if not grantor_subject:
            return None
        client = _clean(client_id)
        try:
            candidates = await self._legacy_resident_records(
                grantor_subject=grantor_subject,
                client_id=client,
                exclude=[target_access_id],
            )
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_cards_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        if not candidates:
            return None
        try:
            target = await self._load_record(target_access_id, grantor_subject=grantor_subject)
            target_revision = await self._committed_revision(
                target_access_id, grantor_subject=grantor_subject
            )
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_cards_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        sources = ([target] if target is not None else []) + list(candidates)

        identity_scopes = {
            _clean(record.identity_scope) or "grantor" for record in sources
        }
        if len(identity_scopes) > 1:
            return self._migration_conflict(
                reason="identity_scope_conflict",
                target_access_id=target_access_id,
                candidates=candidates,
                evidence={"identity_scopes": sorted(identity_scopes)},
            )
        scope = next(iter(identity_scopes), "grantor")

        # Account bindings: one agreed non-empty binding, or none.
        bindings: dict[str, Mapping[str, Any]] = {}
        for record in sources:
            if record.account_scope:
                key = json.dumps(
                    {p: {a: sorted(c) for a, c in accounts.items()} for p, accounts in record.account_scope.items()},
                    sort_keys=True,
                )
                bindings[key] = record.account_scope
        if len(bindings) > 1:
            return self._migration_conflict(
                reason="account_scope_conflict",
                target_access_id=target_access_id,
                candidates=candidates,
                evidence={"distinct_bindings": len(bindings)},
            )
        merged_account_scope: dict[str, dict[str, list[str]]] = {
            provider: {account_id: list(claims) for account_id, claims in accounts.items()}
            for provider, accounts in (next(iter(bindings.values())) if bindings else {}).items()
        }

        # Resources: disjoint by construction of the legacy id, but a resource
        # held twice must agree exactly.
        merged_grants: dict[str, list[str]] = {}
        merged_operations: dict[str, list[str]] = {}
        merged_acceptance: dict[str, ResourceAcceptance] = {}
        merged_selection = NamedServiceSelection.none()
        for record in sources:
            merged_acceptance = preserve_descriptor_acceptance(
                record.resource_acceptance,
                merged_acceptance,
            )
            frozen = _inherited_selection(record)
            merged_selection = merged_selection.union(frozen) if not merged_selection.is_none else frozen
            for resource, grants in record.resource_grants.items():
                held_grants = sorted(_as_list(list(grants)))
                held_ops = sorted(record.resource_operations.get(resource, ()))
                if resource in merged_grants:
                    if merged_grants[resource] != held_grants or merged_operations.get(resource, []) != held_ops:
                        return self._migration_conflict(
                            reason="resource_selection_conflict",
                            target_access_id=target_access_id,
                            candidates=candidates,
                            evidence={"resource": resource},
                        )
                    continue
                merged_grants[resource] = held_grants
                merged_operations[resource] = held_ops
                accepted = record.resource_acceptance.get(resource)
                if accepted is not None:
                    merged_acceptance[resource] = accepted

        # Policies travel with their operation; a spent one-use permit drops it.
        if self._invocation_policies is None:
            return self._migration_conflict(
                reason="invocation_policies_unverifiable",
                target_access_id=target_access_id,
                candidates=candidates,
                evidence={"policy_service": "not_configured"},
            )
        from connection_hub.invocation_policy import (
            POLICY_ALWAYS,
            POLICY_CONSUMED,
            POLICY_ONCE,
            InvocationAuthority,
        )

        transfers: list[tuple[InvocationAuthority, str]] = []
        dropped: list[dict[str, str]] = []
        target_policies: dict[tuple[str, str, str, str], Any] = {}
        if target is not None:
            for policy in await self._invocation_policies.list_for_card(
                owner_subject=grantor_subject, access_id=target.access_id
            ):
                auth = policy.authority
                target_policies[(auth.resource, auth.operation, auth.provider_id, auth.account_id)] = policy
        for record in candidates:
            for policy in await self._invocation_policies.list_for_card(
                owner_subject=grantor_subject, access_id=record.access_id
            ):
                auth = policy.authority
                key = (auth.resource, auth.operation, auth.provider_id, auth.account_id)
                if policy.mode == POLICY_ONCE and policy.state == POLICY_CONSUMED:
                    dropped.append({"resource": auth.resource, "operation": auth.operation})
                    ops = merged_operations.get(auth.resource, [])
                    merged_operations[auth.resource] = [op for op in ops if op != auth.operation]
                    continue
                held = target_policies.get(key)
                if held is not None and held.mode != policy.mode:
                    return self._migration_conflict(
                        reason="invocation_policy_conflict",
                        target_access_id=target_access_id,
                        candidates=candidates,
                        evidence={"resource": auth.resource, "operation": auth.operation},
                    )
                if held is None:
                    transfers.append(
                        (
                            InvocationAuthority(
                                access_id=target_access_id,
                                resource=auth.resource,
                                surface=auth.surface,
                                operation=auth.operation,
                                provider_id=auth.provider_id,
                                account_id=auth.account_id,
                            ),
                            POLICY_ALWAYS if policy.mode == POLICY_ALWAYS else POLICY_ONCE,
                        )
                    )

        merged_grants = {resource: grants for resource, grants in merged_grants.items() if grants}
        if not merged_grants:
            return self._migration_conflict(
                reason="no_authority_to_fold",
                target_access_id=target_access_id,
                candidates=candidates,
                evidence={},
            )
        merged_operations = {
            resource: merged_operations.get(resource, []) for resource in merged_grants
        }

        try:
            active = await self._active_catalog()
            catalog_version = self._version_of(active)
        except CatalogUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_catalog_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        catalog_config = await self._catalog_config(active, owner_subject=grantor_subject)
        named_services: dict[str, Any] = {}
        for resource, grants in merged_grants.items():
            boundary = self._materialized_boundary_for(
                selection=merged_selection,
                resource=resource,
                grants=grants,
                account_scope=merged_account_scope,
                config=catalog_config,
            )
            if boundary:
                named_services = self._merge_named_service_configs(named_services, boundary)

        now = int(time.time())
        expires_at = min(record.expires_at for record in sources if record.expires_at) if any(
            record.expires_at for record in sources
        ) else now + AUTOMATION_ACCESS_DEFAULT_TTL_SECONDS
        if expires_at <= now:
            return self._migration_conflict(
                reason="every_source_expired",
                target_access_id=target_access_id,
                candidates=candidates,
                evidence={},
            )
        selected_grants = _as_list([g for grants in merged_grants.values() for g in grants])
        selected_operations = list(operation_union(merged_operations))
        minted = await self._mint_card_credential(
            user,
            grantor_subject=grantor_subject,
            client_id=client,
            access_id=target_access_id,
            grants=selected_grants,
            operations=selected_operations,
            resource_grants=merged_grants,
            resource_operations=merged_operations,
            account_scope=merged_account_scope,
            identity_scope=scope,
            named_services=named_services,
            ttl=max(60, expires_at - now),
            now=now,
        )
        # Policies first: they are keyed by the stable id and harmless without a
        # card, and a card must never exist without the policy it was granted
        # under.
        for authority, mode in transfers:
            await self._invocation_policies.set_policy(
                owner_subject=grantor_subject, authority=authority, mode=mode
            )
        provenance = copy.deepcopy(dict(target.provenance or {})) if target is not None else {}
        lineage = list(provenance.get("migrated_from") or [])
        lineage.extend(
            {
                "access_id": record.access_id,
                "card_revision": record.card_revision,
                "resources": sorted(record.resource_grants),
            }
            for record in candidates
        )
        provenance["migrated_from"] = lineage
        provenance["migrated_at"] = now
        provenance["schema"] = "connection_hub.resident_profile_fold.v1"
        if dropped:
            provenance["dropped_consumed_once"] = list(provenance.get("dropped_consumed_once") or []) + dropped

        bindings = {
            json.dumps(record.control_card.to_dict(), sort_keys=True): record.control_card
            for record in sources
            if record.control_card is not None
        }
        if len(bindings) > 1:
            return self._migration_conflict(
                reason="control_card_binding_conflict",
                target_access_id=target_access_id,
                candidates=candidates,
                evidence={"distinct_bindings": len(bindings)},
            )
        merged_control_card = next(iter(bindings.values()), None)

        if target is not None:
            merged_properties = copy.deepcopy(dict(target.properties or {}))
        else:
            property_variants = {
                json.dumps(dict(record.properties or {}), sort_keys=True): copy.deepcopy(
                    dict(record.properties or {})
                )
                for record in candidates
                if record.properties
            }
            if len(property_variants) > 1:
                return self._migration_conflict(
                    reason="card_properties_conflict",
                    target_access_id=target_access_id,
                    candidates=candidates,
                    evidence={"distinct_properties": len(property_variants)},
                )
            merged_properties = next(iter(property_variants.values()), {})
        record = AutomationAccessRecord(
            access_id=target_access_id,
            label=(target.label if target is not None else "") or next(
                (item.label for item in candidates if item.label), client
            ),
            client_id=client,
            grantor_subject=grantor_subject,
            delegate_subject=integration_subject(grantor_subject, client_id=client),
            card_kind=CARD_KIND_AGENT,
            operations=tuple(selected_operations),
            resource_grants={key: tuple(value) for key, value in merged_grants.items()},
            resource_operations={key: tuple(value) for key, value in merged_operations.items()},
            named_service_operations=merged_selection,
            named_services=copy.deepcopy(named_services),
            account_scope={
                provider: {account_id: tuple(claims) for account_id, claims in accounts.items()}
                for provider, accounts in merged_account_scope.items()
            },
            identity_scope=scope,
            catalog_version=catalog_version,
            card_revision=target_revision + 1,
            session_id=_clean(minted.get("session_id")),
            created_at=min(item.created_at for item in sources if item.created_at) or now,
            expires_at=expires_at,
            last_four=_clean(minted.get("access_token"))[-4:],
            source=ACCESS_SOURCE_AGENT,
            access_token=_clean(minted.get("access_token")),
            resource_acceptance=next_resource_acceptance(
                resources=merged_grants,
                row_for=lambda resource: self._configured_resource(resource, config=catalog_config),
                catalog_version=catalog_version,
                selected_operations=merged_operations,
                previous=merged_acceptance,
            ),
            provenance=provenance,
            client_metadata=copy.deepcopy(
                dict(
                    (target.client_metadata if target is not None else {})
                    or next(
                        (
                            item.client_metadata
                            for item in candidates
                            if item.client_metadata
                        ),
                        {},
                    )
                )
            ),
            control_card=merged_control_card,
            properties=merged_properties,
        )
        try:
            await self._persist_record(record, expected_revision=target_revision)
        except CardServingUnavailable as exc:
            return _serving_state_unavailable(exc)
        except (CardUnavailable, CardConflict, CardCommitFailed) as exc:
            return {
                "ok": False,
                "error": "delegated_card_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
            }
        folded: list[str] = []
        for legacy in candidates:
            outcome = await self.revoke_access({"user_id": grantor_subject}, access_id=legacy.access_id)
            if outcome.get("ok"):
                folded.append(legacy.access_id)
            else:
                _LOGGER.warning(
                    "[automation-access] legacy resident card %s not revoked after fold into %s: %s",
                    legacy.access_id, target_access_id, outcome.get("error"),
                )
        _LOGGER.info(
            "[automation-access] resident profile folded client=%s target=%s revision=%s sources=%s dropped=%d",
            client, target_access_id, record.card_revision, folded, len(dropped),
        )
        await self.notify_change(grantor_subject, action="migrated", access=record.to_public_dict())
        return {
            "ok": True,
            "access_id": target_access_id,
            "card_revision": record.card_revision,
            "folded": folded,
            "dropped_consumed_once": dropped,
            "expires_at": expires_at,
        }

    async def migrate_resident_profile(
        self,
        user: Mapping[str, Any],
        *,
        client_id: str,
    ) -> dict[str, Any]:
        """Fold one resident profile's legacy cards into its stable card now.

        The same fold ``create_access`` performs before a grant, callable on its
        own so an operator or a host can migrate ahead of the next consent.
        """
        grantor_subject = _subject_from_user(user)
        if not grantor_subject:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}
        refusal = _delegate_mutation_refusal(user)
        if refusal is not None:
            return refusal
        client = _clean(client_id)
        if not client:
            return {"ok": False, "error": "delegated_access_requires_client_id"}
        target = stable_resident_access_id(grantor_subject, client)
        outcome = await self._fold_legacy_resident_records(
            user, client_id=client, target_access_id=target
        )
        if outcome is None:
            return {"ok": True, "access_id": target, "folded": [], "noop": True}
        return outcome

    async def _mint_card_credential(
        self,
        user: Mapping[str, Any],
        *,
        grantor_subject: str,
        client_id: str,
        access_id: str,
        grants: list[str],
        operations: list[str],
        resource_grants: Mapping[str, Any],
        resource_operations: Mapping[str, Any],
        account_scope: Mapping[str, Any],
        identity_scope: str,
        named_services: Mapping[str, Any],
        ttl: int,
        now: int,
    ) -> dict[str, Any]:
        """Mint and bind the reusable bearer of a card whose authority is
        ``access_id``: the same credential build, mint, and pointer binding a
        create performs, factored for the fold."""
        credential = build_delegated_client_credential(
            grantor_subject=grantor_subject,
            client_id=client_id,
            scopes=list(grants),
            operations=list(operations),
            resource_operations={k: list(v) for k, v in resource_operations.items()},
            tenant=self._tenant,
            project=self._project,
            resource_grants={k: list(v) for k, v in resource_grants.items()},
            account_scope={
                provider: {account_id: list(claims) for account_id, claims in accounts.items()}
                for provider, accounts in account_scope.items()
            },
            identity_scope=identity_scope,
            expires_in=ttl,
            issued_at=now,
        )
        minter = self._minter or mint_delegated_client_access_token
        authority = self._authority
        if authority is None:
            if self._authority_factory is None:
                raise RuntimeError("session authority is not configured")
            authority = self._authority_factory(tenant=self._tenant, project=self._project)
        minted = await minter(
            grantor_subject,
            list(grants),
            authority=authority,
            client_id=client_id,
            operations=list(operations),
            credential=credential.to_dict(),
            ttl_seconds=ttl,
        )
        access_token = _clean(minted.get("access_token"))
        expires_in = int(minted.get("expires_in") or ttl)
        inventory = await self._available_inventory(user, requested_grants=grants)
        grantor_authority = _grantor_authority(user, grants=grants, inventory=inventory)
        await self._store.bind_access_grant(
            access_token,
            list(operations),
            expires_in,
            credential=credential.to_dict(),
            resource_operations={k: list(v) for k, v in resource_operations.items()},
            grantor_authority=grantor_authority,
            delegation_edges=list(grantor_authority.get("delegation_edges") or []),
            named_services=dict(named_services or {}),
            registry_access_id=access_id,
        )
        return {
            "access_token": access_token,
            "expires_in": expires_in,
            "session_id": _clean(minted.get("session_id")),
        }

    async def _card_view(
        self,
        record: AutomationAccessRecord,
        *,
        state: str = CARD_STATE_ACTIVE,
        active: Any = None,
        catalog_config: Any = None,
        policies: Iterable[Any] | None = None,
    ) -> DelegatedCardView:
        """The read model of one card against current authority."""
        if active is None:
            try:
                active = await self._active_catalog()
            except CatalogUnavailable:
                active = None
        if active is not None and catalog_config is None:
            catalog_config = await self._catalog_config(
                active, owner_subject=record.grantor_subject
            )
        states = (
            card_resource_states(card=record, active=active, active_config=catalog_config)
            if active is not None
            else {}
        )
        if policies is None:
            policies = []
            if self._invocation_policies is not None:
                try:
                    policies = await self._invocation_policies.list_for_card(
                        owner_subject=record.grantor_subject, access_id=record.access_id
                    )
                except Exception:
                    _LOGGER.warning(
                        "[automation-access] policies unavailable for read model card=%s",
                        record.access_id,
                        exc_info=True,
                    )
        authority = card_authority_from_record(record)
        if state != authority.state:
            authority = dataclasses.replace(authority, state=state)
        return build_card_view(
            authority,
            resource_states=states,
            row_for=(
                (lambda resource: self._configured_resource(resource, config=catalog_config))
                if catalog_config is not None
                else None
            ),
            policies=policies,
        )

    async def describe_card(self, user: Mapping[str, Any], *, access_id: str) -> dict[str, Any]:
        """The portable read model of one card the authenticated grantor owns."""
        grantor_subject = _subject_from_user(user)
        if not grantor_subject:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}
        try:
            record = await self._load_record(_clean(access_id), grantor_subject=grantor_subject)
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_cards_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        if record is None or record.grantor_subject != grantor_subject:
            return {"ok": False, "error": "delegated_access_not_found", "status": 404}
        view = await self._card_view(record)
        effective_control = await self._effective_control_view(record)
        return {
            "ok": True,
            "card": view.to_dict(),
            "control_card": effective_control,
            # Compatibility for widgets deployed before the generic rename.
            "project_control": effective_control,
        }

    async def _control_card_public_view(
        self,
        user: Mapping[str, Any],
        record: AutomationAccessRecord,
        *,
        state: str = CARD_STATE_ACTIVE,
    ) -> dict[str, Any]:
        view = (await self._card_view(record, state=state)).to_dict()
        try:
            drift = await self._catalog_drift(
                [record], owner_subject=record.grantor_subject
            )
            view["catalog_drift"] = drift.get(record.access_id, {})
            options = await self.resource_options(user)
            view["resource_offers"] = compatible_resource_offers(
                card_resources=self._card_resource_keys(record.resource_grants),
                card_identity_scope=record.identity_scope,
                options=options,
                platform_admin=_is_platform_admin(user),
                entry_resource="",
                reachable=None,
            )
        except (CardUnavailable, CatalogUnavailable):
            view["catalog_drift"] = drift_unavailable("catalog_unavailable")
            view["resource_offers"] = []
        view["credential_delivery"] = "credentialless"
        view["credential_reach"] = "multi_resource"
        return view

    async def sync_agent_capability_control(
        self,
        user: Mapping[str, Any],
        *,
        application: str,
        agent_id: str,
        descriptor_revision: str,
        descriptor_payload: Mapping[str, Any],
        capability_authority: Mapping[str, Any],
        capability_metadata: Mapping[str, Any] | None = None,
        capability_catalog: Mapping[str, Any] | None = None,
        selected_capabilities: Mapping[str, Any] | None = None,
        replace_selection: bool = False,
        conversation_target_resources: Iterable[str] = (),
        resource_grants: Mapping[str, Any] | None = None,
        resource_operations: Mapping[str, Any] | None = None,
        named_service_operations: Mapping[str, Any] | str | None = None,
        properties: Mapping[str, Any] | None = None,
        issuer_label: str = "",
        manage_url: str = "",
    ) -> dict[str, Any]:
        """Synchronize one resident agent's live descriptor ceiling."""

        from connection_hub.delegated_credentials.agent_capability_sync import (
            sync_agent_capability_control,
        )

        return await sync_agent_capability_control(
            self,
            user,
            application=application,
            agent_id=agent_id,
            descriptor_revision=descriptor_revision,
            descriptor_payload=descriptor_payload,
            capability_authority=capability_authority,
            capability_metadata=capability_metadata,
            capability_catalog=capability_catalog,
            selected_capabilities=selected_capabilities,
            replace_selection=replace_selection,
            conversation_target_resources=conversation_target_resources,
            resource_grants=resource_grants,
            resource_operations=resource_operations,
            named_service_operations=named_service_operations,
            properties=properties,
            issuer_label=issuer_label,
            manage_url=manage_url,
        )

    async def control_card_get(
        self,
        user: Mapping[str, Any],
        *,
        control_id: str,
    ) -> dict[str, Any]:
        """Read one credentialless Control Card owned by this user."""

        grantor_subject = _subject_from_user(user)
        if not grantor_subject:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}
        try:
            loaded = await self._load_record_any_state(
                _clean(control_id),
                grantor_subject=grantor_subject,
            )
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "control_card_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        record = loaded[0] if loaded is not None else None
        if record is None or not _record_is_credentialless(record):
            return {"ok": False, "error": "control_card_not_found", "status": 404}
        state = loaded[1]
        if state == CARD_STATE_ACTIVE:
            try:
                record = await self._ensure_control_snapshot(record)
            except CardUnavailable as exc:
                return {
                    "ok": False,
                    "error": "control_card_snapshot_unavailable",
                    "reason": exc.reason,
                    "retryable": True,
                    "status": 503,
                }
        card = await self._control_card_public_view(user, record, state=state)
        authority = dataclasses.replace(card_authority_from_record(record), state=state)
        access = record.to_public_dict()
        access["state"] = state
        access["catalog_drift"] = dict(card.get("catalog_drift") or {})
        access["resource_offers"] = list(card.get("resource_offers") or [])
        access["invocation_policies"] = []
        try:
            active = await self._active_catalog()
            catalog_config = await self._catalog_config(
                active,
                owner_subject=grantor_subject,
            )
            rows: dict[str, str] = {}
            for resource in record.resource_grants:
                configured = self._configured_resource(
                    resource,
                    config=catalog_config,
                )
                if configured is not None:
                    rows[resource] = _clean(getattr(configured, "resource", ""))
            if rows:
                access["catalog_row_by_resource"] = rows
        except CatalogUnavailable:
            pass
        return {
            "ok": True,
            "control_card": card,
            "card": card,
            "access": access,
            "authority": authority.to_dict(),
        }

    async def control_card_create(
        self,
        user: Mapping[str, Any],
        *,
        issuer_ref: str,
        issuer_kind: str,
        issuer_label: str = "",
        manage_url: str = "",
        properties: Mapping[str, Any] | None = None,
        composition_mode: str = CONTROL_COMPOSITION_AND,
        initial_selection_access_id: str = "",
        basis_access_id: str = "",
    ) -> dict[str, Any]:
        """Create or return one catalog-driven credentialless Control Card.

        An optional delegated Card supplies the initially checked values only.
        The Control Card's complete option set and every later save are
        resolved against the catalog.

        ``basis_access_id`` is accepted only for callers staged before the
        field was named accurately.
        """

        grantor_subject = _subject_from_user(user)
        if not grantor_subject:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}
        refusal = _delegate_mutation_refusal(user)
        if refusal is not None:
            return refusal
        try:
            control_id = control_card_id_for_issuer(
                issuer_kind,
                issuer_ref,
                grantor_subject=grantor_subject,
            )
        except ControlCardError as exc:
            return {"ok": False, "error": exc.reason, "status": 400}
        try:
            existing = await self._load_record_any_state(
                control_id,
                grantor_subject=grantor_subject,
            )
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "control_card_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        if existing is not None:
            record, state = existing
            if (
                not _record_is_credentialless(record)
                or record.issuer_ref != _clean(issuer_ref)
                or record.issuer_kind != _clean(issuer_kind)
            ):
                return {"ok": False, "error": "control_card_identity_conflict", "status": 409}
            if state != CARD_STATE_ACTIVE:
                return {"ok": False, "error": "control_card_not_active", "status": 409}
            try:
                record = await self._ensure_control_snapshot(record)
            except CardUnavailable as exc:
                return {
                    "ok": False,
                    "error": "control_card_snapshot_unavailable",
                    "reason": exc.reason,
                    "retryable": True,
                    "status": 503,
                }
            return {
                "ok": True,
                "created": False,
                "control_card": await self._control_card_public_view(
                    user,
                    record,
                    state=state,
                ),
                "authority": card_authority_from_record(record).to_dict(),
            }

        try:
            active = await self._active_catalog()
            catalog_version = self._version_of(active)
            catalog_config = await self._catalog_config(
                active,
                owner_subject=grantor_subject,
            )
        except CatalogUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_catalog_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }

        initial_selection_id = _clean(initial_selection_access_id) or _clean(
            basis_access_id
        )
        initial_selection: AutomationAccessRecord | None = None
        if initial_selection_id:
            try:
                initial_selection = await self._load_record(
                    initial_selection_id,
                    grantor_subject=grantor_subject,
                )
            except CardUnavailable as exc:
                return {
                    "ok": False,
                    "error": "delegated_cards_unavailable",
                    "reason": exc.reason,
                    "retryable": True,
                    "status": 503,
                }
            if (
                initial_selection is None
                or _record_is_credentialless(initial_selection)
            ):
                return {
                    "ok": False,
                    "error": "control_card_initial_selection_not_found",
                    "status": 404,
                }
        try:
            authority = new_credentialless_card(
                control_id=control_id,
                grantor_subject=grantor_subject,
                catalog_version=catalog_version,
                initial_selection=(
                    card_authority_from_record(initial_selection)
                    if initial_selection is not None
                    else None
                ),
                issuer_ref=issuer_ref,
                issuer_kind=issuer_kind,
                issuer_label=issuer_label,
                manage_url=manage_url,
                properties=properties,
                composition_mode=composition_mode,
                now=int(time.time()),
            )
            record = record_from_card(authority)
            pruned: dict[str, Any] = {
                "resources": [],
                "claims": [],
                "named_service_operations": [],
            }
            if initial_selection is not None:
                resolved = await self._resolve_card_authority(
                    user=user,
                    existing=record,
                    active=active,
                    resource_grants=initial_selection.resource_grants,
                    resource_operations=initial_selection.resource_operations,
                    operations=(),
                    named_service_operations=(
                        initial_selection.named_service_operations.to_stored()
                    ),
                    account_scope=initial_selection.account_scope,
                    properties=record.properties,
                )
                if resolved.error is not None:
                    return resolved.error
                if resolved.revoke:
                    return {
                        "ok": False,
                        "error": "control_card_initial_selection_empty",
                        "status": 409,
                        "pruned": resolved.reconciled.to_public_dict(),
                        "message": (
                            "The starting Card no longer selects anything the current "
                            "catalog offers. Review the catalog and create the Control "
                            "Card without an initial selection."
                        ),
                    }
                pruned = resolved.reconciled.to_public_dict()
                record = dataclasses.replace(
                    record,
                    operations=tuple(resolved.operations),
                    resource_grants={
                        key: tuple(value)
                        for key, value in resolved.resource_grants.items()
                    },
                    resource_operations={
                        key: tuple(value)
                        for key, value in resolved.resource_operations.items()
                    },
                    named_service_operations=resolved.named_service_operations,
                    named_services=copy.deepcopy(resolved.named_services),
                    account_scope={
                        provider: {
                            account_id: tuple(claims)
                            for account_id, claims in accounts.items()
                        }
                        for provider, accounts in resolved.account_scope.items()
                    },
                    identity_scope=resolved.identity_scope,
                    properties=resolved.properties,
                    resource_acceptance=next_resource_acceptance(
                        resources=resolved.resource_grants,
                        row_for=lambda resource: self._configured_resource(
                            resource,
                            config=catalog_config,
                        ),
                        catalog_version=catalog_version,
                        selected_operations=resolved.resource_operations,
                    ),
                )
            record = record_from_card(
                materialize_control_snapshot(
                    card_authority_from_record(record),
                    basis_catalog_version=catalog_version,
                    origin="created",
                )
            )
            await self._persist_record(record, expected_revision=0)
        except (CardRecordError, ControlCardError) as exc:
            return {"ok": False, "error": exc.reason, "status": 400}
        except CardServingUnavailable as exc:
            return _serving_state_unavailable(exc)
        except (CardUnavailable, CardConflict, CardCommitFailed) as exc:
            return {
                "ok": False,
                "error": "control_card_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
            }
        await self.notify_change(
            grantor_subject,
            action="control_card_created",
            access=record.to_public_dict(),
        )
        return {
            "ok": True,
            "created": True,
            "control_card": await self._control_card_public_view(user, record),
            "authority": card_authority_from_record(record).to_dict(),
            "pruned": pruned,
        }

    async def control_card_update(
        self,
        user: Mapping[str, Any],
        *,
        control_id: str,
        resource_grants: Mapping[str, Any] | None = None,
        resource_operations: Mapping[str, Any] | None = None,
        named_service_operations: Mapping[str, Any] | str | None = None,
        account_scope: Mapping[str, Any] | None = None,
        properties: Mapping[str, Any] | None = None,
        composition_mode: str | None = None,
        label: str | None = None,
        expected_card_revision: int | None = None,
        expected_catalog_version: str | None = None,
        accepted_operations: Mapping[str, Iterable[str]] | None = None,
    ) -> dict[str, Any]:
        """Edit a Control Card through the ordinary catalog-aware Card path."""

        grantor_subject = _subject_from_user(user)
        if not grantor_subject:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}
        try:
            existing = await self._load_record(
                _clean(control_id),
                grantor_subject=grantor_subject,
            )
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "control_card_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        if existing is None or not _record_is_credentialless(existing):
            return {"ok": False, "error": "control_card_not_found", "status": 404}
        updated = await self.update_access(
            user,
            access_id=existing.access_id,
            resource_grants=(
                resource_grants
                if resource_grants is not None
                else existing.resource_grants
            ),
            resource_operations=(
                resource_operations
                if resource_operations is not None
                else existing.resource_operations
            ),
            named_service_operations=(
                named_service_operations
                if named_service_operations is not None
                else existing.named_service_operations.to_stored()
            ),
            account_scope=(
                account_scope if account_scope is not None else existing.account_scope
            ),
            properties=(
                properties if properties is not None else existing.properties
            ),
            composition_mode=(
                composition_mode
                if composition_mode is not None
                else existing.composition_mode
            ),
            label=label,
            expected_card_revision=expected_card_revision,
            expected_catalog_version=expected_catalog_version,
            accepted_operations=accepted_operations,
        )
        if updated.get("ok") is not True:
            return updated
        result = await self.control_card_get(user, control_id=existing.access_id)
        if updated.get("pruned") is not None:
            result["pruned"] = updated["pruned"]
        return result

    async def control_card_basis(
        self,
        user: Mapping[str, Any],
        *,
        access_id: str,
    ) -> dict[str, Any]:
        """Compatibility read used by callers staged before direct attach."""

        grantor_subject = _subject_from_user(user)
        if not grantor_subject:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}
        try:
            record = await self._load_record(
                _clean(access_id),
                grantor_subject=grantor_subject,
            )
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_cards_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        if record is None or record.grantor_subject != grantor_subject:
            return {"ok": False, "error": "delegated_access_not_found", "status": 404}
        if _record_is_credentialless(record):
            return {
                "ok": False,
                "error": "control_card_attachment_target_invalid",
                "status": 409,
            }
        return {"ok": True, "card": card_authority_from_record(record).to_dict()}

    async def attach_control_card(
        self,
        user: Mapping[str, Any],
        *,
        access_id: str,
        control_id: str,
        expected_card_revision: int | None = None,
        replace_control_id: str = "",
    ) -> dict[str, Any]:
        """Attach or compare-and-replace one current Control Card.

        Replacement is one caller-Card revision. It is used when an issuer
        moves an existing binding to a new Control Card without an interval in
        which no rule applies.
        """

        grantor_subject = _subject_from_user(user)
        if not grantor_subject:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}
        refusal = _delegate_mutation_refusal(user)
        if refusal is not None:
            return refusal
        selected_access_id = _clean(access_id)
        selected_control_id = _clean(control_id)
        expected_previous_control_id = _clean(replace_control_id)
        if not selected_access_id or not selected_control_id:
            return {"ok": False, "error": "control_card_binding_invalid", "status": 400}
        try:
            record = await self._load_record(
                selected_access_id,
                grantor_subject=grantor_subject,
            )
            control = await self._resolve_control_record(
                selected_control_id,
                grantor_subject=grantor_subject,
            )
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "control_card_unavailable",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
            }
        except Exception:
            return {
                "ok": False,
                "error": "control_card_unavailable",
                "reason": "control_card_lookup_unavailable",
                "retryable": True,
                "status": 503,
            }
        if record is None or record.grantor_subject != grantor_subject:
            return {"ok": False, "error": "delegated_access_not_found", "status": 404}
        if _record_is_credentialless(record):
            return {"ok": False, "error": "control_card_target_invalid", "status": 409}
        if expected_card_revision is not None and int(expected_card_revision) != int(
            record.card_revision
        ):
            return {
                "ok": False,
                "error": "delegated_access_precondition_failed",
                "status": 409,
                "mismatched": {
                    "card_revision": {
                        "expected": int(expected_card_revision),
                        "actual": int(record.card_revision),
                    }
                },
            }
        if record.control_card is not None:
            if record.control_card.control_id == selected_control_id:
                return {
                    "ok": True,
                    "attached": False,
                    "access": record.to_public_dict(),
                    "control_card": await self._effective_control_view(record),
                }
            if record.control_card.control_id != expected_previous_control_id:
                return {
                    "ok": False,
                    "error": "control_card_already_attached",
                    "status": 409,
                    "control_card": record.control_card.to_dict(),
                }
        if control is None or not _record_is_credentialless(control):
            return {
                "ok": False,
                "error": "control_card_unavailable",
                "reason": "control_card_unresolvable",
                "retryable": True,
                "status": 503,
            }
        if control.grantor_subject != grantor_subject:
            return {"ok": False, "error": "control_card_grantor_mismatch", "status": 403}
        binding = ControlCardBinding(
            control_id=control.access_id,
            issuer_ref=control.issuer_ref,
            issuer_kind=control.issuer_kind,
            issuer_label=control.issuer_label,
            manage_url=control.manage_url,
            control_revision=control.card_revision,
        )
        try:
            effective_card_authority(
                dataclasses.replace(
                    card_authority_from_record(record),
                    control_card=binding,
                ),
                card_authority_from_record(control),
            )
        except ControlCardMismatch as exc:
            return {
                "ok": False,
                "error": "control_card_invalid",
                "reason": exc.reason,
                "status": 409,
            }
        updated = dataclasses.replace(
            record,
            card_revision=record.card_revision + 1,
            control_card=binding,
        )
        try:
            await self._persist_record(updated, expected_revision=record.card_revision)
        except CardServingUnavailable as exc:
            return _serving_state_unavailable(exc)
        except (CardUnavailable, CardConflict, CardCommitFailed) as exc:
            return {
                "ok": False,
                "error": "delegated_card_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
            }
        await self.notify_change(
            grantor_subject,
            action="control_card_attached",
            access=updated.to_public_dict(),
        )
        return {
            "ok": True,
            "attached": True,
            "replaced": bool(expected_previous_control_id),
            "previous_control_id": expected_previous_control_id,
            "access": updated.to_public_dict(),
            "control_card": await self._effective_control_view(updated),
        }

    async def control_card_revoke(
        self,
        user: Mapping[str, Any],
        *,
        control_id: str,
    ) -> dict[str, Any]:
        """Revoke one Control Card through the regular Card lifecycle."""

        grantor_subject = _subject_from_user(user)
        if not grantor_subject:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}
        try:
            loaded = await self._load_record_any_state(
                _clean(control_id),
                grantor_subject=grantor_subject,
            )
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "control_card_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        record = loaded[0] if loaded is not None else None
        if record is None or not _record_is_credentialless(record):
            return {"ok": False, "error": "control_card_not_found", "status": 404}
        result = await self.revoke_access(user, access_id=record.access_id)
        if result.get("ok") is True:
            result["control_id"] = record.access_id
        return result

    async def detach_control_card(
        self,
        user: Mapping[str, Any],
        *,
        access_id: str,
        control_id: str,
        expected_card_revision: int | None = None,
    ) -> dict[str, Any]:
        """Unlink the named Control Card so the caller Card applies alone."""

        grantor_subject = _subject_from_user(user)
        if not grantor_subject:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}
        refusal = _delegate_mutation_refusal(user)
        if refusal is not None:
            return refusal
        selected_access_id = _clean(access_id)
        selected_control_id = _clean(control_id)
        if not selected_access_id or not selected_control_id:
            return {"ok": False, "error": "control_card_binding_invalid", "status": 400}
        try:
            loaded = await self._load_record_any_state(
                selected_access_id,
                grantor_subject=grantor_subject,
            )
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_cards_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        record = loaded[0] if loaded is not None else None
        if record is None or record.grantor_subject != grantor_subject:
            return {"ok": False, "error": "delegated_access_not_found", "status": 404}
        if loaded is not None and loaded[1] != CARD_STATE_ACTIVE:
            return {
                "ok": False,
                "error": "delegated_access_not_active",
                "status": 409,
            }
        if expected_card_revision is not None and int(expected_card_revision) != int(
            record.card_revision
        ):
            return {
                "ok": False,
                "error": "delegated_access_precondition_failed",
                "status": 409,
            }
        if record.control_card is None:
            return {"ok": True, "detached": False, "access": record.to_public_dict()}
        if record.control_card.control_id != selected_control_id:
            return {
                "ok": False,
                "error": "control_card_binding_mismatch",
                "status": 409,
                "control_card": record.control_card.to_dict(),
            }
        updated = dataclasses.replace(
            record,
            card_revision=record.card_revision + 1,
            control_card=None,
        )
        try:
            await self._persist_record(updated, expected_revision=record.card_revision)
        except CardServingUnavailable as exc:
            return _serving_state_unavailable(exc)
        except (CardUnavailable, CardConflict, CardCommitFailed) as exc:
            return {
                "ok": False,
                "error": "delegated_card_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
            }
        await self.notify_change(
            grantor_subject,
            action="control_card_detached",
            access=updated.to_public_dict(),
        )
        return {
            "ok": True,
            "detached": True,
            "access": updated.to_public_dict(),
        }

    # Compatibility for already-staged Problem Board bundles. New callers use
    # the generic Control Card operations above.
    async def project_control_basis(
        self, user: Mapping[str, Any], *, access_id: str
    ) -> dict[str, Any]:
        return await self.control_card_basis(user, access_id=access_id)

    async def attach_project_control(
        self,
        user: Mapping[str, Any],
        *,
        access_id: str,
        control_id: str,
        expected_card_revision: int | None = None,
    ) -> dict[str, Any]:
        result = await self.attach_control_card(
            user,
            access_id=access_id,
            control_id=control_id,
            expected_card_revision=expected_card_revision,
        )
        if "control_card" in result:
            result["project_control"] = result["control_card"]
        return result

    async def detach_project_control(
        self,
        user: Mapping[str, Any],
        *,
        access_id: str,
        control_id: str,
        expected_card_revision: int | None = None,
    ) -> dict[str, Any]:
        return await self.detach_control_card(
            user,
            access_id=access_id,
            control_id=control_id,
            expected_card_revision=expected_card_revision,
        )

    async def card_for_access_id(
        self,
        *,
        grantor_subject: str,
        access_id: str,
    ) -> DelegatedCardView | None:
        """Resolve one active card for a trusted request-bound host adapter.

        The grantor coordinate must come from authenticated credential facts,
        never from caller input. Keeping this as a typed internal read avoids
        making Gateway or Projection parse the public ``describe_card``
        payload or reach into card persistence directly.
        """

        grantor = _clean(grantor_subject)
        selected_access_id = _clean(access_id)
        if not grantor or not selected_access_id:
            return None
        record = await self._load_record(
            selected_access_id,
            grantor_subject=grantor,
        )
        if record is None or record.grantor_subject != grantor:
            return None
        return await self._card_view(record)

    async def resident_profile_card(
        self,
        *,
        grantor_subject: str,
        client_id: str,
    ) -> DelegatedCardView | None:
        """The read model of one resident profile's card, or ``None`` when the
        profile has no active card. Reads the stable card first, then a legacy
        record that has not been folded yet."""
        grantor = _clean(grantor_subject)
        client = _clean(client_id)
        if not grantor or not client:
            return None
        record = await self._load_record(
            stable_resident_access_id(grantor, client),
            grantor_subject=grantor,
        )
        if record is None:
            legacy = await self._legacy_resident_records(
                grantor_subject=grantor,
                client_id=client,
            )
            if len(legacy) == 1:
                record = legacy[0]
            elif legacy:
                # Several legacy records: no single card answers for the profile
                # until they are folded; report nothing rather than one of them.
                return None
        if record is None:
            return None
        return await self._card_view(record)

    async def _resident_agent_token_payload(
        self,
        record: AutomationAccessRecord,
    ) -> dict[str, Any] | None:
        if (
            record.source != ACCESS_SOURCE_AGENT
            or not record.access_token
            or (record.expires_at and record.expires_at <= int(time.time()))
        ):
            return None
        view = await self._card_view(record)
        if view.state != CARD_STATE_ACTIVE:
            return None
        return {
            "access_token": record.access_token,
            "expires_at": record.expires_at,
            "access_id": record.access_id,
            "client_id": record.client_id,
            "identity_scope": record.identity_scope or "grantor",
            "card_revision": record.card_revision,
            "card": view.to_dict(),
        }

    async def resident_agent_access_token_for_access_id(
        self,
        *,
        grantor_subject: str,
        client_id: str,
        access_id: str,
    ) -> dict[str, Any] | None:
        """Resolve one exact resident Card credential by its stable id."""

        grantor = _clean(grantor_subject)
        client = _clean(client_id)
        selected_access_id = _clean(access_id)
        if (
            not grantor
            or not client
            or not selected_access_id
            or not is_resident_client_id(client)
            or selected_access_id != stable_resident_access_id(grantor, client)
        ):
            return None
        record = await self._load_record(
            selected_access_id,
            grantor_subject=grantor,
        )
        if (
            record is None
            or record.grantor_subject != grantor
            or _clean(record.client_id) != client
        ):
            return None
        return await self._resident_agent_token_payload(record)

    async def agent_access_token(
        self,
        *,
        grantor_subject: str,
        client_id: str,
        resources: Iterable[str],
    ) -> dict[str, Any] | None:
        """The consented bearer for a per-agent grant, or ``None`` when the user
        has not granted THIS agent access to these resources (consent pending) or
        the grant has expired. Keyed by the SAME deterministic access_id
        `create_access(client_id=…)` writes, so the per-turn resolver reuses the
        stored, already-bound token instead of minting an unbound one."""
        record = await self._resident_card_for_resources(
            grantor_subject=grantor_subject, client_id=client_id, resources=resources,
        )
        if record is None:
            return None
        if not record.access_token:
            return None
        return {
            "access_token": record.access_token,
            "authorization_header": f"Bearer {record.access_token}",
            "expires_at": record.expires_at,
            "resource_grants": {key: list(value) for key, value in record.resource_grants.items()},
            "resource_operations": {
                key: list(value)
                for key, value in record.resource_operations.items()
            },
            "operations": list(record.operations),
            "client_id": record.client_id,
        }

    async def agent_namespace_grant_state(
        self,
        *,
        grantor_subject: str,
        client_id: str,
        namespace: str,
        operation: str,
        access_id: str = "",
        delegate_identity: str = "",
    ) -> dict[str, Any]:
        """Resolve current delegated authority for one named-service call.

        The ceiling is the registered catalog, not live props, so this answers
        the same question the managed guard answers for the MCP door: the
        catalog resource that publishes ``namespace``, the operation's declared
        grants plus the resource's entry grants, intersected with the selected
        card. ``access_id`` selects the exact bearer-authenticated card; without
        it, ``client_id`` selects the hosted agent's deterministic card.

        Outcomes:

            {"governed": False}                nothing publishes the namespace
            {"governed": True, "granted": …}   the catalog offers it
            {"removed": <structured denial>}   the card holds it, the catalog
                                               no longer offers it
            CatalogUnavailable                 current authority is unknown

        A refusal says WHICH side refused: ``missing_claims`` when the card
        lacks the claims, ``not_granted`` (a structured card-side denial) when
        it holds them and only its own boundary excludes the operation. Without
        that, a caller reads every refusal as "ask for consent" and loops on
        claims the card already carries.
        """
        ns = _clean(namespace).lower().rstrip(":")
        op = _clean(operation)
        if not ns or not op:
            return {"governed": False}
        exact_access_id = _clean(access_id)
        exact_record: AutomationAccessRecord | None = None
        if exact_access_id:
            exact_record = await self._load_record(
                exact_access_id,
                grantor_subject=grantor_subject,
            )
            if exact_record is None:
                return {
                    "governed": True,
                    "granted": False,
                    "access_id": exact_access_id,
                    "card_error": "delegated_card_not_active",
                }
            if _clean(exact_record.client_id) != _clean(client_id):
                return {
                    "governed": True,
                    "granted": False,
                    "access_id": exact_access_id,
                    "card_error": "delegated_card_client_mismatch",
                }
            expected_delegate = _clean(delegate_identity)
            if expected_delegate and _clean(exact_record.delegate_subject) != expected_delegate:
                return {
                    "governed": True,
                    "granted": False,
                    "access_id": exact_access_id,
                    "card_error": "delegated_card_delegate_mismatch",
                }
        active = await self._active_catalog()
        catalog = ActiveCatalogCapabilities(active)
        for cfg in oauth_delegated_config_from_connections(active.connections).resources:
            # Which row an exact card reaches. Membership by key alone reads
            # empty for a card whose grants are keyed by the request URL, so the
            # row that governs it is found the same way the guard finds it.
            if exact_record is not None and not _card_holds_resource(
                exact_record, cfg.resource
            ):
                continue
            named = cfg.named_services if isinstance(cfg.named_services, Mapping) else None
            raw_namespaces = named.get("namespaces") if named else None
            if not isinstance(raw_namespaces, Mapping):
                continue
            policy = None
            for raw_ns, raw_policy in raw_namespaces.items():
                if _clean(raw_ns).lower().rstrip(":") == ns and isinstance(raw_policy, Mapping):
                    policy = raw_policy
                    break
            if policy is None:
                continue
            # The common MCP entry requirement = the grants of the resource's
            # generic tools (e.g. named_services:use) — NOT `cfg.grants`, which
            # is the resource's full scope ceiling.
            required: set[str] = set()
            for tool_cfg in cfg.tools or ():
                required |= set(_as_list(list(getattr(tool_cfg, "grants", ()) or ())))
            raw_tools = policy.get("tools")
            for tool_policy in (raw_tools or {}).values() if isinstance(raw_tools, Mapping) else ():
                if not isinstance(tool_policy, Mapping):
                    continue
                operation_policies = tool_policy.get("operations")
                if isinstance(operation_policies, Mapping) and operation_policies:
                    for op_name, op_policy in operation_policies.items():
                        if _clean(op_name) == op:
                            required |= self._operation_grants(
                                dict(op_policy) if isinstance(op_policy, Mapping) else {},
                                dict(tool_policy),
                            )
                    continue
                if _clean(tool_policy.get("operation") or "") == op:
                    required |= self._operation_grants({}, dict(tool_policy))
            # A service method reads through its own persistence port; the
            # standalone probe exists for callers that have no service.
            if exact_access_id:
                record = exact_record
            else:
                record = await self._resident_card_for_resources(
                    grantor_subject=grantor_subject,
                    client_id=client_id,
                    resources=[cfg.resource],
                )
            if record is not None and record.control_card is not None:
                try:
                    control = await self._resolve_control_record(
                        record.control_card.control_id,
                        grantor_subject=record.grantor_subject,
                    )
                    if control is None:
                        raise ControlCardMismatch("control_card_unresolvable")
                    record = record_from_card(effective_card_authority(
                        card_authority_from_record(record),
                        card_authority_from_record(control),
                    ))
                except (CardUnavailable, ControlCardMismatch) as exc:
                    return {
                        "governed": True,
                        "granted": False,
                        "access_id": exact_access_id or record.access_id,
                        "card_error": getattr(exc, "reason", str(exc)),
                    }
            # The namespace survives, but the operation under it may not. A
            # capability the catalog no longer offers is refused outright —
            # consent cannot restore it.
            removed = authorize_current_capability(
                catalog=catalog,
                provenance=CardProvenance(
                    access_id=record.access_id if record is not None else "",
                    card_revision=record.card_revision if record is not None else 0,
                    catalog_version=record.catalog_version if record is not None else "",
                ),
                request=CapabilityRequest(
                    kind=CAPABILITY_NAMED_SERVICE_OPERATION,
                    resource=cfg.resource,
                    surface="named_service",
                    namespace=ns,
                    operation=op,
                ),
            )
            if removed is not None:
                return {"governed": True, "granted": False, "removed": removed}
            # The claim the operation costs is subject to the same ceiling as the
            # operation itself. A claim the card holds and the catalog no longer
            # publishes is a removal, not a missing grant: it answers `removed`
            # rather than `missing_claims`, or the caller is told to ask for
            # consent that nobody can give.
            for claim in sorted(required):
                removed = authorize_current_capability(
                    catalog=catalog,
                    provenance=CardProvenance(
                        access_id=record.access_id if record is not None else "",
                        card_revision=record.card_revision if record is not None else 0,
                        catalog_version=record.catalog_version if record is not None else "",
                    ),
                    request=CapabilityRequest(
                        kind=CAPABILITY_RESOURCE_CLAIM,
                        resource=cfg.resource,
                        surface="named_service",
                        claim=claim,
                    ),
                )
                if removed is not None:
                    return {"governed": True, "granted": False, "removed": removed}
            granted = False
            missing_claims: list[str] = sorted(required)
            not_granted: dict[str, Any] | None = None
            if record is not None:
                held = _card_claims_for_resource(record, cfg.resource)
                held.update(
                    _account_scope_claims_for_requirements(
                        record,
                        required=required,
                    )
                )
                missing_claims = sorted(required - held)
                granted = not missing_claims
                if granted:
                    # The materialized boundary is the card's answer, the same
                    # tree the named-services door enforces. Reading the
                    # selection instead loses the wildcard, whose meaning is
                    # "everything the acknowledged catalog offered" — an
                    # expansion that lives in the boundary, not in the intent.
                    # A pre-encoding record carries no boundary and keeps its
                    # claims-only compatibility answer.
                    if not record.named_service_operations.is_unknown:
                        granted = boundary_permits_operation(
                            record.named_services, namespace=ns, operation=op
                        )
                        if not granted:
                            # The claims are held and the catalog offers this;
                            # only the card's own boundary excludes it. Saying
                            # so is what keeps the caller out of a consent loop
                            # for claims it already has.
                            not_granted = card_boundary_denial(
                                provenance=CardProvenance(
                                    access_id=record.access_id,
                                    card_revision=record.card_revision,
                                    catalog_version=record.catalog_version,
                                ),
                                request=CapabilityRequest(
                                    kind=CAPABILITY_NAMED_SERVICE_OPERATION,
                                    resource=cfg.resource,
                                    surface="named_service",
                                    namespace=ns,
                                    operation=op,
                                ),
                            )
            return {
                "governed": True,
                "granted": granted,
                "not_granted": not_granted,
                "missing_claims": missing_claims,
                "resource": cfg.resource,
                "claims": sorted(required),
                "client_id": client_id,
                "access_id": record.access_id if record is not None else exact_access_id,
                "card_revision": record.card_revision if record is not None else 0,
                "card_catalog_version": record.catalog_version if record is not None else "",
                "active_catalog_version": catalog.version,
                "delegate_identity": record.delegate_subject if record is not None else "",
                "expires_at": record.expires_at if record is not None else 0,
                # The agent's per-account claim binding, so the native gate can
                # bind it for the connected-account resolver (which account +
                # which claims this agent may use per provider). Empty remains
                # a delegated, default-closed binding.
                "account_scope": {
                    provider: {account_id: list(claims) for account_id, claims in accounts.items()}
                    for provider, accounts in (record.account_scope.items() if record is not None else ())
                },
                "resource_claims": sorted(
                    _card_claims_for_resource(record, cfg.resource)
                    if record is not None else ()
                ),
                "conversation_targets": list(
                    conversation_targets(record.properties if record is not None else None)
                ),
            }
        # Nothing in the active catalog publishes the namespace. A card that
        # still carries it is a removed capability, not an ungoverned call: the
        # user cannot consent their way back to something the deployment no
        # longer offers.
        removed = await self._removed_namespace_denial(
            catalog=catalog,
            grantor_subject=grantor_subject,
            client_id=client_id,
            namespace=ns,
            operation=op,
            exact_record=exact_record,
            exact=bool(exact_access_id),
        )
        if removed is not None:
            return {"governed": True, "granted": False, "removed": removed}
        return {"governed": False}

    async def _removed_namespace_denial(
        self,
        *,
        catalog: "ActiveCatalogCapabilities",
        grantor_subject: str,
        client_id: str,
        namespace: str,
        operation: str,
        exact_record: AutomationAccessRecord | None = None,
        exact: bool = False,
    ) -> dict[str, Any] | None:
        """The structured denial for a namespace this agent's card still holds.

        The card's materialized boundary is the expansion of its selection under
        the catalog version it was saved against, so membership there is what
        proves the capability was granted.
        """
        client = _clean(client_id)
        records = (
            [exact_record]
            if exact and exact_record is not None
            else await self._list_active_records(grantor_subject)
        )
        for record in records:
            if record is None:
                continue
            if not exact and (
                record.source != ACCESS_SOURCE_AGENT
                or _clean(record.client_id) != client
            ):
                continue
            namespaces = (record.named_services or {}).get("namespaces")
            if not isinstance(namespaces, Mapping):
                continue
            if not any(
                _clean(name).lower().rstrip(":") == namespace for name in namespaces
            ):
                continue
            resource = next(iter(record.resource_grants), "")
            return capability_denial(
                catalog=catalog,
                provenance=CardProvenance(
                    access_id=record.access_id,
                    card_revision=record.card_revision,
                    catalog_version=record.catalog_version,
                ),
                request=CapabilityRequest(
                    kind=CAPABILITY_NAMED_SERVICE_OPERATION,
                    resource=str(resource),
                    surface="named_service",
                    namespace=namespace,
                    operation=operation,
                ),
            )
        return None

    @staticmethod
    def _catalog_named_service_operations(
        active: Any,
        resource_grants: Mapping[str, Any],
    ) -> dict[str, set[str]]:
        """``namespace -> operations`` the active catalog offers for these resources."""
        capabilities = ActiveCatalogCapabilities(active)
        offered: dict[str, set[str]] = {}
        for resource in resource_grants:
            cfg = capabilities.resource_config(
                CapabilityRequest(
                    kind=CAPABILITY_NAMED_SERVICE_OPERATION,
                    resource=_clean(resource),
                    surface="named_service",
                    namespace="-",
                    operation="-",
                )
            )
            block = getattr(cfg, "named_services", None) if cfg is not None else None
            if not isinstance(block, Mapping):
                continue
            for namespace, operations in configured_named_service_operations(block).items():
                offered.setdefault(namespace, set()).update(operations)
        return offered

    def _materialized_boundary_for(
        self,
        *,
        selection: NamedServiceSelection,
        resource: str,
        grants: Iterable[str],
        account_scope: Mapping[str, Any] | None = None,
        config: Any = None,
    ) -> dict[str, Any]:
        """The boundary tree a selection expands to for one resource.

        The selection is looked up under the caller's ``resource``, which is the
        key the card stores it under. The configured row is still resolved by
        pattern, but its selector is not that key: an OAuth consent records the
        concrete request URL, so looking up under ``cfg.resource`` misses and
        narrows the boundary to nothing.
        """
        source = config or self._config
        cfg = source.resource_config(resource) if resource else None
        if cfg is None or not isinstance(getattr(cfg, "named_services", None), Mapping):
            return {}
        try:
            return named_service_policy_for_resource(
                named_services=cfg.named_services,
                resource=resource,
                selection=_selection_policy_argument(selection),
                grants=_effective_named_service_grants(
                    grants,
                    account_scope=account_scope,
                    required=getattr(cfg, "grants", ()) or (),
                ),
            )
        except ValueError:
            return {}

    async def oauth_consent_card_seed(
        self,
        *,
        grantor_subject: str,
        client_id: str,
        resource: str,
        client_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return the non-secret card state used to seed OAuth review.

        A reconnect edits the exact OAuth card identified by grantor and client,
        plus the entry resource for a connector. A first connection has no
        stored card and starts from the request's proposed authority in the
        HTTP adapter.
        """

        grantor = _clean(grantor_subject)
        client = _clean(client_id)
        entry_resource = _clean(resource)
        if not grantor or not client:
            return {"ok": False, "error": "oauth_consent_identity_incomplete"}
        identity = await self.resolve_oauth_card_identity(
            grantor_subject=grantor,
            client_id=client,
            entry_resource=entry_resource,
            client_metadata=client_metadata,
        )
        if identity.get("ok") is not True:
            return identity
        if not entry_resource and not _whole_card_consent(
            client_metadata=client_metadata, identity=identity
        ):
            return {"ok": False, "error": "oauth_consent_identity_incomplete"}
        access_id = _clean(identity.get("access_id"))
        card_kind = _clean(identity.get("card_kind"))
        try:
            loaded = await self._load_record_any_state(
                access_id,
                grantor_subject=grantor,
            )
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_cards_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        current_record, current_state = loaded or (None, "")
        record = current_record if current_state == CARD_STATE_ACTIVE else None
        payload: dict[str, Any] = {
            "ok": True,
            "access_id": access_id,
            "card_kind": card_kind,
            "card_revision": int(
                current_record.card_revision if current_record is not None else 0
            ),
        }
        try:
            catalog_config = await self._catalog_config(
                await self._active_catalog(), owner_subject=grantor
            )
        except CatalogUnavailable:
            catalog_config = None
        view_record = (
            self._canonical_oauth_record(record, config=catalog_config)
            if record is not None and catalog_config is not None
            else record
        )
        full_catalog = client_uses_full_card_catalog(client_metadata)
        if not entry_resource:
            # A whole Card has no entry door: a full-catalog client may select
            # from the whole catalog; otherwise the Card's own resources bound it.
            allowed = (
                None
                if full_catalog
                else set(view_record.resource_grants if view_record is not None else ())
            )
        else:
            allowed = (
                self._oauth_allowed_resources(
                    entry_resource=entry_resource,
                    client_metadata=client_metadata,
                    config=catalog_config,
                )
                if catalog_config is not None
                else (None if full_catalog else set())
            )
        payload["catalog_scope"] = {
            "mode": "full" if allowed is None else "entry",
            "resources": sorted(allowed or ()),
        }
        rows: dict[str, str] = {}
        if catalog_config is not None:
            selected_resources: list[str] = []
            if entry_resource:
                entry_key, _literal = resolve_declared_resource(
                    catalog_config,
                    entry_resource,
                )
                selected_resources.append(entry_key)
            if view_record is not None:
                selected_resources.extend(view_record.resource_grants)
            for selected_resource in selected_resources:
                configured = self._configured_resource(
                    selected_resource, config=catalog_config
                )
                if configured is not None:
                    rows[selected_resource] = _clean(
                        getattr(configured, "resource", "")
                    )
        if rows:
            payload["catalog_row_by_resource"] = rows
        if view_record is None:
            return payload
        access = view_record.to_public_dict()
        if rows:
            access["catalog_row_by_resource"] = rows
        if self._invocation_policies is not None:
            policies: dict[tuple[str, str, str, str], tuple[bool, int, dict[str, Any]]] = {}
            for policy in await self._invocation_policies.list_for_card(
                owner_subject=grantor,
                access_id=access_id,
            ):
                view = policy.to_public_dict()
                authority = dict(view.get("authority") or {})
                raw_resource = _clean(authority.get("resource"))
                resource_key = raw_resource
                if catalog_config is not None:
                    resource_key, _literal = resolve_declared_resource(
                        catalog_config,
                        raw_resource,
                    )
                authority["resource"] = resource_key
                view["authority"] = authority
                key = (
                    resource_key,
                    _clean(authority.get("operation")),
                    _clean(authority.get("provider_id")),
                    _clean(authority.get("account_id")),
                )
                candidate = (
                    raw_resource == resource_key,
                    int(view.get("revision") or 0),
                    view,
                )
                if key not in policies or candidate[:2] > policies[key][:2]:
                    policies[key] = candidate
            access["invocation_policies"] = [
                candidate[2] for _key, candidate in sorted(policies.items())
            ]
        payload["access"] = access
        return payload

    async def resolve_oauth_consent_authority(
        self,
        user: Mapping[str, Any],
        *,
        client_id: str,
        entry_resource: str,
        requested_grants: Iterable[str],
        client_metadata: Mapping[str, Any] | None,
        resource_grants: Mapping[str, Any],
        resource_operations: Mapping[str, Any],
        named_service_operations: Mapping[str, Any] | str,
        account_scope: Mapping[str, Any],
        expected_card_revision: int,
        expected_catalog_version: str,
        properties: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate a full card-editor OAuth selection without persisting it."""

        grantor = _subject_from_user(user)
        client = _clean(client_id)
        entry = _clean(entry_resource)
        if not grantor:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}
        refusal = _delegate_mutation_refusal(user)
        if refusal is not None:
            return refusal
        if not client:
            return {"ok": False, "error": "oauth_consent_identity_incomplete"}

        try:
            active = await self._active_catalog()
            catalog_version = self._version_of(active)
        except CatalogUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_catalog_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        if _clean(expected_catalog_version) != catalog_version:
            return {
                "ok": False,
                "error": "consent_catalog_changed",
                "status": 409,
                "expected_catalog_version": _clean(expected_catalog_version),
                "active_catalog_version": catalog_version,
            }

        catalog_config = await self._catalog_config(active, owner_subject=grantor)
        entry_config = (
            self._configured_resource(entry, config=catalog_config) if entry else None
        )
        if entry and entry_config is None:
            return {
                "ok": False,
                "error": "delegated_access_unknown_resources",
                "resources": [entry],
            }

        identity = await self.resolve_oauth_card_identity(
            grantor_subject=grantor,
            client_id=client,
            entry_resource=entry,
            client_metadata=client_metadata,
        )
        if identity.get("ok") is not True:
            return identity
        if not entry and not _whole_card_consent(
            client_metadata=client_metadata, identity=identity
        ):
            return {"ok": False, "error": "oauth_consent_identity_incomplete"}
        access_id = _clean(identity.get("access_id"))
        card_kind = _clean(identity.get("card_kind"))
        try:
            actual_revision = await self._committed_revision(
                access_id,
                grantor_subject=grantor,
            )
            existing = await self._load_record(
                access_id,
                grantor_subject=grantor,
            )
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_cards_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        if int(expected_card_revision) != actual_revision:
            return {
                "ok": False,
                "error": "delegated_card_save_conflict",
                "status": 409,
                "mismatched": {
                    "card_revision": {
                        "expected": int(expected_card_revision),
                        "actual": actual_revision,
                    }
                },
            }

        selected = self._resource_grants(resource_grants)
        selected, _rewritten = resolve_declared_resource_keys(
            catalog_config,
            selected,
        )
        requested = _as_list(requested_grants)
        if entry:
            allowed = self._oauth_allowed_resources(
                entry_resource=entry,
                client_metadata=client_metadata,
                config=catalog_config,
            )
        else:
            # A whole Card: the full catalog for a full-catalog client, else
            # the resources this client's existing Card already holds.
            allowed = (
                None
                if client_uses_full_card_catalog(client_metadata)
                else set(existing.resource_grants if existing is not None else ())
            )
        outside_entry = sorted(
            resource for resource in selected if allowed is not None and resource not in allowed
        )
        if outside_entry:
            return {
                "ok": False,
                "error": "oauth_client_resource_unreachable",
                "status": 400,
                "resources": outside_entry,
                "entry_resource": entry,
            }

        if entry:
            entry_key, _literal = resolve_declared_resource(catalog_config, entry)
            seeded_grants = {entry_key: tuple(requested)}
            seeded_operations: dict[str, tuple[str, ...]] = {entry_key: ()}
        else:
            # A whole Card seeds no entry row; its authority is the exact
            # selection validated below.
            seeded_grants = {}
            seeded_operations = {}
        baseline = existing or AutomationAccessRecord(
            access_id=access_id,
            label=client,
            client_id=client,
            grantor_subject=grantor,
            delegate_subject=integration_subject(grantor, client_id=client),
            card_kind=card_kind,
            operations=(),
            resource_grants=seeded_grants,
            resource_operations=seeded_operations,
            named_service_operations=NamedServiceSelection.none(),
            identity_scope=_clean(getattr(entry_config, "identity_scope", "")) or "grantor",
            catalog_version=catalog_version,
            card_revision=0,
            source=ACCESS_SOURCE_OAUTH,
            entry_resource=entry,
        )
        resolved = await self._resolve_card_authority(
            user=user,
            existing=baseline,
            active=active,
            resource_grants=selected,
            resource_operations=resource_operations,
            operations=(),
            named_service_operations=named_service_operations,
            account_scope=account_scope,
            properties=properties,
        )
        if resolved.error is not None:
            return resolved.error
        if resolved.revoke:
            return {
                "ok": False,
                "error": "delegated_access_requires_resource_grants",
            }
        return {
            "ok": True,
            "access_id": access_id,
            "card_kind": card_kind,
            "catalog_version": catalog_version,
            "card_revision": actual_revision,
            "resource_grants": resolved.resource_grants,
            "resource_operations": resolved.resource_operations,
            "operations": resolved.operations,
            "named_service_operations": resolved.named_service_operations.to_stored() or {},
            "named_services": resolved.named_services,
            "account_scope": resolved.account_scope,
            "identity_scope": resolved.identity_scope,
            "properties": resolved.properties,
        }

    async def apply_oauth_invocation_policies(
        self,
        *,
        grantor_subject: str,
        access_id: str,
        resource_operations: Mapping[str, Any],
        invocation_policies: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Apply the card editor's explicit outer-operation use policies."""

        selected_operations = normalize_resource_operations(resource_operations)
        submitted = {
            _clean(resource): {
                _clean(operation): _clean(mode).lower()
                for operation, mode in dict(operations or {}).items()
                if _clean(operation)
            }
            for resource, operations in dict(invocation_policies or {}).items()
            if _clean(resource) and isinstance(operations, Mapping)
        }
        required_keys = {
            (resource, operation)
            for resource, operations in selected_operations.items()
            for operation in operations
        }
        submitted_keys = {
            (resource, operation)
            for resource, operations in submitted.items()
            for operation in operations
        }
        if submitted_keys != required_keys:
            raise ValueError("every selected outer operation requires one invocation policy")
        if not submitted_keys:
            return []
        if self._invocation_policies is None:
            raise ValueError("invocation policy service is unavailable")

        from connection_hub.invocation_policy import (
            POLICY_ALWAYS,
            POLICY_ONCE,
            SURFACE_OUTER,
            InvocationAuthority,
        )

        allowed_modes = {POLICY_ALWAYS, POLICY_ONCE}
        invalid_modes = sorted({
            mode
            for operations in submitted.values()
            for mode in operations.values()
            if mode not in allowed_modes
        })
        if invalid_modes:
            raise ValueError("invalid invocation policy mode(s): " + ", ".join(invalid_modes))

        broad_secret_once = sorted(
            resource
            for resource, operations in submitted.items()
            if any(mode == POLICY_ONCE for mode in operations.values())
            and resource.startswith("urn:kdcube:management:secret:")
            and SecretResource.parse(resource).broad
        )
        if broad_secret_once:
            raise ValueError(
                "broad secret selectors cannot use an allow-once policy: "
                + ", ".join(broad_secret_once)
            )

        current = await self._invocation_policies.list_for_card(
            owner_subject=_clean(grantor_subject),
            access_id=_clean(access_id),
        )
        revisions = {
            (policy.authority.resource, policy.authority.operation): policy.revision
            for policy in current
            if policy.authority.surface == SURFACE_OUTER
            and not policy.authority.provider_id
            and not policy.authority.account_id
        }
        written = []
        for resource, operations in sorted(submitted.items()):
            for operation, mode in sorted(operations.items()):
                policy = await self._invocation_policies.set_policy(
                    owner_subject=_clean(grantor_subject),
                    authority=InvocationAuthority(
                        access_id=_clean(access_id),
                        resource=resource,
                        surface=SURFACE_OUTER,
                        operation=operation,
                    ),
                    mode=mode,
                    expected_revision=revisions.get((resource, operation), 0),
                )
                written.append(policy.to_public_dict())
        return written

    async def record_oauth_grant(
        self,
        *,
        grantor_subject: str,
        client_id: str,
        client_label: str = "",
        scopes: Iterable[str] = (),
        operations: Iterable[str] | None = None,
        resource_grants: Mapping[str, Any] | None = None,
        resource_operations: Mapping[str, Any] | None = None,
        resource: str = "",
        access_id: str = "",
        card_kind: str = "",
        identity_scope: str = "",
        access_token: str = "",
        refresh_token: str = "",
        account_scope: Mapping[str, Any] | None = None,
        named_service_operations: Any = None,
        catalog_version: str = "",
        client_metadata: Mapping[str, Any] | None = None,
        properties: Mapping[str, Any] | None = None,
        replace_authority: bool = False,
        expected_card_revision: int | None = None,
    ) -> AutomationAccessRecord | None:
        """Register (or update) an OAuth-flow delegated grant in the registry.

        Called on every token issuance for an external client (initial consent
        and refresh rotations), so the user sees the connection in Connection
        Hub and revoking it invalidates the CURRENT refresh token and access
        grant. Automation Cards are one record per (grantor, client), while
        connector Cards also include their entry resource; reconsent updates
        that identified Card instead of piling up rows.

        ``replace_authority`` distinguishes an authorization-code exchange
        from a refresh rotation. A reviewed consent replaces every authority
        dimension exactly; a refresh carries the existing card forward.

        Every DCR ``client_id`` is an independent caller. Redirect URIs are
        callback channels, not reconnect identity, so a fresh registration
        never inherits from or retires another client's Card.
        """
        grantor = _clean(grantor_subject)
        client = _clean(client_id)
        if not grantor or not client:
            return None
        resource_value = _clean(resource)
        submitted_client_metadata = normalize_public_client_metadata(client_metadata)
        selected_kind = _clean(card_kind)
        identity = (
            await self.resolve_card_identity(
                grantor_subject=grantor,
                client_id=client,
                card_kind=selected_kind,
                entry_resource=resource_value,
            )
            if selected_kind
            else await self.resolve_oauth_card_identity(
                grantor_subject=grantor,
                client_id=client,
                entry_resource=resource_value,
                client_metadata=submitted_client_metadata,
            )
        )
        if identity.get("ok") is not True:
            raise CardConflict(_clean(identity.get("error")) or "card_identity_invalid")
        selected_kind = _clean(identity.get("card_kind"))
        resolved_access_id = _clean(identity.get("access_id"))
        requested_access_id = _clean(access_id)
        if requested_access_id and requested_access_id != resolved_access_id:
            raise CardConflict("card_identity_mismatch")
        access_id = resolved_access_id
        now = int(time.time())
        created_at = now
        existing_resource_grants: dict[str, tuple[str, ...]] = {}
        existing_account_scope: dict[str, dict[str, list[str]]] = {}
        # A refresh rotation must not widen the card: the named-service
        # selection and its materialized boundary carry forward untouched.
        existing_selection = NamedServiceSelection.unknown()
        existing_named_services: dict[str, Any] = {}
        existing_resource_operations: dict[str, tuple[str, ...]] = {}
        # Token rotation is not an authority change: the card keeps the catalog
        # generation it was last saved against and only advances its revision.
        existing_catalog_version = ""
        existing_client_metadata: dict[str, Any] = {}
        existing_properties: dict[str, Any] = {}
        try:
            existing_card_revision = await self._committed_revision(
                access_id, grantor_subject=grantor
            )
        except CardUnavailable:
            existing_card_revision = 0
        if (
            replace_authority
            and expected_card_revision is not None
            and int(expected_card_revision) != existing_card_revision
        ):
            raise CardConflict(
                "card_revision_moved",
                current_revision=existing_card_revision,
            )
        try:
            existing_card = await self._load_record(access_id, grantor_subject=grantor)
        except CardUnavailable:
            existing_card = None
        if existing_card is not None:
            created_at = existing_card.created_at or now
            existing_resource_grants = dict(existing_card.resource_grants)
            existing_account_scope = {
                provider: {account_id: list(claims) for account_id, claims in accounts.items()}
                for provider, accounts in existing_card.account_scope.items()
            }
            existing_selection = existing_card.named_service_operations
            existing_catalog_version = existing_card.catalog_version
            existing_named_services = copy.deepcopy(dict(existing_card.named_services or {}))
            existing_resource_operations = dict(existing_card.resource_operations)
            existing_client_metadata = copy.deepcopy(
                dict(existing_card.client_metadata or {})
            )
            existing_properties = copy.deepcopy(dict(existing_card.properties or {}))
        selected_properties = existing_properties
        if properties is not None:
            selected_properties = {
                **selected_properties,
                **copy.deepcopy(dict(properties)),
            }
        selected_client_metadata = submitted_client_metadata or existing_client_metadata
        is_initial_consent = existing_card is None
        # A submitted selection is a consent-screen choice and REPLACES what the
        # card held: only the newest consent counts, even if an earlier one said
        # `"*"`. Its absence is a refresh rotation, which carries the selection
        # forward untouched. The grant type already separates the two:
        # authorization_code passes both fields, refresh_token passes neither.
        try:
            submitted_selection = self._named_service_operation_selection(
                named_service_operations
            )
        except ValueError:
            submitted_selection = None
        materialize_boundary = submitted_selection is not None or is_initial_consent
        if materialize_boundary:
            existing_selection = submitted_selection or NamedServiceSelection.none()
            existing_catalog_version = _clean(catalog_version)
        authority_config = None
        try:
            authority_config = await self._catalog_config(
                await self._active_catalog(), owner_subject=grantor
            )
        except CatalogUnavailable:
            authority_config = None
        consent_config = authority_config if materialize_boundary else None
        ttl = max(60, int(getattr(self._store, "refresh_ttl", None) or 86400))
        if resource_grants is not None:
            selected_resource_grants = self._resource_grants(resource_grants)
        elif existing_card is not None and operations is None and resource_operations is None:
            selected_resource_grants = {
                selector: list(grants)
                for selector, grants in existing_resource_grants.items()
            }
        else:
            selected_resource_grants = {
                resource_value or "*": _as_list(list(scopes))
            }
        if authority_config is not None:
            selected_resource_grants, _rewritten_grants = (
                resolve_declared_resource_keys(
                    authority_config,
                    selected_resource_grants,
                )
            )
            existing_selection = self._declared_named_service_selection(
                authority_config,
                existing_selection,
            )
        # A reviewed consent replaces account binding. A refresh merges its
        # carried snapshot with the card so token rotation never drops it.
        merged_account_scope: dict[str, dict[str, list[str]]] = {
            provider: {account_id: list(claims) for account_id, claims in accounts.items()}
            for provider, accounts in normalize_account_scope(account_scope).items()
        }
        for source_scope in (() if replace_authority else (existing_account_scope,)):
            for provider, accounts in source_scope.items():
                target = merged_account_scope.setdefault(provider, {})
                for account_id, claims in accounts.items():
                    held = target.setdefault(account_id, [])
                    for claim in claims:
                        if claim not in held:
                            held.append(claim)
        if materialize_boundary:
            existing_named_services = {}
            for selected_resource, selected_grants in selected_resource_grants.items():
                boundary = self._materialized_boundary_for(
                    selection=existing_selection,
                    resource=selected_resource,
                    grants=selected_grants,
                    account_scope=merged_account_scope,
                    config=consent_config,
                )
                if boundary:
                    existing_named_services = self._merge_named_service_configs(
                        existing_named_services,
                        boundary,
                    )
        if resource_operations is not None:
            selected_resource_operations = normalize_resource_operations(
                resource_operations
            )
        elif operations is None and existing_card is not None:
            selected_resource_operations = existing_resource_operations
        else:
            selected_resource_operations = project_legacy_operations(
                selected_resource_grants, operations or ()
            )
        if authority_config is not None:
            selected_resource_operations, _rewritten_operations = (
                resolve_declared_resource_keys(
                    authority_config,
                    selected_resource_operations,
                )
            )
        record = AutomationAccessRecord(
            access_id=access_id,
            label=(
                (_clean(client_label) or client)
                if existing_card is None or replace_authority
                else existing_card.label
            ),
            client_id=client,
            grantor_subject=grantor,
            delegate_subject=integration_subject(grantor, client_id=client),
            card_kind=(existing_card.card_kind if existing_card is not None else selected_kind),
            operations=operation_union(selected_resource_operations),
            resource_grants={
                selector: tuple(grants)
                for selector, grants in selected_resource_grants.items()
            },
            resource_operations=selected_resource_operations,
            named_service_operations=existing_selection,
            named_services=existing_named_services,
            catalog_version=existing_catalog_version,
            card_revision=existing_card_revision + 1,
            account_scope=normalize_account_scope(merged_account_scope),
            identity_scope=_clean(identity_scope),
            created_at=created_at,
            expires_at=now + ttl,
            source=ACCESS_SOURCE_OAUTH,
            refresh_token=_clean(refresh_token),
            access_token=_clean(access_token),
            last_issued_at=now,
            control_card=(
                existing_card.control_card if existing_card is not None else None
            ),
            # A consent accepts each resource as the consent catalog showed it;
            # a refresh rotation is not a review and carries the card's
            # acceptance forward untouched.
            resource_acceptance=(
                next_resource_acceptance(
                    resources=selected_resource_grants,
                    row_for=(
                        (lambda resource: consent_config.card_selector_config(resource))
                        if consent_config is not None
                        else (lambda resource: None)
                    ),
                    catalog_version=existing_catalog_version,
                    selected_operations=selected_resource_operations,
                    previous=(
                        existing_card.resource_acceptance if existing_card is not None else None
                    ),
                )
                if materialize_boundary
                else (
                    dict(existing_card.resource_acceptance) if existing_card is not None else {}
                )
            ),
            provenance=(
                copy.deepcopy(dict(existing_card.provenance or {}))
                if existing_card is not None
                else {}
            ),
            # The door the client connected to. A refresh rotation passes no
            # resource, so the card's own value carries forward.
            entry_resource=(
                resource_value
                or (existing_card.entry_resource if existing_card is not None else "")
            ),
            client_metadata=selected_client_metadata,
            properties=selected_properties,
        )
        if authority_config is not None:
            record = self._canonical_oauth_record(
                record,
                config=authority_config,
            )
        await self._persist_record(record, expected_revision=existing_card_revision)
        _LOGGER.info(
            "[automation-access] oauth grant recorded card=%s client=%s initial=%s "
            "account_scope_providers=%s",
            access_id, client, is_initial_consent,
            sorted(merged_account_scope.keys()) or "-",
        )
        await self.notify_change(grantor, action="granted", access=record.to_public_dict())
        return record

    async def oauth_seed_account_scope(
        self,
        *,
        grantor_subject: str,
        client_id: str,
        resource: str,
        client_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, dict[str, list[str]]]:
        """The account binding a consent screen should pre-check from this
        exact client's existing Card. Another DCR registration is an
        independent caller and contributes no defaults."""
        grantor = _clean(grantor_subject)
        client = _clean(client_id)
        if not grantor or not client:
            return {}
        seed: dict[str, dict[str, list[str]]] = {}
        sources: list[Mapping[str, Mapping[str, Any]]] = []
        identity = await self.resolve_oauth_card_identity(
            grantor_subject=grantor,
            client_id=client,
            entry_resource=_clean(resource),
            client_metadata=client_metadata,
        )
        if identity.get("ok") is not True:
            return {}
        try:
            own = await self._load_record(
                _clean(identity.get("access_id")),
                grantor_subject=grantor,
            )
        except CardUnavailable:
            own = None
        if own is not None:
            sources.append(own.account_scope)
        for source_scope in sources:
            for provider, accounts in source_scope.items():
                target = seed.setdefault(str(provider), {})
                for account_id, claims in dict(accounts).items():
                    held = target.setdefault(str(account_id), [])
                    for claim in claims:
                        if claim not in held:
                            held.append(str(claim))
        return seed

    async def oauth_seed_named_service_operations(
        self,
        *,
        grantor_subject: str,
        client_id: str,
        resource: str,
        client_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, list[str]]:
        """The named-service operations a consent screen should pre-check.

        Read from the card's MATERIALIZED boundary, not its selection: a
        wildcard means "everything the acknowledged catalog offered", and the
        boundary is that expansion under the version the card is pinned to. So
        the pre-check reproduces exactly what the card grants today.

        Only this exact client's Card contributes defaults. Other DCR client
        ids identify independent callers, even when their redirect URIs use
        the same loopback host.
        """
        grantor = _clean(grantor_subject)
        client = _clean(client_id)
        if not grantor or not client:
            return {}
        identity = await self.resolve_oauth_card_identity(
            grantor_subject=grantor,
            client_id=client,
            entry_resource=_clean(resource),
            client_metadata=client_metadata,
        )
        if identity.get("ok") is not True:
            return {}
        try:
            record = await self._load_record(
                _clean(identity.get("access_id")),
                grantor_subject=grantor,
            )
        except CardUnavailable:
            return {}
        if record is None:
            return {}
        offered = configured_named_service_operations(record.named_services)
        return {
            namespace: sorted(operations)
            for namespace, operations in offered.items()
            if operations
        }

    async def oauth_seed_resource_operations(
        self,
        *,
        grantor_subject: str,
        client_id: str,
        resource: str,
        client_metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, list[str]]:
        """Exact child-resource operations held by this OAuth client's card."""

        grantor = _clean(grantor_subject)
        client = _clean(client_id)
        if not grantor or not client:
            return {}
        identity = await self.resolve_oauth_card_identity(
            grantor_subject=grantor,
            client_id=client,
            entry_resource=_clean(resource),
            client_metadata=client_metadata,
        )
        if identity.get("ok") is not True:
            return {}
        try:
            record = await self._load_record(
                _clean(identity.get("access_id")),
                grantor_subject=grantor,
            )
        except CardUnavailable:
            return {}
        if record is None:
            return {}
        return {
            selector: list(operations)
            for selector, operations in record.resource_operations.items()
            if operations
        }

    async def prune_account_from_grants(
        self, *, grantor_subject: str, provider_id: str, account_id: str
    ) -> dict[str, Any]:
        """Drop a connected account from every grant of this user that binds it.

        Called when the user DISCONNECTS the account. Account ids are
        deterministic (provider + connector app + subject), so a binding left
        behind would silently come back to life if the same account were
        reconnected later - re-granting access nobody ticked again. Disconnect
        therefore closes the bindings too; ``Reconnect`` (re-approval without
        disconnecting) is the action that preserves them.

        Never raises: a pruning failure must not fail the disconnect itself.
        """
        subject = _clean(grantor_subject)
        provider = _clean(provider_id)
        account = _clean(account_id)
        if not subject or not provider or not account:
            return {"pruned": 0, "grants": []}
        try:
            candidates = await self._list_active_records(subject)
        except Exception:
            return {"pruned": 0, "grants": []}
        pruned: list[str] = []
        for record in candidates:
            access_id = record.access_id
            try:
                accounts = dict(record.account_scope.get(provider) or {})
                if account not in accounts:
                    continue
                accounts.pop(account, None)
                scope = {
                    p: {a: list(cl) for a, cl in bound.items()}
                    for p, bound in record.account_scope.items()
                }
                # A provider with no bound accounts drops out entirely - the
                # runtime is default-closed for delegated callers.
                if accounts:
                    scope[provider] = {a: list(cl) for a, cl in accounts.items()}
                else:
                    scope.pop(provider, None)
                pruned_record = replace_fields(
                    record,
                    account_scope={
                        p: {a: tuple(cl) for a, cl in bound.items()}
                        for p, bound in scope.items()
                    },
                    card_revision=record.card_revision + 1,
                )
                await self._persist_record(
                    pruned_record, expected_revision=record.card_revision
                )
                pruned.append(access_id)
                await self.notify_change(
                    subject, action="edited", access=pruned_record.to_public_dict()
                )
            except Exception:
                _LOGGER.warning(
                    "[connection_hub.disconnect] pruning account binding failed "
                    "(non-fatal): access_id=%s provider=%s account=%s",
                    access_id, provider, account, exc_info=True,
                )
                continue
        if pruned:
            _LOGGER.info(
                "[connection_hub.disconnect] cleared account binding from %d grant(s): "
                "provider=%s account=%s grants=%s",
                len(pruned), provider, account, pruned,
            )
        return {"pruned": len(pruned), "grants": pruned}


    async def extend_client_access(
        self,
        user: Mapping[str, Any],
        *,
        client_id: str,
        access_id: str | None = None,
        resource: str,
        claims: Iterable[str],
        resource_operations: Mapping[str, Any] | None = None,
        account_scope: Mapping[str, Any] | None = None,
        named_service_operations: Mapping[str, Any] | str | None = None,
        replace: bool = False,
        label: str | None = None,
    ) -> dict[str, Any]:
        """Edit an EXISTING delegated client's card (an unknown client is never
        created here). ``access_id`` selects an exact existing card when a
        denial is recovered in place; without it, the OAuth card identity is
        derived from the client and resource. ``replace=False``
        MERGES the claims in (a one-click extension); ``replace=True`` makes the
        submitted claim set the resource's grants EXACTLY (the edit-in-place
        path — allowing narrowing, e.g. read+write -> read).
        ``resource_operations`` applies the same merge/replace rule to the
        outer MCP/REST operations selected for this resource. ``account_scope``
        ({provider_id: [account_ids or "*"]}) edits the client's per-provider
        account binding the same way (merge or replace). The card is the
        authority the guard resolves live, so either takes effect on the
        client's very next call, on the bearer it already holds; a
        pointer-carrying refresh re-derives from the card, so a narrowing
        sticks across token rotations."""
        grantor_subject = _subject_from_user(user)
        if not grantor_subject:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}
        refusal = _delegate_mutation_refusal(user)
        if refusal is not None:
            return refusal
        client = _clean(client_id)
        resource_value = _clean(resource)
        claim_list = _as_list(list(claims))
        operation_scope_provided = resource_operations is not None
        try:
            operation_update = normalize_resource_operations(resource_operations)
        except ValueError as exc:
            return {
                "ok": False,
                "error": "invalid_resource_operation_selection",
                "message": str(exc),
            }
        operation_key = resource_value or "*"
        unknown_operation_resources = sorted(
            set(operation_update) - {operation_key}
        )
        if unknown_operation_resources:
            return {
                "ok": False,
                "error": "invalid_resource_operation_selection",
                "resources": unknown_operation_resources,
            }
        account_scope_provided = account_scope is not None
        scope_update: dict[str, dict[str, list[str]]] = {
            provider: {account_id: list(cl) for account_id, cl in accounts.items()}
            for provider, accounts in normalize_account_scope(account_scope).items()
        }
        if not client or (
            not claim_list
            and not operation_scope_provided
            and not scope_update
            and not (account_scope_provided and replace)
        ):
            return {"ok": False, "error": "delegated_access_requires_client_and_authority"}
        selected_access_id = _clean(access_id)
        if not selected_access_id:
            identity = await self.resolve_oauth_card_identity(
                grantor_subject=grantor_subject,
                client_id=client,
                entry_resource=resource_value,
                client_metadata=None,
            )
            if identity.get("ok") is not True:
                return identity
            selected_access_id = _clean(identity.get("access_id"))
        try:
            record = await self._load_record(
                selected_access_id,
                grantor_subject=grantor_subject,
            )
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_cards_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        if record is None:
            return {"ok": False, "error": "delegated_access_unknown_client",
                    "message": "This client has no existing grant to extend; it connects via its own consent flow first."}
        if record.client_id != client:
            return {
                "ok": False,
                "error": "delegated_access_client_mismatch",
                "status": 409,
            }
        # Claims must stay inside the deployment's delegable ceiling for the
        # resource when the catalog knows it.
        try:
            extend_config = await self._catalog_config(
                await self._active_catalog(), owner_subject=grantor_subject
            )
        except CatalogUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_catalog_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        cfg = extend_config.resource_config(resource_value) if resource_value else None
        ceiling = set(_as_list(list(getattr(cfg, "grants", ()) or ()))) if cfg is not None else set()
        if ceiling:
            outside = sorted(set(claim_list) - ceiling)
            if outside:
                return {"ok": False, "error": "delegated_access_grants_not_delegable", "grants": outside}
        key = operation_key
        resource_grants = {res: tuple(vals) for res, vals in record.resource_grants.items()}
        if claim_list:
            if replace:
                # Edit: the submitted set becomes the resource's grants exactly.
                merged = list(claim_list)
            else:
                merged = list(record.resource_grants.get(key, ()))
                for claim in claim_list:
                    if claim not in merged:
                        merged.append(claim)
            resource_grants[key] = tuple(merged)
        resource_operations_out: dict[str, tuple[str, ...]] = {
            res: tuple(vals) for res, vals in record.resource_operations.items()
        }
        if operation_scope_provided:
            requested_operations = list(operation_update.get(key, ()))
            if replace:
                selected_operations = requested_operations
            else:
                selected_operations = list(resource_operations_out.get(key, ()))
                for operation in requested_operations:
                    if operation not in selected_operations:
                        selected_operations.append(operation)
            resource_operations_out[key] = tuple(selected_operations)
        # Account binding edit, same merge/replace semantics per provider AND
        # per account.
        account_scope_out: dict[str, dict[str, tuple[str, ...]]] = {
            provider: {account_id: tuple(cl) for account_id, cl in accounts.items()}
            for provider, accounts in record.account_scope.items()
        }
        if replace and account_scope_provided:
            # The submitted map is the full desired binding. {} intentionally
            # clears every account; omission preserves the existing binding.
            account_scope_out = {
                provider: {account_id: tuple(cl) for account_id, cl in accounts.items()}
                for provider, accounts in scope_update.items()
            }
        elif account_scope_provided:
            for provider, accounts in scope_update.items():
                target = dict(account_scope_out.get(provider, {}))
                for account_id, cl in accounts.items():
                    current = list(target.get(account_id, ()))
                    for claim in cl:
                        if claim not in current:
                            current.append(claim)
                    target[account_id] = tuple(current)
                account_scope_out[provider] = target
        # A DCR client registers one fixed name (every Claude connector arrives
        # as "Claude"), so the user may rename the card to tell connections
        # apart. Empty/absent label leaves the current one untouched.
        try:
            active = await self._active_catalog()
        except CatalogUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_catalog_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        resolved = await self._resolve_card_authority(
            user=user,
            existing=record,
            active=active,
            resource_grants={res: list(vals) for res, vals in resource_grants.items()},
            resource_operations={
                res: list(vals) for res, vals in resource_operations_out.items()
            },
            operations=(),
            named_service_operations=named_service_operations,
            account_scope={
                provider: {account_id: list(cl) for account_id, cl in accounts.items()}
                for provider, accounts in account_scope_out.items()
            },
            properties=record.properties,
        )
        if resolved.error is not None:
            return resolved.error
        if resolved.revoke:
            revoked = await self.revoke_access(
                user,
                access_id=selected_access_id,
            )
            if not revoked.get("ok"):
                return revoked
            return {
                "ok": True,
                "revoked": True,
                "access_id": selected_access_id,
                "pruned": resolved.reconciled.to_public_dict(),
            }
        resource_grants = {
            res: tuple(vals) for res, vals in resolved.resource_grants.items()
        }
        account_scope_out = {
            provider: {account_id: tuple(cl) for account_id, cl in accounts.items()}
            for provider, accounts in resolved.account_scope.items()
        }
        new_label = _clean(label)
        extend_config = await self._catalog_config(active, owner_subject=grantor_subject)
        updated = replace_fields(
            record,
            resource_acceptance=next_resource_acceptance(
                resources=resource_grants,
                row_for=lambda resource: self._configured_resource(resource, config=extend_config),
                catalog_version=_clean(getattr(active, "version", "")),
                selected_operations=resolved.resource_operations,
                previous=record.resource_acceptance,
            ),
            resource_grants=resource_grants,
            resource_operations={
                res: tuple(vals)
                for res, vals in resolved.resource_operations.items()
            },
            account_scope=account_scope_out,
            named_service_operations=resolved.named_service_operations,
            named_services=copy.deepcopy(resolved.named_services),
            operations=tuple(resolved.operations),
            properties=resolved.properties,
            catalog_version=_clean(getattr(active, "version", "")),
            label=new_label or record.label,
            card_revision=record.card_revision + 1,
        )
        try:
            await self._persist_record(updated, expected_revision=record.card_revision)
        except CardServingUnavailable as exc:
            return _serving_state_unavailable(exc)
        except (CardUnavailable, CardConflict, CardCommitFailed) as exc:
            return {
                "ok": False,
                "error": "delegated_card_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
            }
        await self.notify_change(
            grantor_subject,
            action="edited" if replace else "extended",
            access=updated.to_public_dict(),
        )
        return {
            "ok": True,
            "access_id": selected_access_id,
            "access": updated.to_public_dict(),
            "resource_grants": {res: list(vals) for res, vals in resource_grants.items()},
            "account_scope": {
                p: {a: list(cl) for a, cl in accounts.items()} for p, accounts in account_scope_out.items()
            },
        }

    async def renew_access(
        self,
        user: Mapping[str, Any],
        *,
        access_id: str,
        ttl_seconds: Any = None,
        mode: Any = "reissue",
    ) -> dict[str, Any]:
        """Renew a card's credential, one of two ways.

        ``mode="prolong"`` keeps the credential a connected app already holds
        and extends its life: the card's expiry, the app's refresh token and
        its current access binding. Nothing on the client changes, which is
        the point for a client whose token is buried in its own configuration.
        It works only while the refresh token still exists; an ended one
        answers ``delegated_access_credential_expired`` (reconnect from the
        client). A manual or agent bearer carries its own end date inside the
        token, so it is never prolonged: ``delegated_access_prolong_unsupported``.

        ``mode="reissue"`` issues a fresh credential on an existing manual
        automation card, expired or live.

        Why: the grants, operation selections, account bindings and policies on
        a card are the grantor's work; the token is only the key. When the key
        expires, the work must not have to be redone. Renewal keeps the card,
        its ``access_id`` and everything it holds, mints a new bearer with the
        same authority for ``ttl_seconds`` (default: the card's previous
        lifetime), retires the previous bearer, and returns the new token once,
        as creation does. It works on an expired card as well as on a live
        one. Only manual cards renew here: a connected app renews by
        reconnecting from the client, a hosted agent by being granted again
        from the chat; both keep their card.
        """
        grantor_subject = _subject_from_user(user)
        if not grantor_subject:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}
        refusal = _delegate_mutation_refusal(user)
        if refusal is not None:
            return refusal
        access_id_value = _clean(access_id)
        if not access_id_value:
            return {"ok": False, "error": "delegated_access_id_required"}
        try:
            loaded = await self._load_record_any_state(
                access_id_value, grantor_subject=grantor_subject
            )
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_cards_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        if loaded is None:
            return {"ok": False, "error": "delegated_access_not_found", "status": 404}
        record, state = loaded
        if record.grantor_subject != grantor_subject:
            return {"ok": False, "error": "delegated_access_cross_user_access_denied"}
        if state != CARD_STATE_ACTIVE:
            return {"ok": False, "error": "delegated_access_revoked", "status": 409}
        mode_value = (_clean(mode) or "reissue").lower()
        if mode_value not in ("reissue", "prolong"):
            return {"ok": False, "error": "invalid_renew_mode", "mode": mode_value}
        if mode_value == "prolong":
            return await self._prolong_access(user, record=record, ttl_seconds=ttl_seconds)
        if record.source != ACCESS_SOURCE_MANUAL:
            return {
                "ok": False,
                "error": "delegated_access_renew_unsupported",
                "source": record.source,
                "message": (
                    "A connected app renews by reconnecting from the client."
                    if record.source == ACCESS_SOURCE_OAUTH
                    else "A hosted agent renews when it is granted again from the chat."
                ),
            }
        now = int(time.time())
        previous_lifetime = (
            int(record.expires_at) - int(record.created_at)
            if record.expires_at and record.created_at and record.expires_at > record.created_at
            else 0
        )
        requested = ttl_seconds if ttl_seconds not in (None, "", 0, "0") else previous_lifetime
        ttl = _bounded_ttl(requested or None)
        committed_revision = await self._committed_revision(
            access_id_value, grantor_subject=grantor_subject
        )
        grants = list(dict.fromkeys(
            grant
            for held in record.resource_grants.values()
            for grant in held
            if _clean(grant)
        ))
        minted = await self._mint_card_credential(
            user,
            grantor_subject=grantor_subject,
            client_id=record.client_id,
            access_id=access_id_value,
            grants=grants,
            operations=list(record.operations),
            resource_grants=record.resource_grants,
            resource_operations=record.resource_operations,
            account_scope=record.account_scope,
            identity_scope=record.identity_scope,
            named_services=record.named_services,
            ttl=ttl,
            now=now,
        )
        access_token = _clean(minted.get("access_token"))
        expires_in = int(minted.get("expires_in") or ttl)
        provenance = dict(record.provenance or {})
        provenance["renewals"] = int(provenance.get("renewals") or 0) + 1
        provenance["renewed_at"] = now
        renewed = dataclasses.replace(
            record,
            card_revision=committed_revision + 1,
            session_id=_clean(minted.get("session_id")),
            expires_at=now + expires_in,
            last_issued_at=now,
            last_four=access_token[-4:] if access_token else "",
            # A manual automation keeps its token client-side only.
            access_token="",
            provenance=provenance,
        )
        try:
            await self._persist_record(renewed, expected_revision=committed_revision)
        except CardServingUnavailable as exc:
            return _serving_state_unavailable(exc)
        except (CardUnavailable, CardConflict, CardCommitFailed) as exc:
            return {
                "ok": False,
                "error": "delegated_card_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
            }
        # One live token per manual card: the previous session ends now, so a
        # renewal before expiry is also a rotation. Best effort; the old
        # bearer's binding runs out with its own TTL regardless.
        if record.session_id and record.session_id != renewed.session_id:
            try:
                authority = self._authority
                if authority is None and self._authority_factory is not None:
                    authority = self._authority_factory(
                        tenant=self._tenant, project=self._project
                    )
                if authority is not None:
                    await authority.logout(session_id=record.session_id)
            except Exception:
                _LOGGER.warning(
                    "[connection-hub.delegated-access] previous session logout failed card=%s",
                    access_id_value,
                    exc_info=True,
                )
        await self.notify_change(
            grantor_subject, action="renewed", access=renewed.to_public_dict()
        )
        return {
            "ok": True,
            "access": renewed.to_public_dict(),
            "access_token": access_token,
            "authorization_header": f"Bearer {access_token}" if access_token else "",
        }

    async def _prolong_access(
        self,
        user: Mapping[str, Any],
        *,
        record: AutomationAccessRecord,
        ttl_seconds: Any,
    ) -> dict[str, Any]:
        """Extend the life of the credential the client already holds."""
        now = int(time.time())
        previous_lifetime = (
            int(record.expires_at) - int(record.created_at)
            if record.expires_at and record.created_at and record.expires_at > record.created_at
            else 0
        )
        requested = ttl_seconds if ttl_seconds not in (None, "", 0, "0") else previous_lifetime
        ttl = _bounded_ttl(requested or None)
        new_expires_at = now + ttl
        store = self._store

        def expired(way_back: str) -> dict[str, Any]:
            return {
                "ok": False,
                "error": "delegated_access_credential_expired",
                "status": 409,
                "message": f"The credential has already ended, so it cannot be prolonged. {way_back} The card and everything on it are kept.",
            }

        if record.source != ACCESS_SOURCE_OAUTH:
            # A bearer minted here carries its own end date inside the token,
            # so extending anything server-side would not extend it. A manual
            # token is reissued; an agent's renews itself on the next grant.
            return {
                "ok": False,
                "error": "delegated_access_prolong_unsupported",
                "source": record.source,
                "status": 409,
                "message": (
                    "A manual token carries its own end date and cannot be prolonged. Reissue it; the card keeps everything."
                    if record.source == ACCESS_SOURCE_MANUAL
                    else "An agent's credential renews itself the next time the agent is granted from the chat."
                ),
            }
        extend_refresh = getattr(store, "extend_refresh_token", None)
        if not record.refresh_token or extend_refresh is None:
            return expired("Reconnect from the client.")
        if not await extend_refresh(record.refresh_token, ttl):
            return expired("Reconnect from the client.")
        if record.access_token:
            extend_grant = getattr(store, "extend_access_grant", None)
            if extend_grant is not None:
                await extend_grant(record.access_token, ttl)

        committed_revision = await self._committed_revision(
            record.access_id, grantor_subject=record.grantor_subject
        )
        provenance = dict(record.provenance or {})
        provenance["prolongations"] = int(provenance.get("prolongations") or 0) + 1
        provenance["prolonged_at"] = now
        prolonged = dataclasses.replace(
            record,
            card_revision=committed_revision + 1,
            expires_at=new_expires_at,
            provenance=provenance,
        )
        try:
            await self._persist_record(prolonged, expected_revision=committed_revision)
        except CardServingUnavailable as exc:
            return _serving_state_unavailable(exc)
        except (CardUnavailable, CardConflict, CardCommitFailed) as exc:
            return {
                "ok": False,
                "error": "delegated_card_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
            }
        await self.notify_change(
            record.grantor_subject, action="renewed", access=prolonged.to_public_dict()
        )
        return {"ok": True, "mode": "prolong", "access": prolonged.to_public_dict()}

    async def revoke_access(self, user: Mapping[str, Any], *, access_id: str) -> dict[str, Any]:
        grantor_subject = _subject_from_user(user)
        if not grantor_subject:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}
        refusal = _delegate_mutation_refusal(user)
        if refusal is not None:
            return refusal
        access_id_value = _clean(access_id)
        if not access_id_value:
            return {"ok": False, "error": "delegated_access_id_required"}
        try:
            # An expired card is still listed for its owner, so it must stay
            # revocable; only an already revoked card has nothing left to end.
            loaded = await self._load_record_any_state(
                access_id_value, grantor_subject=grantor_subject
            )
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_cards_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        if loaded is None or loaded[1] != CARD_STATE_ACTIVE:
            return {"ok": True, "removed": False}
        record = loaded[0]
        if record.grantor_subject != grantor_subject:
            return {"ok": False, "error": "delegated_access_cross_user_access_denied"}
        # The revoked revision commits before any credential cleanup, so a
        # failure below cannot leave the card usable.
        serving_error: CardServingUnavailable | None = None
        try:
            await self._forget_record(record)
        except CardServingUnavailable as exc:
            # Durable revocation already won. Continue invalidating the
            # source-specific credential so a stale serving projection cannot
            # preserve access while Redis is being reconstructed.
            serving_error = exc
        except (CardUnavailable, CardConflict, CardCommitFailed) as exc:
            return {
                "ok": False,
                "error": "delegated_card_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
            }
        removed_session = False
        if record.session_id:
            authority = self._authority
            if authority is None:
                if self._authority_factory is None:
                    raise RuntimeError("session authority is not configured")
                authority = self._authority_factory(
                    tenant=self._tenant,
                    project=self._project,
                )
            removed_session = bool(await authority.logout(session_id=record.session_id))
        # OAuth-flow grants: kill the refresh token (no new access tokens) and
        # the current access-grant binding (managed guards reject the bearer
        # immediately).
        refresh_revoked = False
        if record.refresh_token:
            refresh_revoked = bool(await self._store.revoke_refresh_token(record.refresh_token))
        if record.access_token:
            await self._store.revoke_access_grant(record.access_token)
        await self.notify_change(grantor_subject, action="revoked", access_id=access_id_value)
        if serving_error is not None:
            return _serving_state_unavailable(serving_error)
        return {
            "ok": True,
            "removed": True,
            "session_removed": removed_session,
            "refresh_token_revoked": refresh_revoked,
        }

    # ------------------------- live-session delivery -------------------------

    async def register_live_session(
        self, grantor_subject: str, session_id: str, expires_at: int | float | None = None
    ) -> None:
        await register_delegated_access_live_session(
            self._redis,
            tenant=self._tenant,
            project=self._project,
            grantor_subject=grantor_subject,
            session_id=session_id,
            expires_at=expires_at,
        )

    async def notify_change(
        self,
        grantor_subject: str,
        *,
        action: str,
        access: Mapping[str, Any] | None = None,
        access_id: str = "",
    ) -> None:
        await notify_delegated_access_changed(
            self._redis,
            tenant=self._tenant,
            project=self._project,
            grantor_subject=grantor_subject,
            action=action,
            access=access,
            access_id=access_id,
            relay_factory=self._relay_factory,
        )


__all__ = [
    "ALL_RESOURCES_RESOURCE",
    "AUTOMATION_ACCESS_DEFAULT_TTL_SECONDS",
    "AUTOMATION_ACCESS_SCHEMA",
    "DELEGATED_ACCESS_CHANGED_EVENT",
    "AutomationAccessRecord",
    "AutomationAccessService",
    "AuthorityFactory",
    "DELEGATED_SESSION_MAX_TTL_SECONDS",
    "NamedServiceDiscoveryFactory",
    "RelayFactory",
    "ResourceOverlayProvider",
    "notify_delegated_access_changed",
    "register_delegated_access_live_session",
]
