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
    """No Card body anywhere in the stored plan's intent."""
    intent = value["intent"]
    for which in ("original", "candidate"):
        item = intent[which]
        assert item is None or links.is_version_link(item), (which, item)
        assert item is None or not set(item) & _BODY_FIELDS


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
                                          link=link, marker=tag)
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
                                        link=stored["intent"]["candidate"], marker=tag)
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
        authority = await links.load_version(w.store, subject_hash=w.subject_hash, access_id=plan.access_id,
                                             link=link)
        at = datetime(2026, 10, 10, 22, 0, tzinfo=timezone.utc)
        first = await links.write_hidden_version(w.store, subject_hash=w.subject_hash, authority=authority, at=at,
                                                 tag=links.staging_tag("retry", "r"))
        again = await links.write_hidden_version(w.store, subject_hash=w.subject_hash, authority=authority, at=at,
                                                 tag=links.staging_tag("retry", "r"))
        assert first == again


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
            assert is_v1_intent(json.loads((directory / f"{plan.transaction_id}.json").read_text()))
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
