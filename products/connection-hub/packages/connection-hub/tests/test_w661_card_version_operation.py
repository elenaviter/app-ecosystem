"""W661 piece 2: card_version, PB's STAGE / PUBLISH / ROLLBACK of one Card save (contract v6.2).

Authenticated like card_lifecycle_plan; PB's role decision arrives as its
delegable grants, never as a call back to PB; the answer carries links only.
The planner and piece 1's store are stand-ins that record what they were given.
"""

from __future__ import annotations

import os

import pytest

from service_foundation.coordination.durable_wire import canonical_json_bytes
from service_foundation.coordination.participant_answer import AnswerContract, verify_participant_answer

from connection_hub.delegated_credentials.admission import AdmissionRequest, sign_admission_request
from connection_hub.delegated_credentials.cards.participant_card_version import (
    ANSWER_SCHEMA, OPERATION, REQUEST_FIELDS, REQUEST_SCHEMA, CardVersionOperation, CardVersionRefused,
    card_version_request_digest, stage_digest,
)
from connection_hub.delegated_credentials.cards.participant_operation import ParticipantCaller
from connection_hub.delegated_credentials.catalog.reservations import catalog_version_digest
from connection_hub.delegated_credentials.controls.model import new_credentialless_card
from connection_hub.delegated_credentials.project_authorization import PROJECT_PERSON_CONTROL_UPDATE

NOW = 1_800_000_000
PEER, PROJECT, ACTOR, TARGET = "problem-board", "work:project:p1", "user:admin", "user:person"
REQUEST_SECRET, RECEIPT_SECRET = "r" * 40, "s" * 40
TXN = "txn-" + "a" * 40
CATALOG = {"version": "catalog-7", "content_hash": "c" * 64}
AT = "2026-10-10T17:00:00+00:00"
SUBJECT, ACCESS = "h" * 64, "card-control-1"
UPDATE = {"kind": "reselect", "target_subject": TARGET, "access_id": ACCESS, "subject_hash": SUBJECT,
          "original_revision": 3, "selection": {"resource_grants": ["memories:read"]}}
CANDIDATE = new_credentialless_card(grantor_subject=TARGET, catalog_version=CATALOG["version"], control_id=ACCESS,
                                    issuer_ref=PROJECT, issuer_kind="application", revision=4).to_dict()
LINK = {"card": {"subject_hash": SUBJECT, "access_id": ACCESS}, "version": 4, "checksum": "k" * 64}


def _plan(*, digest=None, original_absent=False):
    return {"ok": True, "plan": {
        "catalog_digest": digest or catalog_version_digest(CATALOG["version"], CATALOG["content_hash"]),
        "candidate_value": {"cards": [{"subject_hash": SUBJECT, "access_id": ACCESS, "action": "reselect",
                                       "original_revision": 0 if original_absent else 3,
                                       "original_absent": original_absent, "candidate": CANDIDATE}]}}}


class _Nonces:
    def __init__(self):
        self.keys = set()

    async def set(self, key, value, *, ex, nx):
        if key in self.keys:
            return False
        self.keys.add(key)
        return True


class _Planner:
    def __init__(self, result=None):
        self.calls, self.result = [], result or _plan()

    async def __call__(self, host, **kwargs):
        self.calls.append(kwargs)
        return self.result


