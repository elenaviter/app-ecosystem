"""W661 scope B (b): the OAuth issuance plan holds Card version LINKS, never Card bodies.

Operator rule: "each card has it data ONCE. others LINK". The plan's original is the committed version
current.json names; its candidate is written once as its own txn-tagged version file, hidden behind a
staging tag no transaction has (cards/version_link.py). Real stores, as in test_w603_original_issuance.

DSN- and Redis-gated (CONNECTION_HUB_TEST_POSTGRES_DSN, REDIS_URL).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from connection_hub.delegated_credentials.cards import version_link as links
from connection_hub.delegated_credentials.cards.model import CardRecordError
from connection_hub.delegated_credentials.oauth.authority_schema import TABLE_ISSUANCE_PLANS
from connection_hub.delegated_credentials.oauth.bearers import bearer_sha256
from connection_hub.delegated_credentials.oauth_issuance import IssuanceRefused
from test_w603_original_issuance import _begin, _card, _reserve, _world

def authority_body(link):
    """Any mapping with a Card's fields is not a link."""
    return {**link, "operations": [], "resource_grants": {}}


_BODY_FIELDS = {"operations", "resource_grants", "resource_operations", "properties", "client_metadata"}


async def _stored(w, plan):
    stored = await w.authority.read_issuance_plan(plan.transaction_id)
    assert stored is not None
    return stored["plan"]


def _assert_no_bodies(value):
    """No Card body anywhere in the stored plan: not in its intent, not as a top-level authority copy."""
    assert not {"operations", "resource_grants", "resource_operations"} & set(value)
    intent = value["intent"]
    for which in ("original", "candidate"):
        item = intent[which]
        assert item is None or links.is_version_link(item), (which, item)
        assert item is None or not set(item) & _BODY_FIELDS


def _owners(plan):
    """A begun plan's candidate owners: its staging tag, or (once lane 3's record adopted it) its
    transaction, member 0 of a first-consent group."""
    from connection_hub.delegated_credentials.cards.transaction_store import member_transaction_id

    return {links.staging_tag("oauth-issuance-candidate", plan.decision_request_id),
            plan.transaction_id, member_transaction_id(plan.transaction_id, 0)}


async def _complete(w, plan):
    tokens = await _reserve(w, plan)
    return await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id,
                                                   expect={s: bearer_sha256(t) for s, t in tokens.items()})


@pytest.mark.asyncio
async def test_a_first_consent_plan_links_a_hidden_candidate_version_and_commits_it(tmp_path):
    async with _world(tmp_path) as w:
        plan = await _begin(w)
        stored = await _stored(w, plan)
        _assert_no_bodies(stored)
        assert stored["intent"]["original"] is None  # group creation: no original
        link = stored["intent"]["candidate"]
        assert link["card_revision"] == plan.candidate_revision == 1
        assert link["content_hash"] == plan.card_content_hash
        tag = links.staging_tag("oauth-issuance-candidate", plan.decision_request_id)
        # The candidate exists ONCE as its own version file, hidden: never a serving read, never current.
        assert await w.store.read_revision(subject_hash=w.subject_hash, access_id=plan.access_id,
                                           revision_name=link["revision_name"]) is None
        assert await _card(w, plan.access_id) is None
        loaded = await links.load_version(w.store, subject_hash=w.subject_hash, access_id=plan.access_id,
                                          link=link, owners=_owners(plan))
        assert loaded.content_hash() == plan.card_content_hash
        result = await _complete(w, plan)
        assert (result.state, result.card_revision) == ("committed", 1)
        assert (await _card(w, plan.access_id)).content_hash() == plan.card_content_hash


@pytest.mark.asyncio
async def test_a_reconsent_plan_links_the_committed_original_by_its_pointer(tmp_path):
    async with _world(tmp_path) as w:
        first = await _begin(w)
        assert (await _complete(w, first)).state == "committed"
        pointer, _authority = await w.store.read_current_authority(subject_hash=w.subject_hash,
                                                                   access_id=first.access_id)
        plan = await _begin(w, request="exchange-2", scopes=["memories:read", "memories:write"])
        stored = await _stored(w, plan)
        _assert_no_bodies(stored)
        assert stored["intent"]["original"] == links.pointer_link(pointer)
        assert (await _complete(w, plan)).state == "committed"
        assert (await _card(w, plan.access_id)).card_revision == plan.candidate_revision


