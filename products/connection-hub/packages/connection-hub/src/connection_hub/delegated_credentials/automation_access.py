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

import contextvars
import dataclasses
import asyncio

import copy
from fnmatch import fnmatchcase
import hashlib
import json

import secrets
import time
from dataclasses import dataclass, field
from dataclasses import replace as replace_fields
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable, Mapping, Sequence

from connection_hub.concurrency import bounded_gather
from connection_hub.delegated_credentials.managed_grants import (
    MANAGED_GRANT_NOT_EDITABLE,
    MANAGED_GRANTS_UNKNOWN,
    ManagedGrantsUnknown,
    managed_grant_changes,
    managed_grant_refusal,
    managed_operation_changes,
    managed_operations,
)
from connection_hub.delegated_credentials.issuer_gate import (
    IssuerDecision, IssuerRegistry, IssuerRequest, IssuerWriteRefused,
    change_digest, issuer_write_refusal,
)
from connection_hub.delegated_credentials.caller_writer_gate import (
    CallerWrite, CallerWriteRefused, binding_of, caller_write_outcome, caller_writer_before_commit,
    control_named_services_entry, reset_candidate,
)
from connection_hub.authority_inventory import (
    AuthorityGrantInventory,
    PlatformAuthorityInventoryProvider,
    platform_identity_from_user,
    selected_delegation_edge,
)
from connection_hub.authority_projection import (
    authority_has_platform_privilege,
)
from connection_hub.delegated_credentials.authority_config import (
    AUTHORITY_BACKEND_POSTGRESQL,
    AUTHORITY_BACKEND_REDIS_MIGRATION_SOURCE,
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
    GrantStoreUnavailable,
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
    AGENT_DESCRIPTOR_ISSUER_KIND,
    align_resident_selection_to_card_authority,
    preserve_descriptor_acceptance,
    resident_selection_properties,
)
from connection_hub.delegated_credentials.agent_capability_policy import (
    AGENT_CAPABILITY_AUTHORITY_PROPERTY,
    AGENT_CAPABILITY_METADATA_PROPERTY,
    AGENT_CAPABILITY_SELECTION_PROPERTY,
    AgentCapabilityPolicy,
    AgentCapabilityPolicyError,
    descriptor_control,
)
from connection_hub.delegated_credentials.resource_operations import (
    normalize_resource_grants,
    normalize_resource_operations,
    operation_union,
    project_legacy_operations,
    resolve_declared_resource,
    resolve_declared_resource_keys,
)
from connection_hub.delegated_credentials.cards.model import (
    CARD_STATE_ACTIVE,
    CARD_STATE_REVOKED,
    CREDENTIALLESS_CARD_SOURCE,
    PROJECT_PERSON_SELECTION_SOURCE,
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
from connection_hub.delegated_credentials.controls.project_person_composition import (
    compose_with_project_held_control,
    project_held_control,
)
from connection_hub.delegated_credentials.controls.effective import (
    ControlCardMismatch,
    effective_card_authority,
)
from connection_hub.delegated_credentials.controls.hierarchy import compose_control_hierarchy
from connection_hub.delegated_credentials.conversation_target_policy import (
    conversation_targets,
)
from connection_hub.delegated_credentials.controls.model import (
    ControlCardError,
    control_card_id_for_issuer,
    new_credentialless_card,
)
from connection_hub.delegated_credentials.card_lifecycle_plan import build_application_control
from connection_hub.delegated_credentials.controls.snapshot import (
    CONTROL_SNAPSHOT_PROPERTY,
    control_snapshot_is_exact,
    control_snapshot_refusal,
    fail_closed_control_snapshot,
    materialize_control_snapshot,
    reviewed_control_snapshot_properties,
)
from connection_hub.delegated_credentials.project_authorization import (
    ProjectAuthorizationPort,
    ViewerAuthority,
)
from connection_hub.delegated_credentials.project_identity_authorization import (
    ProjectOperationRequest,
)
from connection_hub.delegated_credentials.project_identity_lifecycle import (
    ProjectIdentityLifecycleError,
)
from connection_hub.delegated_credentials.project_invitation_access import (
    ProjectInvitationControlLifecycle,
)
from connection_hub.delegated_credentials.project_invitation_binding import (
    ProjectInvitationBindingResolver,
)
from connection_hub.delegated_credentials.project_person_access import (
    ProjectPersonControlLifecycle,
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
    CardStorageError,
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


def admin_only_closed(resource: Any, delegable: Iterable[str], *, platform_admin: bool) -> bool:
    """Whether an admin-only resource stays closed to this approver.

    The operator's rule (2026-09-26): "that must be for all, with the choice of
    roles from those that are available for that account." An admin-only row
    (the "All platform and application APIs" row, resource ``*``) is offered
    to every approver who may delegate at least one of its grants, and only
    those grants are offered; it stays closed only when none is theirs. A
    Card still never exceeds what its approver holds: each selected grant is
    checked against the approver's delegable set.
    """

    if not bool(getattr(resource, "admin_only", False)) or platform_admin:
        return False
    held = {str(grant) for grant in delegable}
    return not any(str(grant) in held for grant in (getattr(resource, "grants", ()) or ()))


def role_closed(resource: Any, delegable: Iterable[str]) -> bool:
    """Whether a catalog row is closed to this person by their role (W379).

    The operator's rule (2026-09-28 ~20:20Z): "it must show all the
    resources that are allowed to be shown according to the role of logged
    in user". The descriptor's grants on the row decide, and nothing else: a
    row is offered to a person who may delegate at least one of its grants.
    A row that declares no grants costs nothing and stays offered.
    """

    grants = [str(grant) for grant in (getattr(resource, "grants", ()) or ()) if str(grant)]
    if not grants:
        return False
    held = {str(grant) for grant in delegable}
    return not any(grant in held for grant in grants)


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
# The Card lever (W313 step 2) records who applied which profile on the
# revision it wrote, like the project-person control audit.
AUTHORIZATION_PROFILE_AUDIT_PROVENANCE = "authorization_profile_audit"
AUTHORIZATION_PROFILE_AUDIT_SCHEMA = "connection_hub.authorization_profile.audit.v1"
OPERATIONS_ADDED_AUDIT_PROVENANCE = "operations_added_audit"
OPERATIONS_ADDED_AUDIT_SCHEMA = "connection_hub.operations_added.audit.v1"
# A per-agent delegated grant: the consenting user grants a hosted agent
# (a "Delegated By KDCube" entity, keyed by a deterministic client_id) access to
# a resource. Unlike a MANUAL automation (which mints its own random client), the
# client_id is caller-supplied and stable, so re-consent updates one record.
ACCESS_SOURCE_AGENT = "agent"
ACCESS_SOURCE_CONTROL = CREDENTIALLESS_CARD_SOURCE
ACCESS_SOURCE_PROJECT_PERSON = PROJECT_PERSON_SELECTION_SOURCE


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


def _held_binding_refusal(
    record: "AutomationAccessRecord", *, caller_is_project: bool
) -> dict[str, Any] | None:
    """A project's Control Card on another person's Card stays until the project removes it.

    W260 (review on app-ecosystem#192): a binding that records a holder other
    than the Card's own grantor was attached through the project, with the
    project host deciding. Only the project path detaches or replaces it; the
    Card's owner cannot drop the project's narrowing on the plain path.
    """

    binding = record.control_card
    holder = str(getattr(binding, "holder_subject", "") or "") if binding is not None else ""
    if caller_is_project or not holder or holder == record.grantor_subject:
        return None
    return {
        "ok": False,
        "error": "control_card_held_by_project",
        "message": "This Card is narrowed by a project's Control Card; an admin of that project removes it through the project.",
        "status": 409,
        "control_card": binding.to_dict(),
    }



def _profile_selection(profile: str, *, config: Any) -> dict[str, Any] | None:
    """The selection first consent proposes for ``profile`` (W377).

    Every catalog resource that declares the profile contributes, as
    ``apply_authorization_profile`` computes it for one Card resource. None
    when no resource declares it.
    """

    from connection_hub.delegated_credentials.oauth.consent import (
        requested_card_selection,
    )

    resource_grants: dict[str, list[str]] = {}
    resource_operations: dict[str, list[str]] = {}
    named_service_operations: dict[str, Any] = {}
    for row in getattr(config, "resources", ()) or ():
        chosen = next(
            (
                item
                for item in (getattr(row, "authorization_profiles", ()) or ())
                if _clean(getattr(item, "name", "")).lower() == profile
            ),
            None,
        )
        if chosen is None:
            continue
        selection = requested_card_selection(
            [_clean(getattr(chosen, "scope", ""))],
            config=config,
            resource=row.resource,
        )
        for key, values in dict(selection.get("resource_grants") or {}).items():
            resource_grants[key] = list(values)
            resource_operations[key] = list(
                dict(selection.get("resource_operations") or {}).get(key) or ()
            )
        named_service_operations.update(
            dict(selection.get("named_service_operations") or {})
        )
    if not resource_grants:
        return None
    return {
        "resource_grants": resource_grants,
        "resource_operations": resource_operations,
        "named_service_operations": named_service_operations,
        "account_scope": {},
    }

def _control_card_started_from(record: "AutomationAccessRecord") -> dict[str, Any]:
    """What a Control Card started from: a profile, a seeding Card, or nothing."""

    return dict(
        (record.provenance or {}).get("control_card_initial_selection") or {}
    )


def _control_card_unstarted(record: "AutomationAccessRecord") -> bool:
    """Created with no start and never given a selection since (W377)."""

    return not _control_card_started_from(record) and not any(
        (record.resource_grants or {}).values()
    )

def _record_is_credentialless(record: "AutomationAccessRecord") -> bool:
    """The one material difference between a linked Card and a caller Card."""
    return (
        record.source == ACCESS_SOURCE_CONTROL
        and not record.delegate_subject
        and record.expires_at == 0
    )


def _descriptor_serializable_resource_option(option: Mapping[str, Any]) -> bool:
    """Whether a catalog row can round-trip through an app descriptor."""

    resource = _clean(option.get("resource"))
    if (
        not resource
        or resource == APPLICATION_API_RESOURCE
        or _clean(option.get("kind")) != RESOURCE_KIND_CATALOG
    ):
        return False
    named_services = option.get("named_services")
    if isinstance(named_services, (list, tuple)) and named_services:
        return True
    lowered = resource.lower().rstrip("/")
    return "/mcp/" in lowered or lowered.endswith("/mcp") or ":mcp:" in lowered


def _descriptor_control_resource_options(
    properties: Mapping[str, Any] | None,
    options: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Catalog rows an administrator can persist on a descriptor Control Card."""

    rows = [dict(option) for option in options]
    try:
        control = descriptor_control(properties)
    except AgentCapabilityPolicyError:
        return []
    if control is None:
        return rows
    return [row for row in rows if _descriptor_serializable_resource_option(row)]


def _resource_matches_any_pattern(resource: str, patterns: Iterable[Any]) -> bool:
    return any(
        fnmatchcase(resource, pattern)
        for value in patterns
        for pattern in (_clean(value),)
        if pattern
    )


def _descriptor_agent_resource_options(
    control_authority: Mapping[str, Any],
    options: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Exact Control resources and dynamic family rows offered to its Agent Card."""

    rows = [dict(option) for option in options]
    properties = control_authority.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    try:
        control = descriptor_control(properties)
    except AgentCapabilityPolicyError:
        return [], []
    if control is None:
        return rows, []

    raw_grants = control_authority.get("resource_grants")
    exact_resources = {
        _clean(resource)
        for resource in (raw_grants if isinstance(raw_grants, Mapping) else {})
        if _clean(resource)
    }
    try:
        authority = AgentCapabilityPolicy.from_property(
            properties.get(AGENT_CAPABILITY_AUTHORITY_PROPERTY)
        )
    except AgentCapabilityPolicyError:
        return [], []
    family_ids = set(authority.capabilities.get("resource_families", ()))
    metadata = properties.get(AGENT_CAPABILITY_METADATA_PROPERTY)
    entries = metadata.get("entries") if isinstance(metadata, Mapping) else None
    raw_families = entries.get("resource_families") if isinstance(entries, Mapping) else None
    families = {
        _clean(family_id): dict(raw)
        for family_id, raw in (
            raw_families.items() if isinstance(raw_families, Mapping) else ()
        )
        if _clean(family_id) in family_ids and isinstance(raw, Mapping)
    }
    family_patterns = [
        pattern
        for family in families.values()
        for pattern in (
            family.get("resource_patterns")
            if isinstance(family.get("resource_patterns"), (list, tuple))
            else ()
        )
    ]

    def exact_control_row(row: Mapping[str, Any]) -> bool:
        resource = _clean(row.get("resource"))
        return bool(resource and resource != APPLICATION_API_RESOURCE) and (
            resource in exact_resources
            or any(fnmatchcase(resource, grant) for grant in exact_resources)
        )

    family_resources = {
        _clean(row.get("resource"))
        for row in rows
        if _resource_matches_any_pattern(_clean(row.get("resource")), family_patterns)
    }
    remote_mcp_family = _resource_matches_any_pattern(
        "urn:connection-hub:remote-mcp:descriptor-family-probe",
        family_patterns,
    )
    roots: list[str] = []
    for row in rows:
        if not row.get("resource_selection"):
            continue
        selectable = {
            _clean(resource)
            for resource in row.get("selectable_resources", ())
            if _clean(resource)
        }
        if selectable & family_resources or (
            remote_mcp_family and "external_mcp:use" in row.get("grants", ())
        ):
            roots.append(_clean(row.get("resource")))

    allowed = exact_resources | family_resources | set(roots)
    return [
        row
        for row in rows
        if (
            _clean(row.get("resource")) != APPLICATION_API_RESOURCE
            and (
                _clean(row.get("resource")) in allowed
                or exact_control_row(row)
            )
        )
    ], roots


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
            ACCESS_SOURCE_PROJECT_PERSON: "platform_session",
        }.get(self.source, self.source or "issued_token")
        public["credential_reach"] = (
            "multi_resource"
            if self.source
            in {
                ACCESS_SOURCE_AGENT,
                ACCESS_SOURCE_MANUAL,
                ACCESS_SOURCE_CONTROL,
                ACCESS_SOURCE_PROJECT_PERSON,
            }
            or client_uses_full_card_catalog(self.client_metadata)
            else "single_resource"
        )
        return public


def _authority_snapshot(name: str, value: Any) -> Any:
    """W603: one authority field in comparable form; a missing or malformed value is never equal to any."""
    if name == "operations":
        if not isinstance(value, (list, tuple)) or any(type(item) is not str or not item for item in value):
            return _MALFORMED
        return tuple(sorted(set(value)))
    if not isinstance(value, Mapping) or any(
            not isinstance(items, (list, tuple)) or any(type(item) is not str or not item for item in items)
            for items in value.values()):
        return _MALFORMED
    try:
        normalized = (normalize_resource_grants if name == "resource_grants" else normalize_resource_operations)(value)
    except (TypeError, ValueError):
        return _MALFORMED
    if len(normalized) != len(value):
        return _MALFORMED  # an empty or duplicate key after cleaning is malformed, not dropped
    return {key: tuple(sorted(set(items))) for key, items in normalized.items()}


_MALFORMED = object()


# P0 (10 Oct 2026): the bundle-load legacy binding repair's switch. Only
# legacy_binding_repair.repair_legacy_project_bindings sets it, for its own run; no operation, request
# or descriptor can. While it is set, W578 makes exactly two exceptions, each for the binding a NEW
# Card gets at creation and nothing else (see _legacy_unbound_c_to_its_root_p and
# _refuse_bound_direct_write).
LEGACY_BINDING_REPAIR: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "connection_hub_legacy_binding_repair", default=False)


def _legacy_unbound_c_to_its_root_p(record: Any, control: Any) -> bool:
    """W578 exception 1: attach an UNBOUND person Control C to its own project's root Control P."""
    if not LEGACY_BINDING_REPAIR.get() or record.control_card is not None or control.control_card is not None:
        return False
    if _clean(record.issuer_kind) != "project" or _clean(control.issuer_kind) != "application":
        return False
    # A person Control's issuer_ref IS its project (ProjectPersonControlIdentity.from_authority requires it;
    # the repair validated that identity, and bind_project_control checked P against it, before attaching).
    project_ref = _clean(record.issuer_ref)
    return bool(project_ref) and _clean(control.issuer_ref) == project_ref


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


def card_handles_unchanged(stored: CardCredentialHandles, record: AutomationAccessRecord) -> bool:
    """Whether persisting ``record`` leaves the Card's stored credential handles as they are.

    The comparison is made in the handle store's own terms. A store that holds
    a bearer for this Card (a resident agent Card, or the pre-cutover Redis
    records) compares the full material. The PostgreSQL store keeps no token
    for an OAuth Card: its access and refresh tokens live in the OAuth
    authority store, which a refresh rotates after checking the presented
    refresh token and the live Card. For such a Card the Card's handles are its
    access id and session, so a rotation that keeps them changes no credential
    the Card holds.
    """
    if stored == card_handles_from_record(record):
        return True
    if stored.access_token or stored.refresh_token:
        return False
    return (stored.access_id, stored.session_id) == (record.access_id, record.session_id)


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


def _newly_selected_operation_grants(
    existing: "AutomationAccessRecord",
    *,
    resource_operations: Mapping[str, Iterable[str]],
    named_service_operations: NamedServiceSelection,
    resource_pairs: Iterable[tuple[str, Any]],
) -> set[str] | None:
    """Grants carried by operations a save selects that the stored Card did not.

    ``None`` when the growth cannot be told apart operation by operation (a
    selection widened to every operation, a legacy selection, an operation the
    catalog does not map to a grant): the caller then treats every grant the
    save carries as added.
    """

    from connection_hub.delegated_credentials.agent_capability_sync import (
        _named_operation_grants,
    )

    added: set[str] = set()
    configs = {resource: cfg for resource, cfg in resource_pairs}
    for resource, cfg in configs.items():
        now = set(_as_list(list(resource_operations.get(resource) or ())))
        before = set(_as_list(list(dict(existing.resource_operations or {}).get(resource) or ())))
        carried = resource in dict(existing.resource_grants or {})
        if not now:
            if carried and before:
                return None  # an exact selection widened to every tool
            if not carried:
                added.update(getattr(cfg, "grants", ()) or ())
            continue
        if carried and not before:
            continue  # every tool before, an exact subset now
        tools = {tool.name: tuple(tool.grants) for tool in getattr(cfg, "tools", ()) or ()}
        for operation in now - before:
            if operation not in tools:
                return None
            added.update(tools[operation])
    prior = existing.named_service_operations
    kind = named_service_operations.kind
    if kind == "none" or (kind == "exact" and prior.kind == "all"):
        return added
    if kind == "all":
        return added if prior.kind == "all" else None
    if kind != "exact" or prior.kind == "unknown":
        return None
    for resource, namespaces in named_service_operations.operations.items():
        cfg = configs.get(resource)
        named = getattr(cfg, "named_services", None) if cfg is not None else None
        if not isinstance(named, Mapping):
            return None
        before_namespaces = (
            dict(prior.operations).get(resource, {}) if prior.kind == "exact" else {}
        )
        for namespace, operations in namespaces.items():
            grants_by_operation = _named_operation_grants(named, namespace)
            for operation in set(operations) - set(before_namespaces.get(namespace, ())):
                if operation not in grants_by_operation:
                    return None
                added.update(grants_by_operation[operation])
    return added


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
        authority_backend: str = AUTHORITY_BACKEND_REDIS_MIGRATION_SOURCE,
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
        project_authorization_port: ProjectAuthorizationPort | None = None,
        project_invitation_binding_resolver: (
            ProjectInvitationBindingResolver | None
        ) = None,
        issuer_registry: IssuerRegistry | None = None,
        issuer_actor_subject: str = "",
    ) -> None:
        self._redis = redis
        self._tenant = _clean(tenant)
        self._project = _clean(project)
        self._config = config
        selected_backend = _clean(authority_backend).lower()
        if grant_store is None and selected_backend == AUTHORITY_BACKEND_POSTGRESQL:
            raise GrantStoreUnavailable(
                "selected_authority.automation_grant_store_not_bound"
            )
        self._store = grant_store or GrantStore(
            redis,
            self._tenant,
            self._project,
        )
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
        # Ordinary owner-managed application/descriptor Controls retain their
        # local rules. Configured adapters override that exception. Every other
        # opaque issuer requires an adapter, even when its provider is missing.
        self._issuers = issuer_registry or IssuerRegistry(
            owner_managed_kinds=("application", AGENT_DESCRIPTOR_ISSUER_KIND)
        )
        self._issuer_actor_subject = _clean(issuer_actor_subject)
        self._issuer_actor_subject_bound = bool(self._issuer_actor_subject)
        self._issuer_snapshots = None
        self._issuer_snapshot_host = ()
        # Optional: lets the resident-profile migration carry once/always
        # policies to the stable card and the read model report them. Without
        # it, migration refuses to fold a record whose policies it cannot see.
        self._invocation_policies = invocation_policy_service
        self._project_authorization_port = project_authorization_port
        self._project_invitation_binding_resolver = project_invitation_binding_resolver
        self._bind_project_lifecycles()

    def _bind_project_lifecycles(self) -> None:
        self._project_person_controls = ProjectPersonControlLifecycle(
            host=self,
            authorization_port=self._project_authorization_port,
            authority_from_record=card_authority_from_record,
            record_from_authority=record_from_card,
        )
        self._project_invitation_controls = ProjectInvitationControlLifecycle(
            host=self,
            authorization_port=self._project_authorization_port,
            binding_resolver=self._project_invitation_binding_resolver,
            authority_from_record=card_authority_from_record,
            record_from_authority=record_from_card,
        )

    def bind_issuer_registry(self, registry: IssuerRegistry, *, actor_subject: str) -> None:
        """Bind trusted request composition without requiring host wrapper kwargs.

        Hosting adapters may inherit this method while retaining their existing
        constructor. The app calls it before exposing the request-local service.
        Nothing in the browser update/revoke payload can select these ports.
        """
        if not isinstance(registry, IssuerRegistry):
            raise ValueError("issuer_registry_invalid")
        self._issuers = registry
        self._issuer_actor_subject = _clean(actor_subject)
        # An explicitly bound but unavailable session actor must not fall back
        # to a legacy owner proxy on a managed write.
        self._issuer_actor_subject_bound = True

    def bind_issuer_read_registry(self, registry: Any, *, actor_subject: str, actor_classification: str,
                                  tenant: str, project: str) -> None:
        from .issuer_read import IssuerReadRegistry
        if type(registry) is not IssuerReadRegistry:
            raise ValueError("issuer_read_registry_invalid")
        self._issuer_reads = registry
        self._issuer_read_host = (actor_subject, actor_classification, tenant, project)

    def bind_issuer_snapshot_registry(self, registry: Any, *, actor_subject: str,
                                      actor_classification: str, tenant: str, project: str) -> None:
        """Bind the separate full-export capability and actual host context."""
        from .issuer_snapshot import IssuerSnapshotRegistry
        if type(registry) is not IssuerSnapshotRegistry:
            raise ValueError("issuer_snapshot_registry_invalid")
        self._issuer_snapshots = registry
        self._issuer_snapshot_host = (actor_subject, actor_classification, tenant, project)

    def bind_issuer_update_host(self, *, actor_subject: str, actor_classification: str,
                                tenant: str, project: str, host_is_current: Any) -> None:
        """Bind actual request context and its live, trusted confinement check."""
        self._issuer_update_host = (actor_subject, actor_classification, tenant, project)
        self._issuer_update_host_is_current = host_is_current

    def bind_project_authorization_port(
        self,
        authorization_port: ProjectAuthorizationPort | None,
    ) -> None:
        """Bind the host policy port for this request-scoped service instance."""

        self._project_authorization_port = authorization_port
        self._bind_project_lifecycles()

    def bind_project_invitation_binding_resolver(
        self,
        resolver: ProjectInvitationBindingResolver | None,
    ) -> None:
        """Bind the current request's invitation-to-person evidence resolver."""

        self._project_invitation_binding_resolver = resolver
        self._bind_project_lifecycles()

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
        *,
        actor_subject: str = "",
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
                caller_write=CallerWrite("control_snapshot", actor_subject) if actor_subject else None,
            )
        except CallerWriteRefused as exc:
            if exc.reason == "card_transactions_direct_write_refused":
                # W578: while Card transactions are on, a bound Control's
                # migration is not persisted outside its owner's transaction
                # (the fail-closed boundary could narrow it); it is used in
                # memory, at the CURRENT revision, as below.
                return record_from_card(dataclasses.replace(migrated, card_revision=current.card_revision))
            if exc.reason != "caller_writer_not_enlisted" or actor_subject:
                raise
            # W580/B: a governed Control read on a path with no authenticated
            # actor is not migrated durably here. The migrated snapshot (the
            # historical or fail-closed boundary) is used in memory for this
            # read; the next actor-bearing write persists it under the gate.
            # It keeps the CURRENT revision: nothing new was committed.
            return record_from_card(dataclasses.replace(migrated, card_revision=current.card_revision))
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

    async def _compose_with_control(
        self, record: AutomationAccessRecord
    ) -> tuple[AutomationAccessRecord | None, CardAuthority | None]:
        """The record's Control Card and its effective authority; (None, None) when unresolvable.

        The first result is the current parent-composed immediate Control.
        Exact project-held person edges use their derived holder; all other
        edges retain ordinary owner/holder checks. No read writes My Card.
        Raises ControlCardMismatch or CardUnavailable, never falls open.
        """

        caller = card_authority_from_record(record)

        async def load_control(control_id: str, *, grantor_subject: str) -> CardAuthority | None:
            current = await self._load_record(control_id, grantor_subject=grantor_subject)
            return card_authority_from_record(current) if current is not None else None

        try:
            result = await compose_control_hierarchy(caller, load_control=load_control)
        except ControlCardMismatch as exc:
            if exc.reason == "control_card_unresolvable":
                return None, None
            raise
        return (
            record_from_card(result.effective_control_card)
            if result.effective_control_card is not None else None,
            result.effective_card,
        )

    async def _effective_control_view(
        self,
        record: AutomationAccessRecord,
    ) -> dict[str, Any]:
        """Owner-facing explanation of the authority currently in force."""

        binding = record.control_card
        if binding is None:
            return {"state": "not_controlled"}
        try:
            control, composed = await self._compose_with_control(record)
        except CardUnavailable as exc:
            return {
                "state": "unavailable",
                "reason": exc.reason,
                "fail_closed": True,
                "binding": binding.to_dict(),
            }
        except ControlCardMismatch as exc:
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
        effective = composed
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

    async def _enlisted_gate(
        self, authority: CardAuthority, *, expected_revision: int, caller_write: CallerWrite | None,
        candidate: Mapping[str, Any] | None = None,
    ) -> tuple[Callable[[], Awaitable[None]] | None, Any]:
        """W580/B: the bound-Card gate for a writer that did not bring its own.

        The binding is the stored Card's (or, for a create, the one the
        candidate takes). A Card bound to a registered kind is never written
        by an unnamed writer: it is refused by name, before any effect.
        """
        registry = getattr(self, "_caller_writers", None)
        if registry is None:
            return None, None
        current = None
        if expected_revision > 0:
            loaded = await self._cards().load(authority.access_id, subject_hash=_subject_key(authority.grantor_subject))
            current = loaded[0] if loaded is not None else None
        binding = binding_of(current) if current is not None else ("", "")
        if not binding[0]:
            binding = binding_of(authority)
        if not binding[0] or not registry.is_bound(binding[0]):
            return None, None
        if caller_write is None:
            raise CallerWriteRefused("caller_writer_not_enlisted")
        return await caller_writer_before_commit(
            registry, current, actor_subject=caller_write.actor_subject, action=caller_write.action,
            candidate=dict(candidate) if candidate is not None else authority.to_dict(),
            request_id=caller_write.request_id or secrets.token_urlsafe(18),
            context_ref=caller_write.context_ref, binding=binding,
        )

    async def card_credential_limits(self, access_id: str, *, grantor_subject: str) -> tuple[int | None, int] | None:
        """W585: the live Card's credential limits for the SDK token routes (Infra, 17:35).

        ``(expires_at or None, card_revision)`` of the ACTIVE Card the trusted
        server-side ``registry_access_id`` names, under its grantor; ``None``
        when the Card is absent, not active, or Card storage is unavailable, so
        the caller forwards no cap and keeps its existing refusal for an
        ended Card. Never read from a request; no module state.
        """
        if not access_id or not grantor_subject:
            return None
        try:
            loaded = await self._cards().load(str(access_id), subject_hash=_subject_key(str(grantor_subject)))
        except CardUnavailable:
            return None
        if loaded is None:
            return None
        current = loaded[0]
        if current.state != CARD_STATE_ACTIVE or current.access_id != access_id:
            return None
        return (int(current.expires_at) or None, int(current.card_revision))

    def bind_card_coordinator(self, coordinator: Any, *, intents: Any, decisions: Any,
                              intent_ttl_seconds: int = 60) -> None:
        """W502 ONE protocol: Card edits run through the generic coordinator (W581).

        The composition binds the Coordinator with the Hub's HubCardParticipant
        (built on the SDK DelegatedCardService and its per-Card flock), the
        LocalCardIntentSource and the coordinator's DecisionStore.
        """
        self._card_coordinator = (coordinator, intents, decisions, max(1, int(intent_ttl_seconds)))

    def _managed_direct_write_refused(self) -> dict[str, Any] | None:
        """W578: with Card transactions enabled, a managed project Card write never takes a direct path.

        A project's P, a person's C and their My Card, their bindings and an
        invitation's redemption change together with the project host's own
        membership, so they are written only by a transaction the project host
        coordinates (the Hub takes part through ``card_transaction_participant``).
        Enabled means ``bind_card_coordinator`` was called. Disabled changes nothing.
        """

        if getattr(self, "_card_coordinator", None) is None:
            return None
        return {
            "ok": False,
            "error": "card_transactions_direct_write_refused",
            "message": "This project Card change is made through the project's own transaction.",
            "retryable": False,
            "status": 409,
        }

    def bind_card_credential_handles(self, handles: Any) -> None:
        """W606: the Card handle store whose rows a coordinated edit moves (composition only)."""
        self._card_credential_handles = handles

    async def _handle_binding_effects(
        self, pairs: Sequence[tuple[CardAuthority | None, CardAuthority]],
    ) -> list[dict[str, Any]]:
        """W606: one ``handle_binding`` per edited credential-bearing Card, decided from its own handle row.

        A Card edit never changes the credential a remote party holds (the
        token is a pointer to the live Card); only the serving row's Card
        revision and expiry follow the committed edit, by compare-and-set on
        the row identity pinned here. No row (a credential-less Card, or a
        Redis-held one that binds no revision), a row already not at the base
        revision, a create, or an ending Card carries no effect.
        """
        handles = getattr(self, "_card_credential_handles", None)
        if handles is None or not callable(getattr(handles, "binding_identity", None)):
            return []
        effects = []
        for original, candidate in pairs:
            if original is None or candidate.state != CARD_STATE_ACTIVE:
                continue
            identity = await handles.binding_identity(candidate.access_id)
            if not isinstance(identity, Mapping) or identity.get("from_revision") != original.card_revision:
                continue  # no row, or a row not at the base revision
            effects.append({"kind": "handle_binding", "key": f"handle:{candidate.access_id}", "payload": {
                "access_id": candidate.access_id, "from_identity": identity["from_identity"],
                "from_fingerprint": identity["from_fingerprint"],
                # An agent row's re-wrap envelope is prepared at this fixed instant (0 when there is none).
                "prepared_at": int(time.time()) if identity["from_fingerprint"] else 0,
                "from_revision": identity["from_revision"], "from_expires_at": identity["from_expires_at"],
                "card_revision": candidate.card_revision, "expires_at": candidate.expires_at}})
        return effects

    def bind_managed_card_edit(self, forwarders: Mapping[str, Any]) -> None:
        """W638: per plan scope prefix, the project host that coordinates a managed Card edit.

        Bound with the Card transactions (bundle props, never a request). A
        managed write the direct writers refuse is forwarded to that host's
        transaction instead; without a forwarder it stays refused.
        """
        self._managed_card_edit_forwarders = tuple(sorted(
            (prefix, forwarder) for prefix, forwarder in dict(forwarders).items()
            if isinstance(prefix, str) and prefix and callable(getattr(forwarder, "forward", None))
        ))

    def _managed_card_edit_forwarder(self, project_ref: str) -> Any:
        for prefix, forwarder in getattr(self, "_managed_card_edit_forwarders", ()):
            if isinstance(project_ref, str) and project_ref.startswith(prefix):
                return forwarder
        return None

    async def _with_current_person_control(
        self, user: Mapping[str, Any], result: dict[str, Any], *, project_ref: str, target_subject: str,
        request_id: str,
    ) -> dict[str, Any]:
        """W661 save #2 (operator, 10 Oct: bookkeeping never makes the next edit stale). After a managed
        person-Control save, committed or refused, the answer carries the Card as it is NOW (the same read as
        project_person_control_get, under the caller's own authority), so the editor reloads it: its next save
        echoes the current revision and properties. A failed read leaves the answer as it was."""
        if not isinstance(result, dict) or ("managed_card_edit" not in result and result.get("status") != 409):
            return result
        try:
            current = await self.project_person_control_get(
                user, project_ref=project_ref, target_subject=target_subject, request_id=request_id)
        except Exception:  # noqa: BLE001 - the save's own answer stands; the editor can still reload
            return result
        if isinstance(current, dict) and current.get("ok") is not False and isinstance(current.get("access"), dict):
            return {**result, "access": current["access"]}
        return result

    async def _forward_person_control_edit(
        self, user: Mapping[str, Any], *, project_ref: str, target_subject: str, request_id: str,
        selection: Mapping[str, Any], expected_card_revision: int | None, properties: Any,
        composition_mode: str | None,
    ) -> dict[str, Any] | None:
        """The managed person-Control edit, made by the project's own transaction."""
        from .managed_card_edit_forward import ManagedCardEditError, managed_card_edit_body
        forwarder = self._managed_card_edit_forwarder(project_ref)
        actor_subject = _subject_from_user(user)
        if forwarder is None or not _clean(target_subject) or not actor_subject:
            return None
        if properties is not None:
            # The editor sends the Card's properties back with every Save.
            # They are not part of this edit: only an unchanged copy may travel.
            from .managed_card_edit_forward import managed_card_location
            try:
                access_id, grantor = managed_card_location(
                    "person_control", project_ref=project_ref, ref=_clean(target_subject))
                stored = await self._load_record(access_id, grantor_subject=grantor)
            except ValueError:
                stored = None
            if stored is None or dict(properties) != dict(stored.properties or {}):
                properties = {"changed": True}
            else:
                properties = None
        if properties or composition_mode not in (None, "and"):
            # Changing properties or composition here would not be the change
            # the person saw for this selection.
            return {"ok": False, "error": "managed_card_edit_fields_unsupported", "status": 409,
                    "message": "Composition and Card properties are not changed by this edit. Nothing was saved."}
        if type(expected_card_revision) is not int:
            return {"ok": False, "error": "managed_card_edit_revision_required", "status": 409,
                    "message": "Reload the Card and save again. Nothing was saved."}
        try:
            body = managed_card_edit_body(actor_subject=actor_subject, project_ref=project_ref,
                request_id=request_id, kind="person_control", principal_key="user:" + _clean(target_subject),
                original_revision=expected_card_revision, selection=selection)
            outcome = await forwarder.forward(body)
        except ManagedCardEditError as exc:
            return exc.to_dict()
        return {"ok": outcome["state"] == "committed", "managed_card_edit": outcome,
                **({} if outcome["state"] == "committed" else {
                    "error": "managed_card_edit_" + outcome["state"], "status": 409,
                    "message": "The project did not save this change. Your draft is kept."})}

    async def _forward_agent_card_edit(
        self, user: Mapping[str, Any], *, record: AutomationAccessRecord, project_ref: str, request_id: str,
        changes: Mapping[str, Any], kind: str = "agent_card",
    ) -> dict[str, Any] | None:
        """W638: a project agent Card's (or a pending invitation Control's) Save, made by the project's transaction."""
        from .cards.store import subject_hash_for
        from .managed_card_edit_forward import ManagedCardEditError, managed_card_edit_body
        forwarder = self._managed_card_edit_forwarder(project_ref)
        actor_subject = _subject_from_user(user)
        if forwarder is None or not actor_subject or (kind == "agent_card" and record.control_card is None):
            return None
        unchanged = (
            ("properties", dict(record.properties or {})),
            ("composition_mode", record.composition_mode),
            ("label", record.label),
        )
        for field, stored in unchanged:
            value = changes.get(field)
            if field == "composition_mode":
                value, stored = (value or "and") if value is not None else None, stored or "and"
            if value is not None and (dict(value) if isinstance(value, Mapping) else value) != stored:
                return {"ok": False, "error": "managed_card_edit_fields_unsupported", "status": 409,
                        "message": "Only the Card's selection is changed by this edit. Nothing was saved."}
        revision = changes.get("expected_card_revision")
        if type(revision) is not int:
            return {"ok": False, "error": "managed_card_edit_revision_required", "status": 409,
                    "message": "Reload the Card and save again. Nothing was saved."}
        selection = {field: changes[field] for field in
                     ("resource_grants", "resource_operations", "named_service_operations", "account_scope")
                     if changes.get(field) is not None}
        try:
            body = managed_card_edit_body(actor_subject=actor_subject, project_ref=project_ref,
                request_id=request_id, kind=kind, principal_key="card:" + record.access_id,
                original_revision=revision, selection=selection,
                access_id=record.access_id, subject_hash=subject_hash_for(record.grantor_subject))
            outcome = await forwarder.forward(body)
        except ManagedCardEditError as exc:
            return exc.to_dict()
        return {"ok": outcome["state"] == "committed", "managed_card_edit": outcome,
                **({} if outcome["state"] == "committed" else {
                    "error": "managed_card_edit_" + outcome["state"], "status": 409,
                    "message": "The project did not save this change. Your draft is kept."})}

    async def _forward_managed_project_control(
        self, user: Mapping[str, Any], *, record: AutomationAccessRecord, request_id: str | None,
        changes: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """W638: a managed project Control's (P) Save, made by the project's own transaction.

        None when ``record`` is not a managed P or no forwarder is configured:
        the caller then keeps its direct-write refusal. A Save without a route
        request id gets one stable per Save (actor, P, expected revision and the
        selection sent), so retrying the same Save replays the project's one
        decision and never makes a second.
        """
        if self._managed_project_control_refused(record) is None:
            return None
        if not request_id:
            selection = {field: changes[field] for field in
                         ("resource_grants", "resource_operations", "named_service_operations", "account_scope")
                         if changes.get(field) is not None}
            digest = hashlib.sha256(json.dumps(
                [_subject_from_user(user), record.access_id, changes.get("expected_card_revision"), selection],
                sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
            request_id = "project-control-save:" + digest[:48]
        return await self._forward_agent_card_edit(user, record=record, project_ref=_clean(record.issuer_ref),
                                                   request_id=request_id, changes=changes, kind="project_control")

    def bind_managed_control_scopes(self, prefixes: Iterable[str]) -> None:
        """W578: the scopes in which a configured coordinator plans a project's Control Card (P).

        Each card-transaction caller's trusted ``plan_scope_prefix`` (bundle
        props, never a request). A P of a scope inside one is created, changed,
        attached and revoked only through that caller's lifecycle plan and
        group transaction while Card transactions are enabled.
        """
        self._managed_control_scopes = tuple(sorted({
            prefix for prefix in prefixes if isinstance(prefix, str) and prefix
        }))

    def _managed_project_control(self, *, access_id: str, issuer_kind: str, issuer_ref: str,
                                 grantor_subject: str) -> bool:
        """W578: is this exact Card a managed project's P while Card transactions are enabled?

        The same identity the lifecycle planner requires of P: an application
        Control whose id is derived from its issuer ref and holder, in a scope
        a configured coordinator plans. Read from the stored Card (or, for a
        create, the id the Card would have), never from a request label.
        """
        if getattr(self, "_card_coordinator", None) is None:
            return False
        ref = _clean(issuer_ref)
        if _clean(issuer_kind) != "application" or not ref:
            return False
        if not any(ref.startswith(prefix) for prefix in getattr(self, "_managed_control_scopes", ())):
            return False
        try:
            expected = control_card_id_for_issuer("application", ref, grantor_subject=_clean(grantor_subject))
        except ControlCardError:
            return False
        return _clean(access_id) == expected

    def _managed_project_control_refused(self, record: Any) -> dict[str, Any] | None:
        """W578: refuse a direct write to a stored managed P (its own fields or its parent link)."""

        if (record is None or _clean(getattr(record, "issuer_kind", "")) != "application"
                or not _record_is_credentialless(record)):
            return None
        if not self._managed_project_control(access_id=record.access_id, issuer_kind=record.issuer_kind,
                                             issuer_ref=record.issuer_ref, grantor_subject=record.grantor_subject):
            return None
        return self._managed_direct_write_refused()

    def _managed_project_binding_refused(self, record: Any) -> dict[str, Any] | None:
        """W578: refuse a direct change of a stored link to a managed P (detach, or attach replacing it)."""

        binding = getattr(record, "control_card", None)
        if binding is None:
            return None
        holder = _clean(getattr(binding, "holder_subject", "")) or record.grantor_subject
        if not self._managed_project_control(access_id=binding.control_id, issuer_kind=binding.issuer_kind,
                                             issuer_ref=binding.issuer_ref, grantor_subject=holder):
            return None
        return self._managed_direct_write_refused()

    # W578: the fields of a Control-bound Card whose change can lower (or move) its
    # authority. While Card transactions are on a direct write may change none of
    # them; ``expires_at`` may only move forward. Display and bookkeeping fields
    # (label, manage_url, client_metadata, provenance, revision, issuance
    # metadata) grant nothing. ``properties`` counts whole: some properties are
    # authorization (card_property_classes).
    _BOUND_AUTHORITY_FIELDS = frozenset({
        "account_scope", "catalog_version", "composition_mode", "control_card", "entry_resource",
        "identity_scope", "named_service_operations", "named_services", "operations", "properties",
        "resource_acceptance", "resource_grants", "resource_operations", "state",
        "access_id", "grantor_subject", "delegate_subject", "client_id", "issuer_kind", "issuer_ref", "source",
        "card_kind",
    })

    async def _refuse_bound_direct_write(self, record: AutomationAccessRecord, authority: CardAuthority, *,
                                         expected_revision: int) -> None:
        """W578: with Card transactions on, a Control-bound Card changes only through its owner's transaction.

        A person's My Card or a project-bound agent Card carries authority its
        Control's owner relies on (under an AND composition a project needs the
        My Card of its usable administrators), so creating one, replacing its
        credentials or changing anything that can lower its authority would
        bypass that owner's decision. Until it is enlisted such a write refuses.
        A write passes only when it changes no authority field, moves the
        expiry only forward and keeps the credentials (a prolongation, or a
        governed edit of display fields).
        """

        if getattr(self, "_card_coordinator", None) is None:
            return
        if expected_revision <= 0:
            if record.control_card is not None:
                raise CallerWriteRefused("card_transactions_direct_write_refused")
            return
        loaded = await self._cards().load(record.access_id, subject_hash=_subject_key(record.grantor_subject))
        if loaded is None:
            if record.control_card is not None:
                raise CallerWriteRefused("card_transactions_direct_write_refused")
            return
        current, handles = loaded
        if record.control_card is None and current.control_card is None:
            return  # unbound before and after: not this rule's (a detach is bound before)
        before, after = current.to_dict(), authority.to_dict()
        changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
        if (LEGACY_BINDING_REPAIR.get() and changed & self._BOUND_AUTHORITY_FIELDS == {"control_card"}
                and int(after.get("expires_at") or 0) >= int(before.get("expires_at") or 0)
                and card_handles_unchanged(handles, record)):
            # W578 exception 2 (P0 legacy repair): only the binding moves, to the value a new Card gets.
            return
        if (changed & self._BOUND_AUTHORITY_FIELDS
                or int(after.get("expires_at") or 0) < int(before.get("expires_at") or 0)
                or not card_handles_unchanged(handles, record)):
            raise CallerWriteRefused("card_transactions_direct_write_refused")

    async def _coordinated_write(
        self, record: AutomationAccessRecord, authority: CardAuthority, *, expected_revision: int,
        caller_write: CallerWrite | None, gate: Callable[[], Awaitable[None]] | None, witness: str,
        effects: Sequence[Mapping[str, Any]] = (),
    ) -> bool:
        """Run one existing-Card write as a one-participant transaction; False if not routable yet.

        Routable now: an existing Card whose credential handles do not change
        (update, extend, prune, reset, a fold's Card write). Writes that change
        handles, revokes and creation still take the direct path until their
        handle changes are recorded effects (W582); nothing binds the
        coordinator in production yet.
        """
        bound = getattr(self, "_card_coordinator", None)
        if bound is None or expected_revision <= 0:
            return False
        from service_foundation.coordination.durable_decision_log import DecisionRefused, IntentDraft

        from .cards.card_participant import PARTICIPANT, CardIntent, card_intent_payload_digest, hub_participant_input
        coordinator, intents, decisions, ttl = bound
        subject_hash = _subject_key(record.grantor_subject)
        loaded = await self._cards().load(record.access_id, subject_hash=subject_hash)
        if loaded is None:
            return False
        current, handles = loaded
        if not card_handles_unchanged(handles, record):
            return False
        action = caller_write.action if caller_write is not None else "update"
        actor = (caller_write.actor_subject if caller_write is not None else "") or record.grantor_subject
        actor_kind = "caller" if caller_write is not None and caller_write.actor_subject else "grantor"
        # W606: the edited Card's handle row moves with the edit, inside the same decision.
        effects = [*effects, *await self._handle_binding_effects([(current, authority)])]
        payload = card_intent_payload_digest(original=current, candidate=authority, effects=effects)
        request_id = (caller_write.request_id if caller_write is not None else "") or secrets.token_urlsafe(18)
        # W581 v2 (46a29992): one global intent; the Hub's input is its projection,
        # whose candidate_digest commits to base, candidate and every effect.
        draft = IntentDraft(
            replay_scope=f"{PARTICIPANT}:{subject_hash}:{actor}", request_id=request_id,
            expires_at=int(datetime.now(timezone.utc).timestamp()) + ttl, participants=(PARTICIPANT,),
            payload={"participant_inputs": {PARTICIPANT: hub_participant_input(
                original=current, candidate=authority, subject_hash=subject_hash, action=action,
                actor_subject=actor, actor_kind=actor_kind, effects=effects)}})
        try:
            row = await decisions.begin(draft)
            transaction_id = row.transaction_id
            await intents.record(CardIntent(transaction_id=transaction_id, intent_digest=row.intent.digest,
                                            subject_hash=subject_hash, original=current, candidate=authority,
                                            effects=tuple(dict(effect) for effect in effects),
                                            action=action, actor_subject=actor, actor_kind=actor_kind))
        except DecisionRefused as exc:
            raise CardConflict(str(exc)) from exc
        try:
            await coordinator.prepare_existing(transaction_id)
            if gate is not None:
                await gate()  # the binding's policy revalidates before the one decision
        except BaseException as exc:
            await coordinator.decide(transaction_id, "aborted")
            await coordinator.finish(transaction_id)
            if isinstance(exc, DecisionRefused):
                raise CardConflict(str(exc)) from exc
            raise
        try:
            # Governed Cards carry the gate's authorized change digest; an
            # ungoverned Card's witness is only its payload digest, which is
            # not an authorization (audit tells them apart by the binding).
            await coordinator.decide(transaction_id, "committed", witness_digest=witness or payload)
        except DecisionRefused as exc:
            # A refused COMMIT (an approval that expired, say) must not leave
            # the Card prepared: record the ABORT if nothing is decided yet,
            # then finish whatever the store holds (W581 S2).
            try:
                await coordinator.decide(transaction_id, "aborted")
            except DecisionRefused:
                pass  # already decided; finish materializes that decision
            await coordinator.finish(transaction_id)
            raise CardConflict(str(exc)) from exc
        await coordinator.finish(transaction_id)
        return True

    async def _persist_record(
        self, record: AutomationAccessRecord, *, expected_revision: int,
        before_commit: Callable[[], Awaitable[None]] | None = None,
        expected_issuer_digest: str = "",
        caller_write: CallerWrite | None = None,
        pre_gate: tuple[Any, Any] | None = None,
        effects: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        persistence = self._cards()
        persist = persistence.persist
        authority = card_authority_from_record(record)
        await self._refuse_bound_direct_write(record, authority, expected_revision=expected_revision)
        if self._managed_project_control_refused(record) is not None:
            # W578 (claude-main, #648): every direct writer, today's and any later
            # one, meets this; a managed P is written only through its project's
            # transaction (the participant stages it, never through this path).
            raise CallerWriteRefused("card_transactions_direct_write_refused")
        caller_request = None
        if before_commit is None:
            # pre_gate: a writer with side effects decided BEFORE them (W580 F5);
            # the same decision is revalidated inside the commit here.
            before_commit, caller_request = pre_gate if pre_gate is not None else await self._enlisted_gate(
                authority, expected_revision=expected_revision, caller_write=caller_write)
            if caller_request is not None:
                expected_issuer_digest = caller_request.change_digest
        guard = {}
        if before_commit is not None:
            persist = getattr(persistence, "persist_guarded", None)
            if not callable(persist):
                raise IssuerWriteRefused("issuer_commit_gate_unavailable")
            supplied_gate = before_commit

            async def bound_commit() -> None:
                if expected_issuer_digest and change_digest(authority.to_dict()) != expected_issuer_digest:
                    raise IssuerWriteRefused("issuer_candidate_changed")
                await supplied_gate()
                if expected_issuer_digest and change_digest(authority.to_dict()) != expected_issuer_digest:
                    raise IssuerWriteRefused("issuer_candidate_changed")

            guard["before_commit"] = bound_commit
        witness = caller_request.change_digest if caller_request is not None else expected_issuer_digest
        try:
            coordinated = await self._coordinated_write(
                record, authority, expected_revision=expected_revision, caller_write=caller_write,
                gate=guard.get("before_commit"), witness=witness, effects=effects)
            if effects and not coordinated:
                # Effects exist only inside the protocol: never dropped silently.
                raise CardConflict("card_effects_unroutable")
        except BaseException:
            if caller_request is not None:
                await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                           state="refused", card_revision=expected_revision)
            raise
        if coordinated:
            if caller_request is not None:
                await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                           state="committed", card_revision=authority.card_revision)
            return
        try:
            await persist(
                authority,
                card_handles_from_record(record),
                subject_hash=_subject_key(record.grantor_subject),
                expected_revision=expected_revision,
                **guard,
            )
        except CardServingUnavailable:
            # The durable commit happened; only serving is behind.
            if caller_request is not None:
                await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                           state="committed", card_revision=authority.card_revision)
            raise
        except BaseException:
            if caller_request is not None:
                await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                           state="refused", card_revision=expected_revision)
            raise
        if caller_request is not None:
            await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                       state="committed", card_revision=authority.card_revision)

    async def _forget_record(
        self,
        record: AutomationAccessRecord,
        *,
        revoked_record: AutomationAccessRecord | None = None,
        before_commit: Callable[[], Awaitable[None]] | None = None,
        caller_write: CallerWrite | None = None,
    ) -> None:
        authority = card_authority_from_record(record)
        subject_hash = _subject_key(record.grantor_subject)
        caller_request = None
        if before_commit is None and getattr(self, "_caller_writers", None) is not None:
            # Ops B3 (12:14): a revoke can remove the last usable admin, so a
            # governed Card is never revoked by an unnamed writer either.
            before_commit, caller_request = await self._enlisted_gate(
                authority, expected_revision=authority.card_revision, caller_write=caller_write,
                candidate={"action": "revoke", "access_id": authority.access_id,
                           "card_revision": authority.card_revision})
        if caller_request is not None:
            forget = getattr(self._cards(), "forget_guarded", None)
            if not callable(forget) or revoked_record is not None:
                await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                           state="refused", card_revision=authority.card_revision)
                raise IssuerWriteRefused("issuer_commit_gate_unavailable")
            try:
                await forget(authority, subject_hash=subject_hash, before_commit=before_commit)
            except BaseException:
                await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                           state="refused", card_revision=authority.card_revision)
                raise
            await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                       state="committed", card_revision=authority.card_revision + 1)
            return
        if before_commit is not None:
            forget = getattr(self._cards(), "forget_guarded", None)
            if not callable(forget) or revoked_record is not None:
                raise IssuerWriteRefused("issuer_commit_gate_unavailable")
            await forget(authority, subject_hash=subject_hash, before_commit=before_commit)
            return
        if revoked_record is None:
            await self._cards().forget(authority, subject_hash=subject_hash)
            return
        # W489: a record has no lifecycle state, and the authority rebuilt
        # from it reads active. The revision handed over is the revoked one,
        # so restore that state; the Card service still refuses any other
        # difference from the current revision.
        await self._cards().forget(
            authority,
            subject_hash=subject_hash,
            revoked_authority=dataclasses.replace(
                card_authority_from_record(revoked_record),
                state=CARD_STATE_REVOKED,
            ),
        )

    async def issuer_managed_lifecycle_read(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Both identities or neither, with no reservation or mutation effects."""
        from .issuer_read import (IssuerReadQuery, IssuerReadRequest, IssuerReadRefused,
                                  issuer_read_request_from_mapping)
        try:
            query = IssuerReadQuery.from_mapping(body)
            host = getattr(self, "_issuer_read_host", ())
            registry = getattr(self, "_issuer_reads", None)
            if len(host) != 4 or registry is None:
                raise IssuerReadRefused("issuer_read_host_unavailable")
            actor, classification, tenant, project = host
            request = IssuerReadRequest(actor, classification, tenant, project, query.context_ref, query.request_id, query.targets)
            request = issuer_read_request_from_mapping(request.to_dict())
            reader = getattr(self._cards(), "read_lifecycle_identities", None)
            if not callable(reader):
                raise IssuerReadRefused("issuer_read_port_unavailable")
            async with asyncio.timeout(30):
                first = []
                for kind in dict.fromkeys(t.issuer_kind for t in request.targets):
                    decision = await registry.decide(request, issuer_kind=kind)
                    registry.require(request, decision, issuer_kind=kind, phase="authorize")
                    first.append(decision)
                # No peer call within this port's two ordered Card fences.
                authorities = await reader(request)
                if len(authorities) != 2:
                    raise IssuerReadRefused("issuer_read_pair_required")
                snapshots = [{"target": target.to_dict(), "card_revision": authority.card_revision,
                    "authority_fingerprint": authority.content_hash(), "identity": registry.identity(authority)}
                    for target, authority in zip(request.targets, authorities)]
                fresh = [await registry.revalidate(request, decision, snapshots=snapshots) for decision in first]
                # Earlier decisions must still be live after the last peer await.
                for decision in fresh:
                    registry.require(request, decision, issuer_kind=decision.issuer_kind, phase="validate", snapshots=snapshots)
                return {"ok": True, "status": 200, "request": request.to_dict(),
                        "read_digest": request.read_digest, "snapshots": snapshots}
        except IssuerReadRefused as exc:
            return {"ok": False, "status": 409 if exc.retryable else 403,
                    "error": exc.reason, "retryable": exc.retryable}
        except TimeoutError:
            return {"ok": False, "status": 503, "error": "issuer_read_timeout", "retryable": True}
        except Exception:
            # Never log/return raw authorities or transport/credential details.
            _LOGGER.warning("[connection-hub] protected identity read unavailable")
            return {"ok": False, "status": 503, "error": "issuer_read_unavailable", "retryable": True}

    async def issuer_managed_card_snapshots(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Delegate full authority export to its separate sealed package contract."""
        from .issuer_snapshot import issuer_managed_card_snapshots
        host = getattr(self, "_issuer_snapshot_host", ())
        if len(host) != 4:
            return {"ok": False, "status": 403, "error": "issuer_snapshot_host_unavailable", "retryable": False}
        actor, classification, tenant, project = host
        return await issuer_managed_card_snapshots(body, actor_subject=actor,
            actor_classification=classification, tenant=tenant, project=project,
            registry=getattr(self, "_issuer_snapshots", None), persistence=self._persistence)

    async def issuer_managed_lifecycle_apply(self, body: Mapping[str, Any]) -> dict[str, Any]:
        """Host-authenticated generic lifecycle; target hints authorize nothing.

        The hosting operation rejects non-human principals BEFORE exposing
        this request-local service. Its bound actor cannot come from the DTO.
        Every target requires its configured issuer, including a credential-
        backed participant: this is not the old credentialless-only shortcut.
        """
        from .cards.lifecycle import LifecycleRefused, LifecycleRequest

        try:
            lifecycle = LifecycleRequest.from_mapping(body)
            actor = self._issuer_actor_subject
            if not self._issuer_actor_subject_bound or not actor:
                raise LifecycleRefused("issuer_lifecycle_actor_unavailable")
            persistence = self._cards()
            apply = getattr(persistence, "revoke_lifecycle", None)
            read_receipt = getattr(persistence, "load_lifecycle_receipt", None)
            if not callable(apply) or not callable(read_receipt):
                raise LifecycleRefused("issuer_lifecycle_commit_gate_unavailable")
            recorded = await read_receipt(lifecycle, actor_subject=actor)
            decisions = []
            if recorded is None:
                for target in lifecycle.targets:
                    loaded = await persistence.load_current(target.access_id, subject_hash=target.subject_hash)
                    authority = None if loaded is None else loaded[0]
                    target.assert_authority(authority)
                    request = IssuerRequest(actor_subject=actor, request_id=lifecycle.request_id,
                        action="revoke", access_id=target.access_id, card_revision=target.expected_card_revision,
                        issuer_kind=target.issuer_kind, issuer_ref=target.issuer_ref,
                        change_digest=lifecycle.change_digest, context_ref=lifecycle.context_ref)
                    decision = await self._issuers.decide(request)
                    refusal = issuer_write_refusal(authority, request, decision, now=datetime.now(timezone.utc))
                    if refusal is not None:
                        raise IssuerWriteRefused(refusal["reason"])
                    decisions.append((target, authority, request, decision))

            async def before_commit(authorities):
                if len(decisions) != 2:
                    raise IssuerWriteRefused("issuer_lifecycle_replay_not_mutation")
                deadlines = []
                for (target, _original, request, decision), current in zip(decisions, authorities):
                    target.assert_authority(current)
                    fresh = await self._issuers.revalidate(request, decision)
                    refusal = issuer_write_refusal(current, request, fresh, now=datetime.now(timezone.utc))
                    if refusal is not None:
                        raise IssuerWriteRefused(refusal["reason"])
                    deadlines.append(fresh.valid_until)
                return min(deadlines)

            receipt = await apply(lifecycle, actor_subject=actor, before_commit=before_commit)
            committed = receipt["state"] == "committed"
            complete = receipt["serving_state"] in ("complete", "not_required")
            # Issuer-owned orchestration consumes this shared outcome. Hub does
            # not invent a second per-participant policy/finalization protocol.
            return {"ok": committed and complete, "status": 200 if committed and complete else 202 if committed else 409,
                "state": receipt["state"], "serving_state": receipt["serving_state"],
                "transaction_id": receipt["transaction_id"], "request_id": lifecycle.request_id,
                "change_digest": lifecycle.change_digest, "reason": receipt["reason"],
                "retryable": not complete, "requires_new_request_id": not committed,
                "targets": [{"access_id": entry["access_id"], "before_revision": entry["before"]["card_revision"],
                    "after_revision": entry["after"]["card_revision"] if committed else entry["before"]["card_revision"]}
                    for entry in receipt["targets"]]}
        except IssuerWriteRefused as exc:
            return {"ok": False, "error": "issuer_managed_card", "reason": exc.reason, "status": 403}
        except LifecycleRefused as exc:
            return {"ok": False, "error": exc.reason, "status": 409, "requires_new_request_id": True}
        except CardConflict as exc:
            return {"ok": False, "error": exc.reason, "status": 409, "retryable": True}
        except Exception:
            _LOGGER.exception("[connection-hub] issuer lifecycle unavailable")
            return {"ok": False, "error": "issuer_lifecycle_unavailable", "status": 503, "retryable": True}

    async def issuer_managed_card_update(self, body: Mapping[str, Any]) -> dict[str, Any]:
        from .issuer_update import issuer_managed_card_update
        host = getattr(self, "_issuer_update_host", ())
        if (len(host) != 4 or host[1] not in ("registered", "privileged")
                or not all(type(v) is str and v.strip() for v in host)):
            return {"ok": False, "status": 403, "error": "issuer_update_host_unavailable"}
        return await issuer_managed_card_update(body, actor_subject=host[0],
            registry=getattr(self, "_issuers", None), persistence=self._persistence,
            host_is_current=getattr(self, "_issuer_update_host_is_current", None))

    def _issuer_managed(self, record: AutomationAccessRecord) -> bool:
        return _record_is_credentialless(record) and self._issuers.is_managed(record.issuer_kind)

    async def _issuer_before_commit(
        self, record: AutomationAccessRecord, user: Mapping[str, Any], *,
        action: str, candidate: Mapping[str, Any], request_id: str,
        context_ref: str, decision: object,
    ) -> tuple[Callable[[], Awaitable[None]] | None, IssuerRequest | None]:
        if not self._issuer_managed(record):
            return None, None
        guarded_method = "persist_guarded" if action == "update" else "forget_guarded"
        if not callable(getattr(self._cards(), guarded_method, None)):
            raise IssuerWriteRefused("issuer_commit_gate_unavailable")
        if action == "update" and (
            any(candidate.get(key) != getattr(record, key) for key in
                ("access_id", "grantor_subject", "issuer_kind", "issuer_ref", "source", "expires_at", "delegate_subject"))
            or candidate.get("card_revision") != record.card_revision + 1
        ):
            raise IssuerWriteRefused("issuer_candidate_binding_mismatch")
        request = IssuerRequest(
            actor_subject=(self._issuer_actor_subject if self._issuer_actor_subject_bound
                           else _subject_from_user(user)),
            request_id=_clean(request_id) or secrets.token_urlsafe(18),
            action=action, access_id=record.access_id,
            card_revision=record.card_revision, issuer_kind=record.issuer_kind,
            issuer_ref=record.issuer_ref, change_digest=change_digest(candidate),
            context_ref=_clean(context_ref),
        )
        if decision is None:
            if not request.context_ref:
                request = await self._issuers.prepare(
                    request, current=card_authority_from_record(record).to_dict(),
                    candidate=candidate,
                )
            decision = await self._issuers.decide(request)
        refusal = issuer_write_refusal(record, request, decision, now=datetime.now(timezone.utc))
        if refusal is not None:
            confirmed = await self._issuers.finalize(request, state="refused", card_revision=record.card_revision)
            raise IssuerWriteRefused(refusal["reason"], outcome_confirmed=confirmed)

        async def before_commit() -> None:
            # The durable port invokes this INSIDE the target mutation lock,
            # after checking its current revision, before all mutation effects.
            fresh = await self._issuers.revalidate(request, decision)
            refusal = issuer_write_refusal(record, request, fresh, now=datetime.now(timezone.utc))
            if refusal is not None:
                raise IssuerWriteRefused(refusal["reason"])

        return before_commit, request

    def bind_caller_writers(self, registry: Any) -> None:
        """W578: the hosting composition registers the caller-writer policies once."""
        self._caller_writers = registry

    def _caller_actor_subject(self, user: Mapping[str, Any]) -> str:
        # The authenticated hosting actor when the host bound one (a person
        # acting on another owner's Card through a hosting route), else the
        # caller itself; never the storage owner.
        return self._issuer_actor_subject if self._issuer_actor_subject_bound else _subject_from_user(user)

    async def reset_service_to_control(
        self, user: Mapping[str, Any], *, access_id: str, resource: str,
        expected_card_revision: int, request_id: str = "", context_ref: str = "",
    ) -> dict[str, Any]:
        """W578: explicit per-service Reset to Control, decided like any bound owner write.

        One service's selection becomes the current Control's selection for
        that service, resolved through the authoritative Control (never a
        caller-supplied selection); every other service, the identity and the
        credentials are unchanged. Nothing calls this on a read or on a
        Control change.
        """
        grantor_subject = _subject_from_user(user)
        existing = await self._load_record(_clean(access_id), grantor_subject=grantor_subject)
        if existing is None:
            return {"ok": False, "status": 404, "error": "access_not_found"}
        if int(expected_card_revision) != int(existing.card_revision):
            return {"ok": False, "status": 409, "error": "card_revision_conflict",
                    "expected": int(expected_card_revision), "actual": int(existing.card_revision)}
        if existing.control_card is None:
            return {"ok": False, "status": 409, "error": "caller_writer_reset_requires_control"}
        # TODO(W577): take effective_control_card from the hierarchy resolver
        # once it lands; the raw current Control is not the effective ceiling
        # under a parent chain (Infra, 11:06). Until then reset is not claimed
        # complete for chained Controls.
        try:
            control, _ = await self._compose_with_control(existing)
        except (CardUnavailable, ControlCardMismatch) as exc:
            return {"ok": False, "status": 503, "error": "control_card_unavailable",
                    "reason": getattr(exc, "reason", ""), "retryable": True}
        if control is None:
            return {"ok": False, "status": 503, "error": "control_card_unresolvable", "retryable": True}
        control_authority = card_authority_from_record(control)
        resource = _clean(resource)
        try:
            candidate = reset_candidate(
                card_authority_from_record(existing), resource=resource,
                control_operations=control_authority.resource_operations.get(resource, ()),
                control_grants=control_authority.resource_grants.get(resource, ()),
                control_named_services=control_named_services_entry(control_authority, resource),
            )
        except CallerWriteRefused as exc:
            return exc.to_dict()
        # The reset service's named-service selection follows the Control too (reset_candidate set it);
        # update_access reads an omitted selection as "keep", so the computed one must be forwarded.
        selection = CardAuthority.from_mapping(candidate).named_service_operations
        forwarded_named = (None if selection.is_all or selection.is_unknown
                           else {key: dict(value) for key, value in selection.operations.items()})
        return await self.update_access(
            user, access_id=existing.access_id,
            _application_write=True,  # W661 S5: the Reset copies the Control's grants, managed ones included
            resource_grants={key: list(value) for key, value in candidate["resource_grants"].items()},
            resource_operations={key: list(value) for key, value in candidate["resource_operations"].items()},
            named_service_operations=forwarded_named,
            expected_card_revision=existing.card_revision,
            _issuer_request_id=request_id, _issuer_context_ref=context_ref,
            _caller_write_action="reset",
        )

    async def _issuer_outcome(self, request: IssuerRequest | None, *,
                              state: str, card_revision: int) -> dict[str, Any]:
        if request is None:
            return {}
        confirmed = await self._issuers.finalize(request, state=state, card_revision=card_revision)
        return {"issuer_outcome_confirmed": confirmed}

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

    async def _viewer_authority(self, user: Mapping[str, Any]) -> ViewerAuthority:
        """What the signed-in person's own role may delegate (W379).

        The project routes read and write a Card under its owner and ask the
        project host who may act; the catalog rows are still offered by the
        signed-in person's role, as on their own Cards.
        """

        grants: tuple[str, ...] = ()
        try:
            offer = await self._offer_config(owner_subject=_subject_from_user(user))
            if offer is not None:
                grants = tuple(
                    sorted((await self._available_inventory(user, config=offer)).grant_names())
                )
        except Exception:  # noqa: BLE001 - no catalog adds nothing: the host's answer alone bounds the route
            grants = ()
        return ViewerAuthority(grants=grants, platform_admin=_is_platform_admin(user))

    async def resource_options(
        self,
        user: Mapping[str, Any],
        *,
        _delegable_grants: Iterable[str] | None = None,
        _platform_admin: bool | None = None,
    ) -> list[dict[str, Any]]:
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
        platform_admin = (
            _is_platform_admin(user)
            if _platform_admin is None
            else bool(_platform_admin)
        )
        delegable = (
            set((await self._available_inventory(user, config=offer)).grant_names())
            if _delegable_grants is None
            else set(_as_list(_delegable_grants))
        )
        out: list[dict[str, Any]] = []
        for resource in offer.resources:
            if admin_only_closed(resource, delegable, platform_admin=platform_admin):
                continue
            if role_closed(resource, delegable):
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
                # The "admin" mark is for a platform admin: anyone else sees the
                # row as an ordinary one, bounded to the grants that are theirs.
                "admin_only": bool(resource.admin_only) and platform_admin,
                # Grants the owning application alone sets: shown, never editable here.
                **({"managed_grants": list(managed)} if (managed := tuple(
                    grant for grant in (getattr(resource, "managed_grants", ()) or ())
                    if grant in delegable)) else {}),
                # W667: the operations a person's Card decides; a person's editor hides the rest.
                **({"person_card_operations": list(person_ops)} if (person_ops := tuple(
                    getattr(resource, "person_card_operations", ()) or ())) else {}),
                "operations": [
                    {
                        "name": tool.name,
                        "label": tool.label,
                        "description": tool.description,
                        "grants": list(tool.grants),
                        **({"group": tool.group} if getattr(tool, "group", "") else {}),
                        **({} if getattr(tool, "person_card", True) else {"person_card": False}),
                        # Needs a managed grant: shown as the Card holds it, never changed by an editor.
                        **({"managed": True} if tool.name in managed_operations(resource) else {}),
                    }
                    for tool in resource.tools
                    if _grants_delegable(tool.grants, delegable)
                ],
            }
            # How the service groups these operations for a Card editor.
            operation_groups = list(getattr(resource, "operation_groups", ()) or ())
            if operation_groups:
                option["operation_groups"] = [dict(group) for group in operation_groups]
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
        # W419: each Card's control view is its own read; read them together,
        # a few at a time, instead of one after another.
        control_views = await bounded_gather(
            [self._effective_control_view(record) for record in records_found]
        )
        records = []
        for record, effective_control in zip(records_found, control_views):
            item = record.to_public_dict()
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
            card_resource_options = resource_option_rows
            if record.source == ACCESS_SOURCE_AGENT and record.control_card is not None:
                control_authority = effective_control.get("control_authority")
                if (
                    effective_control.get("state") != CARD_STATE_ACTIVE
                    or not isinstance(control_authority, Mapping)
                ):
                    card_resource_options = []
                else:
                    card_resource_options, family_roots = (
                        _descriptor_agent_resource_options(
                            control_authority,
                            resource_option_rows,
                        )
                    )
                    if family_roots:
                        item["resource_family_roots"] = family_roots
            item["resource_offers"] = compatible_resource_offers(
                card_resources=self._card_resource_keys(
                    record.resource_grants, config=listing_config
                ),
                card_identity_scope=record.identity_scope,
                options=card_resource_options,
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
        _application_write: bool = False,
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
        if not _application_write:
            # Only the application's own write may assign an application-managed grant (default: refused).
            changes = managed_grant_changes(catalog_config, self._resource_grants(resource_grants), {})
            changes += managed_operation_changes(catalog_config, self._resource_grants(resource_operations or {}), {})
            if changes:
                return managed_grant_refusal(changes)

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
        if selected_named_service_operations is not None:
            selected_named_service_operations = self._declared_named_service_selection(
                catalog_config,
                selected_named_service_operations,
            )
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
        admin_required = [
            cfg.resource for cfg in resource_configs
            if admin_only_closed(cfg, available, platform_admin=_is_platform_admin(user))
        ]
        if admin_required:
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
            allowed_for_resource = set(catalog_config.resource_grants(resource_value))
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
                            required=catalog_config.resource_grants(resource_value),
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
                allowed_roles=catalog_config.resource_grants(
                    APPLICATION_API_RESOURCE
                ),
            )
        except ApplicationOperationPolicyError as exc:
            return _application_policy_refusal(exc)

        ttl = _bounded_ttl(ttl_seconds)
        now = int(time.time())
        created_at = created_at_override or now
        grantor_authority = _grantor_authority(
            user,
            grants=authority_grants,
            inventory=inventory,
        )
        minted = await self._mint_card_credential(
            user,
            grantor_subject=grantor_subject,
            client_id=client_id,
            access_id=access_id,
            grants=selected_grants,
            operations=selected_operations,
            resource_grants=selected_resource_grants,
            resource_operations=selected_resource_operations,
            account_scope=selected_account_scope,
            identity_scope=identity_scope,
            named_services=named_services,
            ttl=ttl,
            now=now,
            grantor_authority=grantor_authority,
        )
        access_token = _clean(minted.get("access_token"))
        expires_in = int(minted.get("expires_in") or ttl)
        expires_at = now + expires_in
        session_id = _clean(minted.get("session_id"))

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
            await self._persist_record(record, expected_revision=committed_revision, caller_write=CallerWrite(
                "create" if committed_revision == 0 else "replace", self._caller_actor_subject(user)))
        except CallerWriteRefused as exc:
            return exc.to_dict()
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
        _delegable_grants: Iterable[str] | None = None,
        _platform_admin: bool | None = None,
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
        capability_agent = (
            existing.source == ACCESS_SOURCE_AGENT
            and existing.card_kind == CARD_KIND_AGENT
            and AGENT_CAPABILITY_SELECTION_PROPERTY
            in dict(existing.properties or {})
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
        if not any(selected_resource_grants.values()) and not capability_agent:
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
        if reconciled.empty and capability_agent:
            return ResolvedCardAuthority(
                resource_grants={},
                resource_operations={},
                operations=[],
                named_service_operations=NamedServiceSelection.none(),
                named_services={},
                account_scope={},
                identity_scope=existing.identity_scope or "grantor",
                properties=selected_properties,
                reconciled=reconciled,
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
        delegable_grants = (
            set(
                (
                    await self._available_inventory(
                        user,
                        requested_grants=authority_grants,
                        config=catalog_config,
                    )
                ).grant_names()
            )
            if _delegable_grants is None
            else set(_as_list(_delegable_grants))
        )
        denied = [grant for grant in selected_grants if grant not in delegable_grants]
        if denied and _delegable_grants is not None:
            # A save bounded by its project host refuses only what it ADDS
            # beyond that bound (W296, 2026-09-26): a grant the stored Card
            # already carries stays, unless an operation newly selected here
            # carries it. Removing a permission or accepting a changed
            # descriptor never needs the editor to hold everything the Card holds.
            carried = {
                grant
                for grants in dict(existing.resource_grants or {}).values()
                for grant in grants
            }
            added = _newly_selected_operation_grants(
                existing,
                resource_operations=selected_resource_operations,
                named_service_operations=selected_named_service_operations,
                resource_pairs=resource_pairs,
            )
            denied = [
                grant
                for grant in denied
                if grant not in carried or added is None or grant in added
            ]
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
                    if _delegable_grants is None
                    else "These permissions are beyond what you may add to this Card: "
                    + ", ".join(denied)
                    + "."
                ),
            })
        try:
            _application_role_policy(
                properties=selected_properties,
                resource_grants=selected_resource_grants,
                resource_operations=selected_resource_operations,
                delegable_roles=delegable_grants,
                allowed_roles=catalog_config.resource_grants(
                    APPLICATION_API_RESOURCE
                ),
            )
        except ApplicationOperationPolicyError as exc:
            return ResolvedCardAuthority(error=_application_policy_refusal(exc))
        platform_admin = (
            _is_platform_admin(user)
            if _platform_admin is None
            else bool(_platform_admin)
        )
        admin_required = [
            cfg.resource for cfg in resource_configs
            if admin_only_closed(cfg, delegable_grants, platform_admin=platform_admin)
        ]
        if admin_required:
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
            allowed_for_resource = set(catalog_config.resource_grants(resource_value))
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
                        required=catalog_config.resource_grants(resource_value),
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

    async def apply_authorization_profile(
        self,
        user: Mapping[str, Any],
        *,
        access_id: str,
        profile: str,
        expected_card_revision: int | None = None,
        request_id: str = "",
        resources: Iterable[str] | None = None,
        _actor_subject: str = "",
        _delegable_grants: Iterable[str] | None = None,
        _extra_record_transform: Callable[[Any, Any], Any] | None = None,
        _actor_platform_admin: bool = False,
    ) -> dict[str, Any]:
        """Re-apply a descriptor authorization profile to an existing Card, in place.

        W313 step 2, the Card lever: one action gives an agent's Card the
        coordinator operation set, and one returns it to the default worker
        set, instead of revoking the Card and consenting a new one (which
        changed the access id and the agent's principal). The selection is the
        one first consent would propose for that profile (``worker`` is the
        declared worker list, never the Card's earlier grants), computed for
        each Card resource that declares the profile; a resource that does not
        declare it keeps its selection. The save goes through
        ``update_access``, so ownership, the administrator preset, the revision
        precondition and pruning apply unchanged, and the new revision carries
        an audit of who applied which profile, from which revision.

        ``resources`` scopes the reset (W420): the declared selectors whose
        Card resources take the profile. Every other Card resource keeps its
        selection, even when it declares the same profile, so a service's
        worker refresh never resets another service's permissions. A scope
        the Card holds none of is refused. Without it, every resource that
        declares the profile takes it, as before.
        """

        grantor_subject = _subject_from_user(user)
        if not grantor_subject:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}
        refusal = _delegate_mutation_refusal(user)
        if refusal is not None:
            return refusal
        access_id = _clean(access_id)
        name = _clean(profile).lower()
        if not access_id:
            return {"ok": False, "error": "delegated_access_requires_access_id"}
        if not name:
            return {"ok": False, "error": "delegated_access_profile_required"}
        scope: set[str] | None = None
        if resources is not None:
            scope = {_clean(item) for item in resources if _clean(item)}
            if not scope:
                return {
                    "ok": False,
                    "error": "delegated_access_profile_resource_scope_empty",
                    "status": 400,
                }
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
        if _clean(existing.source) != ACCESS_SOURCE_OAUTH:
            # A profile is what an OAuth consent proposes; other families
            # (manual, agent, control, project person) have their own editors.
            return {
                "ok": False,
                "error": "delegated_access_profile_requires_oauth_card",
                "status": 409,
                "source": _clean(existing.source),
            }
        try:
            active = await self._active_catalog()
            catalog_config = await self._catalog_config(
                active, owner_subject=grantor_subject
            )
        except CatalogUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_catalog_unavailable",
                "reason": getattr(exc, "reason", "") or str(exc),
                "retryable": True,
                "status": 503,
            }
        from connection_hub.delegated_credentials.oauth.consent import (
            requested_card_selection,
        )

        resource_grants = {
            key: list(values or ()) for key, values in (existing.resource_grants or {}).items()
        }
        resource_operations = {
            key: list(values or ()) for key, values in (existing.resource_operations or {}).items()
        }
        applied: list[dict[str, Any]] = []
        available: set[str] = set()
        in_scope = 0
        for resource in list(resource_grants):
            row = self._configured_resource(resource, config=catalog_config)
            if row is None:
                continue
            if scope is not None:
                # The Card key or the selector of the row that governs it.
                if not ({_clean(resource), _clean(getattr(row, "resource", ""))} & scope):
                    continue
                in_scope += 1
            declared = {
                _clean(getattr(item, "name", "")).lower(): item
                for item in (getattr(row, "authorization_profiles", ()) or ())
            }
            available.update(key for key in declared if key)
            chosen = declared.get(name)
            if chosen is None:
                continue
            selection = requested_card_selection(
                [_clean(getattr(chosen, "scope", ""))],
                config=catalog_config,
                resource=resource,
            )
            selected_grants = dict(selection.get("resource_grants") or {})
            selected_operations = dict(selection.get("resource_operations") or {})
            if not selected_grants:
                continue
            # The proposal is keyed by the declared selector; a legacy Card
            # key (a concrete URL) is replaced by it, as a save would store it.
            resource_grants.pop(resource, None)
            resource_operations.pop(resource, None)
            for key, values in selected_grants.items():
                resource_grants[key] = list(values)
                resource_operations[key] = list(selected_operations.get(key) or ())
                applied.append(
                    {
                        "resource": key,
                        "scope": _clean(getattr(chosen, "scope", "")),
                        "operations": list(selected_operations.get(key) or ()),
                    }
                )
        if scope is not None and not in_scope:
            return {
                "ok": False,
                "error": "delegated_access_profile_resource_not_on_card",
                "status": 409,
                "profile": name,
                "resources": sorted(scope),
            }
        if not applied:
            return {
                "ok": False,
                "error": "delegated_access_profile_not_declared",
                "status": 409,
                "profile": name,
                "available_profiles": sorted(available),
            }
        before_operations = {
            key: list(values or ()) for key, values in (existing.resource_operations or {}).items()
        }
        occurred_at = int(time.time())

        def _stamp_profile_audit(previous: Any, candidate: Any) -> Any:
            audit = {
                "schema": AUTHORIZATION_PROFILE_AUDIT_SCHEMA,
                "action": "profile_applied",
                "profile": name,
                "applied": applied,
                **({"resource_scope": sorted(scope)} if scope is not None else {}),
                # The project path (W319) acts under the owner's key for an
                # admin of the agent's project: the audit names that admin.
                "actor_subject": _clean(_actor_subject) or grantor_subject,
                "request_id": _clean(request_id),
                "occurred_at": occurred_at,
                "before_revision": int(getattr(previous, "card_revision", 0) or 0),
                "after_revision": int(getattr(candidate, "card_revision", 0) or 0),
                "before_operations": before_operations,
                "after_operations": {
                    key: list(values or ())
                    for key, values in (getattr(candidate, "resource_operations", None) or {}).items()
                },
            }
            provenance = dict(getattr(candidate, "provenance", None) or {})
            provenance[AUTHORIZATION_PROFILE_AUDIT_PROVENANCE] = audit
            stamped = replace_fields(candidate, provenance=provenance)
            return (
                _extra_record_transform(previous, stamped)
                if _extra_record_transform is not None
                else stamped
            )

        result = await self.update_access(
            user,
            access_id=access_id,
            resource_grants=resource_grants,
            resource_operations=resource_operations,
            expected_card_revision=expected_card_revision,
            _delegable_grants=_delegable_grants,
            # W379: the acting person's own platform-admin fact on the
            # project path, never a fixed False.
            _platform_admin=bool(_actor_platform_admin) if _actor_subject else None,
            _record_transform=_stamp_profile_audit,
        )
        if result.get("ok"):
            result = {**result, "profile": name, "applied": applied}
        return result

    async def add_operations(
        self,
        user: Mapping[str, Any],
        *,
        access_id: str,
        operations: Iterable[str],
        expected_card_revision: int | None = None,
        request_id: str = "",
        _actor_subject: str = "",
        _delegable_grants: Iterable[str] | None = None,
        _extra_record_transform: Callable[[Any, Any], Any] | None = None,
        _actor_platform_admin: bool = False,
    ) -> dict[str, Any]:
        """Add operations to an existing Card, keeping everything else (W371).

        The generic additive write: for each Card resource whose catalog row
        offers a requested operation, the operation and the grants it needs are
        added; the Card's other selections, including a deliberate narrowing,
        stay as they are. Nothing is written when every offered operation is
        already on the Card. The save goes through ``update_access`` (ownership,
        catalog validation, the delegable-grant ceiling, the revision
        precondition) and the new revision carries an audit of who added what.
        It knows no project: a project path authorizes the actor before it.
        """

        grantor_subject = _subject_from_user(user)
        if not grantor_subject:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}
        refusal = _delegate_mutation_refusal(user)
        if refusal is not None:
            return refusal
        access_id = _clean(access_id)
        requested = list(dict.fromkeys(_clean(item) for item in (operations or ()) if _clean(item)))
        if not access_id:
            return {"ok": False, "error": "delegated_access_requires_access_id"}
        if not requested:
            return {"ok": False, "error": "delegated_access_operations_required", "status": 400}
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
        if _clean(existing.source) != ACCESS_SOURCE_OAUTH:
            return {
                "ok": False,
                "error": "delegated_access_add_requires_oauth_card",
                "status": 409,
                "source": _clean(existing.source),
            }
        try:
            active = await self._active_catalog()
            catalog_config = await self._catalog_config(active, owner_subject=grantor_subject)
        except CatalogUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_catalog_unavailable",
                "reason": getattr(exc, "reason", "") or str(exc),
                "retryable": True,
                "status": 503,
            }
        resource_grants = {key: list(values or ()) for key, values in (existing.resource_grants or {}).items()}
        resource_operations = {
            key: list(values or ()) for key, values in (existing.resource_operations or {}).items()
        }
        added: list[dict[str, Any]] = []
        offered: set[str] = set()
        for resource in list(resource_grants):
            row = self._configured_resource(resource, config=catalog_config)
            if row is None:
                continue
            tools = {_clean(getattr(tool, "name", "")): tool for tool in (getattr(row, "tools", ()) or ())}
            wanted = [name for name in requested if name in tools]
            offered.update(wanted)
            missing = [name for name in wanted if name not in resource_operations.get(resource, [])]
            if not missing:
                continue
            grants = resource_grants[resource]
            for name in missing:
                for grant in (getattr(tools[name], "grants", None) or getattr(row, "grants", None) or ()):
                    if _clean(grant) and _clean(grant) not in grants:
                        grants.append(_clean(grant))
            resource_operations[resource] = [*resource_operations.get(resource, []), *missing]
            added.append({"resource": resource, "operations": missing})
        if not offered:
            return {
                "ok": False,
                "error": "delegated_access_operation_not_offered",
                "status": 409,
                "operations": requested,
            }
        if not added:
            return {"ok": True, "changed": False, "access_id": access_id, "added": []}
        occurred_at = int(time.time())

        def _stamp_operations_added(previous: Any, candidate: Any) -> Any:
            audit = {
                "schema": OPERATIONS_ADDED_AUDIT_SCHEMA,
                "action": "operations_added",
                "added": added,
                "actor_subject": _clean(_actor_subject) or grantor_subject,
                "request_id": _clean(request_id),
                "occurred_at": occurred_at,
                "before_revision": int(getattr(previous, "card_revision", 0) or 0),
                "after_revision": int(getattr(candidate, "card_revision", 0) or 0),
            }
            provenance = dict(getattr(candidate, "provenance", None) or {})
            provenance[OPERATIONS_ADDED_AUDIT_PROVENANCE] = audit
            stamped = replace_fields(candidate, provenance=provenance)
            return (
                _extra_record_transform(previous, stamped)
                if _extra_record_transform is not None
                else stamped
            )

        result = await self.update_access(
            user,
            access_id=access_id,
            resource_grants=resource_grants,
            resource_operations=resource_operations,
            expected_card_revision=expected_card_revision,
            _delegable_grants=_delegable_grants,
            # W379: the acting person's own platform-admin fact on the
            # project path, never a fixed False.
            _platform_admin=bool(_actor_platform_admin) if _actor_subject else None,
            _record_transform=_stamp_operations_added,
        )
        if result.get("ok"):
            result = {**result, "changed": True, "added": added}
        return result

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
        _delegable_grants: Iterable[str] | None = None,
        _platform_admin: bool | None = None,
        _record_transform: Callable[
            [AutomationAccessRecord, AutomationAccessRecord],
            AutomationAccessRecord,
        ]
        | None = None,
        _notification_subject: str = "",
        _issuer_decision: IssuerDecision | None = None,
        request_id: str | None = None,
        _issuer_request_id: str = "",
        _issuer_context_ref: str = "",
        _caller_write_action: str = "update",
        _caller_actor_subject: str = "",
        _application_write: bool = False,
        _person_control: bool = False,
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
        refused = self._managed_project_control_refused(existing)
        if refused is not None:
            # W638: the owner's Save of a managed P goes through the project's own transaction.
            forwarded = await self._forward_managed_project_control(user, record=existing, request_id=request_id,
                changes={"resource_grants": resource_grants, "resource_operations": resource_operations,
                         "named_service_operations": named_service_operations, "account_scope": account_scope,
                         "properties": properties, "composition_mode": composition_mode, "label": label,
                         "expected_card_revision": expected_card_revision})
            return forwarded if forwarded is not None else refused
        try:
            descriptor_marker = descriptor_control(existing.properties)
        except AgentCapabilityPolicyError as exc:
            return {"ok": False, "error": exc.reason, "status": 409}
        administrator_preset = (
            descriptor_marker is not None
            or existing.issuer_kind == AGENT_DESCRIPTOR_ISSUER_KIND
        )
        if administrator_preset and not _is_platform_admin(user):
            return {
                "ok": False,
                "error": "platform_admin_required",
                "status": 403,
            }
        if self._issuer_managed(existing) and not control_snapshot_is_exact(
            card_authority_from_record(existing)
        ):
            return IssuerWriteRefused("issuer_snapshot_requires_explicit_migration").to_dict()
        if _record_is_credentialless(existing) and not self._issuer_managed(existing):
            try:
                existing = await self._ensure_control_snapshot(existing, actor_subject=self._caller_actor_subject(user))
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
        if (
            descriptor_marker is not None
            and selected_composition_mode != CONTROL_COMPOSITION_AND
        ):
            return {
                "ok": False,
                "error": "agent_descriptor_control_requires_and",
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
        if not _application_write:
            # A Card write keeps every application-managed grant exactly as the Card holds it, unless it is
            # the application's own write (explicit opt-out; every client route is guarded by default).
            changes = managed_grant_changes(
                catalog_config, self._resource_grants(resource_grants), self._resource_grants(existing.resource_grants))
            if resource_operations is not None:
                # An operation that needs a managed grant (every Card), and on a person's Control Card an
                # operation the application decides for a person (W560), stays as the Card holds it.
                changes += managed_operation_changes(
                    catalog_config, self._resource_grants(resource_operations),
                    self._resource_grants(existing.resource_operations), person_control=_person_control)
            if changes:
                return managed_grant_refusal(changes)
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
            _delegable_grants=_delegable_grants,
            _platform_admin=_platform_admin,
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
        if (
            existing.source == ACCESS_SOURCE_AGENT
            and existing.card_kind == CARD_KIND_AGENT
            and existing.control_card is not None
            and AGENT_CAPABILITY_SELECTION_PROPERTY
            in dict(existing.properties or {})
        ):
            try:
                loaded_control = await self._load_record_any_state(
                    existing.control_card.control_id,
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
            if loaded_control is None or loaded_control[1] != CARD_STATE_ACTIVE:
                return {
                    "ok": False,
                    "error": "agent_capability_control_not_active",
                    "status": 409,
                }
            control = loaded_control[0]
            try:
                authority = AgentCapabilityPolicy.from_property(
                    dict(control.properties or {}).get(
                        AGENT_CAPABILITY_AUTHORITY_PROPERTY
                    )
                )
                current = AgentCapabilityPolicy.from_property(
                    dict(existing.properties or {}).get(
                        AGENT_CAPABILITY_SELECTION_PROPERTY
                    )
                )
                requested_raw = dict(selected_properties or {}).get(
                    AGENT_CAPABILITY_SELECTION_PROPERTY
                )
                requested = (
                    AgentCapabilityPolicy.from_property(requested_raw)
                    if requested_raw is not None
                    else current
                )
                aligned = align_resident_selection_to_card_authority(
                    current=current,
                    requested=requested,
                    authority=authority,
                    resource_grants=selected_resource_grants,
                    resource_operations=selected_resource_operations,
                    named_service_operations=selected_named_service_operations,
                    targets=conversation_targets(selected_properties),
                )
            except AgentCapabilityPolicyError as exc:
                return {
                    "ok": False,
                    "error": exc.reason,
                    "status": 400,
                }
            selected_properties = resident_selection_properties(
                selected_properties,
                selection=aligned,
            )
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
            resource_acceptance=preserve_descriptor_acceptance(
                existing.resource_acceptance,
                next_resource_acceptance(
                    resources=selected_resource_grants,
                    row_for=lambda resource: self._configured_resource(
                        resource,
                        config=catalog_config,
                    ),
                    catalog_version=catalog_version,
                    selected_operations=selected_resource_operations,
                    previous=existing.resource_acceptance,
                    accepted_operations=accepted_operations,
                ),
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
        if _record_transform is not None:
            updated = _record_transform(existing, updated)
        del remaining
        try:
            before_commit, issuer_request = await self._issuer_before_commit(
                existing, user, action="update",
                candidate=card_authority_from_record(updated).to_dict(),
                request_id=_issuer_request_id, context_ref=_issuer_context_ref,
                decision=_issuer_decision,
            )
        except IssuerWriteRefused as exc:
            return exc.to_dict()
        # W578: a Card bound to a Control whose kind has a registered caller
        # policy is decided by that policy, before and inside the commit, when
        # the issuer gate does not already govern it.
        caller_request = None
        if before_commit is None:
            try:
                before_commit, caller_request = await caller_writer_before_commit(
                    getattr(self, "_caller_writers", None), card_authority_from_record(existing),
                    actor_subject=_clean(_caller_actor_subject) or self._caller_actor_subject(user),
                    action=_caller_write_action,
                    candidate=card_authority_from_record(updated).to_dict(),
                    request_id=_clean(_issuer_request_id) or secrets.token_urlsafe(18),
                    context_ref=_issuer_context_ref,
                )
            except CallerWriteRefused as exc:
                return exc.to_dict()
        gate_digest = (issuer_request or caller_request).change_digest if before_commit is not None else ""
        try:
            guard = {"before_commit": before_commit, "expected_issuer_digest": gate_digest} if before_commit is not None else {}
            await self._persist_record(updated, expected_revision=existing.card_revision, **guard)
        except (IssuerWriteRefused, CallerWriteRefused) as exc:
            outcome = await self._issuer_outcome(issuer_request, state="refused", card_revision=existing.card_revision)
            outcome.update(await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                                      state="refused", card_revision=existing.card_revision))
            return {**exc.to_dict(), **outcome}
        except CardServingUnavailable as exc:
            outcome = await self._issuer_outcome(issuer_request, state="committed", card_revision=updated.card_revision)
            outcome.update(await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                                      state="committed", card_revision=updated.card_revision))
            return {**_serving_state_unavailable(exc), **outcome}
        except (CardUnavailable, CardConflict, CardCommitFailed) as exc:
            outcome = await self._issuer_outcome(issuer_request, state="refused", card_revision=existing.card_revision)
            outcome.update(await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                                      state="refused", card_revision=existing.card_revision))
            return {
                "ok": False,
                "error": "delegated_card_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
                **outcome,
            }
        outcome = await self._issuer_outcome(issuer_request, state="committed", card_revision=updated.card_revision)
        outcome.update(await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                                  state="committed", card_revision=updated.card_revision))
        await self.notify_change(
            _clean(_notification_subject) or grantor_subject,
            action="updated",
            access=updated.to_public_dict(),
        )
        saved = updated.to_public_dict()
        saved["catalog_drift"] = card_drift(card=updated, active=active, baseline=active)
        return {"ok": True, "access": saved, "pruned": reconciled.to_public_dict(), **outcome}

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
            await self._persist_record(record, expected_revision=target_revision,
                                       caller_write=CallerWrite("fold", self._caller_actor_subject(user)))
        except CallerWriteRefused as exc:
            return exc.to_dict()
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
        grantor_authority: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Mint and bind one reusable bearer to live Card authority.

        Creation, resident-profile folding, renewal, and descriptor-synced
        agents all use this path. The access binding stores a pointer to the
        Card, so later Card revisions change the bearer's effective authority
        without copying or reissuing the credential.
        """
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
        if not access_token:
            raise RuntimeError("delegated_card_credential_missing")
        expires_in = int(minted.get("expires_in") or ttl)
        if grantor_authority is None:
            inventory = await self._available_inventory(
                user,
                requested_grants=grants,
            )
            selected_grantor_authority = _grantor_authority(
                user,
                grants=grants,
                inventory=inventory,
            )
        else:
            selected_grantor_authority = copy.deepcopy(
                dict(grantor_authority)
            )
        await self._store.bind_access_grant(
            access_token,
            list(operations),
            expires_in,
            credential=credential.to_dict(),
            resource_grants={k: list(v) for k, v in resource_grants.items()},
            resource_operations={k: list(v) for k, v in resource_operations.items()},
            grantor_authority=selected_grantor_authority,
            delegation_edges=list(
                selected_grantor_authority.get("delegation_edges") or []
            ),
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
        _delegable_grants: Iterable[str] | None = None,
        _platform_admin: bool | None = None,
    ) -> dict[str, Any]:
        view = (await self._card_view(record, state=state)).to_dict()
        try:
            drift = await self._catalog_drift(
                [record], owner_subject=record.grantor_subject
            )
            view["catalog_drift"] = drift.get(record.access_id, {})
            options = await self.resource_options(
                user,
                _delegable_grants=_delegable_grants,
                _platform_admin=_platform_admin,
            )
            options = _descriptor_control_resource_options(
                record.properties,
                options,
            )
            view["resource_offers"] = compatible_resource_offers(
                card_resources=self._card_resource_keys(record.resource_grants),
                card_identity_scope=record.identity_scope,
                options=options,
                platform_admin=(
                    _is_platform_admin(user)
                    if _platform_admin is None
                    else bool(_platform_admin)
                ),
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
        selected_resource_grants: Mapping[str, Any] | None = None,
        selected_resource_operations: Mapping[str, Any] | None = None,
        selected_named_service_operations: Mapping[str, Any] | str | None = None,
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
            selected_resource_grants=selected_resource_grants,
            selected_resource_operations=selected_resource_operations,
            selected_named_service_operations=selected_named_service_operations,
            properties=properties,
            issuer_label=issuer_label,
            manage_url=manage_url,
        )

    async def update_agent_capability_selection(
        self,
        user: Mapping[str, Any],
        *,
        access_id: str,
        selected_capabilities: Mapping[str, Any] | None,
        expected_card_revision: int | None,
        reset_to_control_defaults: bool = False,
    ) -> dict[str, Any]:
        """Replace the visible selection on one resident Agent Card."""

        from connection_hub.delegated_credentials.agent_capability_sync import (
            update_agent_capability_selection,
        )

        return await update_agent_capability_selection(
            self,
            user,
            access_id=access_id,
            selected_capabilities=selected_capabilities,
            expected_card_revision=expected_card_revision,
            reset_to_control_defaults=reset_to_control_defaults,
        )

    async def control_card_get(
        self,
        user: Mapping[str, Any],
        *,
        control_id: str,
        _delegable_grants: Iterable[str] | None = None,
        _platform_admin: bool | None = None,
    ) -> dict[str, Any]:
        """Read one credentialless Control Card owned by this user.

        ``_delegable_grants`` and ``_platform_admin`` are for the project path
        (W379): the Card is read under its creator, and what it may take is
        offered by the reading person's own role.
        """

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
                record = await self._ensure_control_snapshot(record, actor_subject=self._caller_actor_subject(user))
            except CardUnavailable as exc:
                return {
                    "ok": False,
                    "error": "control_card_snapshot_unavailable",
                    "reason": exc.reason,
                    "retryable": True,
                    "status": 503,
                }
        card = await self._control_card_public_view(
            user,
            record,
            state=state,
            _delegable_grants=_delegable_grants,
            _platform_admin=_platform_admin,
        )
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
        initial_profile: str = "",
    ) -> dict[str, Any]:
        """Create or return one catalog-driven credentialless Control Card.

        An optional delegated Card supplies the initially checked values only.
        The Control Card's complete option set and every later save are
        resolved against the catalog.

        ``initial_profile`` instead starts it from a descriptor authorization
        profile: what first consent proposes for that profile, on every
        catalog resource that declares it (W377). No other Card is read, so an
        issuer can create its Control Card before any Card exists. Every
        answer names what the Card started from in ``started_from``, so a
        caller can refuse a Card an older Connection Hub made empty. Such a
        Card, never started, is started from the profile when it is asked
        for again.

        ``basis_access_id`` is accepted only for callers staged before the
        field was named accurately.
        """

        grantor_subject = _subject_from_user(user)
        if not grantor_subject:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}
        refusal = _delegate_mutation_refusal(user)
        if refusal is not None:
            return refusal
        initial_selection_id = _clean(initial_selection_access_id) or _clean(
            basis_access_id
        )
        profile_name = _clean(initial_profile).lower()
        if initial_selection_id and profile_name:
            return {
                "ok": False,
                "error": "control_card_initial_selection_ambiguous",
                "status": 400,
            }
        try:
            control_id = control_card_id_for_issuer(
                issuer_kind,
                issuer_ref,
                grantor_subject=grantor_subject,
            )
        except ControlCardError as exc:
            return {"ok": False, "error": exc.reason, "status": 400}
        # W578: a managed project's P is created only through its lifecycle
        # plan; an existing one is neither started nor repaired here.
        if self._managed_project_control(access_id=control_id, issuer_kind=issuer_kind, issuer_ref=issuer_ref,
                                         grantor_subject=grantor_subject):
            return self._managed_direct_write_refused()
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
            if profile_name and _control_card_unstarted(record):
                return await self._start_control_card(user, record, profile_name)
            try:
                record = await self._ensure_control_snapshot(record, actor_subject=self._caller_actor_subject(user))
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
                "started_from": _control_card_started_from(record),
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

        profile_start: dict[str, Any] | None = None
        if profile_name:
            profile_start = _profile_selection(profile_name, config=catalog_config)
            if profile_start is None:
                return {
                    "ok": False,
                    "error": "control_card_initial_profile_not_declared",
                    "status": 409,
                    "profile": profile_name,
                }
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
            created_at = int(time.time())
            authority = build_application_control(
                project_ref=issuer_ref,
                holder_subject=grantor_subject,
                catalog_version=catalog_version,
                initial_selection=(
                    card_authority_from_record(initial_selection)
                    if initial_selection is not None
                    else None
                ),
                issuer_kind=issuer_kind,
                issuer_label=issuer_label,
                manage_url=manage_url,
                properties=properties,
                composition_mode=composition_mode,
                profile_marker=(
                    {"profile": profile_name, "catalog_version": catalog_version}
                    if profile_start is not None else None
                ),
                now=created_at,
            )
            record = record_from_card(authority)
            pruned: dict[str, Any] = {
                "resources": [],
                "claims": [],
                "named_service_operations": [],
            }
            start = (
                {
                    "resource_grants": initial_selection.resource_grants,
                    "resource_operations": initial_selection.resource_operations,
                    "named_service_operations": (
                        initial_selection.named_service_operations.to_stored()
                    ),
                    "account_scope": initial_selection.account_scope,
                }
                if initial_selection is not None
                else profile_start
            )
            if start is not None:
                resolved = await self._resolve_card_authority(
                    user=user,
                    existing=record,
                    active=active,
                    resource_grants=start["resource_grants"],
                    resource_operations=start["resource_operations"],
                    operations=(),
                    named_service_operations=start["named_service_operations"],
                    account_scope=start["account_scope"],
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
                authority = build_application_control(
                    project_ref=issuer_ref,
                    holder_subject=grantor_subject,
                    catalog_version=catalog_version,
                    issuer_kind=issuer_kind,
                    base=card_authority_from_record(record),
                    resolved=resolved,
                    resource_row_for=lambda resource: self._configured_resource(
                        resource, config=catalog_config
                    ),
                )
            record = record_from_card(authority)
            await self._persist_record(record, expected_revision=0,
                                       caller_write=CallerWrite("create", self._caller_actor_subject(user)))
        except CallerWriteRefused as exc:
            return exc.to_dict()
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
            "started_from": _control_card_started_from(record),
            "pruned": pruned,
        }

    async def _start_control_card(
        self,
        user: Mapping[str, Any],
        record: AutomationAccessRecord,
        profile: str,
    ) -> dict[str, Any]:
        """Start a Card that was created empty from ``profile``, once (W377).

        A Connection Hub that predates ``initial_profile`` ignored it and made
        the Card with nothing selected. Its id is fixed per issuer and person,
        so it cannot be replaced; the first create that names the profile
        starts it through the ordinary Control Card edit.
        """

        try:
            active = await self._active_catalog()
            catalog_config = await self._catalog_config(
                active,
                owner_subject=record.grantor_subject,
            )
        except CatalogUnavailable as exc:
            return {
                "ok": False,
                "error": "delegated_catalog_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        start = _profile_selection(profile, config=catalog_config)
        if start is None:
            return {
                "ok": False,
                "error": "control_card_initial_profile_not_declared",
                "status": 409,
                "profile": profile,
            }
        started_from = {"profile": profile, "catalog_version": self._version_of(active)}

        def _stamp(previous: Any, candidate: Any) -> Any:
            provenance = dict(getattr(candidate, "provenance", None) or {})
            provenance["control_card_initial_selection"] = dict(started_from)
            return replace_fields(candidate, provenance=provenance)

        updated = await self.control_card_update(
            user,
            control_id=record.access_id,
            resource_grants=start["resource_grants"],
            resource_operations=start["resource_operations"],
            named_service_operations=start["named_service_operations"],
            account_scope={},
            expected_card_revision=record.card_revision,
            _record_transform=_stamp,
        )
        if updated.get("ok") is not True:
            return updated
        return {
            "ok": True,
            "created": False,
            "started": True,
            "control_card": updated.get("control_card"),
            "authority": updated.get("authority"),
            "started_from": started_from,
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
        request_id: str | None = None,
        _delegable_grants: Iterable[str] | None = None,
        _record_transform: Callable[
            [AutomationAccessRecord, AutomationAccessRecord],
            AutomationAccessRecord,
        ]
        | None = None,
        _platform_admin: bool | None = None,
    ) -> dict[str, Any]:
        """Edit a Control Card through the ordinary catalog-aware Card path.

        ``_delegable_grants`` and ``_record_transform`` are for the project
        path (W260, ``project_control_card_access``): the edit is bounded by
        the acting person's grants and stamped with who made it, while the
        Card stays stored under its creator.
        """

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
        refused = self._managed_project_control_refused(existing)
        if refused is not None:
            # W638: a managed P's Save goes through the project's own transaction.
            forwarded = await self._forward_managed_project_control(user, record=existing, request_id=request_id,
                changes={"resource_grants": resource_grants, "resource_operations": resource_operations,
                         "named_service_operations": named_service_operations, "account_scope": account_scope,
                         "properties": properties, "composition_mode": composition_mode, "label": label,
                         "expected_card_revision": expected_card_revision})
            return forwarded if forwarded is not None else refused
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
            _delegable_grants=_delegable_grants,
            _record_transform=_record_transform,
            _platform_admin=_platform_admin,
        )
        if updated.get("ok") is not True:
            return updated
        result = await self.control_card_get(
            user,
            control_id=existing.access_id,
            _delegable_grants=_delegable_grants,
            _platform_admin=_platform_admin,
        )
        if updated.get("pruned") is not None:
            result["pruned"] = updated["pruned"]
        return result

    async def project_person_control_get(
        self,
        user: Mapping[str, Any],
        *,
        project_ref: str,
        target_subject: str = "",
        invitation_ref: str = "",
        control_id: str = "",
        request_id: str,
    ) -> dict[str, Any]:
        """Read one live-person or pending-invitation project Control Card."""

        actor_subject = _subject_from_user(user)
        if not actor_subject:
            return {
                "ok": False,
                "error": "delegated_access_requires_authenticated_user",
            }
        if _clean(invitation_ref):
            return await self._project_invitation_controls.get(
                viewer=await self._viewer_authority(user),
                actor_subject=actor_subject,
                project_ref=project_ref,
                invitation_ref=invitation_ref,
                control_id=control_id,
                request_id=request_id,
            )
        return await self._project_person_controls.get(
            viewer=await self._viewer_authority(user),
            actor_subject=actor_subject,
            project_ref=project_ref,
            target_subject=target_subject,
            request_id=request_id,
        )

    async def project_person_control_create(
        self,
        user: Mapping[str, Any],
        *,
        project_ref: str,
        target_subject: str = "",
        invitation_ref: str = "",
        target_email: str = "",
        request_id: str,
        resource_grants: Mapping[str, Any] | None = None,
        resource_operations: Mapping[str, Any] | None = None,
        named_service_operations: Mapping[str, Any] | str | None = None,
        account_scope: Mapping[str, Any] | None = None,
        properties: Mapping[str, Any] | None = None,
        composition_mode: str = CONTROL_COMPOSITION_AND,
        label: str = "",
        manage_url: str = "",
        migration: bool = False,
        project_creation: bool = False,
    ) -> dict[str, Any]:
        """Create a live-person or pending-invitation project Control Card."""

        refused = self._managed_direct_write_refused()
        if refused is not None:
            return refused
        actor_subject = _subject_from_user(user)
        if not actor_subject:
            return {
                "ok": False,
                "error": "delegated_access_requires_authenticated_user",
            }
        if _clean(invitation_ref):
            if migration or project_creation:
                return {
                    "ok": False,
                    "error": "project_invitation_control_seed_origin_invalid",
                    "status": 400,
                }
            return await self._project_invitation_controls.create(
                viewer=await self._viewer_authority(user),
                actor_subject=actor_subject,
                project_ref=project_ref,
                invitation_ref=invitation_ref,
                target_email=target_email,
                request_id=request_id,
                resource_grants=resource_grants,
                resource_operations=resource_operations,
                named_service_operations=named_service_operations,
                account_scope=account_scope,
                properties=properties,
                composition_mode=composition_mode,
                label=label,
                manage_url=manage_url,
            )
        return await self._project_person_controls.create(
            viewer=await self._viewer_authority(user),
            actor_subject=actor_subject,
            project_ref=project_ref,
            target_subject=target_subject,
            request_id=request_id,
            resource_grants=resource_grants,
            resource_operations=resource_operations,
            named_service_operations=named_service_operations,
            account_scope=account_scope,
            properties=properties,
            composition_mode=composition_mode,
            label=label,
            manage_url=manage_url,
            migration=migration,
            project_creation=project_creation,
        )

    async def project_person_control_update(
        self,
        user: Mapping[str, Any],
        *,
        project_ref: str,
        target_subject: str = "",
        invitation_ref: str = "",
        control_id: str = "",
        request_id: str,
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
        """Replace one live or pending project selection under host policy."""

        refused = self._managed_direct_write_refused()
        if refused is not None and _clean(invitation_ref) and self._managed_card_edit_forwarder(project_ref) is not None:
            # W638: a pending invitation's Control, edited by the project's own transaction.
            from .managed_card_edit_forward import ManagedCardEditError, managed_card_location
            try:
                access_id, grantor = managed_card_location(
                    "invitation_control", project_ref=project_ref, ref=_clean(invitation_ref))
            except ManagedCardEditError as exc:
                return exc.to_dict()
            stored = await self._load_record(access_id, grantor_subject=grantor)
            forwarded = None if stored is None else await self._forward_agent_card_edit(
                user, record=stored, project_ref=project_ref, request_id=request_id, kind="invitation_control",
                changes={"resource_grants": resource_grants, "resource_operations": resource_operations,
                         "named_service_operations": named_service_operations, "account_scope": account_scope,
                         "properties": properties, "composition_mode": composition_mode, "label": label,
                         "expected_card_revision": expected_card_revision})
            return refused if forwarded is None else forwarded
        if refused is not None:
            forwarded = await self._forward_person_control_edit(
                user, project_ref=project_ref, target_subject=target_subject, request_id=request_id,
                selection={field: value for field, value in (
                    ("resource_grants", resource_grants), ("resource_operations", resource_operations),
                    ("named_service_operations", named_service_operations), ("account_scope", account_scope),
                ) if value is not None},
                expected_card_revision=expected_card_revision, properties=properties,
                composition_mode=composition_mode)
            if forwarded is None:
                return refused
            return await self._with_current_person_control(
                user, forwarded, project_ref=project_ref, target_subject=target_subject, request_id=request_id)
        actor_subject = _subject_from_user(user)
        if not actor_subject:
            return {
                "ok": False,
                "error": "delegated_access_requires_authenticated_user",
            }
        if _clean(invitation_ref):
            return await self._project_invitation_controls.update(
                viewer=await self._viewer_authority(user),
                actor_subject=actor_subject,
                project_ref=project_ref,
                invitation_ref=invitation_ref,
                control_id=control_id,
                request_id=request_id,
                resource_grants=resource_grants,
                resource_operations=resource_operations,
                named_service_operations=named_service_operations,
                account_scope=account_scope,
                properties=properties,
                composition_mode=composition_mode,
                label=label,
                expected_card_revision=expected_card_revision,
                expected_catalog_version=expected_catalog_version,
                accepted_operations=accepted_operations,
            )
        return await self._project_person_controls.update(
            viewer=await self._viewer_authority(user),
            actor_subject=actor_subject,
            project_ref=project_ref,
            target_subject=target_subject,
            request_id=request_id,
            resource_grants=resource_grants,
            resource_operations=resource_operations,
            named_service_operations=named_service_operations,
            account_scope=account_scope,
            properties=properties,
            composition_mode=composition_mode,
            label=label,
            expected_card_revision=expected_card_revision,
            expected_catalog_version=expected_catalog_version,
            accepted_operations=accepted_operations,
        )

    async def project_person_control_revoke(
        self,
        user: Mapping[str, Any],
        *,
        project_ref: str,
        target_subject: str = "",
        invitation_ref: str = "",
        control_id: str = "",
        request_id: str,
    ) -> dict[str, Any]:
        """Revoke one live-person or pending-invitation project Card."""

        refused = self._managed_direct_write_refused()
        if refused is not None:
            return refused
        actor_subject = _subject_from_user(user)
        if not actor_subject:
            return {
                "ok": False,
                "error": "delegated_access_requires_authenticated_user",
            }
        if _clean(invitation_ref):
            return await self._project_invitation_controls.revoke(
                viewer=await self._viewer_authority(user),
                actor_subject=actor_subject,
                project_ref=project_ref,
                invitation_ref=invitation_ref,
                control_id=control_id,
                request_id=request_id,
            )
        return await self._project_person_controls.revoke(
            viewer=await self._viewer_authority(user),
            actor_subject=actor_subject,
            project_ref=project_ref,
            target_subject=target_subject,
            request_id=request_id,
        )

    async def project_person_control_bind_project(
        self,
        user: Mapping[str, Any],
        *,
        project_ref: str,
        target_subject: str,
        request_id: str,
    ) -> dict[str, Any]:
        """W502 repair: bind one person's project Control under the project's Control Card.

        The project host decides the operation and names the Control Card;
        the result names this person's outcome for a migration report.
        """

        refused = self._managed_direct_write_refused()
        if refused is not None:
            return refused
        actor_subject = _subject_from_user(user)
        if not actor_subject:
            return {
                "ok": False,
                "error": "delegated_access_requires_authenticated_user",
            }
        return await self._project_person_controls.bind_project_control(
            viewer=await self._viewer_authority(user),
            actor_subject=actor_subject,
            project_ref=project_ref,
            target_subject=target_subject,
            request_id=request_id,
        )

    async def project_invitation_pending_revision(
        self,
        user: Mapping[str, Any],
        *,
        project_ref: str,
        invitation_ref: str,
        control_id: str,
    ) -> dict[str, Any]:
        """W502 join: the signed-in invitee reads their pending invitation Card's revision and state.

        A read under bind's own authority (the board's binding resolver for
        this session's verified email); no Card is written.
        """

        actor_subject = _subject_from_user(user)
        if not actor_subject:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}
        return await self._project_invitation_controls.pending_revision(
            actor_subject=actor_subject,
            project_ref=project_ref,
            invitation_ref=invitation_ref,
            control_id=control_id,
        )

    async def project_person_control_bind_invitation(
        self,
        user: Mapping[str, Any],
        *,
        project_ref: str,
        invitation_ref: str,
        control_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Bind a provider-verified pending Card to the signed-in person."""

        refused = self._managed_direct_write_refused()
        if refused is not None:
            return refused
        actor_subject = _subject_from_user(user)
        if not actor_subject:
            return {
                "ok": False,
                "error": "delegated_access_requires_authenticated_user",
            }
        return await self._project_invitation_controls.bind(
            actor_subject=actor_subject,
            project_ref=project_ref,
            invitation_ref=invitation_ref,
            control_id=control_id,
            request_id=request_id,
        )

    async def project_person_my_card_seed(
        self,
        user: Mapping[str, Any],
        *,
        project_ref: str,
        target_subject: str,
        request_id: str,
        resource_grants: Mapping[str, Any],
        resource_operations: Mapping[str, Any],
        named_service_operations: Mapping[str, Any] | str | None = None,
        account_scope: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Seed an existing person's untouched My Card under project policy."""

        refused = self._managed_direct_write_refused()
        if refused is not None:
            return refused
        actor_subject = _subject_from_user(user)
        if not actor_subject:
            return {
                "ok": False,
                "error": "delegated_access_requires_authenticated_user",
            }
        return await self._project_person_controls.seed_my_card(
            actor_subject=actor_subject,
            project_ref=project_ref,
            target_subject=target_subject,
            request_id=request_id,
            resource_grants=resource_grants,
            resource_operations=resource_operations,
            named_service_operations=named_service_operations,
            account_scope=account_scope,
        )

    async def project_person_my_card_settings_get(
        self,
        user: Mapping[str, Any],
        *,
        project_ref: str,
        target_subject: str = "",
        request_id: str,
    ) -> dict[str, Any]:
        """Read the person-owned settings on a My Card (W371): GitHub link, commit email."""

        actor_subject = _subject_from_user(user)
        if not actor_subject:
            return {
                "ok": False,
                "error": "delegated_access_requires_authenticated_user",
            }
        return await self._project_person_controls.my_card_person_properties(
            actor_subject=actor_subject,
            project_ref=project_ref,
            target_subject=target_subject,
            request_id=request_id,
        )

    async def project_person_my_card_settings_set(
        self,
        user: Mapping[str, Any],
        *,
        project_ref: str,
        changes: Mapping[str, Any],
        target_subject: str = "",
        request_id: str,
        expected_card_revision: int | None = None,
    ) -> dict[str, Any]:
        """Set or clear person-owned settings on a My Card (W371)."""

        actor_subject = _subject_from_user(user)
        if not actor_subject:
            return {
                "ok": False,
                "error": "delegated_access_requires_authenticated_user",
            }
        return await self._project_person_controls.set_my_card_person_properties(
            actor_subject=actor_subject,
            project_ref=project_ref,
            changes=changes,
            target_subject=target_subject,
            request_id=request_id,
            expected_card_revision=expected_card_revision,
        )

    async def project_operation_authorize(
        self,
        user: Mapping[str, Any],
        *,
        project_ref: str,
        resource: str,
        operation: str,
        required_grants: Any = (),
        request_resource: str = "",
        surface: str = "application",
    ) -> dict[str, Any]:
        """Evaluate one signed-in person's operation against the live edge."""

        person_subject = _subject_from_user(user)
        if not person_subject:
            return {
                "ok": False,
                "error": "delegated_access_requires_authenticated_user",
            }
        request = ProjectOperationRequest(
            person_subject=person_subject,
            project_ref=project_ref,
            resource=resource,
            operation=operation,
            required_grants=required_grants,
            request_resource=request_resource,
            surface=surface,
        )
        try:
            decision = await self._project_person_controls.authorize_operation(request)
        except ProjectIdentityLifecycleError as exc:
            return {
                "ok": False,
                "error": exc.reason,
                "status": 409,
            }
        except CardUnavailable as exc:
            return {
                "ok": False,
                "error": "project_identity_edge_unavailable",
                "reason": exc.reason,
                "retryable": True,
                "status": 503,
            }
        return {"ok": True, **decision.to_dict()}

    async def project_operations_authorize(
        self,
        user: Mapping[str, Any],
        *,
        project_ref: str,
        resource: str,
        operations: Any,
        surface: str = "application",
    ) -> dict[str, Any]:
        """Evaluate many operations of one signed-in person in ONE exchange (the chain is resolved once).

        Each entry is ``{"operation", "required_grants"?, "request_resource"?}``; ``decisions`` answers them
        in order, each exactly the single-operation decision for that entry alone.
        """

        person_subject = _subject_from_user(user)
        if not person_subject:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}
        if (not isinstance(operations, (list, tuple)) or not operations
                or any(not isinstance(entry, Mapping) for entry in operations)):
            return {"ok": False, "error": "operation_batch_invalid", "status": 400}
        requests = [
            ProjectOperationRequest(
                person_subject=person_subject,
                project_ref=project_ref,
                resource=resource,
                operation=str(entry.get("operation") or "").strip(),
                required_grants=entry.get("required_grants", ()),
                request_resource=str(entry.get("request_resource") or "").strip(),
                surface=surface,
            )
            for entry in operations
        ]
        try:
            decisions = await self._project_person_controls.authorize_operations(requests)
        except ProjectIdentityLifecycleError as exc:
            # The batch bound is the lifecycle's (refused before any read): a malformed request, 400.
            status = 400 if exc.reason == "operation_batch_invalid" else 409
            return {"ok": False, "error": exc.reason, "status": status}
        except CardUnavailable as exc:
            return {"ok": False, "error": "project_identity_edge_unavailable", "reason": exc.reason,
                    "retryable": True, "status": 503}
        return {"ok": True, "decisions": [decision.to_dict() for decision in decisions]}

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
        _control_holder: str = "",
        _record_transform: Callable[[Any, Any], Any] | None = None,
    ) -> dict[str, Any]:
        """Attach or compare-and-replace one current Control Card.

        Replacement is one caller-Card revision. It is used when an issuer
        moves an existing binding to a new Control Card without an interval in
        which no rule applies.

        ``_control_holder`` is for the project path only (W260,
        ``project_control_card_access``): the project host decided the attach
        and named the Control Card's creator, who holds it; the binding
        records that holder so the runtime resolves the Control Card under it.
        No operation passes it from a request, so a caller never names a
        holder: the plain attach binds only the caller's own Control Card.
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
        control_holder = _clean(_control_holder) or grantor_subject
        if not selected_access_id or not selected_control_id:
            return {"ok": False, "error": "control_card_binding_invalid", "status": 400}
        try:
            record = await self._load_record(
                selected_access_id,
                grantor_subject=grantor_subject,
            )
            control = await self._resolve_control_record(
                selected_control_id,
                grantor_subject=control_holder,
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
        refused = self._managed_project_control_refused(record)
        if refused is not None:
            return refused
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
            refusal = self._managed_project_binding_refused(record) or _held_binding_refusal(
                record, caller_is_project=bool(_clean(_control_holder)))
            if refusal is not None:
                return refusal
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
        if control.grantor_subject != control_holder:
            return {"ok": False, "error": "control_card_grantor_mismatch", "status": 403}
        refused = self._managed_project_control_refused(control)
        if refused is not None and not _legacy_unbound_c_to_its_root_p(record, control):
            return refused
        binding = ControlCardBinding(
            control_id=control.access_id,
            issuer_ref=control.issuer_ref,
            issuer_kind=control.issuer_kind,
            issuer_label=control.issuer_label,
            manage_url=control.manage_url,
            control_revision=control.card_revision,
            # Recorded only when the holder is not the Card's own grantor.
            holder_subject=control_holder if control_holder != grantor_subject else "",
        )
        try:
            resolved_control, _ = await self._compose_with_control(dataclasses.replace(record, control_card=binding))
            if resolved_control is None:
                raise ControlCardMismatch("control_card_unresolvable")
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
        if _record_transform is not None:
            updated = _record_transform(record, updated)
        # W578: a bound attach/detach is decided by the binding's policy.
        try:
            before_commit, caller_request = await caller_writer_before_commit(
                getattr(self, "_caller_writers", None), card_authority_from_record(record),
                actor_subject=self._caller_actor_subject(user), action="attach",
                candidate=card_authority_from_record(updated).to_dict(),
                request_id=secrets.token_urlsafe(18), binding=binding_of(card_authority_from_record(record)) if record.control_card is not None else binding_of(card_authority_from_record(updated)),
            )
        except CallerWriteRefused as exc:
            return exc.to_dict()
        guard = ({"before_commit": before_commit, "expected_issuer_digest": caller_request.change_digest}
                 if before_commit is not None else {})
        try:
            await self._persist_record(updated, expected_revision=record.card_revision, **guard)
        except CallerWriteRefused as exc:
            outcome = await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                                 state="refused", card_revision=record.card_revision)
            return {**exc.to_dict(), **outcome}
        except CardServingUnavailable as exc:
            outcome = await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                                 state="committed", card_revision=updated.card_revision)
            return {**_serving_state_unavailable(exc), **outcome}
        except (CardUnavailable, CardConflict, CardCommitFailed) as exc:
            outcome = await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                                 state="refused", card_revision=record.card_revision)
            return {
                "ok": False,
                "error": "delegated_card_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
                **outcome,
            }
        await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                   state="committed", card_revision=updated.card_revision)
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
        refused = self._managed_project_control_refused(record)
        if refused is not None:
            return refused
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
        _record_transform: Callable[[Any, Any], Any] | None = None,
        _through_project: bool = False,
    ) -> dict[str, Any]:
        """Unlink the named Control Card so the caller Card applies alone.

        A binding whose Control Card another person holds (a project's, W260)
        is removed only through the project path, which asked the project host
        (``_through_project``); the agent's owner cannot drop it here.
        """

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
        refusal = (self._managed_project_control_refused(record) or self._managed_project_binding_refused(record)
                   or _held_binding_refusal(record, caller_is_project=_through_project))
        if refusal is not None:
            return refusal
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
        if _record_transform is not None:
            updated = _record_transform(record, updated)
        # W578: a bound attach/detach is decided by the binding's policy.
        try:
            before_commit, caller_request = await caller_writer_before_commit(
                getattr(self, "_caller_writers", None), card_authority_from_record(record),
                actor_subject=self._caller_actor_subject(user), action="detach",
                candidate=card_authority_from_record(updated).to_dict(),
                request_id=secrets.token_urlsafe(18), binding=None,
            )
        except CallerWriteRefused as exc:
            return exc.to_dict()
        guard = ({"before_commit": before_commit, "expected_issuer_digest": caller_request.change_digest}
                 if before_commit is not None else {})
        try:
            await self._persist_record(updated, expected_revision=record.card_revision, **guard)
        except CallerWriteRefused as exc:
            outcome = await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                                 state="refused", card_revision=record.card_revision)
            return {**exc.to_dict(), **outcome}
        except CardServingUnavailable as exc:
            outcome = await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                                 state="committed", card_revision=updated.card_revision)
            return {**_serving_state_unavailable(exc), **outcome}
        except (CardUnavailable, CardConflict, CardCommitFailed) as exc:
            outcome = await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                                 state="refused", card_revision=record.card_revision)
            return {
                "ok": False,
                "error": "delegated_card_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
                **outcome,
            }
        await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                   state="committed", card_revision=updated.card_revision)
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
                    control, composed = await self._compose_with_control(record)
                    if control is None or composed is None:
                        raise ControlCardMismatch("control_card_unresolvable")
                    record = record_from_card(composed)
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
        # An authorization profile chooses what the consent page pre-ticks.
        # The person's selection is the Card: any catalog service this client
        # reaches, checked below exactly as every Card selection is (W272).
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

        submitted = self._oauth_policy_selection(resource_operations, invocation_policies)
        if not submitted:
            return []

        from connection_hub.invocation_policy import SURFACE_OUTER, InvocationAuthority

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

    def _oauth_policy_selection(
        self, resource_operations: Mapping[str, Any], invocation_policies: Mapping[str, Any],
    ) -> dict[str, dict[str, str]]:
        """The consent's outer-operation policies, validated: one per selected operation (ValueError).

        ``apply_oauth_invocation_policies`` and the W585 issuance plan use the
        same rules, so the one-decision path admits exactly what the old route did.
        """

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
            return {}
        if self._invocation_policies is None:
            raise ValueError("invocation policy service is unavailable")

        from connection_hub.invocation_policy import POLICY_ALWAYS, POLICY_ONCE

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
        return submitted

    async def _oauth_grant_record(
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
        now: int | None = None,
        _application_write: bool = False,
    ) -> tuple[AutomationAccessRecord, int, bool, dict[str, Any]] | None:
        """The Card ``record_oauth_grant`` writes: (record, committed revision, initial consent, account scope).

        Computed, not written. W603's original issuance plans the same candidate
        from the same inputs, so both paths grant exactly the same authority.
        ``now`` is the issuance instant (the database clock for W603).
        """
        grantor = _clean(grantor_subject)
        client = _clean(client_id)
        if not grantor or not client:
            return None
        if grantor.startswith("integration:"):
            # W585 gate (d): the same rule as _delegate_mutation_refusal. A
            # delegated bearer that approved a consent is not a grantor, so no
            # Card is created or changed under an "integration:" identity
            # (outside every human's Control chain). Refused before any effect;
            # the token route withholds the token on CardConflict.
            raise CardConflict("delegated_access_requires_grantor")
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
            _LOGGER.warning(
                "[automation-access] oauth grant card identity refused client=%s error=%s reason=%s",
                client, _clean(identity.get("error")), _clean(identity.get("reason")),
            )
            raise CardConflict(_clean(identity.get("error")) or "card_identity_invalid")
        selected_kind = _clean(identity.get("card_kind"))
        resolved_access_id = _clean(identity.get("access_id"))
        requested_access_id = _clean(access_id)
        if requested_access_id and requested_access_id != resolved_access_id:
            raise CardConflict("card_identity_mismatch")
        access_id = resolved_access_id
        now = int(time.time()) if now is None else int(now)
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
            # A refresh rotation carries the Card's identity scope when its stored refresh record names none
            # (records issued through the original exchange keep no identity scope): a rotation is not a
            # review and must not clear the Card's authority.
            identity_scope=(_clean(identity_scope) or (
                _clean(existing_card.identity_scope) if existing_card is not None and not replace_authority else "")),
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
            if not _application_write:
                # W661 S5 (G2): an OAuth consent never adds or removes an application-managed grant or
                # operation; refused before the Card is persisted (the route then withholds the tokens).
                held_grants = dict(existing_card.resource_grants) if existing_card is not None else {}
                held_operations = dict(existing_card.resource_operations) if existing_card is not None else {}
                changes = managed_grant_changes(
                    authority_config, self._resource_grants(record.resource_grants), self._resource_grants(held_grants))
                changes += managed_operation_changes(
                    authority_config, self._resource_grants(record.resource_operations),
                    self._resource_grants(held_operations))
                if changes:
                    raise CallerWriteRefused(MANAGED_GRANT_NOT_EDITABLE)
        elif not _application_write:
            # L2, decided fail-CLOSED (Main, 2026-10-09 10:39Z): with the catalog unavailable no managed
            # grant is known, so a consent that changes the Card's authority is refused (retryable); a refresh
            # that carries the Card forward unchanged still passes.
            held_grants = self._resource_grants(dict(existing_card.resource_grants)) if existing_card is not None else {}
            held_operations = (self._resource_grants(dict(existing_card.resource_operations))
                               if existing_card is not None else {})
            if (self._resource_grants(dict(record.resource_grants)) != held_grants
                    or self._resource_grants(dict(record.resource_operations)) != held_operations):
                raise ManagedGrantsUnknown()
        return record, existing_card_revision, is_initial_consent, merged_account_scope

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
        _application_write: bool = False,
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
        built = await self._oauth_grant_record(
            grantor_subject=grantor_subject,
            client_id=client_id,
            client_label=client_label,
            scopes=scopes,
            operations=operations,
            resource_grants=resource_grants,
            resource_operations=resource_operations,
            resource=resource,
            access_id=access_id,
            card_kind=card_kind,
            identity_scope=identity_scope,
            access_token=access_token,
            refresh_token=refresh_token,
            account_scope=account_scope,
            named_service_operations=named_service_operations,
            catalog_version=catalog_version,
            client_metadata=client_metadata,
            properties=properties,
            replace_authority=replace_authority,
            expected_card_revision=expected_card_revision,
            _application_write=_application_write,
        )
        if built is None:
            return None
        record, existing_card_revision, is_initial_consent, merged_account_scope = built
        grantor, client, access_id = record.grantor_subject, record.client_id, record.access_id
        # Raises CallerWriteRefused when the binding's policy refuses. The SDK
        # OAuth route then withholds the tokens but does NOT yet revoke them:
        # its _issue_tokens has already bound the access grant and created the
        # refresh token, and only its CardConflict branch revokes (KD 3fc59611,
        # oauth/http/routes.py). Until that route revokes on every withheld
        # branch (Ops D1, SDK scope), no host may bind a caller-writer
        # registry: a refused grant would leave live, unreturned tokens, and a
        # refresh rotation's withheld grant still resolves to the active Card.
        await self._persist_record(record, expected_revision=existing_card_revision,
                                   caller_write=CallerWrite("oauth_grant", grantor))
        _LOGGER.info(
            "[automation-access] oauth grant recorded card=%s client=%s initial=%s "
            "account_scope_providers=%s",
            access_id, client, is_initial_consent,
            sorted(merged_account_scope.keys()) or "-",
        )
        await self.notify_change(grantor, action="granted", access=record.to_public_dict())
        return record

    # ── W603: an original OAuth issuance under ONE Card decision ────────────
    #
    # begin_oauth_issuance plans the Card ``record_oauth_grant`` would write and
    # its two ``credential_issue`` effects, stores that plan once (database
    # clock, first writer wins) and begins the decision from it, so every
    # replay begins the identical decision. The SDK mints and reserves each
    # credential (reserve_oauth_issuance); complete_oauth_issuance prepares,
    # commits and finishes that decision, whose COMMIT activates the reserved
    # credentials. A first consent is a one-member Card group ``create`` (the
    # existing shape for an absent original); a later consent is the existing
    # single-Card transaction with action ``oauth_grant``. Neither is a direct
    # write, and no bearer, code or verifier reaches the Hub.

    def bind_oauth_issuance_store(self, store: Any) -> None:
        """W603: the PostgreSQL OAuth authority holding issuance plans and reservations (composition only)."""
        self._oauth_issuance_store = store

    def _issuance_parts(self) -> tuple[Any, Any, Any, int, Any]:
        from .oauth_issuance import IssuanceRefused

        bound = getattr(self, "_card_coordinator", None)
        store = getattr(self, "_oauth_issuance_store", None)
        if bound is None or store is None:
            raise IssuanceRefused("card_transactions_unavailable", retryable=True)
        coordinator, intents, decisions, ttl = bound
        return coordinator, intents, decisions, ttl, store

    async def begin_oauth_issuance(
        self,
        *,
        grantor_subject: str,
        client_id: str,
        original_request_id: str,
        client_label: str = "",
        scopes: Iterable[str] = (),
        operations: Iterable[str] | None = None,
        resource_grants: Mapping[str, Any] | None = None,
        resource_operations: Mapping[str, Any] | None = None,
        resource: str = "",
        access_id: str = "",
        card_kind: str = "",
        identity_scope: str = "",
        account_scope: Mapping[str, Any] | None = None,
        named_service_operations: Any = None,
        catalog_version: str = "",
        client_metadata: Mapping[str, Any] | None = None,
        properties: Mapping[str, Any] | None = None,
        replace_authority: bool = True,
        expected_card_revision: int | None = None,
        invocation_policies: Mapping[str, Any] | None = None,
    ) -> Any:
        """W603: plan one authorization-code exchange's Card change and begin its ONE decision.

        Returns the ``OAuthIssuancePlan``. ``original_request_id`` is the SDK's
        identity of the original exchange; the same request with the same
        inputs returns the same plan, deadlines included, and with other
        inputs refuses ``issuance_replay_changed``. The inputs are those of
        ``record_oauth_grant``'s consent, and the candidate is computed by the
        same code. Refuses ``card_transactions_unavailable`` (retryable) when
        Card transactions or the issuance store are not bound.

        ``invocation_policies`` (W585 host gap) are the consent's outer-operation
        policies, ``{resource: {operation: "always"|"once"}}``, validated as
        ``apply_oauth_invocation_policies`` does. They become this decision's
        ``invocation_policy`` effects, never a second write. ``None`` (the
        default) is today's issuance: no policy effect, and the original digest
        is unchanged because the field is then absent from it.
        """
        from .oauth.issuance_store import IssuanceStoreRefused
        from .oauth_issuance import IssuanceRefused, decision_request_id, original_input_digest
        from .cards.card_participant import PARTICIPANT

        _coordinator, intents, decisions, ttl, store = self._issuance_parts()
        grantor, client = _clean(grantor_subject), _clean(client_id)
        if (not grantor or not client or type(original_request_id) is not str or not original_request_id
                or len(original_request_id) > 256 or original_request_id != original_request_id.strip()):
            raise IssuanceRefused("issuance_request_invalid")
        record_inputs = {
            "client_label": client_label, "scopes": list(scopes),
            "operations": None if operations is None else list(operations),
            "resource_grants": resource_grants, "resource_operations": resource_operations,
            "resource": resource, "access_id": access_id, "card_kind": card_kind, "identity_scope": identity_scope,
            "account_scope": account_scope, "named_service_operations": named_service_operations,
            "catalog_version": catalog_version, "client_metadata": client_metadata, "properties": properties,
            "replace_authority": bool(replace_authority), "expected_card_revision": expected_card_revision,
        }
        try:
            # Canonical JSON both digests and freezes the inputs (tuples become lists).
            record_inputs = json.loads(json.dumps(record_inputs, sort_keys=True, allow_nan=False))
            policies = None if invocation_policies is None else json.loads(
                json.dumps(invocation_policies, sort_keys=True, allow_nan=False))
            digest_inputs = {"grantor_subject": grantor, "client_id": client, **record_inputs}
            if policies is not None:
                digest_inputs["invocation_policies"] = policies
            input_digest = original_input_digest(digest_inputs)
        except (TypeError, ValueError):
            raise IssuanceRefused("issuance_request_invalid") from None
        request = decision_request_id(scope=f"{PARTICIPANT}:oauth-issuance", grantor_subject=grantor,
                                      client_id=client, original_request_id=original_request_id)
        stored = await store.read_issuance_plan_request(request)
        if stored is None:
            planned = await self._plan_oauth_issuance(store=store, ttl=ttl, request=request,
                                                      input_digest=input_digest, grantor=grantor, client=client,
                                                      record_inputs=record_inputs, invocation_policies=policies)
            try:
                stored = await store.put_issuance_plan(decision_request_id=request,
                                                       original_input_digest=input_digest, plan=planned,
                                                       reserved_until=planned["reserved_until"])
            except IssuanceStoreRefused as exc:
                raise IssuanceRefused(exc.reason) from None
        elif stored["original_input_digest"] != input_digest:
            raise IssuanceRefused("issuance_replay_changed")
        return await self._begin_planned_issuance(stored["plan"], intents=intents, decisions=decisions, store=store)

    async def _plan_oauth_issuance(self, *, store: Any, ttl: int, request: str, input_digest: str, grantor: str,
                                   client: str, record_inputs: Mapping[str, Any],
                                   invocation_policies: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """The immutable plan: the candidate Card, the decision draft, both effects and every deadline."""
        from connection_hub.authority_registry import DELEGATED_CLIENT_AUTHORITY_ID

        from .cards.card_group import group_member, hub_group_participant_input
        from .cards.card_participant import PARTICIPANT, hub_participant_input
        from .cards.participant_effects import MAX_EFFECTS
        from .oauth_issuance import (
            ISSUANCE_DELIVERY_SECONDS, ISSUANCE_PLAN_SCHEMA, ISSUANCE_SLOTS, IssuanceRefused,
            credential_issue_effect, effect_digest,
        )

        now = await store.issuance_clock()
        try:
            built = await self._oauth_grant_record(grantor_subject=grantor, client_id=client, access_token="",
                                                   refresh_token="", now=now, **record_inputs)
        except CardConflict as exc:
            raise IssuanceRefused(str(getattr(exc, "reason", "") or exc)) from None
        if built is None:
            raise IssuanceRefused("issuance_request_invalid")
        record, base_revision, _initial, _scope = built
        candidate = card_authority_from_record(record)
        try:
            await self._refuse_bound_direct_write(record, candidate, expected_revision=base_revision)
        except CallerWriteRefused as exc:
            raise IssuanceRefused(exc.reason) from None
        if self._managed_project_control_refused(record) is not None:
            raise IssuanceRefused("card_transactions_direct_write_refused")
        subject_hash = _subject_key(grantor)
        original = None
        if base_revision > 0:
            card_store = getattr(self._cards(), "card_store", None)
            current = None if card_store is None else await card_store.read_current_authority(
                subject_hash=subject_hash, access_id=candidate.access_id)
            if current is None or current[1].card_revision != base_revision:
                raise IssuanceRefused("card_intent_base_moved", retryable=True)
            original = current[1]
        effects = [credential_issue_effect(access_id=candidate.access_id, slot=slot, expires_at=candidate.expires_at,
                                           card_revision=candidate.card_revision) for slot in ISSUANCE_SLOTS]
        # W606: a re-consent moves the existing handle row through handle_binding (one writer per row);
        # credential_issue writes handle metadata only for a create.
        effects += await self._handle_binding_effects([(original, candidate)])
        if invocation_policies is not None:
            effects += await self._oauth_policy_effects(grantor=grantor, candidate=candidate,
                resource_operations=record_inputs.get("resource_operations") or {},
                invocation_policies=invocation_policies)
        if len(effects) > MAX_EFFECTS:
            # One decision carries at most MAX_EFFECTS; never drop, truncate or split (CodeApp, W585).
            raise IssuanceRefused("issuance_too_many_policies")
        actor_kind = "caller"  # the same enlisted actor record_oauth_grant names (CallerWrite oauth_grant)
        if original is None:
            action = "create"
            participant_input = hub_group_participant_input(
                members=[group_member(original=None, candidate=candidate, action=action)],
                actor_subject=grantor, actor_kind=actor_kind, effects=effects)
        else:
            action = "oauth_grant"
            participant_input = hub_participant_input(
                original=original, candidate=candidate, subject_hash=subject_hash, action=action,
                actor_subject=grantor, actor_kind=actor_kind, effects=effects)
        decision_expires = now + ttl
        delivery_deadline = min(candidate.expires_at, now + ISSUANCE_DELIVERY_SECONDS)
        reserved_until = min(delivery_deadline, decision_expires)
        if reserved_until <= now:
            raise IssuanceRefused("issuance_card_expired")
        return {
            "schema": ISSUANCE_PLAN_SCHEMA,
            "draft": {"replay_scope": f"{PARTICIPANT}:{subject_hash}:{grantor}:oauth-issuance", "request_id": request,
                      "expires_at": decision_expires,
                      "payload": {"participant_inputs": {PARTICIPANT: participant_input}}},
            "intent": {"shape": "group" if original is None else "card", "subject_hash": subject_hash,
                       "original": None if original is None else original.to_dict(),
                       "candidate": candidate.to_dict(), "effects": effects, "action": action,
                       "actor_subject": grantor, "actor_kind": actor_kind},
            "decision_request_id": request, "original_input_digest": input_digest,
            "tenant": store.tenant, "project": store.project, "access_id": candidate.access_id,
            "grantor_subject": grantor, "client_id": client, "credential_issuer": DELEGATED_CLIENT_AUTHORITY_ID,
            "credential_subject": integration_subject(grantor, client_id=client),
            "base_revision": base_revision, "candidate_revision": candidate.card_revision,
            "expires_at": candidate.expires_at, "card_content_hash": candidate.content_hash(),
            "operations": list(candidate.operations),
            "resource_grants": {key: list(items) for key, items in candidate.resource_grants.items()},
            "resource_operations": {key: list(items) for key, items in candidate.resource_operations.items()},
            "delivery_deadline": delivery_deadline, "reserved_until": reserved_until, "slots": list(ISSUANCE_SLOTS),
            "effect_digests": {effect["key"]: effect_digest(effect) for effect in effects},
        }

    async def _oauth_policy_effects(self, *, grantor: str, candidate: Any, resource_operations: Mapping[str, Any],
                                    invocation_policies: Mapping[str, Any]) -> list[dict[str, Any]]:
        """W585: the consent's policies as this decision's existing ``invocation_policy`` effects.

        Each is bound to the candidate Card's own access id and owner, and to
        the policy's current revision, which the policy target re-checks at
        PREPARE; a moved policy aborts the whole decision.
        """
        from connection_hub.invocation_policy import SURFACE_OUTER, InvocationAuthority

        from .oauth_issuance import IssuanceRefused

        try:
            selection = self._oauth_policy_selection(resource_operations, invocation_policies)
        except ValueError as exc:
            unavailable = str(exc) == "invocation policy service is unavailable"
            raise IssuanceRefused("issuance_invocation_policies_unavailable" if unavailable
                                  else "issuance_invocation_policies_invalid", retryable=unavailable) from None
        if not selection:
            return []
        current = await self._invocation_policies.list_for_card(owner_subject=grantor, access_id=candidate.access_id)
        revisions = {
            (policy.authority.resource, policy.authority.operation): policy.revision
            for policy in current
            if policy.authority.surface == SURFACE_OUTER
            and not policy.authority.provider_id and not policy.authority.account_id
        }
        effects = []
        for resource, operations in sorted(selection.items()):
            for operation, mode in sorted(operations.items()):
                authority = InvocationAuthority(access_id=candidate.access_id, resource=resource,
                                                surface=SURFACE_OUTER, operation=operation)
                effects.append({"kind": "invocation_policy", "key": authority.key,
                                "payload": {"owner_subject": grantor, "authority": authority.to_dict(), "mode": mode,
                                            "expected_revision": revisions.get((resource, operation), 0)}})
        return effects

    @staticmethod
    def _issuance_intent(plan: Mapping[str, Any], transaction_id: str, intent_digest: str) -> Any:
        from .cards.card_participant import CardGroupIntent, CardGroupMemberIntent, CardIntent

        spec = plan["intent"]
        candidate = CardAuthority.from_mapping(spec["candidate"])
        effects = tuple(dict(effect) for effect in spec["effects"])
        if spec["shape"] == "group":
            return CardGroupIntent(
                transaction_id=transaction_id, intent_digest=intent_digest,
                members=(CardGroupMemberIntent(subject_hash=spec["subject_hash"], original=None, candidate=candidate,
                                               action=spec["action"]),),
                effects=effects, actor_subject=spec["actor_subject"], actor_kind=spec["actor_kind"])
        return CardIntent(transaction_id=transaction_id, intent_digest=intent_digest, subject_hash=spec["subject_hash"],
                          original=CardAuthority.from_mapping(spec["original"]), candidate=candidate, effects=effects,
                          action=spec["action"], actor_subject=spec["actor_subject"], actor_kind=spec["actor_kind"])

    @staticmethod
    def _issuance_plan(plan: Mapping[str, Any], transaction_id: str, intent_digest: str) -> Any:
        from .oauth_issuance import IssuanceRefused, OAuthIssuancePlan

        try:
            return OAuthIssuancePlan.from_mapping({**plan, "transaction_id": transaction_id,
                                                   "intent_digest": intent_digest})
        except (KeyError, TypeError, ValueError):
            # A plan stored before a field existed is never completed from a newer Card.
            raise IssuanceRefused("issuance_plan_outdated") from None

    async def _begin_planned_issuance(self, plan: Mapping[str, Any], *, intents: Any, decisions: Any,
                                      store: Any) -> Any:
        """Begin (or replay) the decision exactly as stored, record its intent, bind the plan to it."""
        from service_foundation.coordination.durable_decision_log import DecisionRefused, IntentDraft

        from .cards.card_participant import PARTICIPANT
        from .oauth.issuance_store import IssuanceStoreRefused
        from .oauth_issuance import IssuanceRefused

        draft_value = plan["draft"]
        draft = IntentDraft(replay_scope=draft_value["replay_scope"], request_id=draft_value["request_id"],
                            expires_at=draft_value["expires_at"], participants=(PARTICIPANT,),
                            payload=draft_value["payload"])
        try:
            row = await decisions.begin(draft)
            await intents.record(self._issuance_intent(plan, row.transaction_id, row.intent.digest))
            await store.bind_issuance_plan_transaction(decision_request_id=plan["decision_request_id"],
                                                       transaction_id=row.transaction_id)
        except DecisionRefused as exc:
            # An expired draft that never began, or a conflicting replay: nothing is reserved yet.
            raise IssuanceRefused("issuance_begin_refused" if str(exc) != "intent_conflict"
                                  else "issuance_replay_changed") from None
        except IssuanceStoreRefused as exc:
            raise IssuanceRefused(exc.reason) from None
        return self._issuance_plan(plan, row.transaction_id, row.intent.digest)

    async def _trusted_issuance(self, transaction_id: str, *, decisions: Any, store: Any) -> tuple[Any, Any, Any]:
        """(stored plan, the plan as the Hub would return it, the decision row) for a transaction id."""
        from .oauth_issuance import IssuanceRefused

        if type(transaction_id) is not str:
            raise IssuanceRefused("issuance_plan_unknown")
        stored = await store.read_issuance_plan(transaction_id)
        row = await decisions.read(transaction_id) if stored is not None else None
        if stored is None or row is None:
            raise IssuanceRefused("issuance_plan_unknown")
        plan = stored["plan"]
        return plan, self._issuance_plan(plan, transaction_id, row.intent.digest), row

    @staticmethod
    def _checked_issuance_record(plan: Mapping[str, Any], slot: str, record: Any) -> dict[str, Any]:
        """The SDK's non-secret credential record, bound to the planned Card, issuer and subjects."""
        from .cards.participant_effects import ParticipantEffectRefused, _no_secret_fields
        from .oauth_issuance import IssuanceRefused

        if not isinstance(record, Mapping) or any(type(key) is not str for key in record):
            raise IssuanceRefused("issuance_record_mismatch")
        credential = record.get("credential")
        envelope_fields = {"credential_id", "credential_kind", "issuer_authority_id", "issuer_authenticator_id",
                           "subject", "tenant", "project", "audience", "session_id", "verified_authority", "attrs",
                           "iat", "exp", "schema"}
        if not isinstance(credential, Mapping) or not set(credential) <= envelope_fields:
            raise IssuanceRefused("issuance_record_mismatch")
        try:
            # The envelope's keys are the fixed CredentialEnvelope fields (``credential_id`` and
            # ``credential_kind`` are identifiers, not secrets); everything else, and every value
            # inside the envelope, meets the secret-field rule.
            _no_secret_fields({key: value for key, value in record.items() if key != "credential"})
            _no_secret_fields({"attrs": credential.get("attrs") or {},
                               "verified_authority": credential.get("verified_authority") or {}})
            if any(not isinstance(credential.get(name, ""), (str, int)) or isinstance(credential.get(name), bool)
                   for name in envelope_fields - {"attrs", "verified_authority"}):
                raise ParticipantEffectRefused("card_effect_record_invalid")
        except ParticipantEffectRefused:
            raise IssuanceRefused("issuance_record_secret_field") from None
        attrs = credential.get("attrs")
        candidate_kind = str(plan["intent"]["candidate"].get("card_kind") or "")
        if (record.get("registry_access_id") != plan["access_id"]
                or credential.get("subject") != plan["credential_subject"]
                or credential.get("issuer_authority_id") != plan["credential_issuer"]
                or credential.get("tenant") != plan["tenant"] or credential.get("project") != plan["project"]
                or not isinstance(attrs, Mapping) or attrs.get("grantor_subject") != plan["grantor_subject"]
                or attrs.get("client_id") != plan["client_id"]
                or (slot == "refresh" and (record.get("sub") != plan["grantor_subject"]
                                           or record.get("client_id") != plan["client_id"]))
                or ("sub" in record and record.get("sub") != plan["grantor_subject"])
                or ("client_id" in record and record.get("client_id") != plan["client_id"])
                or (record.get("card_kind") and record.get("card_kind") != candidate_kind)):
            raise IssuanceRefused("issuance_record_mismatch")
        # The authority a reader takes from the record or its envelope is exactly the planned Card's:
        # every field present, well formed and equal after the shared normalization (no union, no fallback).
        for source in (record, attrs):
            for name in ("operations", "resource_grants", "resource_operations"):
                if _authority_snapshot(name, source.get(name)) != _authority_snapshot(name, plan[name]):
                    raise IssuanceRefused("issuance_record_authority_mismatch")
        return dict(record)

    async def reserve_oauth_issuance(self, *, plan: Any, slot: str, token_sha256: str, record: Mapping[str, Any],
                                     ttl_seconds: int) -> str:
        """W603: reserve one credential the SDK minted for this plan; returns the opaque reservation id.

        Only ``plan.transaction_id`` selects: every other plan field must equal
        the stored plan (``issuance_plan_mismatch`` otherwise), and the Card
        id, subjects, revision, cap and deadline are copied from the stored
        plan, never from the caller. The record must carry no secret field and
        must name the planned Card, issuer, credential subject, grantor and
        client. Idempotent on (transaction, slot); a retry never renews the
        deadline. Refused once the decision is decided.
        """
        from .oauth.issuance_store import IssuanceStoreRefused
        from .oauth_issuance import IssuanceRefused, OAuthIssuancePlan

        _coordinator, _intents, decisions, _ttl, store = self._issuance_parts()
        if isinstance(plan, Mapping):
            try:
                plan = OAuthIssuancePlan.from_mapping(plan)
            except (KeyError, TypeError, ValueError):
                raise IssuanceRefused("issuance_plan_mismatch") from None
        if not isinstance(plan, OAuthIssuancePlan):
            raise IssuanceRefused("issuance_plan_mismatch")
        stored, trusted, row = await self._trusted_issuance(plan.transaction_id, decisions=decisions, store=store)
        if plan != trusted:
            raise IssuanceRefused("issuance_plan_mismatch")
        if row.terminal:
            raise IssuanceRefused("issuance_decision_closed")
        if slot not in trusted.slots:
            raise IssuanceRefused("reservation_slot_undeclared")
        checked = self._checked_issuance_record(stored, slot, record)
        try:
            return await store.reserve_issued_credential(transaction_id=trusted.transaction_id, slot=slot,
                                                         token_sha256=token_sha256, record=checked,
                                                         ttl_seconds=ttl_seconds)
        except IssuanceStoreRefused as exc:
            raise IssuanceRefused(exc.reason) from None

    async def release_oauth_issuance(self, *, transaction_id: str, slot: str) -> str:
        """W603: the SDK releases a reservation STAGE never bound (``reservation_bound`` once it is)."""
        from .oauth.issuance_store import IssuanceStoreRefused
        from .oauth_issuance import IssuanceRefused

        *_parts, store = self._issuance_parts()
        try:
            return await store.release_issued_credential(transaction_id=transaction_id, slot=slot, unbound_only=True)
        except IssuanceStoreRefused as exc:
            raise IssuanceRefused(exc.reason) from None

    async def expire_oauth_issuance_reservations(self, *, limit: int = 100) -> int:
        """W603: the scheduled sweep: never-bound reservations past their deadline end ``expired``."""
        *_parts, store = self._issuance_parts()
        return await store.expire_issuance_reservations(limit=limit)

    async def read_oauth_issuance(self, *, transaction_id: str) -> Any:
        """W603: the issuance's outcome as it stands now, READ ONLY; returns the ``OAuthIssuanceResult``.

        For a caller recovering an uncertain original response: it never
        prepares, decides, finishes or claims anything, so a read before both
        reservations exist cannot ABORT the issuance. ``state`` is
        ``committed`` only once the decision committed and every slot applied;
        ``aborted`` once it aborted; otherwise ``pending``, with each slot's
        reservation outcome (``pending`` until it is reserved and applied).
        Refuses ``issuance_plan_unknown`` for a transaction with no plan.
        """
        _coordinator, _intents, decisions, _ttl, store = self._issuance_parts()
        plan, trusted, _row = await self._trusted_issuance(transaction_id, decisions=decisions, store=store)
        return await self._issuance_result(plan, trusted, decisions=decisions, store=store)

    async def read_oauth_issuance_plan(self, *, transaction_id: str) -> Any:
        """W603: the stored ``OAuthIssuancePlan`` of a transaction, READ ONLY (the plan ``begin`` returned)."""
        _coordinator, _intents, decisions, _ttl, store = self._issuance_parts()
        _plan, trusted, _row = await self._trusted_issuance(transaction_id, decisions=decisions, store=store)
        return trusted

    async def read_oauth_issuance_plan_by_request(self, *, decision_request_id: str) -> Any:
        """W585: the ``OAuthIssuancePlan`` ``begin`` returned for one original exchange, READ ONLY.

        For a caller that lost ``begin``'s answer before it kept the transaction
        id. ``decision_request_id`` is the plan's own field-tagged request
        identity (``oauth_issuance.decision_request_id`` over the scope, grantor,
        client and original request id), which the caller keeps before
        ``begin``. Only the one plan stored for it in this Hub's own
        tenant/project answers; no candidate input is needed or accepted. It
        never plans, begins, prepares, decides, finishes or claims. Refuses
        ``issuance_plan_unknown`` when no plan was stored for that request, and
        ``issuance_plan_unbound`` when the plan exists but ``begin`` did not
        bind its decision (nothing can be reserved or completed for it).
        """
        from .oauth_issuance import IssuanceRefused

        _coordinator, _intents, decisions, _ttl, store = self._issuance_parts()
        if (type(decision_request_id) is not str or len(decision_request_id) != 64
                or decision_request_id.strip("0123456789abcdef")):
            raise IssuanceRefused("issuance_request_invalid")
        stored = await store.read_issuance_plan_request(decision_request_id)
        if stored is None:
            raise IssuanceRefused("issuance_plan_unknown")
        if not stored["transaction_id"]:
            raise IssuanceRefused("issuance_plan_unbound")
        _plan, trusted, _row = await self._trusted_issuance(stored["transaction_id"], decisions=decisions, store=store)
        if trusted.decision_request_id != decision_request_id:
            raise IssuanceRefused("issuance_plan_unknown")
        return trusted

    async def complete_oauth_issuance(self, *, transaction_id: str, expect: Mapping[str, str] | None = None) -> Any:
        """W603: prepare, commit and finish the issuance's decision; returns the ``OAuthIssuanceResult``.

        Every reservation is derived from the transaction; ``expect``
        (slot -> token_sha256) is only an equality check. Completions of one
        transaction are serialized across processes by a short claim on its
        plan (``issuance_completion_section``; no connection is held across
        the work), so a lost-response retry racing the first call answers that
        call's outcome (``pending`` while it runs) instead of aborting it. The
        claim is renewed before each decision and a lost claim decides
        nothing. Under the claim an undecided decision is prepared (STAGE binds each
        reservation), passed through the enlisted caller-writer gate exactly as
        ``record_oauth_grant``'s write, and committed. Only a KNOWN refusal
        (a named decision, transaction, gate or issuance refusal) records an
        ABORT; any other failure leaves the decision undecided for a retry or
        for recovery, which presumes ABORT once it expires. ``state`` is
        ``committed`` only once every slot's effect has applied (``applied`` or
        ``superseded``); otherwise ``aborted`` or ``pending``, and on
        ``pending`` the caller calls again without minting.
        """
        from .oauth.issuance_store import IssuanceStoreRefused
        from .oauth_issuance import IssuanceRefused

        coordinator, _intents, decisions, _ttl, store = self._issuance_parts()
        plan, trusted, _row = await self._trusted_issuance(transaction_id, decisions=decisions, store=store)
        if expect is not None:
            reservations = await store.issuance_reservations(transaction_id)
            if not isinstance(expect, Mapping) or any(
                    reservations.get(slot, {}).get("token_sha256") != digest for slot, digest in expect.items()):
                raise IssuanceRefused("issuance_expect_mismatch")
        candidate = CardAuthority.from_mapping(plan["intent"]["candidate"])
        decided_here, abort_reason = False, ""
        try:
            async with store.issuance_completion_section(transaction_id) as renew:
                row = await decisions.read(transaction_id)  # read again under the claim
                if row is not None and not row.terminal:
                    decided_here, abort_reason = await self._decide_issuance(
                        plan, trusted, candidate, coordinator=coordinator, renew=renew)
                try:
                    await coordinator.finish(transaction_id)
                except Exception:  # noqa: BLE001 - undecided, or effects/serving behind: pending, finished again later
                    _LOGGER.warning("[connection_hub.oauth_issuance] issuance not finished: transaction=%s",
                                    transaction_id, exc_info=True)
        except IssuanceStoreRefused as exc:
            if exc.reason != "issuance_completion_busy":
                raise IssuanceRefused(exc.reason) from None
            # Another completion holds the section: report the decision as it stands (pending until it ends).
        result = await self._issuance_result(plan, trusted, decisions=decisions, store=store,
                                             reason=abort_reason)
        if decided_here and result.state == "committed":
            await self.notify_change(trusted.grantor_subject, action="granted",
                                     access=record_from_card(candidate).to_public_dict())
        return result

    async def _decide_issuance(self, plan: Mapping[str, Any], trusted: Any, candidate: CardAuthority, *,
                               coordinator: Any, renew: Callable[[], Awaitable[bool]]) -> tuple[bool, str]:
        """Prepare, gate and decide under the completion section: (committed here, abort reason).

        A known refusal records ABORT; the refused caller-write outcome is
        recorded only when THIS call's ABORT is the recorded decision. Any
        other failure (an outage, a lost connection, cancellation) decides
        nothing and returns (False, "").
        """
        from service_foundation.coordination.durable_decision_log import DecisionRefused

        from .cards.card_participant import PARTICIPANT
        from .cards.transaction_store import CardTransactionRefused
        from .oauth_issuance import IssuanceRefused

        known = (DecisionRefused, CardTransactionRefused, CallerWriteRefused, IssuerWriteRefused, CardConflict,
                 IssuanceRefused)
        transaction_id = trusted.transaction_id
        caller_request = None
        try:
            gate, caller_request = await self._enlisted_gate(
                candidate, expected_revision=trusted.base_revision,
                caller_write=CallerWrite("oauth_grant", trusted.grantor_subject,
                                         request_id=trusted.decision_request_id))
            await coordinator.prepare_existing(transaction_id)
            if gate is not None:
                await gate()
            witness = (caller_request.change_digest if caller_request is not None
                       else plan["draft"]["payload"]["participant_inputs"][PARTICIPANT]["candidate_digest"])
            if not await renew():
                return False, ""  # the claim lapsed to another completion: it decides
            await coordinator.decide(transaction_id, "committed", witness_digest=witness)
        except known as exc:
            reason = str(getattr(exc, "reason", "") or exc or type(exc).__name__)[:128]
            try:
                if not await renew():
                    return False, ""
                await coordinator.decide(transaction_id, "aborted")
            except DecisionRefused:
                return False, ""  # decided already: finish reports that decision
            except Exception:  # noqa: BLE001 - nothing recorded: recovery presumes the abort on expiry
                _LOGGER.warning("[connection_hub.oauth_issuance] abort not recorded: transaction=%s",
                                transaction_id, exc_info=True)
                return False, ""
            if caller_request is not None:
                await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                           state="refused", card_revision=trusted.base_revision)
            _LOGGER.info("[connection_hub.oauth_issuance] issuance aborted: transaction=%s reason=%s",
                         transaction_id, reason)
            return False, reason
        except Exception:  # noqa: BLE001 - unknown: left undecided for a retry or recovery
            _LOGGER.warning("[connection_hub.oauth_issuance] issuance not decided: transaction=%s", transaction_id,
                            exc_info=True)
            return False, ""
        if caller_request is not None:
            await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                       state="committed", card_revision=candidate.card_revision)
        return True, ""

    async def _issuance_result(self, plan: Mapping[str, Any], trusted: Any, *, decisions: Any, store: Any,
                               reason: str = "") -> Any:
        from .cards.card_participant import PARTICIPANT
        from .oauth_issuance import OAuthIssuanceResult, SlotOutcome

        row = await decisions.read(trusted.transaction_id)
        reservations = await store.issuance_reservations(trusted.transaction_id)
        per_slot = {}
        for slot in trusted.slots:
            held = reservations.get(slot, {})
            state, outcome = held.get("state", ""), held.get("outcome", "")
            per_slot[slot] = SlotOutcome(
                outcome=("applied" if state == "activated" else "superseded" if outcome == "superseded"
                         else "released" if state in ("released", "expired") else "pending"),
                effect_digest=trusted.effect_digests[slot], token_sha256=held.get("token_sha256", ""))
        finished = row.finished.get(PARTICIPANT) if row is not None else None
        if row is not None and row.state == "committed" and finished is not None and all(
                outcome.outcome in ("applied", "superseded") for outcome in per_slot.values()):
            state, receipt = "committed", finished.receipt_digest
        elif row is not None and row.state == "aborted":
            state, receipt = "aborted", ""
        else:
            state, receipt = "pending", ""
        return OAuthIssuanceResult(
            transaction_id=trusted.transaction_id, intent_digest=trusted.intent_digest, state=state,
            access_id=trusted.access_id,
            card_revision=trusted.candidate_revision if state == "committed" else trusted.base_revision,
            expires_at=trusted.expires_at, delivery_deadline=trusted.delivery_deadline, receipt_digest=receipt,
            per_slot=per_slot, reason=reason if state != "committed" else "")

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

    def bind_account_stores(self, accounts_for: Callable[[str], Any] | None) -> None:
        """W578: the grantor's connected-account store, composed with the shared account lock."""
        self._account_stores_for = accounts_for

    async def _account_binding_members(
        self, grantor_subject: str, provider_id: str, account_id: str,
    ) -> list[tuple[CardAuthority, CardAuthority]] | dict[str, Any]:
        """W578: every live Card of the grantor that binds the account, with its candidate minus that account.

        Classified by each Card's actual binding: a Card bound under a Control
        (a My Card, a person or project Control) or a credentialless Control
        changes only through its owner's transaction, so while that route does
        not exist the whole disconnect refuses. Only unbound Cards are members.
        """
        cards = await self._cards().list_active(subject_hash=_subject_key(grantor_subject))
        members = []
        for current in sorted(cards, key=lambda card: card.access_id):
            accounts = dict((current.account_scope or {}).get(provider_id) or {})
            if account_id not in accounts:
                continue
            if current.control_card is not None or authority_is_credentialless(current):
                return {"ok": False, "error": "card_transactions_direct_write_refused", "removed": False,
                        "message": "This account is bound by a project Card; the project must change it.",
                        "retryable": False, "status": 409}
            accounts.pop(account_id, None)
            scope = {p: dict(b) for p, b in (current.account_scope or {}).items()}
            if accounts:
                scope[provider_id] = accounts
            else:
                scope.pop(provider_id, None)
            members.append((current, replace_fields(current, account_scope=scope,
                                                    card_revision=current.card_revision + 1)))
        return members

    async def disconnect_account_in_transaction(
        self, *, grantor_subject: str, provider_id: str, account_id: str,
    ) -> dict[str, Any] | None:
        """W578: an account disconnect as ONE durable transaction, the account deleted only after COMMIT.

        ``None`` only when Card transactions are off (the caller keeps its
        ordered path). Otherwise the account's incarnation is bound and
        ``account_delete`` names that exact incarnation. Every unbound Card
        binding the account is a group member, its candidate minus that
        account; when no Card binds it, the same decision carries the effect
        alone, as a versioned effects-only input (card_effects.py; CodeApp,
        7 October 2026 02:45 UTC), never an empty group. After ``begin`` the
        account is fenced for this transaction and the Cards are listed again:
        a different set (a Card that gained the binding included) aborts,
        retryably. Then prepare (STAGE holds the incarnation), COMMIT and
        FINISH, which applies the deletion before the fence is released.
        ``removed`` is true only when the deletion is applied; an undecided or
        unfinished transaction answers retryably and recovery finishes it.
        """
        bound = getattr(self, "_card_coordinator", None)
        if bound is None:
            return None
        accounts_for = getattr(self, "_account_stores_for", None)
        unavailable = {"ok": False, "error": "card_transactions_unavailable", "removed": False,
                       "retryable": True, "status": 503}
        if accounts_for is None:
            return unavailable
        from service_foundation.coordination.durable_decision_log import DecisionRefused, IntentDraft

        from connection_hub.delegated_to_kdcube.store import AccountLockUnavailable

        from .cards.card_effects import hub_effects_participant_input
        from .cards.card_group import group_member, hub_group_participant_input
        from .cards.card_participant import PARTICIPANT, CardEffectsIntent, CardGroupIntent, CardGroupMemberIntent
        from .cards.transaction_store import CardTransactionRefused

        coordinator, intents, decisions, ttl = bound
        accounts = accounts_for(grantor_subject)
        try:
            incarnation = await accounts.ensure_incarnation(account_id)
        except AccountLockUnavailable:
            return unavailable
        if not incarnation:
            return {"ok": True, "removed": False}
        try:
            members = await self._account_binding_members(grantor_subject, provider_id, account_id)
        except Exception:  # noqa: BLE001 - Cards unreadable (CardUnavailable, a store outage): retry
            _LOGGER.warning("[connection_hub.disconnect] binding Cards unreadable: account=%s", account_id,
                            exc_info=True)
            return unavailable
        if isinstance(members, dict):
            return members
        subject_hash = _subject_key(grantor_subject)
        effect = {"kind": "account_delete", "key": f"{provider_id}:{account_id}",
                  "payload": {"access_id": members[0][0].access_id if members else "",
                              "grantor_subject": grantor_subject, "provider_id": provider_id,
                              "account_id": account_id, "incarnation": incarnation}}
        # W606: each credential-bearing member Card's handle row moves with the disconnect's decision.
        bindings = await self._handle_binding_effects(members) if members else []
        if members:
            participant_input = hub_group_participant_input(
                members=[group_member(original=current, candidate=candidate, action="update")
                         for current, candidate in members],
                actor_subject=grantor_subject, actor_kind="grantor", effects=[effect, *bindings])
        else:
            participant_input = hub_effects_participant_input(
                subject_hash=subject_hash, effects=[effect], actor_subject=grantor_subject, actor_kind="grantor")
        draft = IntentDraft(
            replay_scope=f"{PARTICIPANT}:{subject_hash}:{grantor_subject}:account-disconnect",
            request_id=secrets.token_urlsafe(18),
            expires_at=int(datetime.now(timezone.utc).timestamp()) + ttl, participants=(PARTICIPANT,),
            payload={"participant_inputs": {PARTICIPANT: participant_input}})
        try:
            row = await decisions.begin(draft)
        except Exception:  # noqa: BLE001 - nothing begun: a finite, retryable answer
            _LOGGER.warning("[connection_hub.disconnect] account transaction could not begin", exc_info=True)
            return unavailable
        transaction_id = row.transaction_id
        card_service = getattr(self._cards(), "card_service", None)
        try:
            # Inside the abort scope (claude-main, #645): a failure to record the
            # intent is an ABORT too, never a begun transaction left to recovery.
            await intents.record(CardGroupIntent(
                transaction_id=transaction_id, intent_digest=row.intent.digest,
                members=tuple(CardGroupMemberIntent(subject_hash=subject_hash, original=current,
                                                    candidate=candidate, action="update")
                              for current, candidate in members),
                effects=(effect, *bindings), actor_subject=grantor_subject, actor_kind="grantor") if members else
                CardEffectsIntent(transaction_id=transaction_id, intent_digest=row.intent.digest,
                                  subject_hash=subject_hash, effects=(effect,), actor_subject=grantor_subject,
                                  actor_kind="grantor"))
            if card_service is None:
                raise DecisionRefused("card_transactions_unavailable")
            await card_service.reserve_accounts([(provider_id, account_id)], transaction_id=transaction_id)
            again = await self._account_binding_members(grantor_subject, provider_id, account_id)
            if (not isinstance(again, list) or [(c.access_id, c.card_revision) for c, _ in again]
                    != [(c.access_id, c.card_revision) for c, _ in members]):
                raise DecisionRefused("card_account_binding_changed")
            await coordinator.prepare_existing(transaction_id)
        except BaseException as exc:
            # claude-main #645 P2: nothing escapes undecided after begin. Every
            # refusal or failure before COMMIT is an ABORT, finished at once so
            # the fence is released; a failed abort is left to recovery.
            try:
                await coordinator.decide(transaction_id, "aborted")
                await coordinator.finish(transaction_id)
            except Exception:  # noqa: BLE001 - recovery presumes the abort
                _LOGGER.warning("[connection_hub.disconnect] abort not finished: transaction=%s", transaction_id,
                                exc_info=True)
                if not isinstance(exc, Exception):
                    raise
                return {"ok": False, "error": "card_transaction_pending", "removed": False, "retryable": True,
                        "status": 503}
            if not isinstance(exc, Exception):
                raise  # cancellation propagates, after its transaction was aborted
            if isinstance(exc, (DecisionRefused, CardTransactionRefused)):
                return {"ok": False, "error": "account_binding_not_pruned", "reason": str(exc), "removed": False,
                        "retryable": True, "status": 409}
            _LOGGER.warning("[connection_hub.disconnect] account transaction refused: transaction=%s",
                            transaction_id, exc_info=True)
            return {**unavailable, "reason": "card_transaction_aborted"}
        try:
            await coordinator.decide(transaction_id, "committed", witness_digest=participant_input["candidate_digest"])
            await coordinator.finish(transaction_id)
        except Exception:  # noqa: BLE001 - decided or not, recovery finishes it; never claimed removed
            _LOGGER.warning("[connection_hub.disconnect] account transaction not finished: transaction=%s",
                            transaction_id, exc_info=True)
            return {"ok": False, "error": "card_transaction_pending", "removed": False, "retryable": True,
                    "status": 503}
        stored = await accounts.get_account(account_id)
        removed = stored is None or stored.incarnation != incarnation
        return {"ok": removed, "removed": removed, "bindings_cleared": len(members),
                "bindings_cleared_grants": [current.access_id for current, _ in members],
                **({} if removed else {"error": "card_transaction_pending", "retryable": True, "status": 503})}

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

        Never raises. A Card it could not prune (for example one with an
        unresolved transaction) is reported in ``not_pruned`` with ``ok``
        False, never skipped silently: its binding would otherwise survive
        and revive on reconnect (W580 finding 2). The caller decides whether
        the disconnect may proceed.
        """
        subject = _clean(grantor_subject)
        provider = _clean(provider_id)
        account = _clean(account_id)
        if not subject or not provider or not account:
            return {"ok": True, "pruned": 0, "grants": [], "not_pruned": []}
        try:
            candidates = await self._list_active_records(subject)
        except Exception:
            return {"ok": False, "pruned": 0, "grants": [], "not_pruned": [],
                    "reason": "grants_unreadable", "retryable": True}
        pruned: list[str] = []
        not_pruned: list[str] = []
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
                    pruned_record, expected_revision=record.card_revision,
                    caller_write=CallerWrite("prune", subject),
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
                not_pruned.append(access_id)
                continue
        if pruned:
            _LOGGER.info(
                "[connection_hub.disconnect] cleared account binding from %d grant(s): "
                "provider=%s account=%s grants=%s",
                len(pruned), provider, account, pruned,
            )
        outcome = {"ok": not not_pruned, "pruned": len(pruned), "grants": pruned, "not_pruned": not_pruned}
        if not_pruned:
            outcome.update(reason="account_binding_not_pruned", retryable=True)
        return outcome


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
            # Audit A3: an edit that leaves nothing recognised is REFUSED, never
            # revoked, exactly as update_access does since the 2026-09-12
            # incident. The client holds tokens that point at this Card; an
            # ordinary edit must never invalidate them (operator, 2026-10-07:
            # "changing what this token points into should not invalidate the
            # actual token the parties possess"). Revocation stays the explicit
            # delegated_access_revoke command.
            return {
                "ok": False,
                "error": "delegated_access_requires_resource_grants",
                "pruned": resolved.reconciled.to_public_dict(),
                "message": (
                    "This change would leave the client's card with nothing the service "
                    "catalog still offers, so it was not applied and the card is unchanged. "
                    "Select at least one current service, or revoke the card "
                    "deliberately if that is what you intend."
                ),
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
        # W661 S5 (G2): a consent extension never adds or removes an application-managed grant or operation.
        changes = managed_grant_changes(
            extend_config, self._resource_grants(dict(updated.resource_grants)),
            self._resource_grants(dict(record.resource_grants)))
        changes += managed_operation_changes(
            extend_config, self._resource_grants(dict(updated.resource_operations)),
            self._resource_grants(dict(record.resource_operations)))
        if changes:
            return managed_grant_refusal(changes)
        try:
            await self._persist_record(updated, expected_revision=record.card_revision,
                                       caller_write=CallerWrite("extend", self._caller_actor_subject(user)))
        except CallerWriteRefused as exc:
            return exc.to_dict()
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
            await self._persist_record(renewed, expected_revision=committed_revision,
                                       caller_write=CallerWrite("renew", self._caller_actor_subject(user)))
        except CallerWriteRefused as exc:
            return exc.to_dict()
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
        # The precondition read applies the shared Card fence, so a Card with
        # an unresolved transaction is refused BEFORE the credential's life is
        # extended (W580 finding 1). A stage that lands after this read and
        # before the commit is still refused at the commit, but the extension
        # is not undone: only one shared SQL transaction closes that (N1).
        try:
            committed_revision = await self._committed_revision(
                record.access_id, grantor_subject=record.grantor_subject
            )
        except (CardUnavailable, CardConflict, CardStorageError) as exc:
            return {
                "ok": False,
                "error": "delegated_card_not_committed",
                "reason": getattr(exc, "reason", "") or str(exc),
                "retryable": True,
                "status": 503,
            }
        provenance = dict(record.provenance or {})
        provenance["prolongations"] = int(provenance.get("prolongations") or 0) + 1
        provenance["prolonged_at"] = now
        prolonged = dataclasses.replace(
            record,
            card_revision=committed_revision + 1,
            expires_at=new_expires_at,
            provenance=provenance,
        )
        coordinated = getattr(self, "_card_coordinator", None) is not None
        if coordinated:
            # Ops 13:19: a credential that has already ended is refused, Card
            # unchanged, exactly as before routing. The check is read-only and
            # runs BEFORE the policy is asked (W580 F6, Ops 13:32), so no
            # decision is opened for a write that cannot happen. The effect
            # moves only live credentials, so one that ends later is not revived.
            credentials_live = getattr(store, "card_credentials_live", None)
            if credentials_live is None:
                return expired("Reconnect from the client.")
            try:
                if not await credentials_live(record.access_id):
                    return expired("Reconnect from the client.")
            except GrantStoreUnavailable as exc:
                return {"ok": False, "error": "delegated_credential_store_unavailable",
                        "reason": exc.operation, "retryable": True, "status": 503}
        # W580 F5: the binding's policy (and the prolong shape rule) decides
        # BEFORE the credential's life is touched; a refusal extends nothing.
        try:
            pre_gate = await self._enlisted_gate(
                card_authority_from_record(prolonged), expected_revision=committed_revision,
                caller_write=CallerWrite("prolong", self._caller_actor_subject(user)))
        except CallerWriteRefused as exc:
            return exc.to_dict()
        # Under the ONE protocol the credential's life is a decision-bound
        # effect: one ABSOLUTE deadline, applied only after the COMMIT (W582,
        # Ops 13:16). Without a coordinator (not yet composed) the direct
        # extension below runs only after the policy allowed it (F5).
        lifetime = [{"kind": "credential_lifetime", "key": "card",
                     "payload": {"access_id": record.access_id, "expires_at": int(new_expires_at),
                                 "base_card_revision": int(committed_revision)}}] if coordinated else []

        async def ended_after_decision() -> dict[str, Any]:
            # W580 F6: the policy was asked, so its decision is finalized
            # refused like every other refused bound write, never left open.
            await caller_write_outcome(getattr(self, "_caller_writers", None), pre_gate[1],
                                       state="refused", card_revision=committed_revision)
            return expired("Reconnect from the client.")

        if not coordinated and record.refresh_token:
            extend_refresh = getattr(store, "extend_refresh_token", None)
            if extend_refresh is None:
                return await ended_after_decision()
            if not await extend_refresh(record.refresh_token, ttl):
                return await ended_after_decision()
            if record.access_token:
                extend_grant = getattr(store, "extend_access_grant", None)
                if extend_grant is not None:
                    await extend_grant(record.access_token, ttl)
        elif not coordinated:
            extend_card = getattr(store, "extend_card_credentials", None)
            if extend_card is None or not await extend_card(record.access_id, ttl):
                return await ended_after_decision()

        try:
            await self._persist_record(prolonged, expected_revision=committed_revision,
                                       caller_write=CallerWrite("prolong", self._caller_actor_subject(user)),
                                       pre_gate=pre_gate, effects=lifetime)
        except CallerWriteRefused as exc:
            return exc.to_dict()
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

    async def revoke_access(self, user: Mapping[str, Any], *, access_id: str,
                            expected_access_id: str | None = None,
                            expected_card_revision: int | None = None,
                            _issuer_decision: IssuerDecision | None = None,
                            _issuer_request_id: str = "",
                            _issuer_context_ref: str = "") -> dict[str, Any]:
        grantor_subject = _subject_from_user(user)
        if not grantor_subject:
            return {"ok": False, "error": "delegated_access_requires_authenticated_user"}
        refusal = _delegate_mutation_refusal(user)
        if refusal is not None:
            return refusal
        access_id_value = _clean(access_id)
        if not access_id_value:
            return {"ok": False, "error": "delegated_access_id_required"}
        has_expectations = expected_access_id is not None or expected_card_revision is not None
        if has_expectations:
            if (
                not isinstance(expected_access_id, str)
                or not expected_access_id.strip()
                or type(expected_card_revision) is not int
                or expected_card_revision < 1
            ):
                return {"ok": False, "error": "delegated_access_revoke_precondition_invalid", "status": 400}
            if expected_access_id.strip() != access_id_value:
                return {"ok": False, "error": "delegated_access_revoke_target_mismatch", "status": 409}
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
            if has_expectations:
                return {"ok": False, "error": "delegated_card_revision_conflict",
                        "reason": "card_revision_moved", "status": 409, "retryable": False}
            return {"ok": True, "removed": False}
        record = loaded[0]
        if record.grantor_subject != grantor_subject:
            return {"ok": False, "error": "delegated_access_cross_user_access_denied"}
        refused = self._managed_project_control_refused(record)
        if refused is not None:
            return refused
        if record.control_card is not None:
            # W578: ending a Card bound under a Control (a person's My Card, a
            # project-bound agent Card) changes the authority its Control's
            # owner relies on, so while Card transactions are on only that
            # owner's transaction may end it; until it is enlisted it refuses.
            refused = self._managed_direct_write_refused()
            if refused is not None:
                return refused
        if self._issuer_managed(record) and not has_expectations:
            return {"ok": False, "error": "delegated_access_revoke_precondition_required", "status": 409}
        if has_expectations and record.card_revision != expected_card_revision:
            return {"ok": False, "error": "delegated_card_revision_conflict",
                    "reason": "card_revision_moved", "status": 409, "retryable": False}
        # The loaded record now carries the caller's exact target revision.
        # forget/forget_guarded passes that same revision to CardService.revoke,
        # which compares it under the target mutation lock before any effects.
        # Never reload and adopt a replacement revision after this check.
        if (
            record.source == ACCESS_SOURCE_AGENT
            and record.card_kind == CARD_KIND_AGENT
            and record.control_card is not None
            and record.control_card.issuer_kind == AGENT_DESCRIPTOR_ISSUER_KIND
            and AGENT_CAPABILITY_SELECTION_PROPERTY in dict(record.properties or {})
        ):
            return {
                "ok": False,
                "error": "agent_capability_card_managed",
                "message": (
                    "This hosted Agent Card is managed by its linked Control Card. "
                    "Use Reset to Control defaults to restore its starting selection."
                ),
                "status": 409,
            }
        # The revoked revision commits before any credential cleanup, so a
        # failure below cannot leave the card usable.
        try:
            before_commit, issuer_request = await self._issuer_before_commit(
                record, user, action="revoke", candidate={
                    "action": "revoke", "access_id": record.access_id,
                    "card_revision": record.card_revision,
                }, request_id=_issuer_request_id, context_ref=_issuer_context_ref,
                decision=_issuer_decision,
            )
        except IssuerWriteRefused as exc:
            return exc.to_dict()
        # W578: a revoke of a bound Card is decided by its binding's policy.
        caller_request = None
        if before_commit is None:
            try:
                before_commit, caller_request = await caller_writer_before_commit(
                    getattr(self, "_caller_writers", None), card_authority_from_record(record),
                    actor_subject=self._caller_actor_subject(user), action="revoke",
                    candidate={"action": "revoke", "access_id": record.access_id,
                               "card_revision": record.card_revision},
                    request_id=_clean(_issuer_request_id) or secrets.token_urlsafe(18),
                    context_ref=_issuer_context_ref,
                )
            except CallerWriteRefused as exc:
                return exc.to_dict()
        serving_error: CardServingUnavailable | None = None
        try:
            guard = {"before_commit": before_commit} if before_commit is not None else {}
            await self._forget_record(record, **guard)
        except (IssuerWriteRefused, CallerWriteRefused) as exc:
            outcome = await self._issuer_outcome(issuer_request, state="refused", card_revision=record.card_revision)
            outcome.update(await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                                      state="refused", card_revision=record.card_revision))
            return {**exc.to_dict(), **outcome}
        except CardServingUnavailable as exc:
            # Durable revocation already won. Continue invalidating the
            # source-specific credential so a stale serving projection cannot
            # preserve access while Redis is being reconstructed.
            serving_error = exc
        except CardConflict as exc:
            outcome = await self._issuer_outcome(issuer_request, state="refused", card_revision=record.card_revision)
            outcome.update(await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                                      state="refused", card_revision=record.card_revision))
            if has_expectations and exc.reason == "card_revision_moved":
                return {"ok": False, "error": "delegated_card_revision_conflict",
                        "reason": exc.reason, "retryable": False, "status": 409, **outcome}
            return {"ok": False, "error": "delegated_card_not_committed",
                    "reason": exc.reason, "retryable": True, "status": 503, **outcome}
        except (CardUnavailable, CardCommitFailed) as exc:
            outcome = await self._issuer_outcome(issuer_request, state="refused", card_revision=record.card_revision)
            outcome.update(await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                                      state="refused", card_revision=record.card_revision))
            return {
                "ok": False,
                "error": "delegated_card_not_committed",
                "reason": getattr(exc, "reason", ""),
                "retryable": True,
                "status": 503,
                **outcome,
            }
        outcome = await self._issuer_outcome(issuer_request, state="committed", card_revision=record.card_revision + 1)
        outcome.update(await caller_write_outcome(getattr(self, "_caller_writers", None), caller_request,
                                                  state="committed", card_revision=record.card_revision + 1))
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
        if record.source == ACCESS_SOURCE_OAUTH and not (
            record.refresh_token or record.access_token
        ):
            revoke_card = getattr(self._store, "revoke_card_credentials", None)
            if revoke_card is not None:
                refresh_revoked = bool(await revoke_card(record.access_id))
        elif record.refresh_token:
            refresh_revoked = bool(await self._store.revoke_refresh_token(record.refresh_token))
        if record.access_token:
            await self._store.revoke_access_grant(record.access_token)
        await self.notify_change(grantor_subject, action="revoked", access_id=access_id_value)
        if serving_error is not None:
            return {**_serving_state_unavailable(serving_error), **outcome}
        return {
            "ok": True,
            "removed": True,
            "session_removed": removed_session,
            "refresh_token_revoked": refresh_revoked,
            **outcome,
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
