"""W1031 slice 1: running-entry identity, record format, append-only rule, published history.

Design GO at W1031 revision 23 (pb-knowledge): canonical identity through the shared
``make_ref`` (R6), stable per-record metadata (R4), published first-parent content
versions with trailer-only intent and stated completeness (R3), and admission against
the last accepted generation (R2).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from project_board.client.journal_history import published_chain, running_generations
from project_board.client.journal_running import (
    check_append_only,
    format_record,
    normalize_feature,
    parse_records,
    record_month,
    running_entry_key,
    running_entry_path,
    running_entry_ref,
    running_entry_semantic_name,
)
from project_board.contract.errors import DomainError
from project_board.contract.refs import parse_ref

PROJECT = "work:project:example-project-abc"
OTHER_PROJECT = "work:project:other-abc"
PATH = "journal/features/journal-updates/2026-11.md"


# --- identity (R6) -----------------------------------------------------------------

def test_running_entry_ref_uses_the_shared_grammar_and_is_deterministic():
    ref = running_entry_ref(project_ref=PROJECT, feature="journal-updates", month="2026-11")
    assert ref.startswith("work:journal:20261101T000000Z:")
    assert ref.endswith(":journal-updates-2026-11")
    parse_ref(ref)
    assert ref == running_entry_ref(project_ref=PROJECT, feature="journal-updates", month="2026-11")


def test_running_entry_key_separates_project_feature_and_month():
    base = running_entry_key(project_ref=PROJECT, feature="journal-updates", month="2026-11")
    assert len(base) == 16 and int(base, 16) >= 0
    assert base != running_entry_key(project_ref=OTHER_PROJECT, feature="journal-updates", month="2026-11")
    assert base != running_entry_key(project_ref=PROJECT, feature="journal-search", month="2026-11")
    assert base != running_entry_key(project_ref=PROJECT, feature="journal-updates", month="2026-12")


def test_semantic_name_keeps_the_month_within_64_characters():
    long_feature = "a-very-long-feature-name-that-keeps-going/and-a-second-segment-too/third-x"
    name = running_entry_semantic_name(feature=long_feature, month="2026-11")
    assert len(name) <= 64 and name.endswith("-2026-11")
    parse_ref(running_entry_ref(project_ref=PROJECT, feature=long_feature, month="2026-11"))


def test_feature_and_path_validation():
    assert normalize_feature("/board/journal/") == "board/journal"
    assert running_entry_path(feature="board/journal", month="2026-11") == "journal/features/board/journal/2026-11.md"
    for bad in ("", "../x", "Board", "a/b/c/d", "a//b", "a b"):
        with pytest.raises(DomainError) as caught:
            normalize_feature(bad)
        assert caught.value.code == "journal_feature_invalid"
    with pytest.raises(DomainError):
        running_entry_ref(project_ref=PROJECT, feature="x", month="2026-13")


def test_record_month_is_the_utc_month_of_the_original_time():
    assert record_month("2026-11-30T23:59:59Z") == "2026-11"
    assert record_month("2026-12-01T00:00:10Z") == "2026-12"
    assert record_month("2026-11-30T23:30:00-01:00") == "2026-12"
    with pytest.raises(DomainError) as caught:
        record_month("2026-11-30T23:30:00")
    assert caught.value.code == "journal_recorded_at_invalid"


# --- records (R4) ------------------------------------------------------------------

def _record(worker="claude-code-a", work_ref="work:plan:node:w1", at="2026-11-25T19:00:00Z", body="Tried X.",
            corrects=None):
    return format_record(worker=worker, alias="alias-a", work_ref=work_ref, recorded_at=at, title="t",
                         body=body, corrects=corrects)


def test_records_round_trip_with_per_record_author_item_and_time():
    text = "---\nentry_ref: x\nkind: running\n---\n\n" + _record() + "\n" + _record(
        worker="codex-b", work_ref="work:plan:node:w2", at="2026-11-26T08:00:00Z", body="Did Y.")
    records = parse_records(text)
    assert [(r.worker, r.work_ref, r.recorded_at) for r in records] == [
        ("claude-code-a", "work:plan:node:w1", "2026-11-25T19:00:00Z"),
        ("codex-b", "work:plan:node:w2", "2026-11-26T08:00:00Z"),
    ]
    assert all(r.id.startswith("rec:2026112") for r in records)
    assert records[0].id == parse_records(_record())[0].id  # the same record has the same id


def test_malformed_and_duplicate_records_are_refused():
    with pytest.raises(DomainError) as caught:
        parse_records('<!-- pb-record {"id": "x"} -->\n## h\n')
    assert caught.value.code == "journal_record_malformed"
    one = _record()
    with pytest.raises(DomainError) as caught:
        parse_records(one + one)
    assert caught.value.code == "journal_record_duplicate"
    changed = one.replace("Tried X.", "Tried Z.")
    with pytest.raises(DomainError) as caught:
        parse_records(one + changed)
    assert caught.value.code == "journal_record_conflict"


def test_append_only_accepts_extension_and_refuses_rewrite_removal_and_dangling_correction():
    first = parse_records(_record())
    extended = parse_records(_record() + _record(at="2026-11-26T08:00:00Z", body="Then Y."))
    assert check_append_only(first, extended).accepted
    rewritten = parse_records(_record(body="Tried X, edited.") + _record(at="2026-11-26T08:00:00Z"))
    verdict = check_append_only(first, rewritten)
    assert not verdict.accepted and verdict.reason == "journal_running_entry_rewritten"
    assert not check_append_only(extended, first).accepted
    dangling = parse_records(_record() + _record(at="2026-11-26T08:00:00Z", corrects="rec:nope:000000000000"))
    assert check_append_only(first, dangling).reason == "journal_record_correction_target_missing"
    correction = parse_records(_record() + _record(at="2026-11-26T08:00:00Z", body="X was wrong.",
                                                   corrects=first[0].id))
    assert check_append_only(first, correction).accepted


# --- published history (R3) ----------------------------------------------------------

def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), "-c", "user.name=example-worker",
                             "-c", "user.email=t@example.invalid", *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _entry(body: str, entry_ref: str = "work:journal:20261101T000000Z:0123456789abcdef:journal-updates-2026-11"):
    return f"---\nentry_ref: {entry_ref}\nkind: running\n---\n\n{body}"


def _write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "journal-home"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "commit", "-q", "--allow-empty", "-m", "root")
    return path


def _commit(repo: Path, text: str, message: str, path: str = PATH) -> str:
    _write(repo, path, text)
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def test_branch_commits_are_not_versions_and_legacy_changes_stay_unknown(repo: Path):
    a = _commit(repo, _entry(_record()), "first")
    _git(repo, "switch", "-q", "-c", "work/x")
    _commit(repo, _entry(_record() + _record(at="2026-11-26T08:00:00Z")), "wip 1")
    _commit(repo, _entry(_record() + _record(at="2026-11-26T09:00:00Z")), "wip 2")
    _git(repo, "switch", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "work/x", "-m", "merge x")
    merge = _git(repo, "rev-parse", "HEAD")
    chain = published_chain(repo, PATH, ref="main")
    assert [v.commit for v in chain.versions] == [a, merge]
    assert [v.intent for v in chain.versions] == ["first", "unknown"]
    assert chain.versions[1].worker == "unknown"
    assert chain.complete


def test_trailers_in_the_final_paragraph_mark_a_deliberate_update(repo: Path):
    ref = "work:journal:20261101T000000Z:0123456789abcdef:journal-updates-2026-11"
    _commit(repo, _entry(_record()), "first")
    _commit(repo, _entry(_record() + _record(at="2026-11-26T08:00:00Z")),
            f"append\n\nJournal-Update: {ref}\nJournal-Worker: claude-code-example-worker")
    _commit(repo, _entry(_record() + _record(at="2026-11-26T08:00:00Z") + _record(at="2026-11-27T08:00:00Z")),
            "append without trailers")
    chain = published_chain(repo, PATH, ref="main")
    assert [(v.intent, v.worker) for v in chain.versions] == [
        ("first", "unknown"), ("update", "claude-code-example-worker"), ("unknown", "unknown")]


def test_revert_is_a_new_version_and_paging_reaches_every_version(repo: Path):
    commits = [_commit(repo, _entry(_record(at=f"2026-11-{day:02d}T08:00:00Z")), f"v{day}") for day in range(1, 6)]
    _git(repo, "revert", "--no-edit", "HEAD")
    chain = published_chain(repo, PATH, ref="main")
    assert len(chain.versions) == 6 and chain.versions[-1].content_hash == chain.versions[-3].content_hash
    first = chain.page(limit=4)
    second = chain.page(after=first["next_cursor"], limit=4)
    assert [v["commit"] for v in first["versions"]] == commits[:4]
    assert len(second["versions"]) == 2 and second["next_cursor"] == ""


def test_rename_is_followed_only_with_the_same_entry_ref(repo: Path):
    a = _commit(repo, _entry(_record()), "first", path="journal/old-name.md")
    (repo / PATH).parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "mv", "journal/old-name.md", PATH)
    _git(repo, "commit", "-q", "-m", "move")
    chain = published_chain(repo, PATH, ref="main")
    assert chain.versions[0].commit == a and chain.versions[0].path == "journal/old-name.md"
    assert len(chain.versions) == 2 and not chain.issues


def test_rename_with_a_different_entry_ref_starts_a_new_chain(repo: Path):
    _commit(repo, _entry(_record(), entry_ref="work:journal:20261001T000000Z:fedcba9876543210:other-2026-10"),
            "first", path="journal/old-name.md")
    (repo / PATH).parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "mv", "journal/old-name.md", PATH)
    _write(repo, PATH, _entry(_record()))
    _git(repo, "add", PATH)
    _git(repo, "commit", "-q", "-m", "move and re-identify")
    chain = published_chain(repo, PATH, ref="main")
    assert len(chain.versions) == 1 and chain.versions[0].path == PATH


def test_shallow_history_is_reported_incomplete(repo: Path, tmp_path: Path):
    for day in range(1, 4):
        _commit(repo, _entry(_record(at=f"2026-11-{day:02d}T08:00:00Z")), f"v{day}")
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{repo}", str(shallow)], check=True)
    chain = published_chain(shallow, PATH, ref="HEAD")
    assert not chain.complete and chain.reason == "shallow_clone"


# --- admission against the last accepted generation (R2) -------------------------------

def test_a_valid_b_rewrites_c_restores_and_appends(repo: Path):
    a_text = _entry(_record())
    a = _commit(repo, a_text, "A")
    b = _commit(repo, _entry(_record(body="Tried X, rewritten.")), "B rewrites the record")
    at_b = running_generations(published_chain(repo, PATH, ref="main").versions)
    assert at_b.served.commit == a and at_b.observed.commit == b
    assert at_b.invalid == ({"commit": b, "reason": "journal_running_entry_rewritten",
                             "record_id": parse_records(a_text)[0].id},)
    c = _commit(repo, _entry(_record() + _record(at="2026-11-26T08:00:00Z", body="Correction.")), "C")
    at_c = running_generations(published_chain(repo, PATH, ref="main").versions)
    assert at_c.served.commit == c and at_c.observed.commit == c
    assert [item["commit"] for item in at_c.invalid] == [b]
    assert len(at_c.records) == 2