@pytest.mark.asyncio
async def test_a_changed_candidate_version_file_is_refused_by_name(tmp_path):
    async with _world(tmp_path) as w:
        plan = await _begin(w)
        link = (await _stored(w, plan))["intent"]["candidate"]
        path = w.store.revision_path(subject_hash=w.subject_hash, access_id=plan.access_id,
                                     revision_name=link["revision_name"])
        payload = json.loads(path.read_text())
        payload["client_label"] = "changed after the plan"
        path.write_text(json.dumps(payload))
        with pytest.raises(IssuanceRefused) as refused:
            await _complete(w, plan)
        assert refused.value.reason == "issuance_plan_card_unavailable"


@pytest.mark.asyncio
async def test_a_legacy_plan_that_stored_bodies_still_completes(tmp_path):
    """A v1 plan in flight across the upgrade keeps working until its decision finishes."""
    async with _world(tmp_path) as w:
        plan = await _begin(w)
        stored = await _stored(w, plan)
        tag = links.staging_tag("oauth-issuance-candidate", plan.decision_request_id)
        body = await links.load_version(w.store, subject_hash=w.subject_hash, access_id=plan.access_id,
                                        link=stored["intent"]["candidate"], owners=_owners(plan))
        legacy = {**stored, "intent": {key: value for key, value in stored["intent"].items() if key != "card_kind"}}
        legacy["intent"]["candidate"] = body.to_dict()
        async with w.pool.acquire() as connection:
            await connection.execute(
                f"UPDATE {w.authority.schema}.{TABLE_ISSUANCE_PLANS} SET plan=($1::text)::jsonb "
                "WHERE decision_request_id=$2", json.dumps(legacy), plan.decision_request_id)
        assert (await _complete(w, plan)).state == "committed"


@pytest.mark.asyncio
async def test_the_version_link_helper_refuses_a_wrong_marker_card_or_shape(tmp_path):
    async with _world(tmp_path) as w:
        plan = await _begin(w)
        link = (await _stored(w, plan))["intent"]["candidate"]
        for bad in ({**link, "extra": 1}, {**link, "card_revision": 0}, {**link, "content_hash": "x" * 64},
                    {**link, "revision_name": "../escape.json"}, authority_body(link)):
            assert not links.is_version_link(bad)
        with pytest.raises(CardRecordError, match="version_link_marker_mismatch"):
            await links.load_version(w.store, subject_hash=w.subject_hash, access_id=plan.access_id, link=link,
                                     marker=links.staging_tag("another", "request"))
        with pytest.raises(CardRecordError):
            await links.load_version(w.store, subject_hash=w.subject_hash, access_id="another-card", link=link)
        # A retry of the same write (same at, same tag) reuses the one file.
        tag = links.staging_tag("oauth-issuance-candidate", plan.decision_request_id)
        authority = await links.load_version(w.store, subject_hash=w.subject_hash, access_id=plan.access_id,
                                             link=link, owners=_owners(plan))
        with pytest.raises(CardRecordError, match="version_link_marker_mismatch"):  # Spark N2
            await links.load_version(w.store, subject_hash=w.subject_hash, access_id=plan.access_id, link=link)
        at = datetime(2026, 10, 10, 22, 0, tzinfo=timezone.utc)
        first = await links.write_hidden_version(w.store, subject_hash=w.subject_hash, authority=authority, at=at,
                                                 tag=links.staging_tag("retry", "r"))
        again = await links.write_hidden_version(w.store, subject_hash=w.subject_hash, authority=authority, at=at,
                                                 tag=links.staging_tag("retry", "r"))
        assert first == again


async def _seed_legacy_v1_intent(w, plan):
    """An explicit legacy v1 intent (Card BODIES) for this plan's transaction, as written before intent v2.

    Lane 3 (W697) retires a new intent at FINISH, so the purge tests seed the files they purge instead of
    relying on a new intent to remain (CodeApp, 22:35Z)."""
    stored = await _stored(w, plan)
    tag = links.staging_tag("oauth-issuance-candidate", plan.decision_request_id)
    try:
        body = (await links.load_version(w.store, subject_hash=w.subject_hash, access_id=plan.access_id,
                                         link=stored["intent"]["candidate"], owners={tag},
                                         allow_unmarked=True)).to_dict()
    except CardRecordError:
        body = {"card_revision": plan.candidate_revision, "access_id": plan.access_id}
    path = w.store.root / "card-transactions" / "intents" / f"{plan.transaction_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"transaction_id": plan.transaction_id, "subject_hash": w.subject_hash,
                                "original": None, "candidate": body}))
    return path


