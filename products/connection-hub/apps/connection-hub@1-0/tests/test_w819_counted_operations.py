"""W819 for W812: each Hub operation on the measured paths is one counted scope.

The member poll and Card transactions (W819), and linking an agent to a project (operator, about 20:30Z,
2026-10-13: "i added that agent to project and it took jus ta while. performance is our big problem"),
whose GitHub-use grant reaches the Hub through one of the two add-operations endpoints and whose permission
check (like every board mutation and the poll's person-Card batches) asks one of the three authorize endpoints.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from kdcube_ai_app.apps.chat.sdk.runtime.dynamic_module_loader import load_dynamic_module_for_path

COUNTED = (
    "card_transaction_participant", "card_census_read", "card_read_collection_register", "card_lifecycle_plan",
    "card_version", "project_agent_card_add_operations", "delegated_access_add_operations",
    "project_operation_authorize", "project_operations_authorize", "project_operation_authorize_for_person",
)


def _entrypoint_class():
    module = load_dynamic_module_for_path(Path(__file__).resolve().parents[1] / "entrypoint.py")[1]
    return module.ConnectionHubEntrypoint


@pytest.mark.parametrize("operation", COUNTED)
def test_the_operation_runs_inside_its_own_counted_scope(operation):
    method = getattr(_entrypoint_class(), operation)
    assert inspect.unwrap(method) is not method
    assert f"@request_counters.counted(\"{operation}\")" in inspect.getsource(method)
    assert inspect.iscoroutinefunction(method)