class _Store:
    """Piece 1 stand-in: one marker per txn, effects handed back by the protocol's own rules."""

    def __init__(self, *, rollback_state="rolled_back", stage_refusal=None):
        self.staged, self.markers, self.current = [], {}, {}
        self.rollback_state, self.stage_refusal = rollback_state, stage_refusal

    def _bound(self, txn, scope, caller):
        """Piece 1's binding: a marker answers only the scope and caller that staged it."""
        marker = self.markers.get(txn)
        if marker is not None and marker["binding"] != {"scope": scope, "caller": caller}:
            raise CardVersionRefused("txn_scope_mismatch")
        return marker

    async def stage(self, txn, *, request_id, request_digest, catalog, actor_subject, actor_kind, members,
                    effects, prepare, at, scope, caller, reads=()):
        self.reads = list(reads)
        if self.stage_refusal:
            raise CardVersionRefused(self.stage_refusal)
        existing = self._bound(txn, scope, caller)
        if existing is not None:
            if existing["request_digest"] != request_digest:
                raise CardVersionRefused("stage_txn_conflict")
            return {"members": [LINK]}
        await prepare()
        self.staged.append({"txn": txn, "members": members, "effects": effects, "catalog": catalog, "at": at})
        self.markers[txn] = {"txn": txn, "members": [dict(LINK, base_version=m["base_version"]) for m in members],
                             "effects": [dict(effect, result=None) for effect in effects],
                             "binding": {"scope": scope, "caller": caller}, "request_digest": request_digest}
        return {"members": [LINK]}

    async def publish(self, txn, *, scope, caller, apply):
        marker = self._bound(txn, scope, caller)
        if marker is None:
            raise CardVersionRefused("txn_unknown")
        self.current[ACCESS] = {"version": LINK["version"], "checksum": LINK["checksum"]}
        for effect in marker["effects"]:
            if effect["result"] is None:
                effect["result"] = await apply(effect, marker)
        return {"members": marker["members"]}

    async def rollback(self, txn, *, scope, caller, links, at, apply, release):
        self.rollback_links, self.rollback_at = links, at
        marker = self._bound(txn, scope, caller)
        if marker is not None and self.rollback_state == "rolled_back":
            for effect in marker["effects"]:
                await release(effect, marker)
        return {"state": self.rollback_state}

    async def read_current(self, subject_hash, access_id):
        return self.current.get(access_id)


class _Handles:
    def __init__(self, identity=None, fingerprint=""):
        self.identity, self.fingerprint, self.calls = identity, fingerprint, []

    async def binding_identity(self, access_id):
        return self.identity

    async def advance_binding(self, access_id, **kwargs):
        self.calls.append(("advance", access_id, kwargs))
        return "applied"

    async def stage_rewrap(self, txn, access_id, payload):
        self.calls.append(("stage_rewrap", txn, access_id))

    async def commit_rewrap(self, txn, access_id, payload):
        self.calls.append(("commit_rewrap", txn, access_id))
        return "applied"

    async def discard_rewrap(self, txn, access_id, payload):
        self.calls.append(("discard_rewrap", txn, access_id))


class _Host:
    """The Hub host: today's handle_binding producer over a stand-in Card read."""

    def __init__(self, effects=()):
        self.effects, self.pairs = list(effects), []

    def _cards(self):
        host = self

        class _Cards:
            async def load_current(self, access_id, *, subject_hash):
                return (object(), None)

        return _Cards()

    async def _handle_binding_effects(self, pairs):
        self.pairs.extend(pairs)
        return list(self.effects)


AGENT_EFFECT = {"kind": "handle_binding", "key": f"handle:{ACCESS}", "payload": {
    "access_id": ACCESS, "from_identity": "row-1", "from_fingerprint": "", "prepared_at": 0,
    "from_revision": 3, "from_expires_at": 0, "card_revision": 4, "expires_at": 0}}
ROW = {"from_identity": "row-1", "from_revision": 3, "from_expires_at": 0, "from_fingerprint": ""}


def _operation(*, prefix="work:project:", planner=None, store=None, host=None, handles=None):
    caller = ParticipantCaller(service_id=PEER, request_secret=REQUEST_SECRET, receipt_secret=RECEIPT_SECRET,
                               receipt_signer_id="connection-hub@1-0", audience="problem-board@1-0",
                               hub_resource="connection-hub@1-0", bind=None, scope_field="project_ref",
                               plan_scope_prefix=prefix)
    planner, store = planner or _Planner(), store or _Store()
    operation = CardVersionOperation(callers={PEER: caller}, store=store, planner=planner, host=host or _Host(),
                                     credential_handles=handles or _Handles(), nonces=_Nonces(), clock=lambda: NOW)
    return operation, planner, store