@pytest.mark.asyncio
async def test_the_cutover_purge_deletes_only_finished_v1_intents(tmp_path):
    """W661 scope B (c): one-off; a dry run lists, apply deletes; an in-flight v1 intent stays readable."""
    from connection_hub.delegated_credentials.cards.intent_purge import is_v1_intent, purge_finished_v1_intents

    async with _world(tmp_path) as w:
        done = await _begin(w)
        assert (await _complete(w, done)).state == "committed"
        pending = await _begin(w, request="exchange-in-flight")  # begun, never completed
        directory = w.store.root / "card-transactions" / "intents"
        for plan in (done, pending):
            assert is_v1_intent(json.loads((await _seed_legacy_v1_intent(w, plan)).read_text()))
        dry = await purge_finished_v1_intents(w.store, w.decisions)
        assert (dry["applied"], dry["deleted"], dry["kept_in_flight"]) \
            == (False, [done.transaction_id], [pending.transaction_id])
        assert (directory / f"{done.transaction_id}.json").exists()  # a dry run deletes nothing
        applied = await purge_finished_v1_intents(w.store, w.decisions, apply=True)
        assert applied["deleted"] == [done.transaction_id]
        assert not (directory / f"{done.transaction_id}.json").exists()
        assert (directory / f"{pending.transaction_id}.json").exists()
        assert "original" not in json.dumps(applied) and "candidate" not in json.dumps(applied)  # ids only
        again = await purge_finished_v1_intents(w.store, w.decisions, apply=True)
        assert again["deleted"] == []  # idempotent


@pytest.mark.asyncio
async def test_a_hidden_version_is_never_overwritten_and_is_adopted_by_moving_only_its_marker(tmp_path):
    from connection_hub.delegated_credentials.cards.transaction_store import revision_marker_path

    async with _world(tmp_path) as w:
        plan = await _begin(w)
        stored = await _stored(w, plan)
        trusted = await w.service.read_oauth_issuance_plan(transaction_id=plan.transaction_id)
        assert (trusted.operations, trusted.resource_grants) == (plan.operations, plan.resource_grants)  # derived
        link = stored["intent"]["candidate"]
        tag = links.staging_tag("oauth-issuance-candidate", plan.decision_request_id)
        authority = await links.load_version(w.store, subject_hash=w.subject_hash, access_id=plan.access_id,
                                             link=link, owners=_owners(plan))
        at = datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc)
        other = links.staging_tag("test", "owner")
        first = await links.write_hidden_version(w.store, subject_hash=w.subject_hash, authority=authority,
                                                 at=at, tag=other)
        marker = revision_marker_path(w.store, subject_hash=w.subject_hash, access_id=plan.access_id,
                                      revision_name=first["revision_name"])
        path = w.store.revision_path(subject_hash=w.subject_hash, access_id=plan.access_id,
                                     revision_name=first["revision_name"])
        # A marker naming another owner refuses; nothing is rewritten.
        marker.write_text(json.dumps({"transaction_id": "f" * 64}))
        with pytest.raises(CardRecordError, match="version_link_owner_conflict"):
            await links.write_hidden_version(w.store, subject_hash=w.subject_hash, authority=authority, at=at,
                                             tag=other)
        # A corrupt file refuses; it is never overwritten.
        marker.write_text(json.dumps({"transaction_id": other}))
        original_bytes = path.read_bytes()
        path.write_text(json.dumps({**json.loads(original_bytes), "client_label": "corrupt"}))
        with pytest.raises(CardRecordError, match="revision_content_hash_mismatch"):
            await links.write_hidden_version(w.store, subject_hash=w.subject_hash, authority=authority, at=at,
                                             tag=other)
        path.write_bytes(original_bytes)
        # A replay whose marker is gone (adopted and committed) reuses the file and recreates no marker.
        marker.unlink()
        assert await links.write_hidden_version(w.store, subject_hash=w.subject_hash, authority=authority,
                                                at=at, tag=other) == first
        assert not marker.exists()
        # Adoption: the SAME file, only the marker moves; a retry is accepted; another owner refuses.
        txn = "a" * 64
        candidate_marker = revision_marker_path(w.store, subject_hash=w.subject_hash, access_id=plan.access_id,
                                                revision_name=first["revision_name"])
        candidate_path = w.store.revision_path(subject_hash=w.subject_hash, access_id=plan.access_id,
                                               revision_name=first["revision_name"])
        link, tag = first, other
        candidate_marker.write_text(json.dumps({"transaction_id": other}))  # staged again under its tag
        before = candidate_path.read_bytes()
        for _ in range(2):
            assert await links.adopt_hidden_version(w.store, subject_hash=w.subject_hash, access_id=plan.access_id,
                                                    link=link, from_tag=tag, to_transaction_id=txn) == link
        assert json.loads(candidate_marker.read_text()) == {"transaction_id": txn}
        assert candidate_path.read_bytes() == before
        with pytest.raises(CardRecordError, match="version_link_owner_conflict"):
            await links.adopt_hidden_version(w.store, subject_hash=w.subject_hash, access_id=plan.access_id,
                                             link=link, from_tag=tag, to_transaction_id="e" * 64)


