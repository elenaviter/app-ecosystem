# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""The portable read model of one delegated card.

Gateway lists and routes tools from it; Projection intersects it with
descriptor and conversation ceilings. Neither parses persistence records nor
reproduces identity, acceptance, or migration rules: everything they need is
resolved here from the committed authority, the current descriptor state per
resource, and the public invocation policies.

    DelegatedCardView
      caller profile identity          resident profile, or the client family
      access_id, card_revision         which card, which committed revision
      state, expiry, source, label
      resources[]
        resource id, kind, provider    which authority describes it
        identity scope
        grants                         claims on this resource
        operations[]                   name, accepted/current digest, state,
                                       public invocation policy when one exists
        accepted and current revision and digest
        state                          current | changed | removed | unknown
        named service operations       resource -> namespace -> operations
      account scope                    provider -> account -> claims

Every field is non-secret. The view is a snapshot of committed state at the
moment it was built; callers re-resolve per request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from connection_hub.delegated_credentials.cards.identity import (
    ResidentCallerProfile,
    is_resident_client_id,
)
from connection_hub.delegated_credentials.cards.model import CardAuthority
from connection_hub.delegated_credentials.catalog.descriptors import (
    RESOURCE_KIND_CATALOG,
    ROW_ATTR_KIND,
    ROW_ATTR_PROVIDER,
)
from connection_hub.delegated_credentials.catalog.drift import (
    selected_named_service_operations,
)

CALLER_KIND_RESIDENT = "resident"
CALLER_KIND_OAUTH = "oauth"
CALLER_KIND_MANUAL = "manual"

OPERATION_STATE_CURRENT = "current"
OPERATION_STATE_CHANGED = "changed"
OPERATION_STATE_REMOVED = "removed"
OPERATION_STATE_UNKNOWN = "unknown"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any, *, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"delegated card {field_name} must be a mapping")
    return value


def _sequence(value: Any, *, field_name: str) -> list[Any]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError(f"delegated card {field_name} must be a list")
    return list(value)


def _strings(value: Any, *, field_name: str) -> tuple[str, ...]:
    result = tuple(_clean(item) for item in _sequence(value, field_name=field_name))
    if "" in result or len(result) != len(set(result)):
        raise ValueError(f"delegated card {field_name} contains invalid values")
    return result


