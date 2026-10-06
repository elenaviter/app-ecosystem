"""pb procedure verify --format brief is a verdict per target (W563).

Root, 2026-10-06 05:17 UTC (W563 note_e42209a9): adopting .15, the brief
verify printed the whole package inventory and the same 48 changed files for
both runtimes, and the procedure said to read every changed file whole while
the split indexes said to read only the module for the current act.
"""

from __future__ import annotations

from project_board.client import cli
from project_board.client.procedures import source_package_path
from project_board.client.render import render_envelope

CHANGED = ["SKILL.md", *[f"references/coordinator/module-{index}.md" for index in range(47)]]


def _verify(**extra):
    row = {
        "state": "current", "installed_revision": "2026.10.05.15", "installed_digest": "c" * 64,
        "source_digest": "c" * 64, "files_verified": 58, "errors": [],
        "changed_since_revision": "2026.10.05.12", "changed_files": CHANGED,
        "release_path": "/h/.codex/skills/problem-board-worker/references/releases/x",
    }
    files = {f"references/file-{index}.md": "f" * 64 for index in range(58)}
    return {
        "procedure": "/h/src/SKILL.md",
        "package": {"package_id": "problem-board-worker", "revision": "2026.10.05.15", "source_digest": "c" * 64,
                    "entrypoint": "SKILL.md", "files": files, "references": sorted(files)},
        "targets": ["codex", "claude-code"],
        "targets_from": "installed",
        "verified": [dict(row, target="codex"), dict(row, target="claude-code")],
        **extra,
    }


def test_the_brief_verify_is_a_bounded_verdict_per_target():
    text = render_envelope({"ok": True, "result": _verify()})

    assert "procedure: problem-board-worker · revision 2026.10.05.15 · digest cccccccccccc · 58 files" in text
    assert "--- codex · current · installed 2026.10.05.15 · digest matches · files verified 58 · errors none" in text
    assert "changed since 2026.10.05.12: 48 file(s) · SKILL.md changed: load it once" in text
    assert "references/coordinator/module-3.md" not in text and "f" * 64 not in text
    assert len(text.splitlines()) <= 12 and len(text.encode("utf-8")) < 1_200


def test_detail_brings_back_the_manifest_and_every_changed_file():
    text = render_envelope({"ok": True, "result": _verify(detail=True)})

    assert "references/coordinator/module-46.md" in text
    assert cli.build_parser().parse_args(["procedure", "verify", "--detail"]).detail is True


def test_a_broken_target_shows_its_errors():
    result = _verify()
    result["verified"][1] = dict(result["verified"][1], state="modified", installed_digest="d" * 64,
                                 errors=["references/coordinator.md: digest differs"])
    text = render_envelope({"ok": True, "result": result})

    assert "--- claude-code · modified · installed 2026.10.05.15 · digest DIFFERS" in text
    assert "error: references/coordinator.md: digest differs" in text


def test_a_changed_revision_rereads_only_the_modules_in_use():
    skill = " ".join((source_package_path() / "SKILL.md").read_text(encoding="utf-8").split())
    assert (
        "read in full each changed reference or module you had loaded or now need for your acts, and keep the rest; "
        "a module that is new or that you have not needed is read when its act comes up, not because it is listed"
    ) in skill
    assert "read those files in full and keep the rest" not in skill
    purpose = " ".join(
        (source_package_path() / "references/coordinator/what-the-coordinator-is-for.md").read_text(encoding="utf-8").split()
    )
    assert "read whole the changed modules you had loaded or now need" in purpose
    assert "read those whole, keep the rest" not in purpose