@pytest.mark.asyncio
async def test_the_plan_reads_its_candidate_after_adoption_and_after_commit_but_never_a_foreign_owner(tmp_path):
    """r3: lane 3 adopts the candidate (txn, or member 0 of a first-consent group) and FINISH may drop the
    marker; the plan still reads exactly that file. Any other owner refuses."""
    from connection_hub.delegated_credentials.cards.transaction_store import member_transaction_id, revision_marker_path

    async with _world(tmp_path) as w:
        plan = await _begin(w)  # first consent: a group creation
        link = (await _stored(w, plan))["intent"]["candidate"]
        tag = links.staging_tag("oauth-issuance-candidate", plan.decision_request_id)
        member = member_transaction_id(plan.transaction_id, 0)
        await links.adopt_hidden_version(w.store, subject_hash=w.subject_hash, access_id=plan.access_id, link=link,
                                         from_tag=tag, to_transaction_id=member)
        trusted = await w.service.read_oauth_issuance_plan(transaction_id=plan.transaction_id)
        assert trusted.operations == plan.operations  # read by the adopted owner
        marker = revision_marker_path(w.store, subject_hash=w.subject_hash, access_id=plan.access_id,
                                      revision_name=link["revision_name"])
        marker.write_text(json.dumps({"transaction_id": "d" * 64}))  # a foreign owner
        with pytest.raises(IssuanceRefused) as refused:
            await w.service.read_oauth_issuance_plan(transaction_id=plan.transaction_id)
        assert refused.value.reason == "issuance_plan_card_unavailable"
        marker.write_text(json.dumps({"transaction_id": member}))
        assert (await _complete(w, plan)).state == "committed"
        marker.unlink(missing_ok=True)  # committed: the marker may go; the plan still reads its candidate
        assert (await w.service.read_oauth_issuance_plan(transaction_id=plan.transaction_id)).operations \
            == plan.operations


@pytest.mark.asyncio
async def test_a_present_json_null_version_or_marker_is_corruption_never_absence(tmp_path):
    """r4 (Infra F1): read_json_or_none returns None for an absent file AND a present JSON null; a present
    null is refused and its bytes preserved, never overwritten, never read as "unmarked"."""
    from connection_hub.delegated_credentials.cards.transaction_store import revision_marker_path

    async with _world(tmp_path) as w:
        plan = await _begin(w)
        link = (await _stored(w, plan))["intent"]["candidate"]
        tag = links.staging_tag("oauth-issuance-candidate", plan.decision_request_id)
        authority = await links.load_version(w.store, subject_hash=w.subject_hash, access_id=plan.access_id,
                                             link=link, owners=_owners(plan))
        at = datetime(2026, 10, 11, 0, 30, tzinfo=timezone.utc)
        other = links.staging_tag("test", "null")
        written = await links.write_hidden_version(w.store, subject_hash=w.subject_hash, authority=authority,
                                                   at=at, tag=other)
        path = w.store.revision_path(subject_hash=w.subject_hash, access_id=plan.access_id,
                                     revision_name=written["revision_name"])
        path.write_text("null")
        with pytest.raises(CardRecordError, match="revision_content_hash_mismatch"):
            await links.write_hidden_version(w.store, subject_hash=w.subject_hash, authority=authority, at=at,
                                             tag=other)
        assert path.read_text() == "null"  # preserved, not repaired
        with pytest.raises(CardRecordError, match="revision_content_hash_mismatch"):
            await links.load_version(w.store, subject_hash=w.subject_hash, access_id=plan.access_id, link=written,
                                     marker=other)
        # A present null marker is not "unmarked": the committed-state read refuses it.
        marker = revision_marker_path(w.store, subject_hash=w.subject_hash, access_id=plan.access_id,
                                      revision_name=link["revision_name"])
        marker.write_text("null")
        with pytest.raises(CardRecordError, match="version_link_marker_mismatch"):
            await links.load_version(w.store, subject_hash=w.subject_hash, access_id=plan.access_id, link=link,
                                     owners={tag}, allow_unmarked=True)
        assert marker.read_text() == "null"


