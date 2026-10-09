"""W606 pre-activation classification of a restored copy's Card handle rows: one synthetic row per class.

The predicate is the strict reader's own (``validate_migration_binding`` and
the active-unexpired filter); the read is one READ ONLY transaction that
selects no secret reference and no fingerprint.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace

import pytest

from connection_hub.delegated_credentials.cards.handle_classification import classify_restored_copy, classify_row
from connection_hub.delegated_credentials.cards.model import CARD_STATE_REVOKED
from test_card_credential_handles import CARD_KIND_AGENT, _authority

NOW = 1_900_000_000


def _card(access_id, revision=3, expires_at=NOW + 3600, **changes):
    return replace(_authority(card_kind=CARD_KIND_AGENT, revision=revision), access_id=access_id,
                   expires_at=expires_at, **changes)


def _row(access_id, card_revision=3, state="active", expires_at=NOW + 3600):
    return {"access_id": access_id, "card_revision": card_revision, "state": state, "expires_at": expires_at}


class _Connection:
    def __init__(self, rows):
        self.rows, self.queries, self.modes = rows, [], []

    @asynccontextmanager
    async def _transaction(self, readonly=False):
        self.modes.append(readonly)
        yield

    def transaction(self, readonly=False):
        return self._transaction(readonly=readonly)

    async def fetch(self, query):
        self.queries.append(query)
        return self.rows


CARDS = {
    "aut_readable": _card("aut_readable"),
    "aut_stale": _card("aut_stale", revision=5),
    "aut_retired": _card("aut_retired", state=CARD_STATE_REVOKED),
    "aut_ahead": _card("aut_ahead", revision=2),
    "aut_expiry": _card("aut_expiry", expires_at=NOW + 7200),
    "aut_inert_revoked": _card("aut_inert_revoked"),
    "aut_inert_expired": _card("aut_inert_expired"),
}
ROWS = [
    _row("aut_readable"),
    _row("aut_stale", card_revision=3),          # the Card moved to 5 with no W606 re-bind
    _row("aut_absent"),                          # no Card has this id
    _row("aut_retired"),
    _row("aut_ahead", card_revision=3),          # row AHEAD of the Card (2)
    _row("aut_expiry"),                          # same revision, different expiry
    _row("aut_bad", card_revision=0),            # does not validate
    _row("aut_unreadable"),                      # the Card store raises
    _row("aut_inert_revoked", state="revoked"),  # never served
    _row("aut_inert_expired", expires_at=NOW),   # never served
]


async def _find(access_id):
    if access_id == "aut_unreadable":
        raise RuntimeError("revision_content_hash_mismatch")
    return CARDS.get(access_id)


@pytest.mark.asyncio
async def test_each_class_is_counted_and_only_non_secret_identifiers_are_reported():
    connection = _Connection(ROWS)
    report = await classify_restored_copy(connection, schema="restored", find_card=_find, now=NOW)
    assert report["counts"] == {"readable": 1, "stale": 1, "card_absent": 1, "card_retired": 1, "malformed": 4,
                                "inert": 2}
    assert report["activation_allowed"] is False and report["rows"] == len(ROWS)
    by_id = {finding["access_id"]: finding for finding in report["findings"]}
    assert by_id["aut_stale"] == {"table": "connection_hub_card_handle_metadata", "access_id": "aut_stale",
                                  "class": "stale", "code": "card_handle_revision_mismatch",
                                  "row_card_revision": 3, "card_revision": 5}
    assert {key: by_id[key]["code"] for key in ("aut_absent", "aut_retired", "aut_ahead", "aut_expiry", "aut_bad",
                                                "aut_unreadable")} == {
        "aut_absent": "card_absent", "aut_retired": "card_revoked", "aut_ahead": "card_handle_revision_mismatch",
        "aut_expiry": "card_handle_expiry_mismatch", "aut_bad": "card_handle_row_invalid",
        "aut_unreadable": "card_unreadable"}
    assert by_id["aut_ahead"]["class"] == "malformed"  # ahead is not the pre-W606 stale case
    assert "aut_readable" not in by_id and "aut_inert_revoked" not in by_id
    assert all(set(finding) == {"table", "access_id", "class", "code", "row_card_revision", "card_revision"}
               for finding in report["findings"])
    assert report["out_of_scope"] and "OAuth grant and refresh" in report["out_of_scope"][0]


@pytest.mark.asyncio
async def test_the_read_is_one_read_only_transaction_that_selects_no_secret_column():
    connection = _Connection(ROWS)
    await classify_restored_copy(connection, schema="restored", find_card=_find, now=NOW)
    assert connection.modes == [True] and len(connection.queries) == 1
    query = connection.queries[0].lower()
    assert query.lstrip().startswith("select") and "restored.connection_hub_card_handle_metadata" in query
    assert "secret" not in query and "sha256" not in query and "session" not in query


@pytest.mark.asyncio
async def test_a_copy_with_only_readable_and_inert_rows_allows_activation():
    connection = _Connection([_row("aut_readable"), _row("aut_inert_revoked", state="revoked")])
    report = await classify_restored_copy(connection, schema="restored", find_card=_find, now=NOW)
    assert report["activation_allowed"] is True and report["findings"] == []
    assert report["counts"]["readable"] == 1 and report["counts"]["inert"] == 1


def test_the_strict_reader_predicate_decides_readable_exactly():
    card = _card("aut_x")
    assert classify_row(_row("aut_x"), card, now=NOW) == ("readable", "")
    assert classify_row(_row("aut_x", card_revision=2), card, now=NOW) == ("stale", "card_handle_revision_mismatch")
    assert classify_row(_row("aut_x", expires_at=NOW + 1), card, now=NOW)[0] == "malformed"
