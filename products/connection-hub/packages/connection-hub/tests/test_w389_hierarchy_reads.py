# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""W389: the Control Card chain is read with fewer waits, and every dependency is still re-checked."""

from __future__ import annotations

import asyncio
import collections
import dataclasses

import pytest

from connection_hub.delegated_credentials.controls.effective import ControlCardMismatch
from connection_hub.delegated_credentials.controls.hierarchy import compose_control_hierarchy

from test_control_hierarchy import RESOURCE, binding, caller, control


def _chain():
    parent = control("parent", [RESOURCE], "or")
    middle = dataclasses.replace(control("middle", [], "or"), control_card=binding(parent))
    child = dataclasses.replace(control("child", [], "and"), control_card=binding(middle))
    return parent, middle, child, caller([], child)


def _store(*cards):
    return {("owner", card.access_id): card for card in cards}


async def test_a_preloaded_card_is_not_read_by_the_walk_but_is_still_re_checked():
    parent, middle, child, person = _chain()
    stored = _store(parent, middle, child)
    reads = collections.Counter()

    async def load(control_id, *, grantor_subject):
        reads[control_id] += 1
        return stored.get((grantor_subject, control_id))

    plain = await compose_control_hierarchy(person, load_control=load)
    assert reads == {"child": 2, "middle": 2, "parent": 2}
    reads.clear()
    preloaded = await compose_control_hierarchy(person, load_control=load, preloaded={("owner", "child"): child})
    assert reads == {"child": 1, "middle": 2, "parent": 2}
    assert preloaded.effective_card == plain.effective_card


async def test_a_preloaded_card_that_changed_in_storage_is_refused():
    parent, middle, child, person = _chain()
    stored = _store(parent, middle, dataclasses.replace(child, card_revision=child.card_revision + 1))

    async def load(control_id, *, grantor_subject):
        return stored.get((grantor_subject, control_id))

    with pytest.raises(ControlCardMismatch, match="control_card_dependency_changed"):
        await compose_control_hierarchy(person, load_control=load, preloaded={("owner", "child"): child})


async def test_a_preload_for_another_coordinate_is_ignored():
    parent, middle, child, person = _chain()
    stored = _store(parent, middle, child)
    reads = collections.Counter()

    async def load(control_id, *, grantor_subject):
        reads[control_id] += 1
        return stored.get((grantor_subject, control_id))

    await compose_control_hierarchy(person, load_control=load, preloaded={("someone-else", "child"): child})
    assert reads["child"] == 2


async def test_the_dependency_re_reads_run_together():
    parent, middle, child, person = _chain()
    stored = _store(parent, middle, child)
    seen = collections.Counter()
    in_flight = 0
    peak = 0

    async def load(control_id, *, grantor_subject):
        nonlocal in_flight, peak
        seen[control_id] += 1
        if seen[control_id] == 2:  # the re-read
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
        return stored.get((grantor_subject, control_id))

    await compose_control_hierarchy(person, load_control=load)
    assert peak == 3


@pytest.mark.parametrize("first,second,reason", [
    ("unavailable", "changed", "control_card_lookup_unavailable"),
    ("changed", "unavailable", "control_card_dependency_changed"),
])
async def test_the_first_failing_dependency_in_chain_order_decides(first, second, reason):
    parent, middle, child, person = _chain()
    stored = _store(parent, middle, child)
    seen = collections.Counter()
    failures = {"child": first, "middle": second}

    async def load(control_id, *, grantor_subject):
        seen[control_id] += 1
        card = stored.get((grantor_subject, control_id))
        if seen[control_id] == 2 and control_id in failures:
            if failures[control_id] == "unavailable":
                raise RuntimeError("storage down")
            return dataclasses.replace(card, card_revision=card.card_revision + 1)
        return card

    with pytest.raises(ControlCardMismatch, match=reason):
        await compose_control_hierarchy(person, load_control=load)