@pytest.mark.asyncio
async def test_the_purge_apply_obeys_the_card_store_writer_guard(tmp_path):
    """r4 (Infra F3): deletion goes through durable_io.unlink_guarded; a refusing guard refuses the purge
    and the intent stays. A dry run stays read-only either way."""
    import os

    from connection_hub.delegated_credentials import durable_io
    from connection_hub.delegated_credentials.cards.intent_purge import purge_finished_v1_intents

    async with _world(tmp_path) as w:
        done = await _begin(w)
        assert (await _complete(w, done)).state == "committed"
        path = await _seed_legacy_v1_intent(w, done)
        before = path.read_bytes()

        def refuse():
            raise PermissionError("writer guard: not the proc")

        durable_io.guard_writes_under(w.store.root, refuse)
        try:
            assert (await purge_finished_v1_intents(w.store, w.decisions))["deleted"] == [done.transaction_id]
            with pytest.raises(PermissionError):
                await purge_finished_v1_intents(w.store, w.decisions, apply=True)
            assert path.read_bytes() == before
        finally:
            durable_io._WRITE_GUARDS.pop(os.path.join(os.path.abspath(os.fspath(w.store.root)), ""), None)
        assert (await purge_finished_v1_intents(w.store, w.decisions, apply=True))["deleted"] == [done.transaction_id]
        assert not path.exists()


def test_a_staging_tag_is_structurally_outside_the_transaction_namespace():
    """r5 (Ops): ``stg-`` plus a digest of a length-prefixed encoding; never 64-hex, never ambiguous."""
    import re

    tag = links.staging_tag("oauth-issuance-candidate", "a" * 64)
    assert tag.startswith("stg-") and not re.fullmatch(r"[0-9a-f]{64}", tag) and links.is_staging_tag(tag)
    assert links.staging_tag("a:b", "c") != links.staging_tag("a", "b:c")
    assert links.staging_tag("ab", "c") != links.staging_tag("a", "bc")
    with pytest.raises(CardRecordError):
        links.staging_tag("", "x")


@pytest.mark.asyncio
async def test_a_planned_version_stays_hidden_on_every_by_name_read(tmp_path):
    async with _world(tmp_path) as w:
        plan = await _begin(w)
        link = (await _stored(w, plan))["intent"]["candidate"]
        assert await w.store.read_revision(subject_hash=w.subject_hash, access_id=plan.access_id,
                                           revision_name=link["revision_name"]) is None
        assert link["revision_name"] not in [name for name in await w.store.list_revision_names(
            subject_hash=w.subject_hash, access_id=plan.access_id)
            if await w.store.read_revision(subject_hash=w.subject_hash, access_id=plan.access_id,
                                           revision_name=name) is not None]


async def _unbegun_plan(w, request):
    """A stored plan whose decision never began, already past its deadline."""
    from connection_hub.delegated_credentials.oauth_issuance import decision_request_id, original_input_digest
    from connection_hub.delegated_credentials.cards.card_participant import PARTICIPANT
    from test_w603_original_issuance import CLIENT, GRANTOR, RESOURCE, SCOPES

    _c, _i, _d, ttl, store = w.service._issuance_parts()
    request_id = decision_request_id(scope=f"{PARTICIPANT}:oauth-issuance", grantor_subject=GRANTOR,
                                     client_id=CLIENT, original_request_id=request)
    inputs = {"client_label": "Claude Code", "scopes": SCOPES, "operations": None, "resource_grants": None,
              "resource_operations": None, "resource": RESOURCE, "access_id": "", "card_kind": "",
              "identity_scope": "", "account_scope": None, "named_service_operations": None,
              "catalog_version": "", "client_metadata": None, "properties": None, "replace_authority": True,
              "expected_card_revision": None}
    digest = original_input_digest({"grantor_subject": GRANTOR, "client_id": CLIENT, **inputs})
    planned = await w.service._plan_oauth_issuance(store=store, ttl=ttl, request=request_id, input_digest=digest,
                                                   grantor=GRANTOR, client=CLIENT, record_inputs=inputs)
    planned["draft"]["expires_at"] = 1  # its decision can no longer begin: provably unbegun
    await store.put_issuance_plan(decision_request_id=request_id, original_input_digest=digest, plan=planned,
                                  reserved_until=1)
    return request_id, planned


