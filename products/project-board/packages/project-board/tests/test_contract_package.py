from __future__ import annotations

import importlib

import pytest


CONTRACT_MODULES = (
    "delivery_failures",
    "errors",
    "mail_addresses",
    "mailbox_reconciliation_contract",
    "mailbox_reconciliation_publication",
    "operation_outcomes",
    "operator_mail_contract",
    "plan_assignment_outcomes",
    "plan_host",
    "plan_index",
    "plan_nodes",
    "portable_refs",
    "project_report_contract",
    "reference_artifacts",
    "reference_records",
    "refs",
    "scoped_collection",
    "work_lifecycle",
    "worker_identity",
    "worker_operation_contract",
    "worker_stream",
)


@pytest.mark.parametrize("module_name", CONTRACT_MODULES)
def test_contract_module_imports_from_distribution(module_name: str) -> None:
    module = importlib.import_module(f"project_board.contract.{module_name}")

    assert module.__name__ == f"project_board.contract.{module_name}"


def test_versioned_plan_reference_round_trips() -> None:
    from project_board.contract.plan_nodes import (
        make_plan_node_ref,
        parse_plan_node_ref,
    )

    version = f"20260922T190100Z-{'a' * 64}"
    ref = make_plan_node_ref(
        timestamp="20260922T190000Z",
        key="W255",
        semantic_name="shared-contract",
        version=version,
    )

    parsed = parse_plan_node_ref(ref)
    assert parsed.key == "w255"
    assert parsed.semantic_name == "shared-contract"
    assert parsed.version == version