def _request(op="stage", *, scope=PROJECT, updates=(UPDATE,), creations=(), nonce=None, **override):
    data = {"schema": REQUEST_SCHEMA, "op": op, "request_echo": os.urandom(16).hex(), "scope": scope, "txn": TXN,
            "request_id": None, "at": AT if op == "rollback" else None, "catalog": None, "actor_subject": None, "actor_kind": None,
            "delegable_grants": None, "project_control": None, "creations": None, "updates": None,
            "links": [LINK] if op == "rollback" else None}
    if op == "stage":
        data.update(request_id="save-1", at=AT, catalog=dict(CATALOG), actor_subject=ACTOR, actor_kind="caller",
                    delegable_grants=["memories:read"], creations=list(creations), updates=list(updates))
    data.update(override)
    proof = {"service_id": PEER, "timestamp": str(NOW), "nonce": nonce or os.urandom(12).hex()}
    proof["signature"] = sign_admission_request(
        secret=REQUEST_SECRET, **proof, delegated_token=f"{REQUEST_SCHEMA}:{data['request_echo']}",
        request=AdmissionRequest(resource="connection-hub@1-0", operation=OPERATION,
                                 invocation_id=data["request_echo"], request_digest=card_version_request_digest(data),
                                 approval_context={"protocol": REQUEST_SCHEMA}))
    return {**data, "service_proof": proof}


def _verified(response, request):
    return verify_participant_answer(
        dict(response["participant_answer"]), schema=ANSWER_SCHEMA, secret=RECEIPT_SECRET,
        signer_id="connection-hub@1-0", audience="problem-board@1-0", direction="hub-to-authority",
        request={name: request[name] for name in REQUEST_FIELDS}, now=NOW, contract=AnswerContract.CARD_VERSION)


@pytest.mark.asyncio
async def test_stage_plans_under_pbs_grants_and_answers_links_only():
    operation, planner, store = _operation()
    request = _request()
    response = await operation.answer(request)
    assert response["ok"] is True
    assert _verified(response, request) == {"kind": "staged", "members": [LINK]}
    [call] = planner.calls
    authorization = call["authorization"]
    assert [(s.operation, s.target_subject) for s in authorization.request.steps] == [
        (PROJECT_PERSON_CONTROL_UPDATE, TARGET)]
    decision = authorization.decision_for("update:0")
    assert decision.allowed and decision.delegable_grants == ("memories:read",) and decision.actor_subject == ACTOR
    assert authorization.request.request_digest == stage_digest(request)
    [staged] = store.staged
    assert staged["txn"] == TXN and staged["catalog"] == CATALOG and staged["at"].isoformat() == AT
    assert store.markers[TXN]["binding"] == {"scope": PROJECT, "caller": PEER}
    assert staged["members"] == [{"subject_hash": SUBJECT, "access_id": ACCESS, "base_version": 3,
                                  "value": CANDIDATE}]
    # No Card value leaves the Hub, and the edit is not echoed: the whole answer holds identity and links.
    assert set(response["participant_answer"]) == {
        "schema", "direction", "audience", "request_digest", "op", "request_echo", "scope", "txn", "result",
        "receipt_proof"}
    whole = canonical_json_bytes(response)
    assert b"card_revision" not in whole and TARGET.encode() not in whole and b"memories:read" not in whole


@pytest.mark.asyncio
async def test_a_creation_stages_with_no_base_version():
    operation, _, store = _operation(planner=_Planner(_plan(original_absent=True)))
    assert (await operation.answer(_request()))["ok"] is True
    assert store.staged[0]["members"][0]["base_version"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("code,expected", [
    ("card_plan_original_revision_changed", "card_changed"), ("card_plan_update_target_absent", "card_changed"),
    ("card_plan_target_exists", "card_changed"), ("card_plan_selection_invalid", "edit_invalid"),
    ("delegated_cards_unavailable", "storage_unavailable"),
])
async def test_planner_refusals_map_to_the_contract_codes_and_nothing_is_staged(code, expected):
    operation, _, store = _operation(planner=_Planner({"ok": False, "error": code, "status": 409}))
    request = _request()
    response = await operation.answer(request)
    assert response["ok"] is False and _verified(response, request)["code"] == expected and store.staged == []


@pytest.mark.asyncio
async def test_a_moved_catalog_refuses_before_the_store():
    operation, _, store = _operation(planner=_Planner(_plan(digest="0" * 64)))
    request = _request()
    response = await operation.answer(request)
    assert _verified(response, request)["code"] == "stage_catalog_moved" and store.staged == []


@pytest.mark.asyncio
async def test_a_store_refusal_is_answered_by_its_own_code():
    operation, _, _ = _operation(store=_Store(stage_refusal="txn_closed"))
    request = _request()
    assert _verified(await operation.answer(request), request) == {
        "kind": "refused", "code": "txn_closed", "status": 409}


@pytest.mark.asyncio
async def test_a_scope_outside_the_callers_prefix_is_refused():
    operation, planner, _ = _operation(prefix="work:project:other")
    request = _request()
    assert _verified(await operation.answer(request), request)["code"] == "request_scope_invalid"
    assert planner.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["publish", "rollback"])