@pytest.mark.asyncio
async def test_an_unbegun_plan_ends_with_its_deadline_and_takes_its_planned_version_with_it(tmp_path):
    """r5 (Ops): no scan; the plan names its file. An adopted candidate is never this sweep's."""
    from connection_hub.delegated_credentials.cards.transaction_store import revision_marker_path

    async with _world(tmp_path) as w:
        request_id, planned = await _unbegun_plan(w, "never-begun")
        kept_id, kept = await _unbegun_plan(w, "adopted-meanwhile")
        files = {}
        for rid, plan in ((request_id, planned), (kept_id, kept)):
            link = plan["intent"]["candidate"]
            files[rid] = (w.store.revision_path(subject_hash=w.subject_hash, access_id=plan["access_id"],
                                                revision_name=link["revision_name"]),
                          revision_marker_path(w.store, subject_hash=w.subject_hash, access_id=plan["access_id"],
                                               revision_name=link["revision_name"]))
            assert all(path.exists() for path in files[rid])
        files[kept_id][1].write_text(json.dumps({"transaction_id": "c" * 64}))  # adopted by a transaction
        assert await w.service.release_unbegun_oauth_issuance_plans() == 1
        assert not any(path.exists() for path in files[request_id])  # file and marker gone
        assert await w.authority.read_issuance_plan_request(request_id) is None
        assert all(path.exists() for path in files[kept_id])  # not ours: kept, plan kept
        assert await w.authority.read_issuance_plan_request(kept_id) is not None
        assert await w.service.release_unbegun_oauth_issuance_plans() == 0  # idempotent


@pytest.mark.asyncio
async def test_a_pending_decision_never_treats_a_missing_marker_as_committed(tmp_path):
    """Infra item 8: no marker is accepted only when the bound decision is COMMITTED in the log."""
    from connection_hub.delegated_credentials.cards.transaction_store import revision_marker_path

    async with _world(tmp_path) as w:
        plan = await _begin(w)
        assert not (await w.decisions.read(plan.transaction_id)).terminal
        link = (await _stored(w, plan))["intent"]["candidate"]
        revision_marker_path(w.store, subject_hash=w.subject_hash, access_id=plan.access_id,
                             revision_name=link["revision_name"]).unlink()
        with pytest.raises(IssuanceRefused, match="issuance_plan_card_unavailable"):
            await w.service.read_oauth_issuance_plan(transaction_id=plan.transaction_id)


def _version_files(w, access_id):
    """Every version file of one Card (TEST-only listing, to prove nothing extra exists)."""
    directory = w.store.card_path(subject_hash=w.subject_hash, access_id=access_id) / "revisions"
    return sorted(path.name for path in directory.glob("card_revision_*.json")
                  if not path.name.endswith((".card-transaction.json", ".card-version.json", ".lifecycle.json",
                                             ".issuer-update.json")))



def _staged_files(w, access_id):
    """Every staging-owned candidate file of one Card (a TEST-only listing, to prove nothing extra exists)."""
    directory = w.store.card_path(subject_hash=w.subject_hash, access_id=access_id) / "revisions"
    found = []
    for marker in directory.glob("*.card-transaction.json"):
        owner = json.loads(marker.read_text()).get("transaction_id", "")
        if str(owner).startswith("stg-"):
            found.append(marker.name)
    return sorted(found)


@pytest.mark.asyncio
async def test_a_crash_before_the_plan_is_stored_is_retried_onto_the_same_file(tmp_path, monkeypatch):
    """Spark B2 (a)(c): the attempt row (clock + link) is written first; the retry rebuilds the same
    candidate at the same clock, names the same file and leaves exactly one."""
    async with _world(tmp_path) as w:
        original_put = w.authority.put_issuance_plan

        async def crash(**_kwargs):
            raise RuntimeError("synthetic crash after the candidate file, before the plan row")

        monkeypatch.setattr(w.authority, "put_issuance_plan", crash)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            await _begin(w, request="crashing")
        monkeypatch.setattr(w.authority, "put_issuance_plan", original_put)
        plan = await _begin(w, request="crashing")
        link = (await _stored(w, plan))["intent"]["candidate"]
        assert _version_files(w, plan.access_id) == [link["revision_name"]]  # one file, whoever owns it now
        assert await w.authority.read_plan_attempt(plan.decision_request_id) is None  # gone once stored
        assert (await _complete(w, plan)).state == "committed"


