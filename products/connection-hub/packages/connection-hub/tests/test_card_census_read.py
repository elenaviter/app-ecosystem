"""W502: ``card_census_read`` gives an initiator the exact person Cards and active catalog, signed.

The Hub derives each person's Control and My Card ids from the scope, so a
caller reads only that project's Cards; the read is optimistic (prepare
re-verifies), and every authenticated answer is signed over the echoed request.
"""

from __future__ import annotations

import dataclasses
import os
from datetime import datetime, timezone

import pytest

from connection_hub.delegated_credentials.admission import AdmissionRequest, sign_admission_request
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.census_read import (
    ANSWER_SCHEMA, CAPABILITY_FIELDS, MAX_ANSWER_BYTES, MAX_PERSONS, OPERATION, REQUEST_SCHEMA, CardCensusReadOperation, census_answer_signature,
    census_request_digest,
)
from connection_hub.delegated_credentials.cards.participant_operation import ParticipantCaller
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.catalog.store import BundleStorageDelegatedCatalogStore
from connection_hub.delegated_credentials.project_identity_lifecycle import ProjectPersonCardIdentity
from test_card_transaction_store import INTENT, TX, _setup
from test_catalog_publisher import CONNECTIONS
from test_w502_catalog_reservation import _publish
from test_w580_bound_card_writers import redis_client  # noqa: F401 - fixture

NOW = 1_800_000_000
PEER, PROJECT, ADMIN, OTHER = "problem-board", "work:project:one", "user:admin-1", "user:member-2"
REQUEST_SECRET, RECEIPT_SECRET = "r" * 40, "s" * 40


class _Nonces:
    def __init__(self):
        self.keys = set()

    async def set(self, key, value, *, ex, nx):
        if key in self.keys:
            return False
        self.keys.add(key)
        return True


async def _world(tmp_path, *, catalog=True):
    store, service, before, after = await _setup(tmp_path)
    identity = ProjectPersonCardIdentity.build(project_ref=PROJECT, person_subject=ADMIN)
    control = dataclasses.replace(before, access_id=identity.control_id, grantor_subject=identity.project_subject)
    await service.commit(control, subject_hash=subject_hash_for(identity.project_subject), expected_revision=0,
                         now=NOW)
    catalog_store = None
    if catalog:
        catalog_store = BundleStorageDelegatedCatalogStore(tmp_path / "bundle")
        await _publish(catalog_store, CONNECTIONS)
    caller = ParticipantCaller(service_id=PEER, request_secret=REQUEST_SECRET, receipt_secret=RECEIPT_SECRET,
                               receipt_signer_id="connection-hub@1-0", audience="problem-board@1-0",
                               hub_resource="connection-hub@1-0", bind=None, scope_field="project_ref",
                               census_scope_prefix="work:project:")
    operation = CardCensusReadOperation(callers={PEER: caller}, card_store=store, catalog_store=catalog_store,
                                        nonces=_Nonces(), clock=lambda: NOW)
    return operation, store, control, identity, catalog_store


def _request(persons=(ADMIN, OTHER), *, scope=PROJECT, include_catalog=True, secret=REQUEST_SECRET,
             service_id=PEER, nonce=None):
    data = {"schema": REQUEST_SCHEMA, "request_echo": os.urandom(16).hex(), "scope": scope,
            "persons": sorted(persons), "include_catalog": include_catalog}
    proof = {"service_id": service_id, "timestamp": str(NOW), "nonce": nonce or os.urandom(12).hex()}
    proof["signature"] = sign_admission_request(
        secret=secret, **proof, delegated_token=f"{REQUEST_SCHEMA}:{data['request_echo']}",
        request=AdmissionRequest(resource="connection-hub@1-0", operation=OPERATION,
                                 invocation_id=data["request_echo"], request_digest=census_request_digest(data),
                                 approval_context={"protocol": REQUEST_SCHEMA}))
    return {**data, "service_proof": proof}


