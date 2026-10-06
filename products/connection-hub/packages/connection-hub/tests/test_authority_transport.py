"""W502 phase 3: the Hub transport reads its provider, operation and refusal map from binding configuration."""

from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.cards.authority_transport import (
    AuthorityBindingConfig,
    configured_authority_fetch,
)
from connection_hub.delegated_credentials.cards.card_participant import PARTICIPANT
from connection_hub.delegated_credentials.cards.transaction_authority_v2 import PROTOCOL, TransactionAuthorityRefused

# Problem Board's values (CodeApp 18:06, Apps 8e1e2e12) live in ITS binding configuration, not Hub source.
PB_BINDING = {
    "bundle_id": "problem-board@1-0",
    "operation": "project_card_transaction_authority",
    "request_fields": {"project_ref": "work:project:one"},
    "refusals": {
        "work_card_transaction_authority_decision_pending": "authority_decision_pending",
        "work_card_transaction_authority_late_stage": "authority_late_stage",
        "work_card_transaction_authority_intent_expired": "authority_intent_expired",
        "work_card_transaction_decision_unknown": "authority_transaction_unknown",
        "work_card_transaction_authority_store_unavailable": "authority_unavailable",
        "work_card_transaction_intent_mismatch": "authority_intent_mismatch",
    },
}
ECHO = "ab" * 16
TX = "c" * 64


class _Call:
    def __init__(self, body=None, raise_with=None):
        self.body, self.raise_with, self.calls = body, raise_with, []

    async def __call__(self, *, bundle_id, operation, data):
        self.calls.append((bundle_id, operation, data))
        if self.raise_with:
            raise self.raise_with
        return self.body


async def _sign(unsigned):
    return {"service_id": "connection-hub@1-0", "signature": "sig-over-" + ",".join(sorted(unsigned))}


@pytest.mark.asyncio
async def test_the_request_is_generic_plus_the_bindings_configured_fields():
    call = _Call({"ok": True, "schema": PROTOCOL, "phase": "stage"})
    fetch = configured_authority_fetch(call=call, binding_config=PB_BINDING, sign_request=_sign)
    response = await fetch(TX, "stage", ECHO)
    [(bundle_id, operation, data)] = call.calls
    assert (bundle_id, operation) == ("problem-board@1-0", "project_card_transaction_authority")
    assert data == {"project_ref": "work:project:one", "schema": PROTOCOL, "phase": "stage", "request_echo": ECHO,
                    "transaction_id": TX, "participant": PARTICIPANT,
                    "service_proof": {"service_id": "connection-hub@1-0",
                                      "signature": "sig-over-participant,phase,project_ref,request_echo,schema,transaction_id"}}
    assert response == {"schema": PROTOCOL, "phase": "stage"}  # the ok flag is not part of the signed response


@pytest.mark.asyncio
async def test_a_configured_response_key_selects_the_signed_response():
    fetch = configured_authority_fetch(call=_Call({"ok": True, "authority": {"schema": PROTOCOL}}),
                                       binding_config={**PB_BINDING, "response_key": "authority"}, sign_request=_sign)
    assert await fetch(TX, "decision", ECHO) == {"schema": PROTOCOL}


@pytest.mark.asyncio
@pytest.mark.parametrize(("code", "reason"), [
    *PB_BINDING["refusals"].items(),
    ("work_card_transaction_authority_decision_pending_v2", "authority_refused"),  # exact, never prefix
    ("work_card_transaction_", "authority_refused"),
    ("something_else", "authority_refused"),
])
async def test_refusals_map_by_exact_configured_code(code, reason):
    body = {"ok": False, "status": 409, "error": {"code": code, "message": "provider text never passed through"}}
    fetch = configured_authority_fetch(call=_Call(body), binding_config=PB_BINDING, sign_request=_sign)
    with pytest.raises(TransactionAuthorityRefused) as raised:
        await fetch(TX, "decision", ECHO)
    assert raised.value.reason == reason and "provider text" not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [None, "text", {"ok": False}, {"ok": False, "error": "flat"}, {"ok": True, "x": 1}])
async def test_malformed_bodies_are_named_refusals(body):
    fetch = configured_authority_fetch(call=_Call(body), binding_config={**PB_BINDING, "response_key": "authority"},
                                       sign_request=_sign)
    with pytest.raises(TransactionAuthorityRefused) as raised:
        await fetch(TX, "stage", ECHO)
    assert raised.value.reason in ("authority_response_invalid", "authority_refused")


@pytest.mark.asyncio
async def test_a_transport_failure_is_unavailable_by_name_only():
    fetch = configured_authority_fetch(call=_Call(raise_with=ConnectionError("secret host detail")),
                                       binding_config=PB_BINDING, sign_request=_sign)
    with pytest.raises(TransactionAuthorityRefused) as raised:
        await fetch(TX, "stage", ECHO)
    assert raised.value.reason == "authority_unavailable" and "secret" not in str(raised.value)


@pytest.mark.parametrize("change", [
    {"bundle_id": ""}, {"operation": None}, {"refusals": {"code": "not_a_hub_reason"}},
    {"request_fields": {"participant": "someone-else"}}, {"request_fields": {"service_proof": {}}},
    {"response_key": 3},
])
def test_an_invalid_binding_configuration_is_refused(change):
    with pytest.raises(TransactionAuthorityRefused, match="authority_binding_invalid"):
        AuthorityBindingConfig.from_mapping({**PB_BINDING, **change})


@pytest.mark.asyncio
async def test_end_to_end_with_the_real_board_signer_behind_the_configured_call(tmp_path):
    # The Board's real signer answers through the configured call; the Hub participant stages and commits.
    import test_authority_intent_source as ais

    hub, pb, store, before, after, applier = await ais._hub(tmp_path)

    async def board_endpoint(*, bundle_id, operation, data):
        assert (bundle_id, operation) == ("problem-board@1-0", "project_card_transaction_authority")
        try:
            return {"ok": True, **await pb.fetch(data["transaction_id"], data["phase"], data["request_echo"])}
        except TransactionAuthorityRefused as exc:
            code = {"authority_decision_pending": "work_card_transaction_authority_decision_pending",
                    "authority_late_stage": "work_card_transaction_authority_late_stage"}.get(exc.reason, "other")
            return {"ok": False, "status": 409, "error": {"code": code, "message": "x"}}

    fetch = configured_authority_fetch(call=board_endpoint, binding_config=PB_BINDING, sign_request=_sign)
    hub._intents._fetch = fetch
    hub._decisions._fetch = fetch
    await hub.prepare(ais.TX)
    pb.decide("committed")
    await hub.finish(ais.TX, "committed")
    assert await ais._visible(store, before) == after
