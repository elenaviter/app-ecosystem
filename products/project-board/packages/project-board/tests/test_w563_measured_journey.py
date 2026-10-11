"""W722: one synthetic 350-message journey through pinned Project Board CLI reads.

These tests execute real CLI dispatch/render code with fixture-supplied field
results. They do not exercise a running relay, real PB custody, or adoption.
"""

from __future__ import annotations

import hashlib

from w563_journey_measure import (
    BASE_COMMIT,
    PROCEDURE,
    instruction_and_handoff,
    load_fixture,
    measure,
    synthetic_custody,
)


def test_one_sanitized_fixture_keeps_the_five_decision_cases_and_exact_tuple() -> None:
    fixture, sha = load_fixture()
    messages = fixture["messages"]
    by_ordinal = {row["ordinal"]: row for row in messages}

    assert len(messages) == 350
    assert fixture["code_commit"] == BASE_COMMIT
    assert fixture["procedure_revision"] == "2026.10.11.1"
    assert len(sha) == 64
    assert fixture["targeted_item_keys"] == ["W1003", "W1007", "W1011", "W1023", "W1027"]
    assert by_ordinal[3]["kind"] == "urgent"
    assert by_ordinal[7]["reviewer_availability"] == "unavailable"
    assert by_ordinal[11]["merge_state"] == "merged"
    assert by_ordinal[11]["deployment_state"] == "not_deployed"
    assert by_ordinal[3]["logical_event_ref"] == by_ordinal[23]["logical_event_ref"]
    assert by_ordinal[3]["logical_event_ref"] == by_ordinal[27]["logical_event_ref"]
    assert all(row["message_ref"].startswith("work:mail:synthetic-w563-") for row in messages)
    assert all(row["authority_ref"] and row["freshness_revision"] and row["next_actor"] for row in messages)


def test_same_fixture_traverses_real_cli_full_and_brief_paths_with_raw_samples() -> None:
    result = measure(samples=2)
    full, compact = result["full_read"], result["compact_read"]

    assert result["message_count"] == 350
    assert result["sample_count_per_path"] == 2
    assert len(full) == len(compact) == 2
    assert [run["dispatch_call_count"] for run in full] == [350, 350]
    assert [run["dispatch_call_count"] for run in compact] == [9, 9]
    assert [run["full_history_reads"] for run in full] == [350, 350]
    assert [run["full_history_reads"] for run in compact] == [0, 0]
    assert result["zero_full_history_happy_path"]
    assert result["payload_reduction_target_met"], (
        [run["bytes"] for run in full], [run["bytes"] for run in compact], result["payload_reduction"]
    )
    assert all(reduction >= .70 for reduction in result["payload_reduction"])
    assert result["targeted_evidence_visible"]
    assert result["targeted_case_refs_visible"]
    assert all(run["route_elapsed_ns"] > 0 for run in full + compact)
    assert all(call["elapsed_ns"] > 0 and call["bytes"] > 0 for run in full + compact for call in run["calls"])
    assert all(call["format"] == "brief" for run in compact for call in run["calls"])
    assert len({run["output_sha256"] for run in full}) == 1
    assert len({run["output_sha256"] for run in compact}) == 1
    assert result["boundary"].startswith("real CLI dispatch and render; fixture-supplied")


def test_duplicate_and_recovery_keep_one_synthetic_receipt_custody_and_settlement() -> None:
    fixture, _ = load_fixture()
    ledger = synthetic_custody(fixture)

    assert ledger["boundary"] == "simulated-ledger-only"
    assert ledger["event_count"] == 348
    assert ledger["duplicate_event_count"] == 1
    assert ledger["urgent_event_deliveries"] == 3
    assert ledger["all_once"]
    urgent = ledger["urgent_event"]
    assert urgent["receipt_count"] == urgent["custody_count"] == urgent["settlement_count"] == 1
    assert urgent["authority_ref"] == "work:control:synthetic-003"
    assert urgent["freshness_revision"] == 4
    assert urgent["next_actor"] == "coordinator"
    assert urgent["first_message_ref"] == "work:mail:synthetic-w563-003"


def test_selected_pinned_source_is_complete_reused_and_handoff_is_bounded() -> None:
    fixture, _ = load_fixture()
    result = instruction_and_handoff(fixture)

    assert result["boundary"] == "source-files-complete; reuse-cache-simulated; handoff-proposed"
    assert result["revision"] == fixture["procedure_revision"]
    assert [entry["ref"] for entry in result["selected"]] == fixture["selected_instruction_refs"]
    assert result["all_selected_bytes_loaded"]
    assert result["same_revision_reused"]
    assert result["file_reads"] == len(fixture["selected_instruction_refs"])
    for entry in result["selected"]:
        raw = (PROCEDURE / entry["ref"]).read_bytes()
        assert entry["bytes"] == len(raw)
        assert entry["sha256"] == hashlib.sha256(raw).hexdigest()
    assert result["handoff_words"] <= 1500
    assert "Unavailable reviewer" in result["handoff_text"]
    assert "Merged Source is not deployed Source" in result["handoff_text"]
    assert "P3/P4/P5" in result["handoff_text"]