def _signature_or_refused(answer, **kwargs):
    """A tampered answer either signs differently or is refused outright by the shared helper's shape checks."""
    from service_foundation.coordination.participant_answer import ParticipantAnswerRefused

    try:
        return census_answer_signature(answer, **kwargs)
    except ParticipantAnswerRefused:
        return None


def _verified(response, request):
    answer = dict(response["census_answer"])
    proof = answer.pop("receipt_proof")
    assert answer["schema"] == ANSWER_SCHEMA and answer["audience"] == "problem-board@1-0"
    assert answer["request_digest"] == census_request_digest(request)
    for name in ("request_echo", "scope", "persons", "include_catalog"):
        assert answer[name] == request[name]
    assert census_answer_signature(answer, secret=RECEIPT_SECRET, signer_id=proof["service_id"],
                                   timestamp=proof["timestamp"]) == proof["signature"]
    return answer["result"]


@pytest.mark.asyncio
async def test_present_and_absent_cards_and_the_active_catalog(tmp_path):
    operation, store, control, identity, catalog_store = await _world(tmp_path)
    request = _request()
    result = _verified(await operation.answer(request), request)
    by_person = {entry["person"]: entry for entry in result["persons"]}
    admin = by_person[ADMIN]
    full = control.to_dict()
    assert admin["control"]["revision"] == control.card_revision and admin["control"]["state"] == "present"
    assert {name: admin["control"]["authority"][name] for name in CAPABILITY_FIELDS if name in full} == {
        name: full[name] for name in CAPABILITY_FIELDS if name in full}
    from connection_hub.delegated_credentials.cards.census_read import PERSONAL_PROPERTIES

    assert not {"label", "client_metadata", "last_four", "created_at", "manage_url"} & set(
        admin["control"]["authority"])  # only what an identity/capability evaluation reads (EMain 20:19)
    from connection_hub.delegated_credentials.card_property_classes import authorization_properties

    assert admin["control"]["authority"]["properties"] == authorization_properties(control.properties)
    assert admin["my"] == {"subject_hash": subject_hash_for(ADMIN), "access_id": identity.my_card_id,
                           "state": "absent"}
    assert by_person[OTHER]["control"]["state"] == "absent"
    active = await catalog_store.read_active()
    assert result["catalog"] == {"version": active.version, "content_hash": active.content_hash,
                                 "document": active.to_dict()}


@pytest.mark.asyncio
async def test_a_staged_card_reads_in_transaction_never_a_guess(tmp_path):
    operation, store, control, identity, catalog_store = await _world(tmp_path)
    await tx.stage(store, transaction_id=TX, intent_digest=INTENT, participant="project",
                   subject_hash=subject_hash_for(identity.project_subject), original=control,
                   candidate=dataclasses.replace(control, card_revision=control.card_revision + 1, label="staged"),
                   now=datetime.fromtimestamp(NOW, timezone.utc))
    request = _request([ADMIN])
    assert _verified(await operation.answer(request), request)["persons"][0]["control"]["state"] == "in_transaction"


@pytest.mark.asyncio
async def test_ids_are_derived_from_the_scope_so_another_projects_cards_are_unreadable(tmp_path):
    operation, store, control, identity, catalog_store = await _world(tmp_path)
    request = _request([ADMIN], scope="work:project:other")
    entry = _verified(await operation.answer(request), request)["persons"][0]
    assert entry["control"]["state"] == "absent" and entry["control"]["access_id"] != identity.control_id


@pytest.mark.asyncio
async def test_without_catalog_requested_none_is_read_and_without_a_catalog_store_it_is_refused(tmp_path):
    operation, store, control, identity, catalog_store = await _world(tmp_path, catalog=False)
    request = _request([ADMIN], include_catalog=False)
    assert _verified(await operation.answer(request), request)["catalog"] is None
    request = _request([ADMIN])
    assert _verified(await operation.answer(request), request) == {
        "kind": "refused", "code": "card_catalog_reservation_unavailable", "status": 503}


