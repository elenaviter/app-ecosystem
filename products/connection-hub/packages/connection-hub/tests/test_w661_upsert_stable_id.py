# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter
"""W661 (Mint, acceptance 7): an invitation writes the Card new on the person's stable id, through the v6.2
card_version path (STAGE with base_version null, then PUBLISH), listing no directory and reading no history.

Operator, 10 Oct: "IS new card. even if something existed fine. we simply now UPSERT. overwrite"; the card id
"NEVER changes. reagrldess of how many times that ideneity was kicked of or invited back"; "never nothing is
being scanned". EMain 17:01Z: the v6.2 path is the one that counts; a refusal there is a piece 1 defect.
Independent of Ops' store tests: the removal here is the real revoke (a tombstone), and every listing fails.
"""

from __future__ import annotations

import os
import pathlib
from dataclasses import replace

import pytest

from connection_hub.delegated_credentials.cards import transaction_store as tx
from test_card_transaction_store import SUBJECT_HASH, _setup
from test_w661_card_versions import WHEN

TXN = "w661-mint-upsert-" + "c" * 32


def _no_listing(monkeypatch, root):
    """Any directory listing under the Card store fails the save, as does a call of the old history probe."""
    root = str(root)
    real = (os.listdir, os.scandir, pathlib.Path.iterdir)

    def guard(path):
        text = os.fspath(path) if path is not None else os.getcwd()
        if text.startswith(root):
            raise AssertionError(f"the save listed a Card store directory: {os.path.relpath(text, root)}")

    monkeypatch.setattr(os, "listdir", lambda path=".": (guard(path), real[0](path))[1])
    monkeypatch.setattr(os, "scandir", lambda path=".": (guard(path), real[1](path))[1])
    monkeypatch.setattr(pathlib.Path, "iterdir", lambda self: (guard(self), real[2](self))[1])

    async def history_probe(*args, **kwargs):
        raise AssertionError("the save read the Card's history to decide (no history check under W661)")

    monkeypatch.setattr(tx, "_slot_has_history", history_probe, raising=False)


async def _invite(service, monkeypatch, root, card):
    """STAGE (base_version null: a new Card) and PUBLISH on the Card's stable id, with every listing failing."""
    with monkeypatch.context() as guarded:
        _no_listing(guarded, root)
        await service.stage_card_version(txn=TXN, request_digest="d" * 64, catalog="catalog-1",
                                         members=[(SUBJECT_HASH, card.access_id, None, card)], now=WHEN)
        return await service.publish_card_version(txn=TXN)


async def _current(store, card):
    found = await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=card.access_id)
    return None if found is None else found[1]


@pytest.mark.asyncio
async def test_an_invitation_over_a_current_card_overwrites_it_on_the_same_id(tmp_path, monkeypatch):
    store, service, before, _ = await _setup(tmp_path)
    assert await _current(store, before) == before, "the stable id already holds an active Card"
    invited = replace(before, card_revision=before.card_revision + 1, label="invited: the Card written new")
    links = await _invite(service, monkeypatch, tmp_path, invited)
    current = await _current(store, before)
    assert current is not None and current.access_id == before.access_id, "the same stable id"
    assert current.label == invited.label, "the new Card replaced what existed (upsert)"
    assert [link["access_id"] for link in links] == [before.access_id]
    assert set(links[0]) <= {"subject_hash", "access_id", "version", "checksum"}, "links only"


@pytest.mark.asyncio
async def test_invite_remove_invite_writes_a_new_card_on_the_same_id(tmp_path, monkeypatch):
    store, service, before, _ = await _setup(tmp_path)
    await service.revoke(subject_hash=SUBJECT_HASH, access_id=before.access_id,
                         expected_revision=before.card_revision)  # the person is removed: a tombstone
    # The next version follows the one current pointer (the tombstone); nothing else is read.
    removed = await store.read_current_authority(subject_hash=SUBJECT_HASH, access_id=before.access_id)
    invited = replace(before, card_revision=removed[0].card_revision + 1, label="invited again")
    await _invite(service, monkeypatch, tmp_path, invited)
    current = await _current(store, before)
    assert current is not None and current.access_id == before.access_id and current.label == invited.label
    assert current.state == before.state, "the invited Card is active again"