def _integer(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"delegated card {field_name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"delegated card {field_name} must be an integer") from exc


def caller_kind_for(client_id: str, source: str) -> str:
    if is_resident_client_id(client_id):
        return CALLER_KIND_RESIDENT
    if _clean(source) == CALLER_KIND_OAUTH:
        return CALLER_KIND_OAUTH
    return CALLER_KIND_MANUAL


@dataclass(frozen=True)
class CardOperationView:
    name: str
    state: str
    accepted_digest: str = ""
    current_digest: str = ""
    policy: Mapping[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "state": self.state,
            "accepted_digest": self.accepted_digest,
            "current_digest": self.current_digest,
        }
        if self.policy is not None:
            payload["policy"] = dict(self.policy)
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CardOperationView":
        data = _mapping(value, field_name="operation")
        name = _clean(data.get("name"))
        state = _clean(data.get("state"))
        if not name or state not in {
            OPERATION_STATE_CURRENT,
            OPERATION_STATE_CHANGED,
            OPERATION_STATE_REMOVED,
            OPERATION_STATE_UNKNOWN,
        }:
            raise ValueError("delegated card operation is invalid")
        raw_policy = data.get("policy")
        policy = (
            None
            if raw_policy is None
            else dict(_mapping(raw_policy, field_name="operation policy"))
        )
        return cls(
            name=name,
            state=state,
            accepted_digest=_clean(data.get("accepted_digest")),
            current_digest=_clean(data.get("current_digest")),
            policy=policy,
        )


@dataclass(frozen=True)
class CardResourceView:
    resource: str
    kind: str
    state: str
    identity_scope: str
    grants: tuple[str, ...] = ()
    operations: tuple[CardOperationView, ...] = ()
    provider: str = ""
    label: str = ""
    accepted_revision: str = ""
    current_revision: str = ""
    accepted_digest: str = ""
    current_digest: str = ""
    named_service_operations: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "kind": self.kind,
            "provider": self.provider,
            "label": self.label,
            "state": self.state,
            "identity_scope": self.identity_scope,
            "grants": list(self.grants),
            "operations": [item.to_dict() for item in self.operations],
            "accepted_revision": self.accepted_revision,
            "current_revision": self.current_revision,
            "accepted_digest": self.accepted_digest,
            "current_digest": self.current_digest,
            "named_service_operations": {
                namespace: list(operations)
                for namespace, operations in self.named_service_operations.items()
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CardResourceView":
        data = _mapping(value, field_name="resource")
        resource = _clean(data.get("resource"))
        state = _clean(data.get("state"))
        identity_scope = _clean(data.get("identity_scope")) or "grantor"
        if not resource or state not in {
            OPERATION_STATE_CURRENT,
            OPERATION_STATE_CHANGED,
            OPERATION_STATE_REMOVED,
            OPERATION_STATE_UNKNOWN,
        }:
            raise ValueError("delegated card resource is invalid")
        operations = tuple(
            CardOperationView.from_dict(
                _mapping(item, field_name="resource operation")
            )
            for item in _sequence(
                data.get("operations", []), field_name="resource operations"
            )
        )
        if len({item.name for item in operations}) != len(operations):
            raise ValueError("delegated card resource operations are duplicated")
        raw_named = _mapping(
            data.get("named_service_operations", {}),
            field_name="named service operations",
        )
        named_service_operations = {
            _clean(namespace): _strings(
                selected,
                field_name=f"named service operations for {_clean(namespace)}",
            )
            for namespace, selected in raw_named.items()
            if _clean(namespace)
        }
        if len(named_service_operations) != len(raw_named):
            raise ValueError("delegated card named service namespace is invalid")
        return cls(
            resource=resource,
            kind=_clean(data.get("kind")),
            provider=_clean(data.get("provider")),
            label=_clean(data.get("label")),
            state=state,
            identity_scope=identity_scope,
            grants=_strings(data.get("grants", []), field_name="resource grants"),
            operations=operations,
            accepted_revision=_clean(data.get("accepted_revision")),
            current_revision=_clean(data.get("current_revision")),
            accepted_digest=_clean(data.get("accepted_digest")),
            current_digest=_clean(data.get("current_digest")),
            named_service_operations=named_service_operations,
        )


@dataclass(frozen=True)
class DelegatedCardView:
    access_id: str
    client_id: str
    caller_kind: str
    grantor_subject: str
    delegate_subject: str
    source: str
    label: str
    card_revision: int
    catalog_version: str
    state: str
    created_at: int
    expires_at: int
    identity_scope: str
    resources: tuple[CardResourceView, ...] = ()
    profile: ResidentCallerProfile | None = None
    account_scope: Mapping[str, Mapping[str, tuple[str, ...]]] = field(default_factory=dict)
    provenance: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_resident(self) -> bool:
        return self.caller_kind == CALLER_KIND_RESIDENT

    def resource(self, resource: str) -> CardResourceView | None:
        key = _clean(resource)
        for entry in self.resources:
            if entry.resource == key:
                return entry
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_id": self.access_id,
            "client_id": self.client_id,
            "caller_kind": self.caller_kind,
            "profile": self.profile.to_dict() if self.profile is not None else None,
            "grantor_subject": self.grantor_subject,
            "delegate_subject": self.delegate_subject,
            "source": self.source,
            "label": self.label,
            "card_revision": self.card_revision,
            "catalog_version": self.catalog_version,
            "state": self.state,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "identity_scope": self.identity_scope,
            "resources": [item.to_dict() for item in self.resources],
            "account_scope": {
                provider: {account: list(claims) for account, claims in accounts.items()}
                for provider, accounts in self.account_scope.items()
            },
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DelegatedCardView":
        """Parse the public payload emitted by :meth:`to_dict`.

        The resident profile is derived again from the grantor and client id;
        a supplied profile must agree with that identity exactly. This keeps
        transport adapters from trusting a second identity formula.
        """

        data = _mapping(value, field_name="view")
        access_id = _clean(data.get("access_id"))
        client_id = _clean(data.get("client_id"))
        grantor_subject = _clean(data.get("grantor_subject"))
        caller_kind = _clean(data.get("caller_kind"))
        identity_scope = _clean(data.get("identity_scope")) or "grantor"
        state = _clean(data.get("state"))
        if (
            not access_id
            or not client_id
            or not grantor_subject
            or caller_kind
            not in {CALLER_KIND_RESIDENT, CALLER_KIND_OAUTH, CALLER_KIND_MANUAL}
            or state not in {"active", "revoked"}
        ):
            raise ValueError("delegated card view identity is invalid")
        profile = ResidentCallerProfile.parse(grantor_subject, client_id)
        expected_kind = caller_kind_for(client_id, _clean(data.get("source")))
        if caller_kind != expected_kind:
            raise ValueError("delegated card caller kind does not match its client")
        raw_profile = data.get("profile")
        if raw_profile is not None:
            if profile is None or dict(
                _mapping(raw_profile, field_name="resident profile")
            ) != profile.to_dict():
                raise ValueError("delegated card resident profile is invalid")
        elif profile is not None:
            raise ValueError("delegated card resident profile is missing")

        resources = tuple(
            CardResourceView.from_dict(
                _mapping(item, field_name="resource")
            )
            for item in _sequence(data.get("resources", []), field_name="resources")
        )
        if len({item.resource for item in resources}) != len(resources):
            raise ValueError("delegated card resources are duplicated")

        raw_account_scope = _mapping(
            data.get("account_scope", {}), field_name="account scope"
        )
        account_scope: dict[str, dict[str, tuple[str, ...]]] = {}
        for provider, raw_accounts in raw_account_scope.items():
            provider_id = _clean(provider)
            if not provider_id:
                raise ValueError("delegated card account provider is invalid")
            accounts = _mapping(raw_accounts, field_name="provider account scope")
            parsed_accounts: dict[str, tuple[str, ...]] = {}
            for account, raw_claims in accounts.items():
                account_id = _clean(account)
                if not account_id:
                    raise ValueError("delegated card account id is invalid")
                parsed_accounts[account_id] = _strings(
                    raw_claims,
                    field_name=f"account claims for {provider_id}:{account_id}",
                )
            account_scope[provider_id] = parsed_accounts

        card_revision = _integer(data.get("card_revision"), field_name="revision")
        created_at = _integer(data.get("created_at"), field_name="created_at")
        expires_at = _integer(data.get("expires_at"), field_name="expires_at")
        if card_revision < 0 or created_at < 0 or expires_at < 0:
            raise ValueError("delegated card timestamps or revision are invalid")
        return cls(
            access_id=access_id,
            client_id=client_id,
            caller_kind=caller_kind,
            profile=profile,
            grantor_subject=grantor_subject,
            delegate_subject=_clean(data.get("delegate_subject")),
            source=_clean(data.get("source")),
            label=_clean(data.get("label")),
            card_revision=card_revision,
            catalog_version=_clean(data.get("catalog_version")),
            state=state,
            created_at=created_at,
            expires_at=expires_at,
            identity_scope=identity_scope,
            resources=resources,
            account_scope=account_scope,
            provenance=dict(
                _mapping(data.get("provenance", {}), field_name="provenance")
            ),
        )


def _policy_index(
    policies: Iterable[Any],
) -> dict[tuple[str, str], dict[str, Any]]:
    """``(resource, operation) -> public policy`` for card-wide outer policies.

    Account-specific overrides are not folded in: they qualify one connected
    account and are reported through the account view when Gateway needs them.
    """
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for policy in policies or ():
        public = policy.to_public_dict() if hasattr(policy, "to_public_dict") else policy
        if not isinstance(public, Mapping):
            continue
        authority = public.get("authority")
        authority = authority if isinstance(authority, Mapping) else {}
        if _clean(authority.get("surface") or "outer") != "outer":
            continue
        if _clean(authority.get("account_id")) or _clean(authority.get("account")):
            continue
        key = (_clean(authority.get("resource")), _clean(authority.get("operation")))
        if key[0] and key[1]:
            out[key] = dict(public)
    return out


def build_card_view(
    authority: CardAuthority,
    *,
    resource_states: Mapping[str, Mapping[str, Any]] | None = None,
    row_for: Callable[[str], Any] | None = None,
    policies: Iterable[Any] = (),
) -> DelegatedCardView:
    """Assemble the view from committed authority and current facts.

    ``resource_states`` is the per-resource drift block (see
    ``catalog.drift.resource_states``); without it every resource reads as
    ``unknown``. ``row_for`` resolves the current governing row for labels and
    kinds when the state block does not name them.
    """
    states = dict(resource_states or {})
    index = _policy_index(policies)
    profile = ResidentCallerProfile.parse(
        authority.grantor_subject,
        authority.client_id,
    )
    inner = selected_named_service_operations(authority)
    resources: list[CardResourceView] = []
    for raw_resource, grants in authority.resource_grants.items():
        resource = _clean(raw_resource)
        if not resource:
            continue
        state = dict(states.get(resource) or {})
        row = row_for(resource) if row_for is not None else None
        accepted = authority.resource_acceptance.get(resource)
        kind = (
            _clean(state.get("kind"))
            or (accepted.kind if accepted is not None else "")
            or _clean(getattr(row, ROW_ATTR_KIND, ""))
            or RESOURCE_KIND_CATALOG
        )
        provider = (
            (accepted.provider if accepted is not None else "")
            or _clean(getattr(row, ROW_ATTR_PROVIDER, ""))
        )
        status = _clean(state.get("status")) or OPERATION_STATE_UNKNOWN
        changed = set(state.get("changed_operations") or ())
        removed = set(state.get("removed_operations") or ())
        current_ops = {}
        if row is not None:
            from connection_hub.delegated_credentials.catalog.descriptors import (
                row_acceptance,
            )

            current_ops = row_acceptance(
                row, catalog_version=_clean(state.get("current_revision"))
            ).operations
        operations: list[CardOperationView] = []
        for name in authority.resource_operations.get(resource, ()):
            if name in removed:
                op_state = OPERATION_STATE_REMOVED
            elif name in changed:
                op_state = OPERATION_STATE_CHANGED
            elif status == OPERATION_STATE_UNKNOWN:
                op_state = OPERATION_STATE_UNKNOWN
            elif status == "removed":
                op_state = OPERATION_STATE_REMOVED
            else:
                op_state = OPERATION_STATE_CURRENT
            operations.append(
                CardOperationView(
                    name=name,
                    state=op_state,
                    accepted_digest=(
                        accepted.operations.get(name, "") if accepted is not None else ""
                    ),
                    current_digest=_clean(current_ops.get(name, "")),
                    policy=index.get((resource, name)),
                )
            )
        resources.append(
            CardResourceView(
                resource=resource,
                kind=kind,
                provider=provider,
                label=_clean(getattr(row, "label", "")),
                state=status,
                identity_scope=(
                    _clean(getattr(row, "identity_scope", ""))
                    or authority.identity_scope
                    or "grantor"
                ),
                grants=tuple(grants),
                operations=tuple(operations),
                accepted_revision=_clean(state.get("accepted_revision")),
                current_revision=_clean(state.get("current_revision")),
                accepted_digest=_clean(state.get("accepted_digest")),
                current_digest=_clean(state.get("current_digest")),
                named_service_operations={
                    namespace: tuple(sorted(operations))
                    for namespace, operations in (inner.get(resource) or {}).items()
                },
            )
        )
    return DelegatedCardView(
        access_id=authority.access_id,
        client_id=authority.client_id,
        caller_kind=caller_kind_for(authority.client_id, authority.source),
        profile=profile,
        grantor_subject=authority.grantor_subject,
        delegate_subject=authority.delegate_subject,
        source=authority.source,
        label=authority.label,
        card_revision=authority.card_revision,
        catalog_version=authority.catalog_version,
        state=authority.state,
        created_at=authority.created_at,
        expires_at=authority.expires_at,
        identity_scope=authority.identity_scope or "grantor",
        resources=tuple(resources),
        account_scope=authority.account_scope,
        provenance=authority.provenance,
    )


OFFER_COMPATIBLE = "compatible"
OFFER_ALREADY_ON_CARD = "already_on_card"
OFFER_IDENTITY_SCOPE_INCOMPATIBLE = "identity_scope_incompatible"
OFFER_ADMIN_ONLY = "admin_only"


def compatible_resource_offers(
    *,
    card_resources: Iterable[str],
    card_identity_scope: str,
    options: Iterable[Mapping[str, Any]],
    platform_admin: bool = False,
) -> list[dict[str, Any]]:
    """Which owner-visible delegable resources may join this card, and why the
    others may not.

    ``options`` are the grantor's ``resource_options`` rows (already filtered
    to what the grantor may delegate and, for connectors, to the grantor's own
    connectors). This is the filtering seam a resident ceiling narrows further;
    it embeds no runtime rule of its own.
    """
    held = {_clean(item) for item in card_resources if _clean(item)}
    scope = _clean(card_identity_scope) or "grantor"
    offers: list[dict[str, Any]] = []
    for option in options or ():
        resource = _clean(option.get("resource"))
        if not resource:
            continue
        option_scope = _clean(option.get("identity_scope")) or "grantor"
        if resource in held:
            reason = OFFER_ALREADY_ON_CARD
        elif bool(option.get("admin_only")) and not platform_admin:
            reason = OFFER_ADMIN_ONLY
        elif option_scope != scope:
            reason = OFFER_IDENTITY_SCOPE_INCOMPATIBLE
        else:
            reason = OFFER_COMPATIBLE
        offers.append(
            {
                "resource": resource,
                "label": _clean(option.get("label")) or resource,
                "identity_scope": option_scope,
                "compatible": reason == OFFER_COMPATIBLE,
                "reason": reason,
                "card_identity_scope": scope,
            }
        )
    return offers


__all__ = [
    "CALLER_KIND_MANUAL",
    "CALLER_KIND_OAUTH",
    "CALLER_KIND_RESIDENT",
    "OFFER_ADMIN_ONLY",
    "OFFER_ALREADY_ON_CARD",
    "OFFER_COMPATIBLE",
    "OFFER_IDENTITY_SCOPE_INCOMPATIBLE",
    "OPERATION_STATE_CHANGED",
    "OPERATION_STATE_CURRENT",
    "OPERATION_STATE_REMOVED",
    "OPERATION_STATE_UNKNOWN",
    "CardOperationView",
    "CardResourceView",
    "DelegatedCardView",
    "build_card_view",
    "caller_kind_for",
    "compatible_resource_offers",
]