@pytest.mark.asyncio
async def test_unauthenticated_and_replayed_requests_get_unsigned_refusals(tmp_path):
    operation, *_ = await _world(tmp_path)
    assert (await operation.answer(_request(service_id="someone-else")))["error"]["code"] == \
        "card_participant_caller_unknown"
    assert (await operation.answer(_request(secret="x" * 40)))["error"]["code"] == "card_participant_unauthenticated"
    tampered = _request()
    tampered["scope"] = "work:project:other"
    assert (await operation.answer(tampered))["error"]["code"] == "card_participant_unauthenticated"
    assert (await operation.answer(_request(nonce="ab" * 12)))["ok"] is True
    assert (await operation.answer(_request(nonce="ab" * 12)))["error"]["code"] == "card_participant_unauthenticated"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"persons": [], "include_catalog": False}, {"persons": [OTHER, ADMIN]}, {"persons": [ADMIN, ADMIN]},
    {"persons": [f"user:{index:04d}" for index in range(MAX_PERSONS + 1)]}, {"include_catalog": "yes"},
    {"extra": 1}, {"schema": "card-census-read.v0"}, {"scope": ""},
])
async def test_malformed_requests_are_refused_unsigned(tmp_path, change):
    operation, *_ = await _world(tmp_path)
    assert await operation.answer({**_request(), **change}) == {
        "ok": False, "status": 400, "error": {"code": "card_census_request_invalid"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["schema", "direction", "audience", "request_echo", "request_digest", "scope",
                                   "persons", "include_catalog", "result"])
async def test_every_signed_answer_field_is_tamper_evident(tmp_path, field):
    operation, *_ = await _world(tmp_path)
    answer = dict((await operation.answer(_request()))["census_answer"])
    proof = answer.pop("receipt_proof")
    answer[field] = "f" * 32 if isinstance(answer[field], str) else {"tampered": True}
    assert _signature_or_refused(answer, secret=RECEIPT_SECRET, signer_id=proof["service_id"],
                                 timestamp=proof["timestamp"]) != proof["signature"]


@pytest.mark.asyncio
async def test_the_catalog_can_be_read_alone(tmp_path):
    operation, store, control, identity, catalog_store = await _world(tmp_path)
    request = _request([])
    result = _verified(await operation.answer(request), request)
    assert result["persons"] == [] and result["catalog"]["version"] == (await catalog_store.read_active()).version


@pytest.mark.asyncio
async def test_the_maximum_request_fits_or_is_refused_signed_as_too_large(tmp_path):
    # EMain 20:19: 500 persons must stay under the consumer's bound, or be refused by name, never oversized.
    import json

    operation, store, control, identity, catalog_store = await _world(tmp_path)
    request = _request([f"user:{index:04d}" for index in range(MAX_PERSONS)])
    response = await operation.answer(request)
    size = len(json.dumps(response).encode("utf-8"))
    result = _verified(response, request)
    assert size <= 512 * 1024
    assert result["kind"] == "census" or result == {"kind": "refused", "code": "card_census_too_large",
                                                    "status": 413}


@pytest.mark.asyncio
async def test_an_answer_over_the_bound_is_refused_not_signed_oversized(tmp_path, monkeypatch):
    from connection_hub.delegated_credentials.cards import census_read

    operation, *_ = await _world(tmp_path)
    monkeypatch.setattr(census_read, "MAX_ANSWER_BYTES", 2048)
    request = _request([f"user:{index:04d}" for index in range(40)])
    assert _verified(await operation.answer(request), request) == {
        "kind": "refused", "code": "card_census_too_large", "status": 413}


# ── the qualified identity path over a real lifecycle-created pair (CodeApp 20:20) ──


@pytest.mark.asyncio
async def test_a_real_pair_returns_a_valid_edge_and_the_complete_upstream_chain(tmp_path, redis_client):
    from test_w502_my_card_fence_real_path import TARGET, PROJECT_REF, _create, _service

    h = await _service(tmp_path, redis_client)
    assert (await _create(h, "request-create"))["ok"] is True
    caller = ParticipantCaller(service_id=PEER, request_secret=REQUEST_SECRET, receipt_secret=RECEIPT_SECRET,
                               receipt_signer_id="connection-hub@1-0", audience="problem-board@1-0",
                               hub_resource="connection-hub@1-0", bind=None, scope_field="project_ref",
                               census_scope_prefix="work:project:")
    operation = CardCensusReadOperation(callers={PEER: caller}, card_store=h.store, catalog_store=None,
                                        nonces=_Nonces(), clock=lambda: NOW)
    request = _request([TARGET], scope=PROJECT_REF, include_catalog=False)
    entry = _verified(await operation.answer(request), request)["persons"][0]
    identity = ProjectPersonCardIdentity.build(project_ref=PROJECT_REF, person_subject=TARGET)
    assert entry["my"]["state"] == "present" and entry["my"]["access_id"] == identity.my_card_id
    assert "project_identity_edge" in entry["my"]["authority"]["provenance"]
    assert entry["edge"] == {"state": "valid"}
    chain = entry["chain"]
    assert chain["state"] == "complete" and chain["cards"]
    assert chain["cards"][0]["access_id"] == identity.control_id == entry["control"]["access_id"]
    assert chain["cards"][0]["revision"] == entry["control"]["revision"]
    for card in chain["cards"]:  # every ancestor carries owner, storage identity and revision
        assert {"subject_hash", "access_id", "revision", "authority", "state"} <= set(card)
        assert card["subject_hash"] == subject_hash_for(card["authority"]["grantor_subject"])


@pytest.mark.asyncio
async def test_a_staged_chain_card_makes_the_chain_in_transaction(tmp_path, redis_client):
    from test_w502_my_card_fence_real_path import TARGET, PROJECT_REF, _control, _create, _service

    h = await _service(tmp_path, redis_client)
    assert (await _create(h, "request-create"))["ok"] is True
    control = await _control(h)
    await tx.stage(h.store, transaction_id=TX, intent_digest=INTENT, participant="project",
                   subject_hash=subject_hash_for(control.grantor_subject), original=control,
                   candidate=dataclasses.replace(control, card_revision=control.card_revision + 1, label="staged"),
                   now=datetime.fromtimestamp(NOW, timezone.utc))
    caller = ParticipantCaller(service_id=PEER, request_secret=REQUEST_SECRET, receipt_secret=RECEIPT_SECRET,
                               receipt_signer_id="connection-hub@1-0", audience="problem-board@1-0",
                               hub_resource="connection-hub@1-0", bind=None, scope_field="project_ref",
                               census_scope_prefix="work:project:")
    operation = CardCensusReadOperation(callers={PEER: caller}, card_store=h.store, catalog_store=None,
                                        nonces=_Nonces(), clock=lambda: NOW)
    request = _request([TARGET], scope=PROJECT_REF, include_catalog=False)
    entry = _verified(await operation.answer(request), request)["persons"][0]
    assert entry["control"]["state"] == "in_transaction"
    assert entry["chain"]["state"] != "complete"  # never a chain built around a staged Control


# ── entitlement (EMain #616) and the signature pinned to the shared helper ──


def _caller(service_id, *, prefix):
    return ParticipantCaller(service_id=service_id, request_secret=REQUEST_SECRET, receipt_secret=RECEIPT_SECRET,
                             receipt_signer_id="connection-hub@1-0", audience="problem-board@1-0",
                             hub_resource="connection-hub@1-0", bind=None, census_scope_prefix=prefix)


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "work:other-app:"])
async def test_a_caller_without_entitlement_to_the_scope_reads_nothing(tmp_path, prefix):
    operation, store, control, identity, catalog_store = await _world(tmp_path)
    other = CardCensusReadOperation(callers={"other-app": _caller("other-app", prefix=prefix)}, card_store=store,
                                    catalog_store=catalog_store, nonces=_Nonces(), clock=lambda: NOW)
    request = _request([ADMIN], service_id="other-app")
    assert _verified(await other.answer(request), request) == {
        "kind": "refused", "code": "card_census_scope_forbidden", "status": 403}


