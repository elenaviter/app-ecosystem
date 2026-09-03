# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Connected-account enforcement for PLAIN MCP tools.

A plain ``@mcp`` tool (an MCPServer tool on a managed bundle surface, with no
named-service registration behind it) declares which connected-account
provider claims each of its operations needs and enforces them at execution
time with ONE call. The declaration format is the existing application tool
shape parsed by :class:`ToolClaimPolicy.from_tool_config`::

    "my_tool": {
        "label": "...",
        "description": "...",
        "connections": {
            "delegated_to_kdcube": {
                "connected_accounts": [
                    {"provider_id": "slack", "claims": ["slack:search"]},
                ],
            },
        },
    }

``claims`` are the PROVIDER's own claim vocabulary - the claims a connected
account of that Delegated-to-KDCube provider row can actually hold
(``slack:search``, ``gmail:read``). The tool body then runs the check first::

    denial = await enforce_tool_requirements(
        request,
        tool_name="my_tool",
        operation="search",
        requirements=my_tool_requirements,
    )
    if denial is not None:
        return denial
    # proceed with the real provider work

The check resolves each required claim through the same account broker the
named-services door uses, so a plain MCP tool answers with the SAME demand
ordering and the SAME consent envelopes:

- every claim resolves -> ``None`` (proceed);
- the grantor has ZERO accounts on the backing provider -> the gate-2
  connect-first denial (``reason=connect_required`` with the guided connect
  plan and the agent hand-off);
- an account exists but cannot satisfy the call -> the account-level consent
  the resolver produced (``claim_upgrade_required``, ``agent_grant_required``,
  ``reconnect_required``, ``account_required``).