async def test_publish_and_rollback_carry_the_txn_alone(op):
    operation, _, _ = _operation()
    response = await operation.answer(_request(op, request_id="save-1"))
    assert response == {"ok": False, "status": 400, "error": {"code": "card_version_request_invalid"}}


@pytest.mark.asyncio
async def test_a_bad_txn_or_a_replayed_nonce_is_refused_unsigned():
    operation, _, _ = _operation()
    assert (await operation.answer(_request(txn="UPPER" * 8)))["error"]["code"] == "card_version_request_invalid"
    first = _request(nonce="n" * 24)
    assert (await operation.answer(first))["ok"] is True
    again = _request(nonce="n" * 24)
    assert (await operation.answer(again))["error"]["code"] == "card_participant_unauthenticated"


@pytest.mark.asyncio
async def test_person_card_saves_carry_no_effects():
    operation, _, store = _operation(host=_Host(effects=()))
    await operation.answer(_request())
    assert store.staged[0]["effects"] == []


@pytest.mark.asyncio
async def test_an_agent_edit_binds_its_handle_exactly_once_after_publish():
    handles = _Handles(identity=ROW)
    operation, _, store = _operation(host=_Host(effects=[AGENT_EFFECT]), handles=handles)
    await operation.answer(_request())
    [effect] = store.staged[0]["effects"]
    assert set(effect) == {"kind", "key", "access_id", "payload"} and effect["access_id"] == ACCESS
    publish = _request("publish")
    assert _verified(await operation.answer(publish), publish)["kind"] == "published"
    assert [call[0] for call in handles.calls] == ["advance"]
    assert store.markers[TXN]["effects"][0]["result"] == "applied"
    # A retried PUBLISH finds the result recorded and moves nothing again.
    await operation.answer(_request("publish"))
    assert [call[0] for call in handles.calls] == ["advance"]


@pytest.mark.asyncio
async def test_an_agent_row_that_moved_since_the_plan_refuses_stage_as_card_changed():
    handles = _Handles(identity={**ROW, "from_identity": "row-2"})
    operation, _, store = _operation(host=_Host(effects=[AGENT_EFFECT]), handles=handles)
    request = _request()
    assert _verified(await operation.answer(request), request)["code"] == "card_changed"
    assert store.staged == []


@pytest.mark.asyncio
async def test_an_effect_whose_card_is_no_longer_this_version_is_superseded():
    handles = _Handles(identity=ROW)
    operation, _, store = _operation(host=_Host(effects=[AGENT_EFFECT]), handles=handles)
    await operation.answer(_request())
    marker = store.markers[TXN]
    store.current[ACCESS] = {"version": 5, "checksum": "z" * 64}
    assert await operation._apply(marker["effects"][0], marker) == "superseded"
    assert handles.calls == []


@pytest.mark.asyncio
async def test_an_agent_rewrap_is_prepared_at_stage_and_released_by_rollback():
    payload = {**AGENT_EFFECT["payload"], "from_fingerprint": "fp"}
    handles = _Handles(identity=ROW)
    operation, _, store = _operation(host=_Host(effects=[{**AGENT_EFFECT, "payload": payload}]), handles=handles)
    await operation.answer(_request())
    rollback = _request("rollback")
    assert _verified(await operation.answer(rollback), rollback) == {"kind": "rollback", "state": "rolled_back"}
    assert [call[0] for call in handles.calls] == ["stage_rewrap", "discard_rewrap"]
    assert all(call[1] == TXN for call in handles.calls)


@pytest.mark.asyncio
async def test_an_unqualified_effect_kind_on_the_save_path_is_refused():
    other = {"kind": "credential_issue", "key": "issue:1", "payload": {"access_id": ACCESS}}
    operation, _, store = _operation(host=_Host(effects=[other]))
    request = _request()
    assert _verified(await operation.answer(request), request)["code"] == "edit_invalid"
    assert store.staged == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["rolled_back", "already_published", "unknown_txn"])