def test_the_census_signature_is_the_shared_helpers_construction():
    import json as _json
    from pathlib import Path

    from connection_hub.delegated_credentials.cards.census_read import answer_frame_signature

    shared = _json.loads((Path(__file__).resolve().parents[5] / "packages" / "service-foundation" / "tests"
                          / "fixtures" / "participant_answer_vectors.json").read_text())
    for vector in shared["vectors"]:
        answer = dict(vector["answer"])
        proof = answer.pop("receipt_proof")
        assert answer_frame_signature(answer, schema=shared["config"]["schema"], secret=shared["config"]["secret"],
                                      signer_id=proof["service_id"], timestamp=proof["timestamp"]) == proof["signature"]


# ── a deeper real hierarchy with a staged mid-chain ancestor (EMain #616, before activation) ──


@pytest.mark.asyncio
async def test_a_staged_upstream_ancestor_makes_the_chain_in_transaction_then_complete(tmp_path):
    from test_control_hierarchy import binding, caller, control

    store, service, before, after = await _setup(tmp_path)
    owner_hash = subject_hash_for("owner")
    parent = control("parent", ["https://service.example.test/mcp"])
    child = dataclasses.replace(control("child", []), control_card=binding(parent))
    for card in (parent, child):
        await service.commit(card, subject_hash=owner_hash, expected_revision=0, now=NOW)
    person = caller([], child)
    operation = CardCensusReadOperation(callers={}, card_store=store, catalog_store=None, nonces=_Nonces(),
                                        clock=lambda: NOW)

    complete = await operation._chain(person)
    assert complete["state"] == "complete"
    assert [card["access_id"] for card in complete["cards"]] == ["child", "parent"]  # direct Control first
    assert all(card["subject_hash"] == owner_hash and card["revision"] == 1 for card in complete["cards"])

    await tx.stage(store, transaction_id=TX, intent_digest=INTENT, participant="project", subject_hash=owner_hash,
                   original=parent, candidate=dataclasses.replace(parent, card_revision=2, label="staged"),
                   now=datetime.fromtimestamp(NOW, timezone.utc))
    assert await operation._chain(person) == {"state": "in_transaction", "cards": []}  # never a partial complete

    store._card_transaction_decisions.recorded[TX] = "aborted"
    await tx.decide(store, transaction_id=TX, intent_digest=INTENT, decision="aborted")
    assert (await operation._chain(person))["state"] == "complete"