The worked reference is the ``productivity`` MCP surface of the
``kdcube-services@1-0`` example bundle
(``examples/bundles/kdcube-services@1-0/surfaces/mcp/productivity.py``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence


ConnectorAppsBinder = Callable[[Mapping[str, Any] | None], None]
ConnectorAppResolver = Callable[[str], str]
CredentialResolver = Callable[..., Awaitable[Any]]
CredentialViewResolver = Callable[[Any], Any]
ConnectFirstDenialBuilder = Callable[..., Awaitable[dict[str, Any] | None]]

logger = logging.getLogger(__name__)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def bind_service_connector_apps_from_config(
    config: Mapping[str, Any] | None,
    *,
    connector_apps_binder: ConnectorAppsBinder,
) -> None:
    """Bind the surface's provider -> connector-app declaration for this request.

    A plain MCP surface declares which connector app serves each provider in
    its own surface config block (``connector_apps: {slack: slack-demo,
    google: gmail}``), exactly like a named-services bridge does in its
    ``named_services.connector_apps`` block. Call this once per tool call
    (contextvar binding is request-scoped) before any claim resolution. A
    missing/empty declaration clears to provider-wide matching."""
    mapping = None
    if isinstance(config, Mapping):
        raw = config.get("connector_apps")
        mapping = raw if isinstance(raw, Mapping) else None
    connector_apps_binder(mapping)


def _operation_claims(requirement: Mapping[str, Any], operation: str) -> list[str]:
    """The provider claims ``operation`` needs under ``requirement``.

    ``claims_by_operation`` wins when it maps the operation; otherwise the
    requirement's flat ``claims`` list applies (a plain tool usually declares
    exactly the claims it needs, so the flat list IS the operation's need)."""
    op = _clean(operation)
    by_op = requirement.get("claims_by_operation")
    if isinstance(by_op, Mapping) and by_op:
        mapped = [_clean(c) for c in (by_op.get(op) or []) if _clean(c)]
        if mapped:
            return mapped
    return [_clean(c) for c in (requirement.get("claims") or []) if _clean(c)]


@dataclass(frozen=True)
class ResolvedToolAccount:
    """One requirement satisfied: which provider account answered it."""

    requirement_index: int
    provider_id: str
    connector_app_id: str
    account_id: str
    claims: tuple[str, ...]
    any_of: bool = False


@dataclass(frozen=True)
class ToolRequirementResolution:
    """What enforcement decided. ``denial`` is the envelope to return as the
    tool result when the call may not proceed; otherwise ``accounts`` names
    the account chosen for every requirement, which is how a tool declared
    with an ``any_of`` group learns WHICH provider it is about."""

    denial: dict[str, Any] | None = None
    accounts: tuple[ResolvedToolAccount, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.denial is None

    def account_for(self, index: int = 0) -> ResolvedToolAccount | None:
        for item in self.accounts:
            if item.requirement_index == index:
                return item
        return None


AccountsLister = Callable[[str], Awaitable[Sequence[Any]]]


async def enforce_tool_requirements(
    request: Any,
    *,
    tool_name: str,
    operation: str,
    requirements: Sequence[Mapping[str, Any]],
    account_id: str = "",
    tenant: str = "",
    project: str = "",
    hub_bundle_id: str = "connection-hub@1-0",
    identity: Mapping[str, Any] | None = None,
    resolution_source: Any = None,
    connector_app_resolver: ConnectorAppResolver | None = None,
    credential_resolver: CredentialResolver | None = None,
    credential_view_resolver: CredentialViewResolver | None = None,
    connect_first_denial_builder: ConnectFirstDenialBuilder | None = None,
    accounts_lister: AccountsLister | None = None,
) -> dict[str, Any] | None:
    """The denial-or-None form of :func:`resolve_tool_requirements`, kept for
    every tool that only needs to know whether it may proceed."""
    resolution = await resolve_tool_requirements(
        request,
        tool_name=tool_name,
        operation=operation,
        requirements=requirements,
        account_id=account_id,
        tenant=tenant,
        project=project,
        hub_bundle_id=hub_bundle_id,
        identity=identity,
        resolution_source=resolution_source,
        connector_app_resolver=connector_app_resolver,
        credential_resolver=credential_resolver,
        credential_view_resolver=credential_view_resolver,
        connect_first_denial_builder=connect_first_denial_builder,
        accounts_lister=accounts_lister,
    )
    return resolution.denial


async def resolve_tool_requirements(
    request: Any,
    *,
    tool_name: str,
    operation: str,
    requirements: Sequence[Mapping[str, Any]],
    account_id: str = "",
    tenant: str = "",
    project: str = "",
    hub_bundle_id: str = "connection-hub@1-0",
    identity: Mapping[str, Any] | None = None,
    resolution_source: Any = None,
    connector_app_resolver: ConnectorAppResolver | None = None,
    credential_resolver: CredentialResolver | None = None,
    credential_view_resolver: CredentialViewResolver | None = None,
    connect_first_denial_builder: ConnectFirstDenialBuilder | None = None,
    accounts_lister: AccountsLister | None = None,
) -> ToolRequirementResolution:
    """Enforce a plain MCP tool's declared connected-account requirements.

    ``requirements`` is the tool's ``connected_accounts`` declaration - each
    mapping ``{provider_id, claims, claims_by_operation?, connector_app_id?}``
    (the :class:`ToolClaimRequirement` shape). For each requirement the
    operation's needed claims are resolved through the shared account broker
    under the calling user's identity and the calling client's per-account
    binding (default-closed for delegated callers).

    ``account_id`` is the account selector from the tool's own input. Passing
    it here keeps preflight and the provider body on the same resolution: an
    ambiguous call returns ``account_required``; resending that same call with
    one candidate id resolves before provider work begins.

    Returns ``None`` when every claim resolves for that account - the tool body
    proceeds.

    On the FIRST unsatisfied provider the return value is a consent envelope
    (a dict the tool returns as its MCP result; the shared chat post-processor
    renders it as the consent banner):

    1. the connect-first denial when the grantor has ZERO usable accounts on
       that provider (``reason=connect_required``; the guided plan ends in the
       agent-grant hand-off) - computed via
       :func:`connect_first_denial_for_identity` with THIS requirement passed
       explicitly, no discovery involved;
    2. otherwise the account-level consent the resolver already produced
       (``claim_upgrade_required`` / ``agent_grant_required`` /
       ``reconnect_required`` / ``account_required``).

    This is the same demand ordering the named-services door applies.

    ``tenant``/``project`` default to the bound request identity. The caller
    must have bound the surface's connector-app declaration first
    (:func:`bind_service_connector_apps_from_config`)."""
    if credential_resolver is None:
        raise ValueError("credential_resolver is required")
    identity = dict(identity or {})
    tenant = _clean(tenant) or _clean(identity.get("tenant_id"))
    project = _clean(project) or _clean(identity.get("project_id"))
    source = resolution_source
    op = _clean(operation)
    name = _clean(tool_name)

    # Requirement loop. A flat entry must resolve (AND across entries). An
    # ``any_of`` group resolves through its alternatives: the ones whose
    # provider account resolves are the candidates; exactly one proceeds,
    # several without an explicit account_id answer account_required across
    # providers, none answers the connect-first "connect one of" denial.
    resolved: list[ResolvedToolAccount] = []
    for index, raw in enumerate(requirements or ()):
        if not isinstance(raw, Mapping):
            continue
        requirement = dict(raw)
        group = requirement.get("any_of")
        alternatives: list[dict[str, Any]] = (
            [dict(item) for item in group if isinstance(item, Mapping)]
            if isinstance(group, (list, tuple)) and group
            else [requirement]
        )
        is_group = len(alternatives) > 1 or bool(group)
        passed: list[tuple[dict[str, Any], list[str], Any]] = []
        failed: list[tuple[dict[str, Any], list[str], Any]] = []
        for alternative in alternatives:
            provider_id = _clean(alternative.get("provider_id"))
            if not provider_id:
                continue
            claims = _operation_claims(alternative, op)
            if not claims:
                continue
            connector_app_id = (
                _clean(alternative.get("connector_app_id"))
                or (
                    _clean(connector_app_resolver(provider_id))
                    if connector_app_resolver is not None
                    else ""
                )
            )
            failure: Any = None
            credential: Any = None
            for claim in claims:
                credential = await credential_resolver(
                    source,
                    provider_id=provider_id,
                    connector_app_id=connector_app_id,
                    claim=claim,
                    tool_name=name,
                    account_id=_clean(account_id),
                    connection_hub_bundle_id=hub_bundle_id,
                )
                if not credential.ok:
                    failure = credential
                    break
            resolved_alternative = (
                {**alternative, "connector_app_id": connector_app_id}
                if connector_app_id and not _clean(alternative.get("connector_app_id"))
                else alternative
            )
            if failure is None:
                passed.append((resolved_alternative, claims, credential))
            else:
                failed.append((alternative, claims, failure))
        if not passed and not failed:
            continue
        if passed:
            if is_group and len(passed) > 1 and not _clean(account_id):
                denial = await _cross_provider_account_required(
                    name=name,
                    passed=passed,
                    accounts_lister=accounts_lister,
                )
                if denial is not None:
                    return ToolRequirementResolution(denial=denial)
            alternative, claims, credential = passed[0]
            resolved.append(
                ResolvedToolAccount(
                    requirement_index=index,
                    provider_id=_clean(alternative.get("provider_id")),
                    connector_app_id=_clean(alternative.get("connector_app_id")),
                    account_id=_clean(getattr(credential, "account_id", "")),
                    claims=tuple(claims),
                    any_of=is_group,
                )
            )
            continue
        # Every alternative failed. Demand ordering, identical to the
        # named-services door: with ZERO usable accounts on the backing
        # provider(s) the CONNECT demand leads (the guided plan ends in the
        # agent-grant hand-off), and for a group it names every alternative so
        # the consent reads "connect one of". The requirements are passed
        # explicitly - no named-service discovery behind a plain MCP tool.
        if credential_view_resolver is None:
            raise ValueError(
                "credential_view_resolver is required for a denied requirement"
            )
        view = credential_view_resolver(request)
        grantor = _clean(view.grantor_user_id) or _clean(identity.get("user_id"))
        all_claims = sorted({claim for _alt, claims, _cred in failed for claim in claims})
        try:
            denial = None
            if connect_first_denial_builder is not None:
                denial = await connect_first_denial_builder(
                    grantor_user_id=grantor,
                    agent_client_id=view.agent_client_id,
                    agent_resource=view.resource,
                    namespace=name,
                    tool=name,
                    operation=op,
                    required=all_claims,
                    missing=all_claims,
                    tenant=tenant,
                    project=project,
                    hub_bundle_id=hub_bundle_id,
                    requirements=[alt for alt, _claims, _cred in failed],
                )
        except Exception:
            logger.warning(
                "[mcp-tool-enforcement] connect-first shaping failed (tool=%s providers=%s)",
                name, [alt.get("provider_id") for alt, _c, _f in failed], exc_info=True,
            )
            denial = None
        if denial is not None:
            logger.info(
                "[mcp-tool-enforcement] connect leads (tool=%s operation=%s providers=%s)",
                name, op, [alt.get("provider_id") for alt, _c, _f in failed],
            )
            return ToolRequirementResolution(denial=denial)
        # Account-level consent: for a group, prefer the alternative whose
        # failure is NOT "nothing connected" (an upgrade/grant/reconnect on a
        # real account says more than a connect prompt for the other one).
        _alt, _claims, chosen_failure = failed[0]
        for alt, claims, failure in failed:
            reason = _clean((getattr(failure, "error_payload", None) or {}).get("reason"))
            if reason and reason != "connect_required":
                chosen_failure = failure
                break
        logger.info(
            "[mcp-tool-enforcement] account consent (tool=%s operation=%s provider=%s claim=%s)",
            name, op, getattr(chosen_failure, "provider_id", ""), getattr(chosen_failure, "claim", ""),
        )
        return ToolRequirementResolution(denial=chosen_failure.error_envelope(where=name))
    return ToolRequirementResolution(accounts=tuple(resolved))


async def _cross_provider_account_required(
    *,
    name: str,
    passed: Sequence[tuple[dict[str, Any], list[str], Any]],
    accounts_lister: AccountsLister | None,
) -> dict[str, Any] | None:
    """Several alternatives of an any_of group resolved and no account was
    named: the same reason the broker mints for several accounts of ONE
    provider, raised across providers, with labeled candidates so a client
    renders the same choice UI and resends with account_id."""
    candidates: list[dict[str, Any]] = []
    for alternative, claims, credential in passed:
        provider_id = _clean(alternative.get("provider_id"))
        account_id = _clean(getattr(credential, "account_id", ""))
        label = ""
        email = ""
        if accounts_lister is not None and account_id:
            try:
                for account in await accounts_lister(provider_id):
                    if _clean(getattr(account, "account_id", "")) == account_id:
                        email = _clean(getattr(account, "email", ""))
                        label = _clean(getattr(account, "display_name", "")) or email or account_id
                        break
            except Exception:  # noqa: BLE001 - labels are a courtesy, never a gate
                logger.debug("[mcp-tool-enforcement] account label lookup failed", exc_info=True)
        candidates.append(
            {
                "account_id": account_id,
                "provider_id": provider_id,
                "connector_app_id": _clean(alternative.get("connector_app_id")),
                "claims": list(claims),
                "label": f"{label or account_id} ({provider_id})",
                "email": email,
            }
        )
    message = (
        "Several connected accounts on different providers can answer this "
        "call; choose one and resend with its account_id."
    )
    return {
        "ok": False,
        "error": {
            "code": "account_required",
            "message": message,
            "where": name,
            "retryable": True,
        },
        "ret": {
            "reason": "account_required",
            "candidates": candidates,
            "retry_same_request": False,
            "instructions": "Pick one account_id from candidates and call again with it set.",
        },
        "consent": {
            "kind": "account_choice",
            "reason": "account_required",
            "tool_name": name,
            "candidates": candidates,
        },
    }


__all__ = [
    "AccountsLister",
    "ConnectFirstDenialBuilder",
    "ConnectorAppResolver",
    "ConnectorAppsBinder",
    "CredentialResolver",
    "CredentialViewResolver",
    "bind_service_connector_apps_from_config",
    "enforce_tool_requirements",
    "ResolvedToolAccount",
    "ToolRequirementResolution",
    "resolve_tool_requirements",
]
