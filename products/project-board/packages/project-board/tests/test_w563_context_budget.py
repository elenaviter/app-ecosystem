"""W563: coordinator context budget for the status/dispatch path (independent fixtures).

Each scenario renders a sanitized real-shaped envelope through the public
``render_envelope`` and measures the brief output in UTF-8 bytes. The
fixtures in ``fixtures/w563`` keep the structure and byte lengths of real
results with every value replaced by synthetic text (enum fields and
timestamps kept), so a byte ratio here is a ratio on realistic payloads.

``BASELINE_BYTES`` are the brief bytes measured at app-ecosystem main
``1df4600c`` (the "before"). The W563 targets are the owner's (claude-e-main,
2026-10-05 16:20Z): a >=70% reduction of returned bytes for the status and
dispatch scenario, one line per item or team member, and named
decision-critical fields that must survive the reduction.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_board.client.render import render_envelope

FIXTURES = Path(__file__).parent / "fixtures" / "w563"

BASELINE_BYTES = {
    "plan_index_working_7": 17_059,
    "plan_item_heavy_100n_30a": 7_644,
    "worker_context": 17_984,
}
REDUCTION = 0.70


def _load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _brief(name: str) -> tuple[dict, str]:
    envelope = _load(name)
    return envelope, render_envelope(envelope)


def _bytes(text: str) -> int:
    return len(text.encode("utf-8"))


FIXTURE_FILE_BYTES = {
    "plan_index_working_7": 16_443,
    "plan_item_heavy_100n_30a": 79_742,
    "worker_context": 36_918,
}


def test_the_fixtures_are_the_ones_the_baseline_was_measured_on():
    """A renderer change moves the brief bytes; the fixture files must not move.

    At main 1df4600c these files rendered exactly BASELINE_BYTES (17,059 B for
    the index matches the owner's live 7-item measurement).
    """

    for name, size in FIXTURE_FILE_BYTES.items():
        assert (FIXTURES / f"{name}.json").stat().st_size == size, name
        assert render_envelope(_load(name)).startswith("OK"), name


# 1. project.plan.index: one line per item, no hashes/embeddings/transitions.

def test_plan_index_brief_is_one_line_per_item_without_bulk_fields():
    envelope, text = _brief("plan_index_working_7")
    items = envelope["result"]["object"]["items"]
    lines = text.splitlines()
    for item in items:
        mentions = [line for line in lines if item["item_key"] in line.split()]
        assert len(mentions) == 1, f"{item['item_key']} on {len(mentions)} lines"
        assert item["status"] in mentions[0]
    for bulk in ("search_content_hash", "source_content_hash", "available_transitions",
                 "embedding_model_id", "embedding_present", "keywords", "version_slug"):
        assert bulk not in text, f"{bulk} still rendered"
    assert any("next" in line or "cursor" in line or "page" in line for line in lines), "no paging line"


def test_plan_index_brief_meets_the_byte_target():
    _, text = _brief("plan_index_working_7")
    assert _bytes(text) <= (1 - REDUCTION) * BASELINE_BYTES["plan_index_working_7"], _bytes(text)


# 2. project.plan.item, heavy item: counts and the read command, not file lists.

def test_heavy_item_brief_keeps_decision_fields_and_drops_attachment_lists():
    envelope, text = _brief("plan_item_heavy_100n_30a")
    item = envelope["result"]["object"]
    assignment = item["assignment"]
    for field, value in (
        ("item_key", item["item_key"]), ("status", item["status"]),
        ("identity_ref", item["identity_ref"]), ("item_ref", item["item_ref"]),
        ("assignee", item["assignee"]), ("assignment_ref", assignment["assignment_ref"]),
    ):
        assert value in text, f"decision-critical {field} missing"
    assert str(item["revision"]) in text and str(assignment["ownership_version"]) in text
    assert "attachment.file_ref" not in text, "attachment refs listed one by one"
    lines = text.splitlines()
    assert any("attachment" in line and "30" in line for line in lines), "attachment count not shown"
    assert any("note" in line and "100" in line for line in lines), "note count not shown"
    assert "item-attachment-read" in text, "no command to read an attachment"


def test_heavy_item_brief_does_not_repeat_title_or_item_refs():
    envelope, text = _brief("plan_item_heavy_100n_30a")
    item = envelope["result"]["object"]
    summary = next((line for line in text.splitlines() if line.startswith("summary:")), "")
    assert not summary.removeprefix("summary:").strip().startswith(item["title"][:40]), "summary repeats the title"
    assert text.count(item["identity_ref"]) == 1, "identity_ref repeated in the assignment block"


# 3. pb worker context: one line per team member, required context kept.

def test_worker_context_team_is_one_line_per_member():
    envelope, text = _brief("worker_context")
    team = envelope["result"]["team"]
    lines = text.splitlines()
    for member in team:
        mentions = [line for line in lines if member["worker_name"] in line]
        assert len(mentions) == 1, f"team member on {len(mentions)} lines"
    result = envelope["result"]
    assert result["workspace"] in text
    assert result["commit_identity"]["name"] in text and result["commit_identity"]["email"] in text
    holder = (result.get("coordinator") or {}).get("holder") or {}
    assert holder.get("worker_name", "") in text


# 4. The combined status/dispatch scenario: >=70% fewer returned bytes.

def test_status_and_dispatch_scenario_meets_the_combined_byte_target():
    before = sum(BASELINE_BYTES.values())
    after = sum(_bytes(_brief(name)[1]) for name in BASELINE_BYTES)
    assert after <= (1 - REDUCTION) * before, f"before {before} B, after {after} B, ratio {after / before:.2f}"


# 5-7. Surfaces the owner names next (receive selection, procedure revision,
# merged-not-live). Explicit skips, not silent passes.

@pytest.mark.skip(reason="W563: receive selection surface (mail_delivery.py) to be named by the owner")
def test_an_urgent_current_message_is_delivered_ahead_of_a_350_message_backlog():
    pass


@pytest.mark.skip(reason="W563: bind to `pb procedure verify` installed_revision/source_revision once the owner's change lands")
def test_an_unchanged_procedure_revision_is_not_reread_and_a_newer_one_is_read_once():
    pass


@pytest.mark.skip(reason="W563: merged-not-live status field to be confirmed by the owner")
def test_merged_not_live_is_distinguished_from_live_in_the_status_view():
    pass
