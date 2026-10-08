# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Request-local hosting of the SDK's original code-to-pair workflow.

This module selects no backend, installs no schema and creates no decision.
It uses the host's already activated authorities and the Hub's sole issuance
decision. A configured-but-unavailable binding remains present and closed;
it must never fall through to the older consume-and-mint path.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping
from urllib.parse import urlsplit

from connection_hub.delegated_credentials.cards.model import CardAuthority
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.oauth_issuance import OAuthIssuancePlan


class OriginalExchangeHostingUnavailable(RuntimeError):
    """Finite host-composition reason, with no provider or request details."""


async def prepare_original_exchange_metadata(*, pg_pool: Any, tenant: str, project: str) -> None:
    """The existing governed bundle-load preparation boundary, never HTTP."""
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.oauth.original_exchange_store import PostgresOriginalExchangeStore
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.oauth.original_refresh_store import PostgresOriginalRefreshStore
    await PostgresOriginalExchangeStore(pg_pool=pg_pool, tenant=tenant, project=project).ensure_schema()
    await PostgresOriginalRefreshStore(pg_pool=pg_pool, tenant=tenant, project=project).ensure_schema()


def configured_issuer(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise OriginalExchangeHostingUnavailable("original_exchange_issuer_not_configured")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme in {"https", "http"} and bool(parsed.hostname)
                 and parsed.username is None and parsed.password is None
                 and not parsed.query and not parsed.fragment)
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise OriginalExchangeHostingUnavailable("original_exchange_issuer_not_configured")
    return value.rstrip("/")


def custody_namespace(tenant: str, project: str) -> str:
    scope = json.dumps({"tenant": tenant, "project": project}, sort_keys=True,
                       separators=(",", ":"), ensure_ascii=True)
    return "connection-hub.oauth-original." + hashlib.sha256(scope.encode()).hexdigest()