async def test_rollback_answers_the_stores_state(state):
    operation, _, _ = _operation(store=_Store(rollback_state=state))
    request = _request("rollback")
    assert _verified(await operation.answer(request), request) == {"kind": "rollback", "state": state}


@pytest.mark.asyncio
async def test_publish_of_an_unknown_txn_is_refused():
    operation, _, _ = _operation()
    request = _request("publish")
    assert _verified(await operation.answer(request), request)["code"] == "txn_unknown"


@pytest.mark.asyncio
async def test_the_answer_does_not_grow_with_the_edit_refusals_included():
    sizes = []
    for count in (1, 40):
        selection = {"resource_grants": [f"grant-{index}:read" for index in range(count)]}
        for planner in (_Planner(), _Planner({"ok": False, "error": "card_plan_selection_invalid"})):
            operation, _, _ = _operation(planner=planner)
            response = await operation.answer(_request(updates=[{**UPDATE, "selection": selection}]))
            assert b"grant-0:read" not in canonical_json_bytes(response)
            sizes.append(len(canonical_json_bytes(response["participant_answer"]["result"])))
    assert sizes[0] == sizes[2] and sizes[1] == sizes[3]


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["stage", "publish", "rollback"])
async def test_a_txn_answers_only_the_scope_and_caller_that_staged_it(op):
    store = _Store()
    first, _, _ = _operation(store=store)
    assert (await first.answer(_request(scope="work:project:a")))["ok"] is True
    other, _, _ = _operation(store=store)
    request = _request(op, scope="work:project:b")
    assert _verified(await other.answer(request), request) == {
        "kind": "refused", "code": "request_scope_invalid", "status": 403}
    assert len(store.staged) == 1


@pytest.mark.asyncio
async def test_a_retry_with_a_fresh_echo_is_the_same_stage():
    operation, _, store = _operation()
    first, again = _request(), _request()
    assert first["request_echo"] != again["request_echo"]
    assert stage_digest(first) == stage_digest(again)
    assert (await operation.answer(first))["ok"] is True and (await operation.answer(again))["ok"] is True
    assert len(store.staged) == 1
    changed = _request(updates=[{**UPDATE, "selection": {"resource_grants": ["other:read"]}}])
    assert stage_digest(changed) != stage_digest(first)
    assert _verified(await operation.answer(changed), changed)["code"] == "stage_txn_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize("at", [None, "2026-10-10T17:00:00", "yesterday"])
async def test_stage_needs_the_requests_own_time_with_an_offset(at):
    operation, _, _ = _operation()
    response = await operation.answer(_request(at=at))
    assert response == {"ok": False, "status": 400, "error": {"code": "card_version_request_invalid"}}


@pytest.mark.asyncio
async def test_a_cancelled_publish_still_finishes_its_handle_write_before_the_locks_go():
    """Infra K4: the store's drain scope holds the Card locks until a started handle write has returned."""
    import asyncio

    from connection_hub.delegated_credentials.durable_io import drain_writes_before_release

    order, started = [], asyncio.Event()

    class _SlowHandles(_Handles):
        async def advance_binding(self, access_id, **kwargs):
            started.set()
            await asyncio.sleep(0.05)
            order.append("write_finished")
            return "applied"

    handles = _SlowHandles(identity=ROW)
    operation, _, store = _operation(host=_Host(effects=[AGENT_EFFECT]), handles=handles)
    await operation.answer(_request())
    marker = store.markers[TXN]
    store.current[ACCESS] = {"version": LINK["version"], "checksum": LINK["checksum"]}

    async def locked_publish():
        try:
            async with drain_writes_before_release():  # entered inside the Card locks by the store
                await operation._apply(marker["effects"][0], marker)
        finally:
            order.append("locks_released")

    task = asyncio.create_task(locked_publish())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert order == ["write_finished", "locks_released"]


