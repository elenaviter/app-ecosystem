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
    # The team section only (owner, 18:28Z): the coordinator holder line names
    # one member again outside it, which is not a duplicate team row.
    start = next((n for n, line in enumerate(lines) if line.startswith("team:")), None)
    assert start is not None, "no team section"
    end = next((n for n, line in enumerate(lines[start + 1:], start + 1) if line.startswith("team detail")), len(lines))
    section = lines[start:end]
    for member in team:
        mentions = [line for line in section if member["worker_name"] in line]
        assert len(mentions) == 1, f"team member on {len(mentions)} lines of the team section"
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


# 3b. Context byte target (owner, 16:28Z): context <= 40% of the baseline.

def test_worker_context_brief_meets_its_byte_target():
    _, text = _brief("worker_context")
    assert _bytes(text) <= 0.40 * BASELINE_BYTES["worker_context"], _bytes(text)


# 5. Receive (owner's S5): admitted operator mail in a project mailbox behind a
# direct-mailbox backlog arrives in the first receive batch. Was a strict xfail
# pending Q1; the owner's packet 2 (88b54bae) passes it, so the mark is dropped.

PROJECT_ID = "w563-project"
WORKER = "claude-main"
SENDER = "codex-app"


@pytest.fixture
def mail_field(tmp_path):
    from project_board.client.store import SharedFieldStore

    store = SharedFieldStore(tmp_path / "field")
    store.initialize(field_id="w563-receive")
    for name in (WORKER, SENDER):
        store.register_worker(worker_name=name, runtime_kind="codex", capabilities=[],
                              authority_label=f"authority:{name}")
    store.create_project(project_id=PROJECT_ID, title="W563", goal="Bounded receive.", owner="operator")
    store.sync_worker_attendances(WORKER, [f"work:project:{PROJECT_ID}"])
    store.listen_worker(WORKER)
    return store


def _control(ref: str, *, kind: str, body: str, project: bool, operator: bool) -> dict:
    from project_board.client.io import content_hash

    payload = {"body": body}
    control = {"ref": ref, "recipient": WORKER, "kind": kind, "subject": body[:40],
               "payload": payload, "payload_hash": content_hash(payload)}
    if project:
        control["project_ref"] = f"work:project:{PROJECT_ID}"
    if operator:
        control["sender_identity"] = {"kind": "user", "label": "Operator"}
    return control


def test_operator_mail_in_a_project_is_received_ahead_of_a_direct_backlog(mail_field):
    from project_board.client.session import pull_worker_input

    for number in range(6):
        mail_field.materialize_control(_control(
            f"work:control:20261005T160000Z:command_backlog{number}:direct-ping-{number}",
            kind="ping", body=f"Backlog ping {number}", project=False, operator=False,
        ))
    urgent = mail_field.materialize_control(_control(
        "work:control:20261005T162800Z:command_urgent:operator-request",
        kind="request", body="Urgent operator decision needed now.", project=True, operator=True,
    ))
    first = pull_worker_input(mail_field, worker_name=WORKER, limit=5)
    refs = [item["message"]["message_ref"] for item in first["items"]]
    assert urgent["message_ref"] in refs, "the urgent operator request waits behind the direct backlog"


# 6. Procedure revision (owner's S8): verify reports which package files changed
# since the previous install; an unchanged revision reports none.

def _stand_in_package(tmp_path, revision: str, *, edit: str = ""):
    import shutil

    from project_board.client import procedures

    root = tmp_path / f"package-{revision}"
    shutil.copytree(procedures.source_package_path(), root)
    manifest = root / "package.json"
    definition = json.loads(manifest.read_text(encoding="utf-8"))
    definition["revision"] = revision
    manifest.write_text(json.dumps(definition, indent=2) + "\n", encoding="utf-8")
    if edit:
        target = root / edit
        target.write_text(target.read_text(encoding="utf-8") + "\nW563 fixture edit.\n", encoding="utf-8")
    return root


def test_procedure_verify_names_no_changed_files_for_an_unchanged_revision(tmp_path):
    from project_board.client import procedures

    home = tmp_path / "home"
    procedures.install_agent_procedure(["claude-code"], home=home)
    verified = procedures.verify_agent_procedure(["claude-code"], home=home)[0]
    assert verified["state"] == "current"
    assert verified.get("changed_files") == [], "verify does not report changed_files"


def test_procedure_verify_names_exactly_the_edited_reference_for_a_newer_revision(tmp_path, monkeypatch):
    from project_board.client import procedures

    home = tmp_path / "home"
    procedures.install_agent_procedure(["claude-code"], home=home)
    newer = _stand_in_package(tmp_path, "2099.01.01.1", edit="references/brief-output.md")
    monkeypatch.setattr(procedures, "source_package_path", lambda: newer)
    procedures.install_agent_procedure(["claude-code"], home=home)
    verified = procedures.verify_agent_procedure(["claude-code"], home=home)[0]
    assert verified["installed_revision"] == "2099.01.01.1"
    assert verified.get("changed_files") == ["references/brief-output.md"]


# 7. Merged-not-live: no status surface exists yet (owner, 16:28Z; asked of Root under S10).

@pytest.mark.skip(reason="W563: merged-not-live has no status surface yet; owner asks Root under S10")
def test_merged_not_live_is_distinguished_from_live_in_the_status_view():
    pass