@pytest.mark.asyncio
async def test_concurrent_planners_of_one_request_store_one_plan_and_one_file(tmp_path):
    """Spark B2 (b): the planning section serializes them; the second finds the first's plan."""
    import asyncio

    async with _world(tmp_path) as w:
        first, second = await asyncio.gather(_begin(w, request="raced"), _begin(w, request="raced"))
        assert first == second
        link = (await _stored(w, first))["intent"]["candidate"]
        assert _version_files(w, first.access_id) == [link["revision_name"]]


@pytest.mark.asyncio
async def test_an_aborted_decision_releases_its_planned_candidate(tmp_path):
    async with _world(tmp_path) as w:
        plan = await _begin(w)
        link = (await _stored(w, plan))["intent"]["candidate"]
        path = w.store.revision_path(subject_hash=w.subject_hash, access_id=plan.access_id,
                                     revision_name=link["revision_name"])
        assert path.exists()
        result = await w.service.complete_oauth_issuance(transaction_id=plan.transaction_id, expect=None)
        assert result.state == "aborted"  # nothing was reserved
        with pytest.raises(IssuanceRefused, match="issuance_decision_closed"):
            await _reserve(w, plan, slots=("access",))
        async with w.pool.acquire() as connection:
            await connection.execute(f"UPDATE {w.authority.schema}.connection_hub_oauth_issuance_plans "
                                     "SET reserved_until = to_timestamp(1) WHERE decision_request_id = $1",
                                     plan.decision_request_id)
        await w.service.release_unbegun_oauth_issuance_plans()
        # Gone either way: lane 3's FINISH(ABORT) removes an adopted candidate; the sweep a staging-owned one.
        assert not path.exists() and _version_files(w, plan.access_id) == []
        with pytest.raises(IssuanceRefused, match="issuance_decision_closed"):
            await w.service.read_oauth_issuance_plan(transaction_id=plan.transaction_id)
        assert await w.service.release_unbegun_oauth_issuance_plans() == 0  # released once (flag)


@pytest.mark.asyncio
async def test_an_expired_attempt_that_never_retried_is_swept_by_its_row(tmp_path, monkeypatch):
    async with _world(tmp_path) as w:
        async def crash(**_kwargs):
            raise RuntimeError("synthetic crash before the plan row")

        monkeypatch.setattr(w.authority, "put_issuance_plan", crash)
        with pytest.raises(RuntimeError):
            await _begin(w, request="abandoned")
        async with w.pool.acquire() as connection:
            await connection.execute(f"UPDATE {w.authority.schema}.connection_hub_oauth_issuance_plan_attempts "
                                     "SET expires_at = to_timestamp(1)")
        assert await w.service.release_unbegun_oauth_issuance_plans() >= 1
        for directory in w.store.root.rglob("revisions"):
            assert not [m for m in directory.glob("*.card-transaction.json")
                        if json.loads(m.read_text()).get("transaction_id", "").startswith("stg-")]


@pytest.mark.asyncio
async def test_expiry_cleanup_keeps_an_already_begun_unbound_plan(tmp_path, monkeypatch):
    """Infra H2 witness (W692, sha256 74a7c9f5), unchanged in substance: a real begin paused AFTER the durable
    decision begin, past the real deadline; the sweep must keep the plan and its candidate (begin holds the
    planning section)."""
    import asyncio

    from connection_hub.delegated_credentials.cards.transaction_store import revision_marker_path

    async with _world(tmp_path) as w:
        coordinator, intents, decisions, _ttl, _store = w.service._issuance_parts()
        w.service.bind_card_coordinator(coordinator, intents=intents, decisions=decisions, intent_ttl_seconds=2)
        reached, resume, captured = asyncio.Event(), asyncio.Event(), {}
        original_begin = decisions.begin

        async def begin_then_pause(draft):
            row = await original_begin(draft)
            captured["row"] = row
            reached.set()
            await resume.wait()
            return row

        monkeypatch.setattr(decisions, "begin", begin_then_pause)
        task = asyncio.create_task(_begin(w, request="cleanup-durable-begin-gap"))
        try:
            await asyncio.wait_for(reached.wait(), timeout=10)
            row = captured["row"]
            pending = await w.authority.read_issuance_plan_request(row.intent.request_id)
            plan = pending["plan"]
            link = plan["intent"]["candidate"]
            paths = (w.store.revision_path(subject_hash=w.subject_hash, access_id=plan["access_id"],
                                           revision_name=link["revision_name"]),
                     revision_marker_path(w.store, subject_hash=w.subject_hash, access_id=plan["access_id"],
                                          revision_name=link["revision_name"]))
            before = tuple(path.read_bytes() for path in paths)
            while await w.authority.issuance_clock() <= plan["draft"]["expires_at"]:
                await asyncio.sleep(0.05)
            released = await w.service.release_unbegun_oauth_issuance_plans()
            observed = (released, await w.authority.read_issuance_plan_request(plan["decision_request_id"]) is not None,
                        tuple(path.read_bytes() if path.exists() else None for path in paths) == before)
        finally:
            resume.set()
            result = (await asyncio.gather(task, return_exceptions=True))[0]
        assert observed == (0, True, True), observed
        assert not isinstance(result, Exception), type(result).__name__


