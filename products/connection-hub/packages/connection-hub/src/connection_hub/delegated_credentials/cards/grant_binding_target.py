# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W585 CP2: the Hub's ``grant_binding`` effect target, a bound session for a committed Card.

For one committed ``grant_binding`` effect the SDK issues, or recovers after a
crash, exactly one session whose bearer stays in create-only custody
(``issue_bound_session``). This target supplies only what the SDK trusts the
host for, all from committed state:

- the issuance context (``bound_issuance.build_issuance_context``): the actor
  from the committed projection, the decided payload, the committed revision;
- the subject ``integration:<client>:<grantor>`` from the COMMITTED Card (the
  receipt's ``after`` revision), never the payload;
- the session's roles and permissions from an injected, confirmed mapping of
  that committed Card and payload. Until the mapping is confirmed (W585 CP2
  question, 2026-10-06), the composition injects none and the target refuses;
- the custody, qualified by the secrets layer for whatever mode is
  configured (``require_production_custody``).

The receipt is the SDK's replay-stable public outcome; no bearer is held
here. The W582 helper still refuses ``grant_binding`` before any target until
the custody gates qualify, so composing this target mints nothing by itself.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping, Sequence

from .bound_issuance import (
    BoundIssuanceRefused, build_issuance_context, integration_user_id, require_production_custody,
)
from .participant_effects import EffectBinding, ParticipantEffectRefused

# authority(committed_card, payload) -> (roles, permissions)
SessionAuthority = Callable[[Any, Mapping[str, Any]], tuple[Sequence[str], Sequence[str]]]
# card_at(binding) -> the committed CardAuthority at the receipt's after revision
CommittedCardReader = Callable[[EffectBinding], Awaitable[Any]]

_CUSTODY_REASONS = frozenset({
    "issuance_custody_unavailable", "issuance_custody_namespace_invalid", "issuance_custody_unqualified",
    "issuance_custody_not_durable",
})


def _refusal(reason: str) -> ParticipantEffectRefused:
    """The helper passes only its allow-listed reasons; map ours onto them by kind."""
    if reason in _CUSTODY_REASONS:
        return ParticipantEffectRefused("card_effect_custody_unavailable")
    if reason == "issuance_target_revision_moved":
        return ParticipantEffectRefused("card_effect_target_revision_moved")
    if reason in ("issuance_authority_mapping_unconfirmed", "issuance_issuer_unavailable"):
        return ParticipantEffectRefused("card_effect_adapter_unavailable")
    return ParticipantEffectRefused("card_effect_binding_mismatch")


class GrantBindingTarget:
    """``apply_once`` issues or recovers the one bound session for this committed effect."""

    def __init__(self, *, issuer: Any, decisions: Any, card_at: CommittedCardReader, custody: Any,
                 production: bool, tenant: str, project: str, authority: SessionAuthority | None = None) -> None:
        self._issuer = issuer
        self._decisions = decisions
        self._card_at = card_at
        self._custody = custody
        self._production = production
        self._tenant = tenant
        self._project = project
        self._authority = authority

    async def apply_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        try:
            context = await build_issuance_context(binding=binding, payload=payload, decisions=self._decisions,
                                                   tenant=self._tenant, project=self._project)
            if self._authority is None:
                raise BoundIssuanceRefused("issuance_authority_mapping_unconfirmed")
            card = await self._card_at(binding)
            if card is None or card.access_id != context.access_id or card.card_revision != context.target_incarnation:
                raise BoundIssuanceRefused("issuance_target_revision_moved")
            user_id = integration_user_id(card.client_id, card.grantor_subject)
            roles, permissions = self._authority(card, payload)
            custody = await require_production_custody(self._custody, production=self._production)
            issue = getattr(self._issuer, "issue_bound_session", None)
            if not callable(issue):
                raise BoundIssuanceRefused("issuance_issuer_unavailable")
            receipt = await issue(context, user_id=user_id, roles=list(roles), permissions=list(permissions),
                                  custody=custody)
        except BoundIssuanceRefused as exc:
            raise _refusal(exc.reason) from None
        except Exception as exc:  # noqa: BLE001 - the SDK's named refusal, by class name, never its text
            if type(exc).__name__ != "SessionIssuanceRefused":
                raise
            reason = getattr(exc, "reason", "")
            raise _refusal("issuance_custody_unavailable" if str(reason).startswith("issuance_custody")
                           else "issuance_sdk_refused") from None
        if getattr(receipt, "outcome", None) not in ("issued", "recovered"):
            raise ParticipantEffectRefused("card_effect_binding_mismatch")
        return binding.effect_digest

    async def prepare_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        return binding.effect_digest  # nothing is minted before COMMIT

    async def release_once(self, binding: EffectBinding, payload: Mapping[str, Any]) -> str:
        return binding.effect_digest  # an aborted transaction never reserved a session


__all__ = ["CommittedCardReader", "GrantBindingTarget", "SessionAuthority"]