@pytest.mark.asyncio
async def test_a_missing_upstream_ancestor_is_unavailable_not_complete(tmp_path):
    from test_control_hierarchy import binding, caller, control

    store, service, before, after = await _setup(tmp_path)
    parent = control("parent", [])
    child = dataclasses.replace(control("child", []), control_card=binding(parent))
    await service.commit(child, subject_hash=subject_hash_for("owner"), expected_revision=0, now=NOW)
    operation = CardCensusReadOperation(callers={}, card_store=store, catalog_store=None, nonces=_Nonces(),
                                        clock=lambda: NOW)
    result = await operation._chain(caller([], child))
    assert result["state"] == "unavailable" and result["cards"] == [] and result["reason"]


# ── shared census answer vectors for the PB consumer (EMain #616) ──

CENSUS_VECTORS = __import__("json").loads((__import__("pathlib").Path(__file__).parent / "fixtures"
                                           / "w502_census_answer_vectors.json").read_text())


@pytest.mark.parametrize("vector", CENSUS_VECTORS["vectors"], ids=[v["name"] for v in CENSUS_VECTORS["vectors"]])
def test_the_hub_reproduces_each_shared_census_vector(vector):
    config = CENSUS_VECTORS["config"]
    assert (config["schema"], config["request_schema"]) == (ANSWER_SCHEMA, REQUEST_SCHEMA)
    assert census_request_digest(vector["request"]) == vector["request_digest"]
    answer = dict(vector["answer"])
    proof = answer.pop("receipt_proof")
    assert census_answer_signature(answer, secret=config["secret"], signer_id=proof["service_id"],
                                   timestamp=proof["timestamp"]) == proof["signature"]
    for field in CENSUS_VECTORS["signed_fields"]:
        tampered = {**answer, field: "f" * 32 if isinstance(answer[field], str) else {"tampered": True}}
        assert _signature_or_refused(tampered, secret=config["secret"], signer_id=proof["service_id"],
                                     timestamp=proof["timestamp"]) != proof["signature"], field


