"""The shared return gate must precede queued execution, not just describe age."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "src/project_board/procedures/problem-board-worker"
OWNER = "delivery-and-recovery.md#return-from-prolonged-absence"


def read(path: str) -> str:
    return " ".join((ROOT / path).read_text(encoding="utf-8").split())


def return_rule() -> str:
    text = (ROOT / "references/delivery-and-recovery.md").read_text(encoding="utf-8")
    return " ".join(text.split("## Return from prolonged absence\n", 1)[1].split("\n## ", 1)[0].split())


def test_the_entrypoint_gates_old_requests_before_begin_work() -> None:
    skill = read("SKILL.md")
    assert "## Return From Prolonged Absence" in skill
    assert "references/delivery-and-recovery.md" in skill
    assert '"Return from prolonged absence"' in skill
    assert "before executing queued actions" in skill
    assert "Silence never counts as confirmation" in skill
    assert "A delayed `begin_work` notice does not bypass" in skill


def test_return_inventory_keeps_request_age_and_unknown_deadlines() -> None:
    rule = return_rule()
    for evidence in ("message ref", "what was requested", "original creation time", "deadlines have passed", "unknown"):
        assert evidence in rule
    assert "never infer a deadline from age" in rule
    assert "bounded pages" in rule
    assert "not a whole-plan export" in rule


def test_return_reads_current_authority_and_requires_one_explicit_resume_list() -> None:
    rule = return_rule()
    for evidence in ("current coordinator", "assignment.list", "ownership version", "authoritative work items"):
        assert evidence in rule
    for disposition in ("still needed", "already completed", "superseded", "reassigned"):
        assert disposition in rule
    assert "one current list of work to resume" in rule
    assert "Silence never counts as confirmation" in rule
    assert "a previous coordinator" in rule


def test_read_only_inspection_and_control_remain_possible_while_execution_waits() -> None:
    rule = return_rule()
    assert "Read-only inspection may continue" in rule
    assert "not queued implementation, tests, reviews, runtime changes or work-completion reports" in rule
    assert "reconciliation and current window-control exchange" in rule
    assert "native notification path" in rule
    assert "no polling" in rule


def test_obsolete_disposition_is_not_work_completion_or_age_based_bulk_retirement() -> None:
    rule = return_rule()
    assert "on the relevant items" in rule
    assert "obsolete" in rule
    assert "not completed work" in rule
    assert "exact lease once" in rule
    assert "one-hour lease limit" in rule
    assert "never settle it a second time" in rule
    assert "typed-notice retirement" in rule
    assert "unread" in rule


def test_return_causes_are_shared_and_transport_does_not_renew_authority() -> None:
    rule = return_rule()
    for cause in ("prolonged inactivity", "quota hold", "host sleep", "session suspension"):
        assert cause in rule
    assert "automatic return prompt" in rule
    assert "relay heartbeat" in rule
    assert "not a fresh execution instruction" in rule
    assert "ordinary short idle wake" in rule


def test_start_assignment_and_coordinator_paths_point_to_one_owning_rule() -> None:
    for path in (
        "references/start-or-resume.md",
        "references/identity-and-authorization.md",
        "references/coordinator/reconcile-the-work-not-the-inbox.md",
        "references/coordinator/set-a-teammate-up-to-work.md",
        "references/coordinator/worker-budgets.md",
        "references/collaboration/rule-6-your-visible-state-says-where-you-are-and-what-you-ar.md",
    ):
        assert OWNER in read(path), path
    coordinator = read("references/coordinator/reconcile-the-work-not-the-inbox.md")
    assert "one current list of work to resume" in coordinator
    assert "record the decision on the relevant items" in coordinator
    assert "Silence never counts as confirmation" in coordinator
