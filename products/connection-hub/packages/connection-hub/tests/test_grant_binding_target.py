"""W585 CP2: the grant_binding target issues one bound session for a committed Card, or refuses by name."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from connection_hub.delegated_credentials.cards import bound_issuance
from connection_hub.delegated_credentials.cards.grant_binding_target import GrantBindingTarget
from connection_hub.delegated_credentials.cards.participant_effects import ParticipantEffectRefused
from test_bound_issuance_context import PAYLOAD, _binding, _Custody, _Decisions, _intent, _record

CARD = SimpleNamespace(access_id="aut_card", card_revision=4, client_id="client-1", grantor_subject="user:owner")


class _Issuer:
    def __init__(self, outcome="issued", refuse=None):
        self.calls, self.outcome, self.refuse = [], outcome, refuse

    async def issue_bound_session(self, context, *, user_id, roles, permissions, custody):
        self.calls.append((context, user_id, roles, permissions, custody))
        if self.refuse:
            raise self.refuse
        return SimpleNamespace(session_id="bsn_1", secret_ref="0" * 32, bearer_sha256="b" * 64, outcome=self.outcome)


def _target(*, issuer=None, card=CARD, custody=None, authority=lambda card, payload: ([], ["svc#read"]),
            production=False, state="committed"):
    intent = _intent()

    async def card_at(binding):
        return card

    target = GrantBindingTarget(issuer=issuer or _Issuer(), decisions=_Decisions(_record(intent, state)),
                                card_at=card_at, custody=custody or _Custody(), production=production,
                                tenant="t", project="p", authority=authority)
    return target, _binding(intent.digest)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["issued", "recovered"])
async def test_a_committed_grant_binding_issues_one_bound_session(outcome):
    issuer = _Issuer(outcome)
    target, binding = _target(issuer=issuer)
    assert await target.apply_once(binding, PAYLOAD) == binding.effect_digest
    [(context, user_id, roles, permissions, custody)] = issuer.calls
    assert user_id == "integration:client-1:user:owner"  # from the committed Card, never the payload
    assert (context.actor, context.target_incarnation, context.expires_at) == ("user:admin-1", 4, PAYLOAD["expires_at"])
    assert (roles, permissions) == ([], ["svc#read"])


@pytest.mark.asyncio
async def test_without_a_confirmed_authority_mapping_nothing_is_issued():
    issuer = _Issuer()
    target, binding = _target(issuer=issuer, authority=None)
    with pytest.raises(ParticipantEffectRefused, match="card_effect_adapter_unavailable"):
        await target.apply_once(binding, PAYLOAD)
    assert issuer.calls == []


@pytest.mark.asyncio
async def test_an_uncommitted_transaction_never_issues():
    issuer = _Issuer()
    target, binding = _target(issuer=issuer, state="aborted")
    with pytest.raises(ParticipantEffectRefused, match="card_effect_binding_mismatch"):
        await target.apply_once(binding, PAYLOAD)
    assert issuer.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("card", [None, SimpleNamespace(**{**CARD.__dict__, "card_revision": 5}),
                                  SimpleNamespace(**{**CARD.__dict__, "access_id": "aut_other"})])
async def test_a_card_not_at_the_committed_revision_never_issues(card):
    issuer = _Issuer()
    target, binding = _target(issuer=issuer, card=card)
    with pytest.raises(ParticipantEffectRefused, match="card_effect_target_revision_moved"):
        await target.apply_once(binding, PAYLOAD)
    assert issuer.calls == []


@pytest.mark.asyncio
async def test_custody_that_does_not_qualify_never_issues(monkeypatch):
    class _Wrapper(_Custody):
        pass

    monkeypatch.setattr(bound_issuance, "_issuance_custody_type", lambda: _Wrapper)
    issuer = _Issuer()
    target, binding = _target(issuer=issuer, custody=_Wrapper(running_ok=False), production=True)
    with pytest.raises(ParticipantEffectRefused, match="card_effect_custody_unavailable"):
        await target.apply_once(binding, PAYLOAD)
    assert issuer.calls == []


@pytest.mark.asyncio
async def test_an_sdk_refusal_keeps_its_kind_and_never_its_text():
    class SessionIssuanceRefused(ValueError):
        def __init__(self, reason):
            super().__init__(f"{reason}: secret detail")
            self.reason = reason

    for reason, expected in (("issuance_custody_expired", "card_effect_custody_unavailable"),
                             ("issuance_authority_moved", "card_effect_binding_mismatch")):
        target, binding = _target(issuer=_Issuer(refuse=SessionIssuanceRefused(reason)))
        with pytest.raises(ParticipantEffectRefused, match=expected) as raised:
            await target.apply_once(binding, PAYLOAD)
        assert "secret detail" not in str(raised.value)


@pytest.mark.asyncio
async def test_an_unexpected_sdk_outcome_is_refused():
    target, binding = _target(issuer=_Issuer(outcome="minted-twice"))
    with pytest.raises(ParticipantEffectRefused, match="card_effect_binding_mismatch"):
        await target.apply_once(binding, PAYLOAD)


@pytest.mark.asyncio
async def test_prepare_and_release_mint_nothing():
    issuer = _Issuer()
    target, binding = _target(issuer=issuer)
    assert await target.prepare_once(binding, PAYLOAD) == binding.effect_digest
    assert await target.release_once(binding, PAYLOAD) == binding.effect_digest
    assert issuer.calls == []
