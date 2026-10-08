"""W502 phase 3: the Hub's transport to a Card binding's configured transaction authority.

``AuthorityCardIntentSource`` and ``AuthorityDecisionReader`` read a
transaction another application initiated only through a ``fetch`` port; this
module builds that port from the Card binding's CONFIGURATION, so generic Hub
code names no provider, operation or provider error code (Root, 18:11):

- ``operation`` and ``bundle_id``: the provider's authority operation;
- ``request_fields``: extra fixed request fields the provider needs (for
  Problem Board, its ``project_ref``), merged under the generic ones;
- ``refusals``: the exact map from the provider's refusal codes to the Hub's
  generic reasons; any other code is ``authority_refused``;
- ``response_key``: where the signed response sits in a successful body
  (absent: the body itself, without its ``ok`` flag).

The request carries the generic fields ``schema``, ``phase``,
``request_echo``, ``transaction_id`` and ``participant``, plus the caller's
``service_proof`` over them. The response is returned unchanged for
``verify_card_authority_v2``, which proves everything; this module only
moves bytes and translates refusals by exact code, never by prefix, and
never passes provider text through.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from .card_participant import PARTICIPANT
from .transaction_authority_v2 import PROTOCOL, TransactionAuthorityRefused

# The reasons the Hub's intent source and decision reader act on.
HUB_REASONS = frozenset({
    "authority_decision_pending", "authority_late_stage", "authority_intent_expired",
    "authority_transaction_unknown", "authority_unavailable", "authority_intent_mismatch",
})
_GENERIC_FIELDS = frozenset({"schema", "phase", "request_echo", "transaction_id", "participant", "service_proof"})


@dataclass(frozen=True)
class AuthorityBindingConfig:
    bundle_id: str
    operation: str
    refusals: Mapping[str, str]
    request_fields: Mapping[str, Any]
    response_key: str = ""

    @classmethod
    def from_mapping(cls, raw: Any) -> "AuthorityBindingConfig":
        if not isinstance(raw, Mapping):
            raise TransactionAuthorityRefused("authority_binding_invalid")
        bundle_id, operation = raw.get("bundle_id"), raw.get("operation")
        refusals, fields = raw.get("refusals") or {}, raw.get("request_fields") or {}
        response_key = raw.get("response_key") or ""
        if (type(bundle_id) is not str or not bundle_id or type(operation) is not str or not operation
                or not isinstance(refusals, Mapping) or not isinstance(fields, Mapping)
                or type(response_key) is not str
                or any(type(code) is not str or not code or reason not in HUB_REASONS
                       for code, reason in refusals.items())
                or any(type(name) is not str or name in _GENERIC_FIELDS for name in fields)):
            raise TransactionAuthorityRefused("authority_binding_invalid")
        return cls(bundle_id=bundle_id, operation=operation, refusals=dict(refusals),
                   request_fields=dict(fields), response_key=response_key)


AuthorityCall = Callable[..., Awaitable[Any]]
RequestSigner = Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]


def configured_authority_fetch(*, call: AuthorityCall, binding_config: Any, sign_request: RequestSigner):
    """``fetch(transaction_id, phase, request_echo)`` over the binding's configured authority."""

    config = AuthorityBindingConfig.from_mapping(binding_config)

    async def fetch(transaction_id: str, phase: str, request_echo: str) -> Mapping[str, Any]:
        unsigned = {**config.request_fields, "schema": PROTOCOL, "phase": phase, "request_echo": request_echo,
                    "transaction_id": transaction_id, "participant": PARTICIPANT}
        try:
            proof = await sign_request(dict(unsigned))
            body = await call(bundle_id=config.bundle_id, operation=config.operation,
                              data={**unsigned, "service_proof": proof})
        except TransactionAuthorityRefused:
            raise
        except Exception:  # noqa: BLE001 - transport failure, by name only
            raise TransactionAuthorityRefused("authority_unavailable") from None
        if not isinstance(body, Mapping):
            raise TransactionAuthorityRefused("authority_response_invalid")
        if body.get("ok") is not True:
            error = body.get("error")
            code = error.get("code") if isinstance(error, Mapping) else None
            raise TransactionAuthorityRefused(
                config.refusals.get(code, "authority_refused") if type(code) is str else "authority_refused")
        if config.response_key:
            response = body.get(config.response_key)
        else:
            response = {name: value for name, value in body.items() if name != "ok"}
        if not isinstance(response, Mapping):
            raise TransactionAuthorityRefused("authority_response_invalid")
        return response

    return fetch


__all__ = ["AuthorityBindingConfig", "HUB_REASONS", "configured_authority_fetch"]
