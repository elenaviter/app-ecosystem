"""W502 (EMain #618): every Card property key in source is classified authorization or personal.

A new property constant fails this test until it is classified, so a person-
owned setting can never travel to another application by default.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from connection_hub.delegated_credentials.card_property_classes import AUTHORIZATION, PERSONAL

SOURCE = Path(__file__).resolve().parents[1] / "src" / "connection_hub"
CONSTANT = re.compile(r'^[A-Z_]*PROPERT(?:Y|IES)[A-Z_]* = "([^"]+)"', re.MULTILINE)


def _source_property_keys() -> set[str]:
    found = set()
    for path in SOURCE.rglob("*.py"):
        found.update(CONSTANT.findall(path.read_text(encoding="utf-8")))
    return found


def test_every_property_constant_in_source_is_classified():
    keys = _source_property_keys()
    assert keys, "the scan must find the property constants"
    unclassified = keys - AUTHORIZATION - PERSONAL
    assert not unclassified, f"classify these Card property keys in card_property_classes: {sorted(unclassified)}"


def test_no_key_is_both_authorization_and_personal():
    assert not AUTHORIZATION & PERSONAL


@pytest.mark.asyncio
async def test_a_lifecycle_created_pairs_property_keys_are_all_classified(tmp_path, redis_client):
    from test_w502_my_card_fence_real_path import _control, _create, _my_card, _service

    h = await _service(tmp_path, redis_client)
    assert (await _create(h, "request-create"))["ok"] is True
    cards = [await _control(h), (await _my_card(h))[1]]
    keys = {key for card in cards for key in (card.properties or {})}
    assert not keys - AUTHORIZATION - PERSONAL, sorted(keys - AUTHORIZATION - PERSONAL)


def test_the_census_sends_only_classified_authorization_keys():
    from dataclasses import replace

    from connection_hub.delegated_credentials.cards.census_read import _present
    from test_card_service import _authority

    card = replace(_authority(), properties={
        "connection_hub.github": {"login": "someone"}, "connection_hub.control_snapshot": {"schema": "x"},
        "kdcube.some_future_setting": {"value": 1}, "service_composition_modes": {}})
    assert set(_present(card)["authority"]["properties"]) == {"connection_hub.control_snapshot",
                                                              "service_composition_modes"}


from test_w580_bound_card_writers import redis_client  # noqa: E402,F401 - fixture
