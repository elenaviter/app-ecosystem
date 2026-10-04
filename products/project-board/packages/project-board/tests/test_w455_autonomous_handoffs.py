"""W455 ownership 8 (operator, 2026-10-03): every task has a living route, the
responsibilities are split between coordinator, author, reviewer, merger and
installer, availability is read live, and an unavailable owner is handed off.

The scenarios fixture is the behavioural check: a reviewer gives each situation,
with the installed skill, to an agent that has not seen the expected answer.
These tests check that each governing rule is present, that the files agree with
each other, and that the superseded wording is gone.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from project_board.client.procedures import source_package_path


PROCEDURE_ROOT = source_package_path()
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "w455_autonomous_handoff_scenarios.json"


def _words(relative: str) -> str:
    return " ".join((PROCEDURE_ROOT / relative).read_text(encoding="utf-8").split())


def _section(relative: str, heading: str) -> str:
    text = (PROCEDURE_ROOT / relative).read_text(encoding="utf-8")
    start = text.index(heading)
    following = re.search(r"^## ", text[start + len(heading):], flags=re.MULTILINE)
    end = start + len(heading) + following.start() if following else len(text)
    return " ".join(text[start:end].split())


def test_the_scenarios_cover_the_required_situations_and_name_rules_that_exist() -> None:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    ids = {scenario["id"] for scenario in data["scenarios"]}
    assert ids == {
        "route-through-review-merge-install",
        "quota-held-reviewer-replaced-by-author",
        "valid-review-preserved-after-reviewer-unavailable",
        "owner-unavailable-after-poll-start",
        "unavailable-merger-goes-to-coordinator",
        "ownership-handoff-preserves-checkpoint",
        "second-project-generic",
        "operator-test-is-an-assigned-item",
    }
    for scenario in data["scenarios"]:
        assert scenario["situation"] and scenario["expected"] and scenario["forbidden"], scenario["id"]
        for name, quote in scenario["governing"]:
            assert quote in _words(name), (scenario["id"], name, quote)


def test_every_task_has_a_living_route_with_each_field() -> None:
    rule = _section("references/collaboration.md", "## Rule 16.")
    for field in (
        "the deliverable and its scope",
        "the acceptance",
        "the current actor and its next action",
        "where reports go",
        "the independent reviewer (or how the author selects one, and the fallback)",
        "the named merger",
        "the installer and verifier where the task needs them",
        "the current blockers with who decides each",
        "the handoff after each step",
    ):
        assert field in rule, field
    assert "A missing technical detail never leaves the route without an owner or waiting on an acknowledgement" in rule
    # The coordinator writes and keeps the route on the item at dispatch.
    coordinator = _words("references/coordinator.md")
    assert "The item's description carries the task's living route" in coordinator
    assert "keep it current at every change of actor or phase, saying in the description which earlier instructions it supersedes" in coordinator


def test_the_responsibility_table_splits_owner_handoff_from_reviewer_replacement() -> None:
    rule = _section("references/collaboration.md", "## Rule 16.")
    for row in ("| Coordinator |", "| Author |", "| Reviewer |", "| Merger |", "| Installer and verifier |"):
        assert row in rule, row
    coordinator_row = rule[rule.index("| Coordinator |"):rule.index("| Author |")]
    author_row = rule[rule.index("| Author |"):rule.index("| Reviewer |")]
    # The coordinator hands off work owners; the author replaces only its reviewer.
    assert "handing off a work owner who cannot act, by a reassignment from its checkpoint (Rule 8)" in coordinator_row
    assert "Nothing below transfers this." in coordinator_row
    assert "replacing a reviewer who cannot act (Rule 6)" in author_row
    assert "merger" not in author_row
    assert "An unavailable reviewer is yours to replace; an unavailable work owner is the coordinator's to hand off." in rule
    # The coordinator reference states the same accountability.
    coordinator = _words("references/coordinator.md")
    assert "**You stay accountable for the work and its owners.**" in coordinator
    assert "none of that transfers this accountability" in coordinator


def test_availability_has_one_definition_and_is_read_at_each_deciding_point() -> None:
    coordinator = _words("references/coordinator.md")
    assert "This is the one definition every availability decision in this procedure uses" in coordinator
    for figure in ("reachable and listening", "busy-until and info line", "provider's usage limit"):
        assert figure in coordinator, figure
    assert "An idle mark on its card alone is not availability." in coordinator
    assert (
        "before you form the list a poll or a window waits on, and again when you interpret the answers or a silence, "
        "when a handoff is consumed, when you choose an item's next action, and when you learn that someone's "
        "availability changed"
    ) in coordinator
    rule = _section("references/collaboration.md", "## Rule 16.")
    assert '([coordinator](coordinator.md), "What the coordinator is for")' in rule
    for text in (coordinator, rule):
        assert "Silence is never consent, approval or READY." in text


def test_review_and_merge_need_no_routine_coordinator_acknowledgement() -> None:
    collaboration = _words("references/collaboration.md")
    coordinator = _words("references/coordinator.md")
    skill = _words("SKILL.md")
    assert "no coordinator acknowledgement is needed" in collaboration
    assert "you are not asked to approve that choice" in coordinator
    assert "never again only because the merge passed to another person" in collaboration
    assert "also when the merge passes to another person" in coordinator
    assert "merges after approval and pushes the integration ref, with no further acknowledgement" in skill
    # The superseded defaults are gone from every file.
    for text in (collaboration, coordinator, skill):
        assert "With no specific reviewer, name the acting coordinator" not in text
        assert "with no specific reviewer, name the acting coordinator" not in text
        assert "You may propose a qualified reviewer in the summary" not in text
        assert "the merger runs the suites on the exact head before merging" not in text
        assert "route it with `review.assign` naming the coordinator" not in text
        assert "or a merger it names on the item" not in text


def test_an_unavailable_essential_owner_is_handed_off_not_awaited() -> None:
    collaboration = _words("references/collaboration.md")
    coordinator = _words("references/coordinator.md")
    assert "paused, suspended or unreachable by its own state" in collaboration
    assert "An essential owner who cannot act is never waited on indefinitely." in collaboration
    assert "rerouted or waited for with that reason" not in coordinator
    assert "If the work can safely wait for an imminent reset, wait instead of churning ownership" not in coordinator
    assert "awaits the operator's ruling on poll candidate A3" not in coordinator
    assert "Work another owner waits on is not parked behind the pause" in coordinator


def test_readiness_is_explicit_from_affected_available_owners_everywhere() -> None:
    collaboration = _words("references/collaboration.md")
    assert "silence is not READY" in collaboration
    assert "after collecting an explicit ready from every affected agent that is available (Rule 10)" in collaboration
    assert "Silence is not ready." in _words("references/runtime-actions.md")
    window = _words("references/test-window.md")
    assert "once every affected worker that is available has reported paused" in window
    # Infra review, 13:13Z: an unavailable participant is not proof that its operation stopped.
    assert "being stopped already" not in window
    assert "Absence is not quiescence" in window
    assert "establishes from evidence that it has no operation in flight the freeze would conflict with" in window
    assert "Absence is not quiescence" in collaboration
    assert "Its absence proves neither" in _words("references/runtime-actions.md")
    assert "Its absence neither holds the action nor shows that none applies." in _words("references/coordinator.md")
    assert "Collect one `ready` or `hold` from every attending worker. One" not in _words("references/coordinator.md")


def test_mail_to_the_coordinator_carries_actions_and_old_mail_restarts_nothing() -> None:
    rule = _section("references/collaboration.md", "## Rule 16.")
    # 2026-10-04: the item is the status; mail goes to whoever acts next.
    assert "The item is the status; mail goes to whoever acts next." in rule
    assert "Progress, receipts, evidence and acknowledgements go once, on the item" in rule
    assert "it never restarts completed work or reruns unchanged checks" in rule
    delivery = _words("references/delivery-and-recovery.md")
    assert "neither restart superseded or completed work, rerun unchanged checks" in delivery
    assert "without waiting for your acknowledgement, and copy you" not in _words("references/coordinator.md")


def test_common_rules_stay_generic_and_project_bindings_live_in_project_files() -> None:
    collaboration = _words("references/collaboration.md")
    assert "**Where a rule lives.**" in collaboration
    assert "A reusable runtime command of one product goes in that product's guide" in collaboration
    assert "a project's primary product is its focus, not exclusive ownership of a repository" in collaboration
    coordinator = _words("references/coordinator.md")
    assert "This section is the generic rule; the table itself is the project's." in coordinator
    assert "| Item roles |" in coordinator
    # The new rule names no project's hosts, agents or tickets.
    rule = _section("references/collaboration.md", "## Rule 16.")
    for specific in ("spark1", "dev-main", "Quickstart", "claude-", "codex-", "W4", "PR "):
        assert specific not in rule, specific
    assert "(a spark1 pool, for example)" not in coordinator


def test_the_skill_points_at_the_route_rule() -> None:
    skill = _words("SKILL.md")
    assert "every task has a living route on its item that names each actor's next step" in skill
    assert "raise an unavailable work owner to the coordinator, who hands it off (rule 16)" in skill
    assert "put it in your one consolidated clarification ([collaboration](references/collaboration.md) Rule 16)" in skill


def test_anything_waiting_on_the_operator_is_an_item_assigned_to_them() -> None:
    """Operator, 2026-10-03: an action for the operator is in their assignments, with exact steps, and a notifying message."""
    rule = _section("references/collaboration.md", "## Rule 11.")
    assert "**Anything that waits on the operator is a work item assigned to them.**" in rule
    assert "Whoever needs the operator, worker or coordinator alike" in rule
    assert "The item says exactly what to do and what to expect" in rule
    assert "as `decision` or `question` so it also reaches their Telegram, saying \"urgent\" when it is" in rule
    assert "read the item back (`project.plan.item`). Check that the operator is its assignee" in rule
    assert "Never leave an operator action only in an agent's terminal or only in mail between agents" in rule
    assert "anything that waits on the operator is first a work item assigned to them" in _words("SKILL.md")
    assert "The same holds for anything else that waits on the operator, from you or from a worker" in _words("references/coordinator.md")



def test_an_item_routed_to_the_operator_carries_their_steps_on_its_own_fields() -> None:
    """Operator, 2026-10-03: "the tickets assigned to operator must contain the information for operator"."""
    rule = _section("references/collaboration.md", "## Rule 11.")
    assert "**The item itself carries what the operator needs.**" in rule
    assert "Routing (`review.assign`, `work.assignee.set`) changes who acts next; it does not rewrite those fields." in rule
    assert "rewrites them for the operator with `plan.item.update`, in the same step" in rule
    for field in ("`result`:", "`review.look_at`: the operator's steps", "`review.could_not_verify`:"):
        assert field in rule, field
    assert "**An operator's question is answered where the operator works.**" in rule
    assert "An answer only in your terminal is not durable." in rule
    assert "When the question shows the item was unclear, correct the item" in rule
    assert "are the ones you wrote for them" in rule
    skill = _words("SKILL.md")
    assert "rewritten for them when you route it" in skill
    assert "or your answer to an operator's question, only in a terminal" in skill

def test_each_actor_hands_on_along_the_route_including_to_the_operator() -> None:
    """Operator, 2026-10-03: workers assign along the concluded route, the operator included."""
    collaboration = _words("references/collaboration.md")
    assert "hands the item to the next actor the route names itself, the operator included" in collaboration
    assert "When the next action is the operator's (a test, a decision, an approval, a choice of behaviour), assign the item to the operator yourself, without waiting for the coordinator" in collaboration


def test_the_skill_names_the_requirements_agents_keep_as_memories() -> None:
    """Operator, 2026-10-03: memories come from the skill, tagged with its revision; no self-written PB rules."""
    skill = _words("SKILL.md")
    assert "## Keep The Critical Requirements As Memories" in skill
    # Ops and Root, 14:27Z and 14:40Z: Codex has no persistent-memory writer, so the rule is per runtime.
    assert "A runtime with a persistent memory facility (Claude Code's memory directory) saves each one below as one entry, tagged `source: problem-board-worker <installed revision>`" in skill
    assert "and replaces the whole set when the installed revision changes" in skill
    assert "A runtime with none (Codex) keeps them through this skill itself, which it loads in full once per installed-revision change; it writes no substitute file and claims no memory entries." in skill
    assert "Adoption is `pb procedure verify` plus, by runtime, the tagged entries or the agent's confirmation that it loaded that revision." in skill
    assert "save each one below as one entry in your runtime's persistent memory" not in skill
    assert "In every runtime, do not save your own versions of Problem Board workflow rules" in skill
    assert "goes to the coordinator as a procedure change (collaboration Rule 14), never into private memory" in skill
    section = skill[skill.index("## Keep The Critical Requirements As Memories"):skill.index("## Read pb Output")]
    for requirement in (
        "Anything that waits on the operator is a work item assigned to them",
        "Every task's route on its item names the next actor",
        "Work others wait on is handed off, never parked",
        "names its exact head, tree and evidence",
        "quoted verbatim with its reference",
        "Never print, export or pass a credential",
    ):
        assert requirement in section, requirement


def test_older_directives_agree_with_the_route_split() -> None:
    """Infra review return, 13:16Z: Rule 2 and round-2 items 7 and 8 named the coordinator for every step."""
    collaboration = _words("references/collaboration.md")
    assert "the coordinator pushes `main` after merges" not in collaboration
    assert "the merger the item's route names (the coordinator when none is named) pushes `main` after merges (Rule 16)" in collaboration
    assert "the installer the route names installs procedure revisions and runs reloads and relay restarts" in collaboration
    assert "the verifier the route names (the coordinator when none is named) verifies the way the operator would" in collaboration
    assert "Where it names who merges, installs or verifies, Rules 2, 5 and 16 decide today." in collaboration


def test_the_ownership_fence_is_on_the_implementation_not_on_review_routing() -> None:
    collaboration = _words("references/collaboration.md")
    assert "The fence is on the implementation assignment" in collaboration
    assert "changes who acts next on the item, not who owns the implementation" in collaboration


def test_a_window_retries_only_available_workers() -> None:
    coordinator = _words("references/coordinator.md")
    assert "is asked once more with the deadline only when it is available now" in coordinator
    assert "A worker that has not answered is asked once more with the deadline. Running" not in coordinator


def test_an_operator_request_says_how_to_report_respects_refusals_and_is_not_repeated() -> None:
    """Root GO 13:24Z with the operator instruction: acceptance included, report path, permission caps, no duplicates."""
    rule = _section("references/collaboration.md", "## Rule 11.")
    assert "a test, a decision, an approval, a choice or an acceptance" in rule
    assert "and how to report the result with the controls the person has (below)" in rule
    assert "When the routing or the message is refused, never work around the permission" in rule
    assert "report the refused action, its code and who can clear it" in rule
    assert "Do not send the same unchanged request again" in rule


def test_every_file_names_the_same_actors_for_push_and_host_actions() -> None:
    """CodeSpark review return, 13:32Z: older lines gave push, install and restart to the coordinator."""
    collaboration = _words("references/collaboration.md")
    runtime_actions = _words("references/runtime-actions.md")
    skill = _words("SKILL.md")
    # One owning statement of the order, in Rule 2.
    assert (
        "**Who runs a host-local action** (a relay restart, a source selection, a procedure install) on a machine: "
        "the installer the item's route names for that host (Rule 16); when the route names none, the coordinator on "
        "that host; on a host without one, its elected integrator."
    ) in collaboration
    assert "Who pushes the integration ref: the merger the route names" in collaboration
    # Every passage that names an actor for these actions follows it.
    assert "The installer the route names for that host restarts it" in runtime_actions
    assert "run on each host by the installer the item's route names" in runtime_actions
    assert "Before it, the merger the item's route names (the coordinator when none is named) integrates" in runtime_actions
    assert "then the installer the route names for that host restarts it" in skill
    assert "the installer the route names installs procedure revisions and runs reloads and relay restarts" in collaboration
    # The superseded unconditional assignments are gone everywhere.
    texts = {name: _words(name) for name in (
        "SKILL.md", "references/collaboration.md", "references/coordinator.md",
        "references/runtime-actions.md", "references/test-window.md",
    )}
    for name, text in texts.items():
        for stale in (
            "The coordinator on that host restarts it",
            "then the coordinator on that host restarts it",
            "run by the coordinator on the host",
            "Before it, the coordinator integrates the commits",
            "the coordinator fast-forwards the shared checkout",
            "coordinator fast-forwards the shared checkouts to it before any runtime action",
            "brings that machine's checkouts to the pushed integration ref before any runtime action",
            "the coordinator pushes `main` after merges",
            "After the merge the coordinator names the merged ref on the item",
            "Only the coordinator deploys.",
        ):
            assert stale not in text, (name, stale)
    # CodeSpark return, 13:40Z: the merger records the merged ref; the installer deploys.
    assert "After the merge the merger names the merged ref on the item and hands it to the next actor the route names; the coordinator tracks it (Rule 16)." in collaboration
    assert "Only the installer the route names deploys (the coordinator when none is named;" in _words("references/test-window.md")
    # A runtime action never depends on a checkout being fast-forwarded first.
    assert "a runtime action loads the exact ref it releases, never a checkout" in collaboration
