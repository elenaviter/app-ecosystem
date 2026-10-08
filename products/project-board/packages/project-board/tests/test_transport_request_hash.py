"""The shared transport request hash is pinned byte for byte (W574, Ops condition 4).

The client ledger and the board's transport ledger must compute the same value
for the same request. The vectors were produced by an independent
implementation (hand-built canonical JSON) and include non-ASCII text, emoji,
escapes, nested keys and numbers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_board.client.coordinate_recovery import coordinate_request_hash
from project_board.contract.operation_identity import TRANSPORT_REQUEST_HASH_SCHEMA, transport_request_hash
from project_board.contract.operation_shapes import operation_contract
from project_board.contract.worker_operation_contract import PROBLEM_BOARD_OPERATIONS

VECTORS = json.loads((Path(__file__).parent / "fixtures" / "transport_request_hash_vectors.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("vector", VECTORS["vectors"], ids=[vector["name"] for vector in VECTORS["vectors"]])
def test_the_hash_matches_the_independent_vectors(vector):
    assert VECTORS["schema"] == TRANSPORT_REQUEST_HASH_SCHEMA
    assert transport_request_hash(vector["action"], vector["object_ref"], vector["payload"]) == vector["sha256"]
    assert coordinate_request_hash(vector["action"], vector["object_ref"], vector["payload"]) == vector["sha256"]


def test_key_order_does_not_change_the_hash_and_content_does():
    first = transport_request_hash("work.status.set", "work:project:one", {"a": 1, "b": "ü"})
    assert first == transport_request_hash("work.status.set", "work:project:one", {"b": "ü", "a": 1})
    assert first != transport_request_hash("work.status.set", "work:project:one", {"a": 1, "b": "u"})


def test_the_receipt_read_is_a_published_operation():
    assert "operation.receipt.get" in PROBLEM_BOARD_OPERATIONS
    contract = operation_contract("operation.receipt.get")
    assert set(contract["payload"]) == {"operation", "idempotency_key", "request_hash"}


def test_the_declared_reads_are_contract_operations_named_as_reads():
    from project_board.contract.worker_operation_contract import (
        PROBLEM_BOARD_OPERATIONS, READ_OPERATIONS, operation_is_read,
    )

    assert READ_OPERATIONS <= PROBLEM_BOARD_OPERATIONS
    read_suffixes = (".get", ".list", ".read", ".index", ".item", ".search", ".history", ".resolve", ".embedding_status")
    assert all(operation.endswith(read_suffixes) for operation in READ_OPERATIONS)
    # In the board's mutation table (W502), so never a declared read.
    assert not operation_is_read("project.references.preview")
    assert not operation_is_read("project.control.update")
    assert not operation_is_read("")
