"""W502: the trusted descriptor of each application allowed to drive the Hub's Card participant.

``connections.card_transactions.callers.<service_id>`` (bundle props, never a
request) names, for one peer:

- ``request_secret_ref``: verifies the peer's admission proof on
  ``card_transaction_participant``;
- ``receipt_secret_ref`` and ``receipt_signer_id``: sign the Hub's answers;
- ``audience``: the peer's bundle, bound into every answer;
- ``hub_resource``: the Hub bundle the peer's ``AdmissionRequest`` names;
- ``scope_field``: the verified intent payload key a request's scope must
  equal, and the request field the scope is sent as (``project_ref`` for
  Problem Board);
- ``census_scope_prefix``: the scopes this caller may read with
  ``card_census_read`` (``work:project:`` for Problem Board); absent, the
  caller gets no census;
- ``plan_scope_prefix``: the scopes this caller may plan in with
  ``card_lifecycle_plan`` (W578); absent, the caller gets no planning;
- ``authority``: the peer's transaction authority, which the Hub reads back
  through: ``service_id``, ``audience`` and ``secret_ref`` verify its signed
  responses; ``request_signer_id`` and ``request_secret_ref`` sign the Hub's
  request proof; ``binding`` is the #602 ``AuthorityBindingConfig``.

Secrets are resolved by reference only and never logged. A caller whose
descriptor or secrets are incomplete is left out, so its requests are refused
as an unknown caller rather than guessed.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from service_foundation.coordination.durable_wire import canonical_json_bytes, sha256_hex

from ..admission import AdmissionRequest, sign_admission_request
from .authority_intent_source import AuthorityCardIntentSource, AuthorityDecisionReader, CardAuthorityBinding
from .authority_transport import AuthorityBindingConfig, configured_authority_fetch
from .card_participant import HubCardParticipant
from .lifecycle_plan_authorization import PeerLifecyclePlanAuthorization
from .participant_operation import MIN_SECRET_BYTES, ParticipantCaller, ScopeBinding
from .transaction_authority_v2 import PROTOCOL, TransactionAuthorityRefused

LOGGER = logging.getLogger("kdcube.connection_hub.card_transactions")
_TEXT = ("request_secret_ref", "receipt_secret_ref", "receipt_signer_id", "audience", "hub_resource")
_AUTHORITY_TEXT = ("service_id", "audience", "secret_ref", "request_signer_id", "request_secret_ref")


@dataclass(frozen=True)
class ParticipantCallerDescriptor:
    service_id: str
    request_secret_ref: str
    receipt_secret_ref: str
    receipt_signer_id: str
    audience: str
    hub_resource: str
    scope_field: str
    census_scope_prefix: str
    plan_scope_prefix: str
    authority_service_id: str
    authority_audience: str
    authority_secret_ref: str
    authority_request_signer_id: str
    authority_request_secret_ref: str
    binding: AuthorityBindingConfig

    @classmethod
    def from_mapping(cls, service_id: str, raw: Any) -> "ParticipantCallerDescriptor":
        if not isinstance(raw, Mapping) or not isinstance(raw.get("authority"), Mapping):
            raise ValueError("participant_caller_descriptor_invalid")
        authority = raw["authority"]
        values = {name: raw.get(name) for name in _TEXT}
        values.update({f"authority_{name}": authority.get(name) for name in _AUTHORITY_TEXT})
        scope_field = raw.get("scope_field") or ""
        census_scope_prefix = raw.get("census_scope_prefix") or ""
        plan_scope_prefix = raw.get("plan_scope_prefix") or ""
        if (type(service_id) is not str or not service_id
                or any(type(value) is not str or not value for value in values.values())
                or type(scope_field) is not str or type(census_scope_prefix) is not str
                or type(plan_scope_prefix) is not str):
            raise ValueError("participant_caller_descriptor_invalid")
        binding = AuthorityBindingConfig.from_mapping(authority.get("binding"))
        if scope_field and scope_field in binding.request_fields:
            # The scope comes from the verified request, never fixed in the descriptor.
            raise ValueError("participant_caller_descriptor_invalid")
        return cls(service_id=service_id, scope_field=scope_field, census_scope_prefix=census_scope_prefix,
                   plan_scope_prefix=plan_scope_prefix, binding=binding,
                   authority_service_id=values.pop("authority_service_id"),
                   authority_audience=values.pop("authority_audience"),
                   authority_secret_ref=values.pop("authority_secret_ref"),
                   authority_request_signer_id=values.pop("authority_request_signer_id"),
                   authority_request_secret_ref=values.pop("authority_request_secret_ref"),
                   **values)


def participant_caller_descriptors(connections: Mapping[str, Any] | None) -> dict[str, ParticipantCallerDescriptor]:
    """Every well-formed caller descriptor; a malformed one is logged by id and left out."""
    section = (connections or {}).get("card_transactions") if isinstance(connections, Mapping) else None
    callers = section.get("callers") if isinstance(section, Mapping) else None
    found: dict[str, ParticipantCallerDescriptor] = {}
    for service_id, raw in (callers.items() if isinstance(callers, Mapping) else ()):
        try:
            found[service_id] = ParticipantCallerDescriptor.from_mapping(service_id, raw)
        except (ValueError, TransactionAuthorityRefused):
            LOGGER.warning("[connection-hub.card-transactions] participant caller descriptor invalid caller=%s",
                           service_id)
    return found


def _request_signer(*, secret: str, signer_id: str, binding: AuthorityBindingConfig):
    """The Hub's admission proof over its request to the authority (the #602 ``sign_request`` port)."""

    async def sign(unsigned: Mapping[str, Any]) -> Mapping[str, Any]:
        echo = unsigned["request_echo"]
        proof = {"service_id": signer_id, "timestamp": str(int(time.time())), "nonce": os.urandom(16).hex()}
        proof["signature"] = sign_admission_request(
            secret=secret, **proof, delegated_token=f"{PROTOCOL}:{echo}",
            request=AdmissionRequest(resource=binding.bundle_id, operation=binding.operation, invocation_id=echo,
                                     request_digest=sha256_hex(canonical_json_bytes(dict(unsigned))),
                                     approval_context={"protocol": PROTOCOL}))
        return proof

    return sign