@pytest.mark.asyncio
async def test_rollback_carries_stages_links_and_only_rollback_does():
    operation, _, store = _operation()
    request = _request("rollback")
    assert (await operation.answer(request))["ok"] is True and store.rollback_links == [LINK]
    assert store.rollback_at.isoformat() == AT
    assert (await operation.answer(_request("rollback", links=[])))["ok"] is True and store.rollback_links == []
    for op, override in (("stage", {"links": [LINK]}), ("publish", {"links": [LINK]}), ("publish", {"at": AT}),
                         ("rollback", {"links": None}), ("rollback", {"links": [{**LINK, "body": {}}]}),
                         ("rollback", {"at": None}), ("rollback", {"request_id": "save-1"})):
        response = await operation.answer(_request(op, **override))
        assert response["error"]["code"] == "card_version_request_invalid", (op, override)


@pytest.mark.asyncio
async def test_an_invitation_recreating_an_existing_card_is_the_plain_upsert():
    plan = _plan()
    plan["plan"]["candidate_value"]["cards"][0]["action"] = "recreate"
    operation, _, store = _operation(planner=_Planner(plan))
    assert (await operation.answer(_request()))["ok"] is True
    assert store.staged[0]["members"][0]["base_version"] is None


@pytest.mark.asyncio
async def test_a_retried_stage_prepares_the_identical_rewrap():
    """Infra P1 finding 1: a `staging` retry calls prepare again; the agent envelope must be the same one."""
    from datetime import datetime

    payload = {**AGENT_EFFECT["payload"], "from_fingerprint": "fp", "prepared_at": 111}
    prepared = []

    class _Recording(_Handles):
        async def stage_rewrap(self, txn, access_id, binding):
            prepared.append((txn, access_id, dict(binding)))

    class _RetryStore(_Store):
        async def stage(self, txn, **kwargs):
            await kwargs["prepare"]()  # the first attempt stopped after prepare, before `staged`
            return await super().stage(txn, **kwargs)

    host = _Host(effects=[{**AGENT_EFFECT, "payload": payload}])
    operation, _, _ = _operation(host=host, handles=_Recording(identity=ROW), store=_RetryStore())
    assert (await operation.answer(_request()))["ok"] is True
    first, again = prepared
    assert first == again
    assert first[2]["prepared_at"] == int(datetime.fromisoformat(AT).timestamp())


@pytest.mark.asyncio
async def test_a_failing_handle_write_answers_effects_pending_and_the_retry_applies_it():
    class _Flaky(_Handles):
        failures = 1

        async def advance_binding(self, access_id, **kwargs):
            if self.failures:
                self.failures -= 1
                raise RuntimeError("handle store down")
            return await super().advance_binding(access_id, **kwargs)

    handles = _Flaky(identity=ROW)
    operation, _, store = _operation(host=_Host(effects=[AGENT_EFFECT]), handles=handles)
    await operation.answer(_request())
    publish = _request("publish")
    assert _verified(await operation.answer(publish), publish) == {
        "kind": "refused", "code": "effects_pending", "status": 503}
    retry = _request("publish")
    assert _verified(await operation.answer(retry), retry)["kind"] == "published"
    assert [call[0] for call in handles.calls] == ["advance"]


RESET = {"kind": "reset_to_control", "target_subject": TARGET, "access_id": ACCESS, "subject_hash": SUBJECT,
         "original_revision": 3, "resource": "service-a", "display_digest": "e" * 64,
         "control": {"access_id": "control-c", "subject_hash": "s" * 64}}


@pytest.mark.asyncio
async def test_a_reset_fences_exactly_its_control_as_a_store_read():
    plan = _plan()
    plan["plan"]["reads"] = [{"subject_hash": "s" * 64, "access_id": "control-c", "revision": 7},
                             {"subject_hash": "p" * 64, "access_id": "project-p", "revision": 2}]
    operation, _, store = _operation(planner=_Planner(plan))
    assert (await operation.answer(_request(updates=[RESET])))["ok"] is True
    assert store.reads == [{"card": {"subject_hash": "s" * 64, "access_id": "control-c"}, "version": 7}]
    # An ordinary edit fences no read; a reset whose Control the planner did not read never stages.
    operation, _, store = _operation()
    assert (await operation.answer(_request()))["ok"] is True and store.reads == []
    operation, _, store = _operation(planner=_Planner(_plan()))
    request = _request(updates=[RESET])
    assert _verified(await operation.answer(request), request)["code"] == "edit_invalid" and store.staged == []
