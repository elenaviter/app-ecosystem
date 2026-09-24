# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from connection_hub.bundle_operations import normalize_bundle_operation_result


def test_normalize_bundle_operation_result_keeps_direct_payload() -> None:
    payload = {"ok": True, "membership": {"role": "owner"}}

    assert normalize_bundle_operation_result("membership_resolve", payload) == payload


def test_normalize_bundle_operation_result_reads_operation_alias() -> None:
    payload = {"ok": True, "membership": {"role": "owner"}}

    assert normalize_bundle_operation_result(
        "membership_resolve",
        {"membership_resolve": payload},
    ) == payload


def test_normalize_bundle_operation_result_reads_status_result() -> None:
    payload = {"ok": False, "error": {"code": "membership_required"}}

    assert normalize_bundle_operation_result(
        "membership_resolve",
        {"status": "ok", "result": payload},
    ) == payload


def test_normalize_bundle_operation_result_preserves_unknown_shape() -> None:
    response = {
        "status": "refused",
        "result": {"ok": True},
        "error": {"code": "operation_refused"},
    }

    assert normalize_bundle_operation_result("membership_resolve", response) == response