@dataclass(frozen=True)
class BuiltCallers:
    callers: Mapping[str, ParticipantCaller]
    # authorities[service_id](scope) -> the reader the Card store routes that intent's decision to.
    authorities: Mapping[str, Callable[[str], AuthorityDecisionReader]]


async def build_participant_callers(
    connections: Mapping[str, Any] | None, *, resolve_secret: Callable[[str], Awaitable[str]],
    call: Callable[..., Awaitable[Any]], card_store: Any, card_service: Any,
) -> BuiltCallers:
    """Resolve each caller's secrets and bind its per-scope authority reader and participant."""
    callers: dict[str, ParticipantCaller] = {}
    authorities: dict[str, Callable[[str], AuthorityDecisionReader]] = {}
    for service_id, descriptor in participant_caller_descriptors(connections).items():
        refs = (descriptor.request_secret_ref, descriptor.receipt_secret_ref, descriptor.authority_secret_ref,
                descriptor.authority_request_secret_ref)
        try:
            secrets = [str(await resolve_secret(ref) or "") for ref in refs]
        except Exception:  # noqa: BLE001 - a secret store failure leaves this caller out
            secrets = []
        if len(secrets) != len(refs) or any(len(value.encode("utf-8")) < MIN_SECRET_BYTES for value in secrets):
            LOGGER.warning("[connection-hub.card-transactions] participant caller secrets unavailable caller=%s",
                           service_id)
            continue
        request_secret, receipt_secret, authority_secret, signer_secret = secrets
        authority = CardAuthorityBinding(secret=authority_secret, service_id=descriptor.authority_service_id,
                                         audience=descriptor.authority_audience)
        sign = _request_signer(secret=signer_secret, signer_id=descriptor.authority_request_signer_id,
                               binding=descriptor.binding)

        def fetch_for(scope: str, *, descriptor=descriptor, sign=sign):
            fields = dict(descriptor.binding.request_fields)
            if descriptor.scope_field:
                fields[descriptor.scope_field] = scope
            config = {"bundle_id": descriptor.binding.bundle_id, "operation": descriptor.binding.operation,
                      "refusals": dict(descriptor.binding.refusals), "request_fields": fields,
                      "response_key": descriptor.binding.response_key}
            return configured_authority_fetch(call=call, binding_config=config, sign_request=sign)

        def reader_for(scope: str, *, fetch_for=fetch_for, authority=authority):
            return AuthorityDecisionReader(fetch=fetch_for(scope), authority=authority)

        def bind(scope: str, *, fetch_for=fetch_for, authority=authority, descriptor=descriptor):
            fetch = fetch_for(scope)
            decisions = AuthorityDecisionReader(fetch=fetch, authority=authority)
            intents = AuthorityCardIntentSource(store=card_store, fetch=fetch, authority=authority,
                                                authority_id=descriptor.service_id,
                                                scope_field=descriptor.scope_field)
            return ScopeBinding(participant=HubCardParticipant(service=card_service, store=card_store,
                                                               intents=intents, decisions=decisions),
                                decisions=decisions)

        callers[service_id] = ParticipantCaller(
            service_id=service_id, request_secret=request_secret, receipt_secret=receipt_secret,
            receipt_signer_id=descriptor.receipt_signer_id, audience=descriptor.audience,
            hub_resource=descriptor.hub_resource, bind=bind, scope_field=descriptor.scope_field,
            census_scope_prefix=descriptor.census_scope_prefix, plan_scope_prefix=descriptor.plan_scope_prefix,
            plan_authorization=PeerLifecyclePlanAuthorization(
                call=call, bundle_id=descriptor.binding.bundle_id,
                signer_id=descriptor.authority_request_signer_id, secret=signer_secret))
        authorities[service_id] = reader_for
    return BuiltCallers(callers=callers, authorities=authorities)


__all__ = ["BuiltCallers", "ParticipantCallerDescriptor", "build_participant_callers",
           "participant_caller_descriptors"]
