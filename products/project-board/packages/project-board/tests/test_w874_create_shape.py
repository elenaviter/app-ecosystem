"""Creating an item must not iterate a scalar as a list of characters."""

from __future__ import annotations

import argparse
import copy
import json

import pytest

from project_board.client import cli
from project_board.client.coordinate_queue import CoordinateQueue
from project_board.contract.errors import DomainError
from project_board.contract.operation_shapes import operation_call_problems, operation_contract


PROJECT = "work:project:example"
LIST_FIELDS = ("acceptance", "tags", "keywords", "depends_on", "attachment_refs")


@pytest.mark.parametrize("field", LIST_FIELDS)
@pytest.mark.parametrize("value", ["A criterion", None, 12, True, {}, ["ok", 12]])
def test_create_refuses_bad_list_fields_before_any_relay_call(monkeypatch, tmp_path, field, value):
    def forbidden(*args, **kwargs):
        pytest.fail("A malformed create must never enter the relay queue")

    monkeypatch.setattr(CoordinateQueue, "submit", forbidden)
    args = argparse.Namespace(
        action="plan.item.create", object_ref=PROJECT,
        payload_json=json.dumps({"item": {"title": "Example", field: value}, "idempotency_key": "create"}),
        payload_file="", contract=False, route="relay", timeout_seconds=1.0,
        runtime_kind="", runtime_session_id="", config=str(tmp_path / "no-relay.json"),
    )
    with pytest.raises(DomainError) as refused:
        cli._coordinate_command(args)
    assert refused.value.code == "work_coordinate_shape_invalid"
    assert {p["field"] for p in refused.value.details["problems"]} == {f"item.{field}"}
    assert "Nothing was sent." in str(refused.value)


@pytest.mark.parametrize("field", ["title", "description", "status"])
def test_create_refuses_non_string_scalar_fields(field):
    problems = operation_call_problems(
        "plan.item.create", PROJECT,
        {"item": {"title": "Example", field: ["not a string"]}, "idempotency_key": "create"},
    )
    assert any(p["field"] == f"item.{field}" and p["problem"] == "type" for p in problems)


@pytest.mark.parametrize("item", ["Example", ["Example"], 12])
def test_create_requires_an_item_object(item):
    problems = operation_call_problems(
        "plan.item.create", PROJECT, {"item": item, "idempotency_key": "create"},
    )
    assert any(p["field"] == "item" and p["problem"] == "type" for p in problems)


def test_create_valid_lists_keep_order_duplicates_and_unicode_without_mutation():
    payload = {"item": {
        "title": "Example", "description": "Description", "status": "todo",
        "acceptance": ["Second", "First", "Second", "条件"],
        "tags": [], "keywords": ["example"],
        "depends_on": ["work:plan:node:20261015T000000Z:w1:example"],
        "attachment_refs": [],
    }, "idempotency_key": "create"}
    before = copy.deepcopy(payload)
    assert operation_call_problems("plan.item.create", PROJECT, payload) == []
    assert payload == before


def test_create_omitted_optional_fields_still_work():
    assert operation_call_problems(
        "plan.item.create", PROJECT, {"item": {"title": "Example"}, "idempotency_key": "create"},
    ) == []


def test_create_contract_lists_field_types_and_its_example_has_real_lists():
    contract = operation_contract("plan.item.create")
    fields = contract["payload"]["item"]
    assert fields["title"] == "string"
    for field in LIST_FIELDS:
        assert fields[field].startswith("list of")
    example = json.loads(contract["example"].split("--payload-json '", 1)[1][:-1])
    assert isinstance(example["item"], dict)
    assert all(isinstance(example["item"][field], list) for field in LIST_FIELDS)
    assert operation_call_problems("plan.item.create", PROJECT, example) == []


def test_create_still_refuses_a_caller_allocated_key():
    problems = operation_call_problems(
        "plan.item.create", PROJECT,
        {"item": {"title": "Example", "item_key": "W4"}, "idempotency_key": "create"},
    )
    assert any(p["field"] == "item.item_key" and p["problem"] == "allocated_by_board" for p in problems)