@pytest.mark.asyncio
async def test_a_begin_that_stopped_before_its_binding_is_bound_by_the_sweep_not_deleted(tmp_path, monkeypatch):
    """The begin crashed after the durable decision began, before the plan's binding (lock released): the
    sweep finds the decision by its draft, READ ONLY, and binds it; nothing is deleted."""
    import asyncio

    async with _world(tmp_path) as w:
        coordinator, intents, decisions, _ttl, _store = w.service._issuance_parts()
        w.service.bind_card_coordinator(coordinator, intents=intents, decisions=decisions, intent_ttl_seconds=2)

        async def crash(**_kwargs):
            raise RuntimeError("synthetic crash before the plan's binding")

        original_bind = w.authority.bind_issuance_plan_transaction
        monkeypatch.setattr(w.authority, "bind_issuance_plan_transaction", crash)
        with pytest.raises(Exception):
            await _begin(w, request="stopped-before-bind")
        monkeypatch.setattr(w.authority, "bind_issuance_plan_transaction", original_bind)
        from connection_hub.delegated_credentials.oauth_issuance import decision_request_id
        from connection_hub.delegated_credentials.cards.card_participant import PARTICIPANT
        from test_w603_original_issuance import CLIENT, GRANTOR

        request = decision_request_id(scope=f"{PARTICIPANT}:oauth-issuance", grantor_subject=GRANTOR,
                                      client_id=CLIENT, original_request_id="stopped-before-bind")
        stored = await w.authority.read_issuance_plan_request(request)
        assert stored is not None and not stored["transaction_id"]
        while await w.authority.issuance_clock() <= stored["plan"]["draft"]["expires_at"]:
            await asyncio.sleep(0.05)
        assert await w.service.release_unbegun_oauth_issuance_plans() == 0
        bound = await w.authority.read_issuance_plan_request(request)
        began = await decisions.read_by_request(stored["plan"]["draft"]["replay_scope"], request)
        assert bound["transaction_id"] == began.transaction_id


@pytest.mark.asyncio
async def test_the_issuance_record_adopts_the_planned_candidate_so_each_version_exists_once(tmp_path):
    """Wiring (lane 4 on lane 3): the plan's candidate file IS the version STAGE commits; one file per
    revision, adopted by the real transaction (member 0 of a first-consent group), never a second copy."""
    from connection_hub.delegated_credentials.cards.transaction_store import member_transaction_id, revision_marker_path

    async with _world(tmp_path) as w:
        first = await _begin(w)
        link = (await _stored(w, first))["intent"]["candidate"]
        marker = revision_marker_path(w.store, subject_hash=w.subject_hash, access_id=first.access_id,
                                      revision_name=link["revision_name"])
        assert json.loads(marker.read_text()) == {"transaction_id": member_transaction_id(first.transaction_id, 0)}
        assert (await _complete(w, first)).state == "committed"
        assert _version_files(w, first.access_id) == [link["revision_name"]]
        current = await w.store.read_current_authority(subject_hash=w.subject_hash, access_id=first.access_id)
        assert current[0].revision_name == link["revision_name"]
        second = await _begin(w, request="exchange-2", scopes=["memories:read", "memories:write"])
        link2 = (await _stored(w, second))["intent"]["candidate"]
        assert (await _complete(w, second)).state == "committed"
        assert _version_files(w, second.access_id) == sorted([link["revision_name"], link2["revision_name"]])
        assert (await w.service.read_oauth_issuance_plan(transaction_id=second.transaction_id)).operations \
            == second.operations