async def consumed_candidate_inputs(*, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve candidate-shaping choices from the consumed server record.

    The SDK validates the actual client, redirect and PKCE proof before calling
    this function. No browser plan, factory or provider selector is accepted.
    The SDK owns canonical argument normalization and Card label derivation;
    Hub owns invocation-policy validation and effects in that same issuance.
    """
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.oauth.card_labels import oauth_card_label
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.oauth.original_candidate_inputs import oauth_issuance_arguments
    metadata = payload.get("client_metadata") or {}
    if not isinstance(metadata, Mapping):
        raise OriginalExchangeHostingUnavailable("original_exchange_metadata_invalid")
    asserted = metadata.get("client_metadata")
    answer = {
        "grantor_subject": payload["sub"], "client_id": payload["client_id"],
        "client_label": oauth_card_label(metadata, resource=payload.get("resource") or "",
                                         explicit=payload.get("card_label") or ""),
        "scopes": payload.get("scopes") or [],
        "operations": payload.get("operations"),
        "resource_grants": payload.get("resource_grants"),
        "resource_operations": payload.get("resource_operations"),
        "resource": payload.get("resource") or "",
        "access_id": payload.get("registry_access_id") or "",
        "card_kind": payload.get("card_kind") or "",
        "identity_scope": payload.get("identity_scope") or "",
        "account_scope": payload.get("account_scope"),
        "named_service_operations": payload.get("named_service_operations"),
        "catalog_version": payload.get("catalog_version") or "",
        "client_metadata": asserted if isinstance(asserted, Mapping) else {},
        "properties": payload.get("properties"),
        "replace_authority": True,
        "expected_card_revision": payload.get("expected_card_revision"),
    }
    if "invocation_policies" in payload:
        answer["invocation_policies"] = payload["invocation_policies"]
    return oauth_issuance_arguments(answer)


class OriginalTarget:
    """Full immutable issuance candidate plus authoritative current pointer."""

    def __init__(self, *, tenant: str, project: str, hub: Any, issuance_store: Any, cards: Any):
        self.tenant, self.project = tenant, project
        self.hub, self.issuance_store, self.cards = hub, issuance_store, cards

    async def candidate(self, plan: OAuthIssuancePlan) -> tuple[CardAuthority | None, CardAuthority]:
        if (type(plan) is not OAuthIssuancePlan
                or (plan.tenant, plan.project) != (self.tenant, self.project)
                or (self.issuance_store.tenant, self.issuance_store.project) != (self.tenant, self.project)):
            raise OriginalExchangeHostingUnavailable("original_exchange_namespace_mismatch")
        # The public Hub reader authenticates the stored plan against its real
        # decision. The scoped durable row also retains the complete Card kind,
        # original and properties, which the narrow public plan does not expose.
        authenticated = await self.hub.read_oauth_issuance_plan(transaction_id=plan.transaction_id)
        if type(authenticated) is not OAuthIssuancePlan or authenticated.to_dict() != plan.to_dict():
            raise OriginalExchangeHostingUnavailable("original_exchange_plan_mismatch")
        row = await self.issuance_store.read_issuance_plan(plan.transaction_id)
        if not isinstance(row, Mapping) or row.get("transaction_id") != plan.transaction_id:
            raise OriginalExchangeHostingUnavailable("original_exchange_plan_missing")
        raw = row.get("plan")
        if not isinstance(raw, Mapping):
            raise OriginalExchangeHostingUnavailable("original_exchange_plan_mismatch")
        pinned = OAuthIssuancePlan.from_mapping({**raw, "transaction_id": plan.transaction_id,
                                                 "intent_digest": plan.intent_digest})
        if pinned.to_dict() != plan.to_dict():
            raise OriginalExchangeHostingUnavailable("original_exchange_plan_mismatch")
        intent = raw.get("intent")
        if not isinstance(intent, Mapping):
            raise OriginalExchangeHostingUnavailable("original_exchange_plan_mismatch")
        candidate = CardAuthority.from_mapping(intent["candidate"])
        original = (None if intent.get("original") is None
                    else CardAuthority.from_mapping(intent["original"]))
        identity = (plan.access_id, plan.grantor_subject, plan.client_id, plan.credential_subject)
        if ((candidate.access_id, candidate.grantor_subject, candidate.client_id, candidate.delegate_subject) != identity
                or candidate.card_revision != plan.candidate_revision
                or candidate.expires_at != plan.expires_at
                or candidate.content_hash() != plan.card_content_hash
                or candidate.state != "active"
                or (original is None) != (plan.base_revision == 0)
                or (original is not None and (
                    original.card_revision != plan.base_revision
                    or (original.access_id, original.grantor_subject, original.client_id,
                        original.delegate_subject) != identity))):
            raise OriginalExchangeHostingUnavailable("original_exchange_candidate_mismatch")
        return original, candidate

    async def fence(self, *, plan: OAuthIssuancePlan, result: Any) -> bool:
        original, candidate = await self.candidate(plan)
        now = await self.issuance_store.issuance_clock()
        if now >= min(plan.expires_at, plan.delivery_deadline):
            return False
        current = await self.cards.read_current_authority(
            subject_hash=subject_hash_for(plan.grantor_subject), access_id=plan.access_id)
        if result.state == "pending":
            if now >= plan.reserved_until:
                return False
            if original is None:
                return current is None
            return (current is not None and current[1].to_dict() == original.to_dict()
                    and current[1].state == "active")
        return (result.state == "committed" and current is not None
                and current[1].to_dict() == candidate.to_dict()
                and current[1].state == "active")


class OriginalPairHost:
    """Select kind only from the original immutable, authenticated candidate."""

    def __init__(self, *, target: OriginalTarget, refresh_store: Any, custody: Any,
                 signer: Any, refresh_ttl_seconds: int, authority_factory: Any):
        self.target, self.refresh_store, self.custody = target, refresh_store, custody
        self.signer, self.refresh_ttl_seconds = signer, refresh_ttl_seconds
        self.authority_factory = authority_factory

    async def _provider(self, plan):
        from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.oauth.original_pair_provider import OriginalCredentialPairProvider
        _, candidate = await self.target.candidate(plan)
        return OriginalCredentialPairProvider(
            refresh_store=self.refresh_store, custody=self.custody,
            custody_namespace=self.custody.namespace, refresh_signer=self.signer,
            card_kind=candidate.card_kind, refresh_ttl_seconds=self.refresh_ttl_seconds,
            authority_factory=self.authority_factory)

    async def prepare_pair(self, *, plan, access_expires_at):
        return await (await self._provider(plan)).prepare_pair(plan=plan, access_expires_at=access_expires_at)

    async def read_pair(self, *, plan, access_expires_at):
        return await (await self._provider(plan)).read_pair(plan=plan, access_expires_at=access_expires_at)

    async def activate_access(self, *, plan, result, credential):
        return await (await self._provider(plan)).activate_access(plan=plan, result=result, credential=credential)

    async def retire_pair(self, *, plan, result, access_expires_at):
        return await (await self._provider(plan)).retire_pair(plan=plan, result=result,
                                                             access_expires_at=access_expires_at)


async def bind_original_exchange(request: Any, *, tenant: str, project: str, pg_pool: Any,
                                 configured_public_issuer: object, hub: Any, grant_store: Any,
                                 issuance_store: Any, cards: Any, settings: Any,
                                 refresh_signing_secret_ref: object, resolve_secret: Any) -> None:
    """Bind real capabilities after configuration checks, never perform DDL."""
    request.state.oauth_original_exchange_factory = None
    issuer = configured_issuer(configured_public_issuer)
    if pg_pool is None or not callable(resolve_secret):
        raise OriginalExchangeHostingUnavailable("original_exchange_host_not_bound")
    if (type(refresh_signing_secret_ref) is not str or not refresh_signing_secret_ref
            or refresh_signing_secret_ref != refresh_signing_secret_ref.strip()):
        raise OriginalExchangeHostingUnavailable("original_exchange_signer_not_configured")
    from kdcube_ai_app.auth.bundle import get_bundle_session_authority
    from kdcube_ai_app.auth.session_authority_runtime import bundle_session_store_for
    from kdcube_ai_app.infra.secrets.issuance import issuance_secret_custody
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.oauth.original_code_flow import OriginalCodeExchangeFlow
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.oauth.original_exchange_store import PostgresOriginalExchangeStore
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.oauth.original_refresh_store import PostgresOriginalRefreshStore
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.oauth.original_refresh_issuer import HmacOriginalRefreshSigner
    if bundle_session_store_for(tenant=tenant, project=project) is None:
        raise OriginalExchangeHostingUnavailable("original_exchange_session_authority_not_bound")
    custody = issuance_secret_custody(namespace=custody_namespace(tenant, project), settings=settings)
    await custody.qualify()

    async def signing_key():
        value = await resolve_secret(refresh_signing_secret_ref)
        return value.encode("utf-8") if type(value) is str else value

    target = OriginalTarget(tenant=tenant, project=project, hub=hub, issuance_store=issuance_store, cards=cards)
    provider = OriginalPairHost(target=target,
        refresh_store=PostgresOriginalRefreshStore(pg_pool=pg_pool, tenant=tenant, project=project),
        custody=custody, signer=HmacOriginalRefreshSigner(tenant, project, signing_key),
        refresh_ttl_seconds=grant_store.refresh_ttl, authority_factory=get_bundle_session_authority)
    flow = OriginalCodeExchangeFlow(
        ledger=PostgresOriginalExchangeStore(pg_pool=pg_pool, tenant=tenant, project=project),
        grant_store=grant_store, hub=hub, provider=provider, custody=custody,
        candidate_inputs=consumed_candidate_inputs, fence_target=target.fence)
    request.state.oauth_delegated_issuer = issuer
    request.state.oauth_original_exchange_factory = flow.handler
