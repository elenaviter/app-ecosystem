# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""An unavailable comparison must not erase a catalog the Hub already read."""

from __future__ import annotations

import time

import pytest

from connection_hub.delegated_credentials.automation_access import record_from_card
from connection_hub.delegated_credentials.cards.resolver import CardUnavailable
from connection_hub.delegated_credentials.catalog.resolver import CatalogUnavailable
from connection_hub.delegated_credentials.controls.model import new_credentialless_card
from connection_hub.delegated_credentials.whole_card_save import WholeCardSaveRefused

from test_resident_profile_cards import GRANTOR, TASKS, USER, _Harness


def _old_card(harness, *, control=False):
    caller = harness.legacy_authority(
        resource=TASKS,
        grants=("tasks:use",),
        operations=("search",),
        created_at=int(time.time()),
        expires_at=int(time.time()) + 3600,
    )
    card = (
        new_credentialless_card(
            grantor_subject=GRANTOR,
            catalog_version=caller.catalog_version,
            initial_selection=caller,
            control_id="control-render-pin",
            issuer_ref="render-pin-test",
            issuer_kind="person",
            now=int(time.time()),
        )
        if control else caller
    )
    harness.persistence.seed(card)
    active = harness.catalog.publish(harness.connections)
    assert active.version != card.catalog_version
    return card, active


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [CatalogUnavailable, RuntimeError])
async def test_old_card_baseline_error_keeps_active_render_pin(tmp_path, monkeypatch, failure):
    h = _Harness(tmp_path)
    card, active = _old_card(h)

    async def unreadable(version):
        assert version == card.catalog_version
        raise failure("historical_read_failed")

    monkeypatch.setattr(h.catalog, "resolve_version", unreadable)
    result = await h.service.list_access(USER)
    assert result["ok"] is True
    item = result["items"][0]
    assert item["catalog_version"] == card.catalog_version
    drift = item["catalog_drift"]
    assert drift["status"] == "unavailable"
    assert drift["reason"] == (
        "historical_read_failed" if failure is CatalogUnavailable else "catalog_unavailable"
    )
    assert drift["current_version"] == active.version
    # The widget prefers current_version over the Card's older saved version.
    pin = drift.get("current_version") or item["catalog_version"]
    record, _, unchanged, _ = await h.service._whole_card_save_preflight(
        record=record_from_card(card), kind="agent_card",
        expected_card_revision=card.card_revision,
        expected_catalog_version=pin, selection={},
    )
    assert record.card_revision == card.card_revision
    assert unchanged is True
    assert h.persistence.persist_calls == 0
    # A later active generation still refuses that exact render pin.
    h.catalog.publish(h.connections)
    with pytest.raises(WholeCardSaveRefused, match="consent_catalog_changed"):
        await h.service._whole_card_save_preflight(
            record=record_from_card(card), kind="agent_card",
            expected_card_revision=card.card_revision,
            expected_catalog_version=pin, selection={},
        )
    assert h.persistence.persist_calls == 0


@pytest.mark.asyncio
async def test_confirmed_missing_baseline_is_not_an_unreadable_comparison(tmp_path):
    h = _Harness(tmp_path)
    card, active = _old_card(h)
    del h.catalog.docs[card.catalog_version]
    result = await h.service.list_access(USER)
    drift = result["items"][0]["catalog_drift"]
    assert drift["status"] != "unavailable"
    assert drift["baseline_confirmed_absent"] is True
    assert drift["current_version"] == active.version
    assert h.persistence.persist_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [CatalogUnavailable, RuntimeError])
async def test_active_read_failure_does_not_invent_a_render_pin(tmp_path, monkeypatch, failure):
    h = _Harness(tmp_path)
    card, _ = _old_card(h)

    async def unreadable():
        raise failure("active_read_failed")

    monkeypatch.setattr(h.catalog, "resolve_active", unreadable)
    drift = (await h.service._catalog_drift([record_from_card(card)]))[card.access_id]
    assert drift["status"] == "unavailable"
    assert "current_version" not in drift
    assert h.persistence.persist_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [CatalogUnavailable, CardUnavailable])
async def test_control_offer_error_preserves_the_completed_drift(tmp_path, monkeypatch, failure):
    h = _Harness(tmp_path)
    card, active = _old_card(h, control=True)
    expected = (await h.service._catalog_drift([record_from_card(card)]))[card.access_id]

    async def unreadable(*args, **kwargs):
        raise failure("offers_unavailable")

    monkeypatch.setattr(h.service, "resource_options", unreadable)
    result = await h.service.control_card_get(USER, control_id=card.access_id)
    assert result["ok"] is True
    for view in (result["card"], result["control_card"], result["access"]):
        assert view["catalog_drift"] == expected
        assert view["catalog_drift"]["current_version"] == active.version
        assert view["resource_offers"] == []
    assert h.persistence.persist_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["baseline", "projection"])
async def test_conflict_projection_keeps_the_known_active_pin(tmp_path, monkeypatch, where):
    h = _Harness(tmp_path)
    card, active = _old_card(h)

    async def unreadable(*args, **kwargs):
        raise CatalogUnavailable("comparison_read_failed")

    if where == "baseline":
        monkeypatch.setattr(h.catalog, "resolve_version", unreadable)
    else:
        monkeypatch.setattr(h.service, "_catalog_config", unreadable)
    result = await h.service._save_precondition_conflict(
        existing=record_from_card(card), active=active,
        expected_card_revision=card.card_revision - 1,
        expected_catalog_version=active.version,
    )
    assert result["status"] == 409
    assert result["error"] == "delegated_access_precondition_failed"
    assert result["mismatched"]["card_revision"]["actual"] == card.card_revision
    assert result["access"]["catalog_drift"]["status"] == "unavailable"
    assert result["access"]["catalog_drift"]["current_version"] == active.version
    assert h.persistence.persist_calls == 0