@pytest.mark.asyncio
async def test_the_transmitted_cards_rebuild_and_pass_the_qualified_evaluation(tmp_path, redis_client):
    # CodeApp 20:34: rebuild each CardAuthority from the ANSWER and run the qualified path on it.
    from connection_hub.delegated_credentials.cards.model import CardAuthority
    from connection_hub.delegated_credentials.controls.hierarchy import compose_resolved_control_hierarchy
    from connection_hub.delegated_credentials.controls.project_person import ProjectPersonControlIdentity
    from test_w502_my_card_fence_real_path import TARGET, PROJECT_REF, _create, _service

    h = await _service(tmp_path, redis_client)
    assert (await _create(h, "request-create"))["ok"] is True
    caller = _caller(PEER, prefix="work:project:")
    operation = CardCensusReadOperation(callers={PEER: caller}, card_store=h.store, catalog_store=None,
                                        nonces=_Nonces(), clock=lambda: NOW)
    request = _request([TARGET], scope=PROJECT_REF, include_catalog=False)
    entry = _verified(await operation.answer(request), request)["persons"][0]
    my = CardAuthority.from_mapping(entry["my"]["authority"])
    chain = [CardAuthority.from_mapping(card["authority"]) for card in entry["chain"]["cards"]]
    identity = ProjectPersonCardIdentity.from_my_card(my)
    assert identity.edge_ref == ProjectPersonCardIdentity.build(project_ref=PROJECT_REF,
                                                                person_subject=TARGET).edge_ref
    assert ProjectPersonControlIdentity.from_authority(chain[0]).control_id == identity.control_id
    assert identity.edge(control_card=chain[0], my_card=my).validation_reason() == ""
    hierarchy = compose_resolved_control_hierarchy(my, tuple(chain))
    assert hierarchy.effective_card.access_id == my.access_id
    from connection_hub.delegated_credentials.cards.census_read import PERSONAL_PROPERTIES

    for card in (my, *chain):
        assert not set(card.properties or {}) & set(PERSONAL_PROPERTIES)  # person-owned settings never travel


@pytest.mark.asyncio
async def test_person_owned_settings_are_withheld_and_authorization_properties_kept(tmp_path):
    from connection_hub.delegated_credentials.cards.census_read import _present

    operation, store, control, identity, catalog_store = await _world(tmp_path)
    card = dataclasses.replace(control, properties={
        "connection_hub.github": {"login": "someone"}, "connection_hub.commit_email": "a@example.test",
        "connection_hub.control_snapshot": {"schema": "x"}, "kdcube.application_operations": {"enabled": True},
        "service_composition_modes": {"https://x.test/mcp": "or"}})
    sent = _present(card)["authority"]["properties"]
    assert set(sent) == {"connection_hub.control_snapshot", "kdcube.application_operations",
                         "service_composition_modes"}


def test_the_hub_reproduces_the_foundations_shared_census_vectors():
    # AE #619: the closed AnswerContract.CENSUS vectors in service-foundation, byte for byte.
    import json as _json
    from pathlib import Path

    from service_foundation.coordination.participant_answer import AnswerContract, verify_participant_answer

    shared = _json.loads((Path(__file__).resolve().parents[5] / "packages" / "service-foundation" / "tests"
                          / "fixtures" / "census_answer_vectors.json").read_text())
    config = shared["config"]
    for vector in shared["vectors"]:
        assert census_request_digest(vector["request"]) == vector["request_digest"]
        answer = dict(vector["answer"])
        proof = answer.pop("receipt_proof")
        assert census_answer_signature(answer, secret=config["secret"], signer_id=proof["service_id"],
                                       timestamp=proof["timestamp"]) == proof["signature"]
        assert verify_participant_answer(
            vector["answer"], schema=config["schema"], secret=config["secret"], signer_id=config["signer_id"],
            audience=config["audience"], direction=config["direction"], request=vector["request"],
            now=config["now"], contract=AnswerContract.CENSUS) == vector["answer"]["result"]
