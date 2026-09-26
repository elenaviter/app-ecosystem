"""The worker procedure package: one assertion per rule that stays.

The skill is condensed knowledge the model reads every time (operator ruling,
2026-09-18). Every rule is here as a short phrase so a rewrite that drops or
softens it fails, and nothing here pins story text, which the skill no longer
carries. Situational procedures live in references opened by one
if-this-read-that line each, and the package manifest ships them.
"""

from __future__ import annotations

import os
import re

import json
from pathlib import Path

import pytest

from project_board.client.procedures import (
    source_package,
    source_package_path,
    source_revision_ledger_path,
)


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROCEDURE_ROOT = source_package_path()
OPERATIONAL_PROCEDURE_ROOT = PACKAGE_ROOT / "src" / "project_board" / "procedures"
OPERATIONAL_PROCEDURES = {
    "add-a-worker-host.md",
    "agent-worker.md",
    "first-time-setup.md",
    "live-acceptance.md",
    "local-worker-session.md",
    "enroll-an-agent.md",
    "operator.md",
    "testing.md",
}


def _read(relative: str) -> str:
    return (PROCEDURE_ROOT / relative).read_text(encoding="utf-8")


def _words(text: str) -> str:
    return " ".join(text.split())


REVISION_LEDGER = source_revision_ledger_path()


def _repository_root() -> Path:
    current = PACKAGE_ROOT
    while current != current.parent:
        if (current / "products").is_dir() and (current / "packages").is_dir():
            return current
        current = current.parent
    raise AssertionError("the app-ecosystem repository root was not found above the package")


KDCUBE_MAINTAINER_PROFILE = "products/kdcube/procedures/runtime-profile-maintainer.md"


def _profile() -> str:
    """The KDCube maintainer runtime profile, where the KDCube commands live (W262 PR 2)."""
    return (_repository_root() / KDCUBE_MAINTAINER_PROFILE).read_text(encoding="utf-8")


def test_project_board_owns_its_operational_procedures() -> None:
    present = {
        path.name
        for path in OPERATIONAL_PROCEDURE_ROOT.glob("*.md")
        if path.is_file()
    }
    assert present == OPERATIONAL_PROCEDURES
    for name in OPERATIONAL_PROCEDURES:
        text = (OPERATIONAL_PROCEDURE_ROOT / name).read_text(encoding="utf-8")
        assert "id: app-ecosystem.project-board.procedure." in text
        assert "id: applications.playground.problem-board.procedure." not in text


REQUIRE_REVISION_RECORDED = "PB_REQUIRE_REVISION_RECORDED"
UNRECORDED_REASON = "procedure changed; the merger sets the revision"


def _revision_recorded(package: dict, ledger: dict) -> None:
    """Fail, or skip on an author's head, when the content is not recorded.

    Authors never bump the revision; the merger sets it at merge time
    (coordinator.md, Merge). A head whose content differs from its revision's
    ledger entry therefore skips, and the merger's run after the bump commit
    sets PB_REQUIRE_REVISION_RECORDED=1, which turns the skip into a failure.
    """

    revision = package["revision"]
    recorded = ledger.get(revision)
    assert recorded is not None, (
        f"revision {revision} is not in {REVISION_LEDGER.name}; add "
        f'"{revision}": "{package["source_digest"]}"'
    )
    if recorded == package["source_digest"]:
        return
    if os.environ.get(REQUIRE_REVISION_RECORDED, "").strip() == "1":
        pytest.fail(
            f"the package content changed under revision {revision}; set the next "
            f"revision in package.json and record "
            f'"<revision>": "{package["source_digest"]}" in {REVISION_LEDGER.name}'
        )
    pytest.skip(UNRECORDED_REASON)


def test_package_content_is_recorded_for_its_revision() -> None:
    """A content change under an unchanged revision fails here.

    Why: installed copies compare revisions, so an edit that keeps the
    revision is invisible to every session that already installed it. On
    2026-09-22 the W257 edits to SKILL.md and first-run.md were committed under
    2026.09.22.4, which claude-main had installed minutes earlier, so no worker
    received them. The ledger records the source digest of each revision; a
    changed package needs a new revision and a new ledger line.
    """

    _revision_recorded(
        source_package(), json.loads(REVISION_LEDGER.read_text(encoding="utf-8"))
    )


def test_an_unrecorded_revision_skips_on_an_author_head_and_fails_for_the_merger(monkeypatch) -> None:
    package = {"revision": "2026.01.01.1", "source_digest": "new"}
    monkeypatch.delenv(REQUIRE_REVISION_RECORDED, raising=False)
    with pytest.raises(pytest.skip.Exception, match=UNRECORDED_REASON):
        _revision_recorded(package, {"2026.01.01.1": "old"})
    monkeypatch.setenv(REQUIRE_REVISION_RECORDED, "1")
    with pytest.raises(pytest.fail.Exception, match="changed under revision 2026.01.01.1"):
        _revision_recorded(package, {"2026.01.01.1": "old"})
    _revision_recorded(package, {"2026.01.01.1": "new"})
    with pytest.raises(AssertionError, match="is not in"):
        _revision_recorded(package, {})


def test_package_manifest_ships_every_reference_the_skill_opens() -> None:
    package = json.loads(_read("package.json"))
    skill = _read("SKILL.md")
    assert package["revision"] == "2026.09.26.19"
    assert package["entrypoint"] == "SKILL.md"
    references = set(package["references"])
    assert references == {
        "references/identity-and-authorization.md",
        "references/delivery-and-recovery.md",
        "references/claude-code-wake.md",
        "references/project-report.md",
        "references/runtime-actions.md",
        "references/coordinator.md",
        "references/test-window.md",
        "references/first-run.md",
        "references/shared-runtime-state.md",
        "references/collaboration.md",
        "references/brief-output.md",
        "references/project-workspace.md",
        "references/journaling.md",
        "references/signals.md",
    }
    for reference in references:
        assert (PROCEDURE_ROOT / reference).is_file(), reference
        assert f"({reference})" in skill, f"the skill opens {reference}"


def test_first_run_guides_the_user_from_the_state_command() -> None:
    # A new user is guided through setup, not told the machine is unconfigured (W249).
    skill = _words(_read("SKILL.md"))
    assert "guide the user through setup with [first run](references/first-run.md)" in skill
    assert "report that state instead" not in skill
    first_run = _words(_read("references/first-run.md"))
    assert "Read this when the user asks you to use this skill and `pb status` does not report `session_attending`" in first_run
    for state in ("`machine_not_configured`", "`session_not_attending`", "`session_attending`"):
        assert state in first_run, state
    for step in ("`configure_target`", "`install_relay`", "`enroll_session`", "`authorize_profile`", "`attend_project`"):
        assert step in first_run, step
    assert "preserve the returned profile and append `--device`" in first_run
    assert "append `--device`, and use callback flags (`--no-open --callback-port`) only as the named fallback in add-a-worker-host step 11 when device login fails, never together with `--device`" in skill
    assert "missing identification does not block Card authorization" in skill
    # W305, 2026-09-24: the skill once said "never callback flags" while the
    # host procedure kept the tunnel as its only recovery from a failed device
    # login, so an agent following the skill would refuse that recovery.
    assert "never callback flags" not in skill
    host = _words((OPERATIONAL_PROCEDURE_ROOT / "add-a-worker-host.md").read_text(encoding="utf-8"))
    assert "pb worker authorize <profile> --device" in host
    assert "runs the same command with `--no-open --callback-port 18765` in place of `--device`" in host
    assert "prints `Provider account not reported` and continues authorization" in host
    assert "the credential goes to the native store" in skill
    # The package owns the setup coordinate meanings and source install path.
    assert "What The Setup Coordinates Mean" in first_run
    assert "This section is the owning definition" in first_run
    assert "setup guides point here rather than restating the meanings" in first_run
    assert "`pb` is the console command in the `project-board` distribution" in first_run
    assert "one dependency resolution over all four first-party package paths" in first_run
    # Two install paths, both intended (operator, 2026-09-22): a team host from
    # pinned source exports, a user of a published release from the package
    # index. Neither reads a checkout at run time (W262).
    assert "There are two ways to install it, and the operator names which applies" in first_run
    assert "From the published package" in first_run
    assert "pip install \"project-board==<version>\"" in first_run
    assert "source use-release" in first_run
    assert "--expect-version <version>" in first_run
    assert "Neither path reads a repository checkout at run time" in first_run
    assert "scripts/install_from_source.py" in first_run
    assert "the release ID, the full commit, all four package trees" in first_run
    assert "never becomes a runtime import path" in first_run
    assert "run it after they approve" in first_run
    # A worker whose credential was refused is not attending (coordinator, 2026-09-21 10:41Z).
    assert "its channel is `pending_authorization`" in first_run
    assert "ask before proposing `pb relay-service start`. Do not reinstall it" in first_run
    assert "This state means the channel works (`active`) and the worker attends a project" in first_run
    # W26: Claude Code reports its own usage limit through two settings lines,
    # which the install merges into the user's settings (W304 finding 45).
    assert "Claude Code Says When It Is Out Of Tokens" in first_run
    assert "never inferred from silence" in first_run
    assert '"statusLine": {"type": "command", "command": "pb worker limit-state"}' in first_run
    assert "pb worker limit-state --source stop-failure" in first_run
    assert "`pb procedure install --target claude-code` merges these lines" in first_run
    assert "keeps a copy of the file before it writes, changes nothing on a second run" in first_run


def test_package_preserves_the_application_revision_chain() -> None:
    ledger = json.loads(REVISION_LEDGER.read_text(encoding="utf-8"))
    assert {key: ledger[key] for key in (
        "2026.09.22.5",
        "2026.09.22.6",
        "2026.09.22.7",
        "2026.09.22.8",
        "2026.09.22.9",
        "2026.09.22.10",
        "2026.09.22.11",
        "2026.09.22.12",
    )} == {
        "2026.09.22.5": "7780045d697a87850c8f0726960dd9b4d09b17afec431cee7b78200ef6a684be",
        "2026.09.22.6": "2127819a70989cc25a073b5fbf3a516ea2b4344ccf231727dba74eb73f1c9c66",
        "2026.09.22.7": "f94c9cefb90d44068aa4c609522c07c5d112531a063423d5eb267d8edaed9512",
        "2026.09.22.8": "7e15e9c62d4c8f3b4b11ef0bf18e3da79d39d687b09e9ea169e831edee88057b",
        "2026.09.22.9": "41bbcb76a230d7073cbca4dd09a1a8320afc1640bb1a9ab984c61b138e92484e",
        "2026.09.22.10": "3eaf24b787db4b0b4295352bfd1e2234d44ef3e235baba9b9aa6250a0f1128c3",
        "2026.09.22.11": "cfada6d4d51411e45330b4300d812295b54292d67a9eaa3744abe1c40b775e2e",
        "2026.09.22.12": "3e9c8e1fb4ecd6c3a35ac0eaa08afd417a2f89d5672f67d03f49bf8aef69092f",
    }


def test_source_selection_guidance_is_added_without_rewriting_collaboration() -> None:
    skill = _words(_read("SKILL.md"))
    first_run = _words(_read("references/first-run.md"))
    runtime = _words(_read("references/runtime-actions.md"))
    coordinator = _words(_read("references/coordinator.md"))
    collaboration = _words(_read("references/collaboration.md"))

    assert "Selecting a released client version or an App Ecosystem source commit with `pb source`" in skill
    assert "a clean export of an approved App Ecosystem commit" in first_run
    assert "`pb source use-release --expect-version <version>`" in runtime
    assert "`pb source use-code` with the App Ecosystem repository path, ref, and full approved commit" in runtime
    assert "`client.pinned: false` with `source.mode: checkout`" in runtime
    assert "Move An Existing Host To Release Environments" in runtime
    assert "One Complete Host Release" in runtime
    assert (
        "The bootstrap installer detects those installed current-path relay "
        "units and exits before building or activating a candidate"
        in runtime
    )
    assert "one failed restart does not prevent the remaining relays from being attempted" in runtime
    assert "At that point the former venv has no launcher or service consumer and may be deleted" in runtime
    assert "same released version or code source, including the App Ecosystem commit and all four package trees" in runtime
    assert "the full App Ecosystem commit" in coordinator
    # W278: the coordinator binds every repository at assignment time.
    assert "one entry per repository the work touches" in coordinator
    assert "Fill it when you assign, not later" in coordinator
    assert "Work that touches no repository says so with an empty list" in coordinator
    assert "repositories not declared" in coordinator
    assert "before fast-forwarding that checkout" in coordinator
    # W33 (#69), 2026-09-24: a widget-only PR broke contract tests that read widget source.
    assert "the app's Python suite runs on `main` with the change merged, widget-only changes included" in coordinator
    assert "one recorded source selection, not from any working tree" in collaboration
    assert "The integration ref is source history, not a runtime selection" in collaboration


    assert skill.count("\n") < 530, "the skill is condensed knowledge; grow a reference, not the skill"


def test_skill_carries_rules_not_stories() -> None:
    skill = _read("SKILL.md")
    words = _words(skill)
    assert skill.count("\n") < 520, (
        "SKILL.md is loaded into every worker session's context, so it holds fewer "
        "than 520 newlines of rules with one clause of reason each (operator ruling, "
        "2026-09-18). Content that does not fit moves to a reference behind one "
        "trigger line. Do not tighten existing prose to make room: that traded facts "
        "for lines three times in round 2 (collaboration.md, Rule 5 gate 8, finding "
        "eleven)."
    )
    # Incident narratives belong in journals, found by search, not in text the
    # model reads every turn.
    for story_marker in (
        "in one night",
        "in one evening",
        "in one day",
        "2552",
        "ten hours later",
        "has happened here",
        "This happened",
        "its not readable",
        "Operator ruling, 2026",
    ):
        assert story_marker not in skill, story_marker
    assert "Incidents go to the journal, where search finds them" in words
    assert "not into this skill, which is read every time" in words
    assert "carries the rule with one clause of reason" in words
    # 2026.09.23.2 merged the foundation-claims paragraph into Review Foundations,
    # where the same duty already lived, instead of stating it twice.
    assert "trace the real boundaries and failure states" in words
    assert "an existing assumption is not authority for a costly foundation" in words


def test_team_decision_rule_is_in_rounds() -> None:
    # W262, operator 2026-09-23: a question about how the team collaborates is
    # decided in rounds, ideas alone first, and the result with the votes table
    # reaches every member. The skill points at the rule, the reference carries it.
    skill = _words(_read("SKILL.md"))
    assert "decided in rounds, ideas alone first" in skill
    collaboration = _words(_read("references/collaboration.md"))
    assert "Rule 7. A question about how the team collaborates is decided in rounds" in collaboration
    assert "only the questions: no candidate practices, no coordinator input" in collaboration
    assert "The proposer writes its own answer before any other arrives" in collaboration
    assert "sends every member every idea, attributed, in the words it came in" in collaboration
    assert "amend or change their position" in collaboration
    assert "one row per candidate practice, one column per member" in collaboration
    assert "`pending` for a member who has not answered and never a guess" in collaboration
    assert "Split votes stay `open` for the operator" in collaboration
    assert "The result goes to the operator and to every member" in collaboration
    assert "as a note on the item the question belongs to" in collaboration
    assert "The note is the record" in collaboration


def test_the_first_poll_s_adopted_practices_are_rule_text() -> None:
    # 2026-09-23 team consultation, thirteen adopted rows. The rule text lives
    # where each concept already lived: pushes in rule 2, visible state in
    # rule 6, the estimate in its subsection, and three new rules for handoff,
    # safe publication and runtime windows. The skill points at the new rules.
    skill = _words(_read("SKILL.md"))
    assert "Handoff is an ownership decision the coordinator takes (rule 8)" in skill
    assert "what you publish is safe to publish (rule 9)" in skill
    assert "a runtime window speaks one channel that survives it (rule 10)" in skill
    collaboration = _words(_read("references/collaboration.md"))
    # P1
    assert "Push at every coherent checkpoint" in collaboration
    assert "Local-only work is never a handoff" in collaboration
    # P11, P5, P2 in rule 6
    assert "the actor or event that clears it" in collaboration
    assert "Publish at transitions, not on a clock" in collaboration
    assert "names what was preflighted before the point of no return" in collaboration
    assert "A resume record on the item" in collaboration
    assert "It does not repeat the branch, base, latest commit or change request" in collaboration
    # P8 and the operator's P8+ decision
    assert "The estimate is coarse and it is enough" in collaboration
    assert "there is no confidence value beside it" in collaboration
    assert "marks an overdue estimate apart from a blocked state" in collaboration
    # P12, P13, P14
    assert "Rule 8. Handoff is an ownership decision, not a note" in collaboration
    assert "the coordinator decides: wait for the reset, or reassign" in collaboration
    assert "the predecessor cannot report or mutate under the old version" in collaboration
    assert "Rule 9. What is published is safe to publish" in collaboration
    assert "observed files in flight are tracked paths only" in collaboration
    assert "Rule 10. A runtime window speaks one channel that survives it" in collaboration
    assert "The all-clear on the board is the only resume signal" in collaboration


def test_files_in_flight_and_the_scope_line_are_rule_text() -> None:
    # W278 part B (P4a, P4b): the working report carries one scope line, the
    # worker declares its worktree once, the board shows tracked paths only.
    collaboration = _words(_read("references/collaboration.md"))
    assert "Say your scope in the `working` report, and declare your worktree once" in collaboration
    assert "pb worker report --state working --scope" in collaboration
    assert "pb worker workspace --assignment-ref <assignment-ref> --repository" in collaboration
    assert "`no worktree declared` until you declare" in collaboration
    assert "They are not the handoff and not the contract, the pushed branch is (Rule 2)" in collaboration


def test_authority_and_next_action_rules() -> None:
    words = _words(_read("SKILL.md"))
    # W262, worker estimate: the rule's three moments are in the skill and the
    # command is named, so a worker learns to say until when it expects to finish.
    assert "Your estimate is visible state" in words
    assert "says until when you expect to finish and what you are on" in words
    assert "Set it again with the reason when it slips" in words
    assert "when the work is done" in words
    assert "marks it overdue once the time has passed" in words
    assert "pb worker busy-until" in words
    assert "Connection Hub owns worker credentials" in words
    assert "A worker alias is display text" in words
    assert "Direct conversation, project attendance, and assignment are independent states" in words
    assert "Reassess after a wake or a returned command" in words
    assert "use a bounded attempt count" in words
    # Each idle-wait rule names its runtime first (W177).
    codex = words.index("Codex: ending the model turn is the idle wait")
    assert codex < words.index("does not poll inbox availability")
    assert codex < words.index("background watch, timer, repeated `receive`, or status query")
    assert codex < words.index("Claude Code: the session-owned `pb worker watch` attachment is the idle wait")
    assert "A quiet inbox is not evidence that the watch is running" in words
    assert "`not_listening` label alone is not evidence of a delivery fault" in words


def test_start_or_resume_and_the_claude_code_wake_path() -> None:
    skill = _read("SKILL.md")
    words = _words(skill)
    wake = _words(_read("references/claude-code-wake.md"))
    assert "pb worker whoami" in skill
    assert "pb worker listen --alias <display-name>" in skill
    assert "Do not reconstruct a profile name" in words
    assert "Do not start `pb worker watch` as a wake mechanism" in words
    assert "Output in a background terminal cannot create a Codex model turn" in words
    assert "start exactly one session-scoped notification attachment" in words
    assert "replaces it on a schedule whose every interval, including the wrap of the hour, is shorter than its 30-minute cap" in words
    assert "On the attachment's end notice, start it again and then run `pb worker receive`" in words
    assert "A watch belongs to the session id in its command line, and a session stops only its own" in words
    assert "leave it running" not in words
    assert "`session.inbox_check_state` reads `current`" in words
    assert "Receive once immediately" in words
    # The wake reference holds the mechanics (W177, W182).
    assert "caps an attachment at 30 minutes" in wake
    assert "posts one task notification when the attachment ends, on expiry and on a kill alike" in wake
    assert "usually starts a model turn and sometimes does not" in wake
    assert "The guard does not check liveness" in wake
    # A scheduled prompt waits for an idle REPL (2026-09-19, two fires 11 min late).
    assert "The scheduler adds a fixed per-job delay of up to fifteen minutes, the same on every fire, so the spacing holds" in wake
    assert "A fire during a turn waits for the turn to end" in wake
    assert "every interval between consecutive fires, including the wrap from the last minute of the hour to the first, must be shorter than the 30-minute cap" in wake
    assert "`7,27,47 * * * *`" in wake
    # Start before kill: a turn can end on any tool result (2026-09-19, 19 min deaf).
    assert "start a fresh watch, then end every older watch process carrying this session id keeping the newest" in wake
    assert "Start before kill, because a turn can end on any tool result" in wake
    assert "1. start a fresh watch with the Monitor tool" in wake
    assert "2. end every OLDER watch process for this session, keeping the one with the smallest elapsed time" in wake
    assert "Never end the turn between step 1 and step 3" in wake
    assert 'pkill -f "worker watch.*<id>"' not in wake
    # The kill step cannot end with zero watches (2026-09-19, a hand-written
    # `pkill -o` guard ended a coordinator's only watch).
    assert "The kill step must be unable to end with zero watches and must keep the newest" in wake
    assert "`pkill -o` on the same pattern ends the only watch when one is running" in wake
    # Pids wrap (2026-09-19, two hosts): select by elapsed time, not pid order.
    assert "Keeping the highest pid (`sort -n | sed '$d'`) assumes pids rise with start time" in wake
    assert "keeping the one with the smallest elapsed time" in wake
    assert "ps -o pid=,etime=,comm= -p $(pgrep -d, -f \"worker watch.*<id>\")" in wake
    # The pattern also matches the shell running the command (2026-09-24,
    # claude-ops): shells are dropped before the newest is chosen.
    assert "awk '$0 !~ /(^|[\\/ ])(-?[a-z]*sh|ps)$/ {" in wake
    assert "that wrapper is always the youngest match" in wake
    assert "sort -n | awk 'NR>1{print $2}'" in wake
    assert "pgrep -f \"worker watch.*<id>\" | sort -n | sed '$d'" not in wake.replace("(`sort -n | sed '$d'`)", "")
    assert "`ps -o etimes` is Linux only" in wake
    assert "A brief overlap of two watches costs nothing. A gap of one guard interval cost nineteen minutes" in wake
    assert "`stale` once it has stopped" in wake
    assert "`listener.state` does not move when the watch stops" in wake
    assert "`worker.notification_path` event on the worker's project" in wake
    assert "It is an event, never mail" in wake
    assert "starts no turn, even after the network returns, until a person types in the session" in wake
    assert "runtime_has_no_supported_local_queue" in wake
    assert "`oauth_metadata_request_failed` with `status=404` for every worker" in wake


def test_pb_output_is_read_with_brief_never_with_a_parser() -> None:
    skill = _read("SKILL.md")
    words = _words(skill)
    assert "## Read pb Output With `--format brief`, Never With Your Own Parser" in skill
    assert "`PB_FORMAT=brief` makes it the session default" in words
    assert "pb render --file <path>" in skill
    assert "Do not write a JSON reader for `pb` output" in words
    assert "never composed from a key and a title" in words
    assert "renders as `UNREADABLE` followed by the text itself, exit code 2" in words
    assert "copied as a whole line or obtained from a rendered command, never assembled" in words
    assert "The `--format brief` rendering is the handling ledger" in words


def test_receive_handle_and_settle_rules() -> None:
    skill = _read("SKILL.md")
    words = _words(skill)
    delivery = _read("references/delivery-and-recovery.md")
    assert "pb worker receive --wake-id <wake-id>" in skill
    assert "Truncated output never means the omitted messages do not exist" in words
    assert "pb worker leases --cursor <next-cursor>" in skill
    assert "pb worker lease-read" in skill
    assert "delivery-integrity blocker" in words
    assert "Do not inspect or rewrite private mailbox files" in words
    assert "one current revision marker per attended project" in words
    assert "Work-item refs carry their item version" in words
    for operation in ("project.plan.index", "project.plan.item", "project.plan.search", "plan.notes.list"):
        assert operation in skill and operation in delivery
    assert "Never assemble the plan" in skill and "Never assemble the plan" in delivery
    assert "project.plan.resolve" not in skill and "project.plan.resolve" not in delivery
    assert "A bounded first page is not evidence that no more records exist" in words
    assert "Never use `--route direct` from a managed session" in words
    assert "The wake text is never the task" in words
    assert "message.attachment_count" in skill
    assert "payload.attachments[].local_path" not in skill
    assert "send a visible correlated reply before settlement" in words
    assert "pb worker settle" in skill
    assert "Do not settle an item that did not arrive with a complete body and lease" in words
    assert "Do not repeat a side effect because a wake repeats" in words


def test_assignment_rules() -> None:
    skill = _read("SKILL.md")
    words = _words(skill)
    assert "It is work to begin, not a notification to acknowledge" in words
    for field in ("payload.work_ref", "payload.assignment_ref", "payload.ownership_version", "payload.expected_reaction"):
        assert f"`{field}`" in skill
    assert "work_assignment_version_conflict" in skill
    assert "Take the version from the notice, never assume 1" in words
    assert "Commit what you wrote, by explicit path, as you finish it" in words
    assert "A report without them tells the reviewer nothing about what was checked" in words
    assert "`review.look_at` and `review.could_not_verify`" in words
    assert "State plus `source_event_ref` identifies one immutable report" in words
    assert "An accepted terminal report is final for that ownership version" in words
    assert "Do not acknowledge an assignment and stop" in words
    assert "An item nobody reports stays open however complete the work is" in words
    assert "Report `working` (it sets Working)" in words
    assert "assignment and release never move status" in words


def test_work_report_and_journal_rules() -> None:
    skill = _read("SKILL.md")
    words = _words(skill)
    assert "pb worker send --help" in skill and "pb worker settle --help" in skill
    assert "after one coherent patch" in words and "before the next patch begins" in words
    assert "A clock tick or an unchanged idle state is not a work boundary" in words
    assert "not necessary after each read-only command" in words
    assert "never interrupts an in-flight model response, edit, or command" in words
    assert "Lease renewal (`pb worker renew --message-ref ... --lease-id ...`) only extends a lease already received" in words
    assert "pb worker context --project-ref <project-ref>" in skill
    assert "the command refuses more" in words
    assert "a file written through a heredoc whose delimiter is not quoted" in words
    assert "Everything you write is Markdown" in words
    assert "pb coordinate plan.item.update" in skill
    assert "Keep the runtime-selected notification path live until detach" in words
    assert "only `pb worker receive` and settlement prove model handling" in words
    assert "An empty inbox is not evidence that there is no work" in words
    assert "correction that must survive an unread inbox" in words
    assert "belongs in the assigned plan item" in words
    assert "Mail wakes the worker" in words
    assert "A queued control is intent; only the service's receipt proves a transition" in words
    # One report per prompting event, and the receipt is read, not the exit code (2026-09-20, three refusals in thirty minutes).
    assert "citing as `--source-event-ref` the event that prompted this report" in words
    assert "A source event is spent by the first report that cites it" in words
    assert "refused as `work_event_idempotency_conflict`" in words
    assert "the later mail, item revision or result event, never that notice again" in words
    assert "exits 0 only when the service accepted the report" in words
    assert "A refusal exits nonzero with the service's code and message" in words
    assert "field_assignment_report_outcome_unknown" in skill
    assert "pb worker outbox-status --outbox-id <id>" in skill
    assert "the request may already have applied" in words
    assert "retry the same report unchanged" in words
    assert "A `project.report` request reaches only the coordinator" in words
    assert "work:journal:<created-at>:<entry-id>:<semantic-name>" in skill
    assert "pb worker journal-index" in skill and "pb worker journal-search" in skill
    # The semantic segment cap that refused a real entry on 2026-09-21 00:26Z.
    assert "semantic name at most 64 characters of `a-z0-9-`, else `journal_entry_ref_invalid`" in _words(skill)
    assert "pb worker journal-index-status" in skill
    assert "pb worker journal-index-resume" in skill
    assert "Status is observation only" in words
    assert "Do not rerun the original command to guess what happened" in words
    assert "pb worker journal`" not in skill
    assert "pb worker idle" in skill and "pb worker detach" in skill
    assert "pb mail-renew --project-ref" not in skill
    assert "pb work-update" not in skill
    assert "pb worker await" not in skill
    assert "complete local `project_packet`" not in skill


def test_project_report_reference_holds_the_contract() -> None:
    report = _read("references/project-report.md")
    words = _words(report)
    assert "pb worker project-report preview" in report
    assert "pb worker project-report publish" in report
    assert "Do not settle. The lease is the only way to publish again" in words
    assert "field_project_report_refused" in report
    for reason in ("moved", "blocked", "cancelled_dependency", "mentioned", "dependency_of_moved"):
        assert f"`{reason}`" in report
    assert "An outbox id is not a receipt" in report


def test_repository_sharing_rules() -> None:
    # The change-request model (operator ruling 2026-09-22, W262): the skill
    # carries what every worker does, the collaboration reference carries the
    # rules, their reasons and the rehearsal log.
    skill = _read("SKILL.md")
    words = _words(skill)
    assert "(references/collaboration.md)" in skill
    assert "One working tree per agent" in words
    assert "Never edit a shared checkout except to land an approved change" in words
    assert "Work on a branch, exchange through a change request" in words
    assert "work/<wN>-<short-slug>" in skill
    assert "push it yourself" in words
    assert "Commit each coherent piece as you finish it" in words
    assert "The coordinator merges after approval and pushes the integration ref" in words
    assert "Deploying stays the operator's" in words
    assert "A branch is closed by its merge, a later push is a new change request" in words
    assert "Publish your intent before the first edit" in words
    assert "workspace.shared_write.list" in skill
    assert "workspace.shared_write.publish" in skill
    assert "workspace.shared_write.clear" in skill
    assert "kind=source_in_flight" in skill
    assert "send an overlap to the coordinator" in words
    assert "The dashboard grants nothing and blocks nothing" in words
    assert "Clear your dashboard entry when the change request is open" in words
    assert "TTL is recovery" in words
    assert "git merge-base --is-ancestor origin/main" in words
    assert "regression written for a finding fails without the fix" in words
    assert "list every place the rule you changed is enforced" in words
    assert "Approval is a board mail naming the head, quoted on the change request" in words
    assert "Reporting an item complete means its change request is merged" in words
    # Documentation is part of the item that changes the behaviour it describes (operator, 2026-09-21 09:39Z).
    assert "when behaviour a doc describes changes, the doc changes in the same item" in words
    assert "because undocumented behaviour is how a diagnosis goes wrong" in words
    assert "One home per concept, one-line pointers elsewhere, no links to gitignored paths" in words
    # Landing into a shared checkout stays the interim on a machine that still
    # shares one. Its steps moved to the collaboration reference's Interim
    # section in 2026.09.23.1 (gate 8: content moves, prose is not compressed);
    # the skill keeps the trigger line and the two never-commands.
    collaboration = _read("references/collaboration.md")
    collaboration_words = _words(collaboration)
    assert "follows the Interim steps in [collaboration](references/collaboration.md)" in words
    assert "take a bounded turn for a shared Git operation" in collaboration_words
    assert "Prepare and verify in a private index" in collaboration_words
    assert "GIT_INDEX_FILE" in collaboration
    # The refresh must run outside the private index (two stale shared indexes, 2026-09-21 00:31Z).
    assert "env -u GIT_INDEX_FILE git read-tree HEAD" in collaboration
    assert "Never `git add -A`, never `git stash`" in words
    assert "Never `git add -A`, never `git stash`" in collaboration_words
    # 2026.09.23.1: containment both ways, count comparison, fresh-environment claims.
    assert "compare your count with the author's" in words
    assert "a suite that skips what the change touches is green about everything except the change" in words
    assert "the files the change request lists and the files you read" in words
    assert "git merge-base --is-ancestor <commit> origin/main" in words
    assert "The acceptor runs it on their own clone" in words
    assert "A journal entry that says landed names that merge commit and is written after it is fetched" in words
    assert "settled by installing it into a fresh environment at the named commit" in words
    # Retired with the ruling: pushing a work branch is the author's act.
    assert "Pushing is the operator's decision" not in words
    assert "Being able to push is not being allowed to" not in words


def test_collaboration_reference_carries_the_round_two_gate_clauses() -> None:
    # Revision 2026.09.23.1, from round 2 findings thirteen to eighteen: what a
    # reviewer, merger, acceptor and reporter each do, with the case behind it.
    collaboration = _words(_read("references/collaboration.md"))
    assert "A gate names what a reader does to satisfy it, with a pointer to the means, or it is not a gate yet" in collaboration
    assert "The approval states two counts and reconciles them" in collaboration
    assert "compare your count with the author's and ask about the difference" in collaboration
    assert "settled by installing it into a fresh environment at the named commit" in collaboration
    assert "checked line by line against the item's acceptance text" in collaboration
    assert "the report is a claim and the clone is the evidence" in collaboration
    assert "dependency preflight in `procedures/testing.md`" in collaboration
    # The round-by-round log moved to the project's journal (W340 P4a): it named
    # the project's hosts, agents and change requests. The rules keep what each
    # finding changed; the page says where the log lives.
    assert "The entries go in the project's journal, one per finding, not in this page" in collaboration
    assert "which a public procedure never does (Rule 9)" in collaboration
    assert "finding sixteen" in collaboration and "finding eighteen" in collaboration


# The dependency preflight and the application's testing procedure live with the
# application's own suite. The package's testing procedure and its test arrive
# with revision 2026.09.23.3 (W255 acceptance, the package's procedures folder).
def test_operator_runtime_and_conduct_rules() -> None:
    skill = _read("SKILL.md")
    words = _words(skill)
    assert "Mail to a coordinator makes nothing visible to the operator" in words
    assert "before one hour passes during active work without a visible update" in words
    assert "question blocked decision delivery_failed progress reply update result" in words
    assert "work_mail_kind_invalid" in skill
    assert "executed by the coordinator" in words
    assert "A relay restart is host-local: the agents on that host agree, then the coordinator on that host restarts it, or on a host without one the agents pick one of themselves" in words
    assert "A container-local patch is not an action this team has" in words
    assert "(references/runtime-actions.md)" in skill and "(references/test-window.md)" in skill
    assert "(references/coordinator.md)" in skill
    assert "an agent types `pb relay-service install` only after the operator approves it" in words
    assert "the item wins, and whoever sent the message fixes the item" in words
    assert "Stop and say which two things conflict, quoting both" in words
    assert "that worker hears it before anyone else" in words
    assert "Report the mechanism" in words
    assert "State the mechanism, not a category" in words
    assert "say what you do not know and what would settle it" in words
    # A status is held to the grounding discipline (operator, 2026-09-19).
    assert "A status is held to the same discipline" in words
    assert "Keep apart what happened, what is happening now, and what comes next" in words
    assert "Keep a measured problem apart from a thought" in words
    assert "done, in progress, planned, or blocked, and blocked names what on" in words
    assert "Problem Board infrastructure problems are the most critical unless the operator has prioritised something else" in words
    assert "by key with its title" in words
    assert "every number in it came from a command run in that pass" in words
    assert "Coordination does not waive this duty" in words
    assert "re-read the complete package" in words
    # The capture does not wait for the operator to ask (2026-09-20).
    assert "files it against this package before it settles the work it learned it in" in words
    assert "The capture does not wait for the operator to ask" in words
    assert "A claim that a case cannot occur is grounded the same way" in words
    # Two self-inflicted pipeline cuts read as system defects in one day (2026-09-20).
    assert "is a number about the filter until the command has run once without it" in words
    assert "never an append-only note" in words
    assert "The coordinator names one researcher for a question" in words


def test_situational_references_open_on_their_trigger() -> None:
    runtime = _read("references/runtime-actions.md")
    window = _read("references/test-window.md")
    wake = _read("references/claude-code-wake.md")
    assert "Read this before asking the coordinator for a reload, refresh or restart" in runtime
    profile = _profile()
    assert "kdcube refresh --path \"$REPO\" --build" in profile
    assert "--maintainer-local-python-package DIST=SOURCE" in profile
    assert "Verify in the running artifact, not in the checkout" in runtime
    assert "pb source status" in runtime
    assert "the reload returns before that build finishes" in _words(profile)
    assert "Ask what it released" in runtime
    coordinator = _words(_read("references/coordinator.md"))
    assert "Read this when you are about to accept, return or cancel a submission" in coordinator
    assert "work_review_self_forbidden" in coordinator
    assert "an `idempotency_key` you generate for this decision" in coordinator
    assert "Read the dashboard first" in coordinator
    # The row is intent, git is history (coordinator, 2026-09-20 22:12Z).
    assert "The row says what a worker is about to change and `git status` says what has changed" in coordinator
    assert "and a tree that is dirty anywhere, holds the action" in coordinator
    assert "whether or not git shows the named path yet" in coordinator
    # A declared row over a clean tree is the safe moment, not the unsafe one (2026-09-20 22:21Z).
    assert "The same row with a clean tree is a worker that has declared and not begun" in coordinator
    assert "ask its owner, now or after, and act on the answer" in coordinator
    assert "Collect one `ready` or `hold` from every attending worker" in coordinator
    assert "neither is the guarantee" in coordinator
    assert "A bundle reload returns before the widget build finishes" in _words(profile)
    assert "Verify the deployed artifact, never the commit" in coordinator
    assert "Say what loaded" in coordinator
    # The four things the first list did not carry (coordinator review, 2026-09-20).
    assert "A `hold` names what releases it" in coordinator
    assert "Running without its answer is allowed only when" in coordinator
    assert "the announcement records the missing answer and that reason" in coordinator
    assert "diff the touched entry against the live `config/bundles.yaml` first" in _words(profile)
    assert "never by the first match of a block" in _words(profile)
    assert "a widget has three states after a reload" in _words(profile)
    assert "that case cannot occur" in coordinator
    # A reload row asks for a stage, it does not hold one (coordinator 2026-09-20 23:11Z, filed 2026-09-21).
    assert "A `reload` row is a request and never a hold" in coordinator
    assert "targets `bundle:<id>` and `procedure:<package>@<revision>`" in coordinator
    assert "Read this when the coordinator relays a test window" in window
    assert "Only the coordinator deploys" in window
    assert "Read this when starting or resuming a Claude Code worker" in wake


def test_delivery_reference_keeps_its_runtime_facts() -> None:
    delivery = _read("references/delivery-and-recovery.md")
    words = _words(delivery)
    assert "Codex and Claude Code use different native notification paths" in delivery
    assert "not a second delivery mechanism" in words
    assert "an idle Codex session ends its model turn" in words
    assert "selected session owns exactly one background attachment" in delivery
    assert "leaves the watch running until detach" not in words
    assert "a scheduled guard prompt replaces the watch before its cap" in words
    assert "the same missing checks mean its watch has stopped" in words
    assert "projects[]         {project_ref, revision, leased_messages}" in delivery
    assert "acquired_leases[]" in delivery
    assert "`items[]` has no `project_packet` key" in delivery
    assert "persistent login relay owns the `codex-queue` subscription" in _read("SKILL.md")


def test_shared_runtime_state_rulings_are_read_where_state_is_designed() -> None:
    # Operator rulings 2026-09-21: nothing durable in Redis, and shared state is
    # written only when its source changed, at that moment.
    skill = _words(_read("SKILL.md"))
    assert "For Redis or other shared state, read [shared runtime state](references/shared-runtime-state.md)" in skill
    state = _words(_read("references/shared-runtime-state.md"))
    assert "Redis holds only projections that can be rebuilt from a durable source" in state
    assert "A write happens only when its source changed, at the moment it changed" in state
    assert "a check that decides whether to write, and a background republish loop" in state
    assert "the reader reads through to that source and answers from it" in state
    assert "treats a record from another run as a miss" in state
    assert "it is a blocker" in state


def test_failure_diagnosis_is_layered_and_reached_from_the_skill() -> None:
    skill = _words(_read("SKILL.md"))
    delivery = _read("references/delivery-and-recovery.md")
    assert "or a call fails at an unclear layer, read [delivery and recovery]" in skill
    assert "## Diagnosing A Failure Layer By Layer" in delivery
    assert "Compare with a working peer at the same time." in delivery


def test_route_points_only_at_what_the_worker_can_read() -> None:
    coordinator = _read("references/coordinator.md")
    assert "## Put what a worker must read where that worker can read it" in coordinator
    assert "belongs in the item" in coordinator
    assert "Never send a local filesystem path as the carrier" in coordinator
    assert "field_attachments_operator_only" in coordinator


def test_skill_keeps_the_small_facts_that_compression_removed() -> None:
    # Round 2, finding eleven: three repacks to hold the line budget removed
    # these facts one clause at a time. Each is pinned so the budget is met by
    # moving content to a reference, never by tightening prose.
    words = _words(_read("SKILL.md"))
    for fact in (
        "accepts `--format brief` anywhere on the line",
        "and for a question or request the correlated `send`",
        "renders saved output the same way",
        "the collaboration procedure, [collaboration](references/collaboration.md), revised one rehearsal round at a time",
        "on a machine where the shared checkout is also the live `pb` runtime an edit there is live for every worker at once",
        "GitHub sees one account for all agents and refuses its own author",
        "`ERROR <code>` is not a receipt, and its code decides the retry",
        "unclaimed or refused before any write is known, outcome unknown repeats under the same `idempotency_key`",
    ):
        assert fact in words, fact
    # The reference carries what the skill only names, and the three error
    # classes by code (codex-ui, review of #21: not every ERROR is outcome unknown).
    brief = _words(_read("references/brief-output.md"))
    assert "The recovery is the same request under the same `idempotency_key`" in brief
    assert "A fresh key is a second write" in brief
    assert "`work_coordinate_relay_unavailable`, after the client has checked that no claim happened" in brief
    assert "The outcome is known, nothing applied" in brief
    assert "`work_coordinate_outcome_unknown` names this class on the `pb coordinate` path" in brief
    assert "refused before any write, on its shape or its admission" in brief
    assert "a code it does not know is treated as outcome unknown" in brief
    assert "`observed_revision` is in every receipt, applied or refused" in brief
    assert "never taken from a wrapped fragment, and never composed from a key and a title" in brief


def test_revision_2026_09_23_3_carries_the_brief_rule_and_the_w265_lines() -> None:
    # Finding twenty: codex-main compacted every few turns because settlement
    # commands printed full JSON envelopes into its context. The skill states
    # the brief rule with its reason and the session-wide export; the reference
    # carries the words. The research paragraph moved to the coordinator
    # reference to make room (gate 8: content moves, prose is not compressed).
    words = _words(_read("SKILL.md"))
    assert "`export PB_FORMAT=brief` once before the first command" in words
    assert "settle and send included" in words
    assert "compacts every few turns" in words
    assert "The coordinator names one researcher for a question" in words
    brief = _words(_read("references/brief-output.md"))
    assert "Bounded output is the session default" in brief
    assert "export PB_FORMAT=brief" in brief
    assert "settlement commands print full JSON bodies instead of --format brief" in brief
    coordinator = _words(_read("references/coordinator.md"))
    assert "Research Is Coordinated Progressively" in coordinator
    assert "A second investigation starts only when the coordinator or operator names a specific reason" in coordinator
    # W265: the reconnecting channel's new messages and the receipt rule for a send.
    delivery = _words(_read("references/delivery-and-recovery.md"))
    assert "`work_send_channel_reconnecting`" in delivery
    assert "A send counts as delivered only when it returns a receipt" in delivery
    assert "the relay's cycle waits ten seconds for it" in delivery
    assert "`event=awaiting_reconnect`" in delivery


def test_testing_procedure_runs_the_packaged_dependency_preflight_before_the_suite() -> None:
    # The preflight moved into the package with the procedures that name it,
    # so a published-package reader has the script the procedure runs.
    testing = (PACKAGE_ROOT / "src" / "project_board" / "procedures" / "testing.md").read_text(encoding="utf-8")
    script = PACKAGE_ROOT / "src" / "project_board" / "procedures" / "dependency_preflight.py"
    assert script.is_file()
    assert "project_board/procedures/dependency_preflight.py" in testing
    assert "$PB/procedures/dependency_preflight.py" not in testing


def test_testing_procedure_separates_the_fast_pr_gate_from_the_merge_gate() -> None:
    testing = _words(
        (PACKAGE_ROOT / "src" / "project_board" / "procedures" / "testing.md").read_text(
            encoding="utf-8"
        )
    )

    assert "Before a pull request" in testing
    assert "touched test files" in testing
    assert '`-n auto -m "not slow"`' in testing
    assert "Before merge" in testing
    assert "disposable PostgreSQL database" in testing
    assert "PROBLEM_BOARD_HOST_PYTHON" in testing
    assert "`-n 8`" in testing
    assert "default 100-connection budget" in testing
    assert "injected clock, event, or retry schedule" in testing
    assert "@pytest.mark.slow" in testing
    assert "--durations=20" in testing


def test_operator_input_goes_through_the_board_not_a_terminal_prompt() -> None:
    # Operator, 2026-09-23: "in PB the agents cannot be sure the operator is
    # looking into their terminals. and if there are inputs needed, the agent
    # must send this in project chat to operator, or if urgent then also in
    # telegram." The skill points at the rule, the collaboration reference
    # carries it, and first-run names the launch flag.
    skill = _words(_read("SKILL.md"))
    assert "Ask for their input this way, never in a terminal prompt (collaboration Rule 11)" in skill
    collaboration = _words(_read("references/collaboration.md"))
    assert "Rule 11. The operator is asked on the board, and on Telegram when it is urgent" in collaboration
    assert "send it as mail to `operator` in the project conversation" in collaboration
    assert "send it as `question`, `decision` or `blocked`" in collaboration
    first_run = _words(_read("references/first-run.md"))
    # The official start command carries the flag (operator, 2026-09-26).
    assert "--disallowedTools AskUserQuestion" in first_run
    assert "`claude --resume <session-uuid>` with the same flags resumes one" in first_run


def test_coordinator_checks_a_silent_worker_instead_of_waiting() -> None:
    # Operator, 2026-09-23: "you every time are calm while the workers might
    # be idle for a long time and you even do not check their status."
    coordinator = _words(_read("references/coordinator.md"))
    assert "Check a silent worker, do not wait for it" in coordinator
    assert "`~/.codex/queue_1.sqlite` `queued_items`" in coordinator
    assert "Codex takes a queued wake only when its current turn ends" in coordinator
    assert "silence is not progress" in coordinator


def test_coordinator_rebalances_work_when_a_worker_runs_short_on_tokens() -> None:
    # Operator, 2026-09-24 and 2026-09-25: token pressure changes routing
    # immediately, while scarce workers on hosts with unique resources are
    # preserved as the hands for work that only those hosts can perform.
    coordinator = _words(_read("references/coordinator.md"))
    operator = _words(
        (OPERATIONAL_PROCEDURE_ROOT / "operator.md").read_text(encoding="utf-8")
    )

    assert "Worker budgets" in coordinator
    assert "usage and limit line on its worker card" in coordinator
    assert "what the worker reports and what the operator says" in coordinator
    assert "keep a small routing inventory in the project's facts or environment page" in coordinator
    assert "the machine-local resources and capabilities" in coordinator
    assert "workers that can act as hands on that host" in coordinator
    assert "workers that share a provider account or quota, grouped as one quota pool" in coordinator
    assert "Their limits are coupled, not independent capacity" in coordinator
    assert "Record `Not known yet` instead of assuming" in coordinator
    assert "Reserve scarce host-local workers" in coordinator
    assert "do not spend the last capable local worker on portable work" in coordinator
    assert "Route portable work, including review, research and planning" in coordinator
    assert "to another host or an independent quota pool first" in coordinator
    assert "the reset is not soon enough for the work, replan before exhaustion" in coordinator
    assert "If the work can safely wait for an imminent reset, wait instead of churning ownership" in coordinator
    assert "Do not build a scheduler or assign token scores" in coordinator
    assert "these three questions are the whole rule" in coordinator
    assert "acts without waiting to be asked" in coordinator
    assert "Move its unstarted work to a worker with budget left" in coordinator
    assert "hand over the exact branch and head" in coordinator
    assert "only work it can finish cheaply" in coordinator
    assert "Apply the same rule to the coordinator" in coordinator
    assert "project.coordinator.hand_over" in coordinator
    assert "project.coordinator.return" in coordinator
    assert "project reports route to that holder" in coordinator
    assert "where those operations are not live" in coordinator
    assert "W313 is the planned durable coordinator handover mechanism" not in coordinator
    assert "The runtime reported the worker's current token capacity" in operator
    assert (
        "[the coordinator reference]"
        "(./problem-board-worker/references/coordinator.md)"
    ) in operator


def test_coordinator_sets_a_teammate_up_to_work() -> None:
    # Operator, 2026-09-24: the team prepares missing setup instead of making
    # each worker spend its budget rediscovering it.
    coordinator = _words(_read("references/coordinator.md"))

    assert "Set a teammate up to work" in coordinator
    assert "development environment, access grant, repository, or piece of project context" in coordinator
    assert "update the owning procedure for setup shared by projects" in coordinator
    assert "update the project's facts or environment page" in coordinator
    assert "ask the teammate who already knows the answer" in coordinator
    assert "turns every reported gap into a procedure or project-page fix" in coordinator
    assert "welcomes it, names the project's prepared context" in coordinator


def test_the_project_journal_accumulates_everything_from_day_one() -> None:
    # Operator, 2026-09-24: all project knowledge is journal-readable by every
    # attending agent, including a project that starts with no prior record.
    coordinator = _words(_read("references/coordinator.md"))
    setup = _words(
        (OPERATIONAL_PROCEDURE_ROOT / "first-time-setup.md").read_text(
            encoding="utf-8"
        )
    )

    assert "Keep everything known in the project journal" in coordinator
    assert "project journal home is the team's complete shared record" in coordinator
    assert "operator rulings with their reasons" in coordinator
    assert "runtime-window outcomes" in coordinator
    assert "Whoever learns a project-wide fact writes a journal entry" in coordinator
    assert "a successor coordinator begins by searching the journal" in coordinator
    assert "Starting a project" in coordinator
    assert "creates `project-facts.md` and `project-environment.md`" in coordinator
    assert "either the current fact or `Not known yet`" in coordinator
    assert "Automatic page seeding by the board is a product follow-up" in coordinator
    assert "[Starting a project](./problem-board-worker/references/coordinator.md#starting-a-project)" in setup


def test_coordinator_stays_reachable_through_every_window() -> None:
    # Operator, 2026-09-24: "if i did not write to you now that would stand
    # still forever?" The coordinator's watch had expired during a window.
    coordinator = _words(_read("references/coordinator.md"))
    assert "Stay reachable through every window" in coordinator
    assert "including during a runtime window" in coordinator
    assert "runs as a background check with a bounded end" in coordinator
    assert "receive immediately, before the all-clear" in coordinator


def test_first_run_states_what_a_relay_restart_does_with_each_refusal():
    """W292: a restart retries transient refusals once and parks dead credentials."""

    text = (PROCEDURE_ROOT / "references" / "first-run.md").read_text(encoding="utf-8")
    section = text[text.index("## `session_reconnecting`"):text.index("## `session_attending`")]
    assert "`data_bus_connect_refused`) once at once" in section
    assert "never a faster one" in section
    assert "`oauth_token_request_failed` answered with `invalid_grant`" in section
    assert "stays parked across restarts" in section
    assert "token endpoint that was down (a 5xx) is tried at once" in section
    assert "`attempted`, `kept_backoff` or `parked_permanent`" in section


def test_every_activation_is_addressed_to_the_approved_commit_and_its_receipt_is_checked():
    """W202: the shared checkout can change between the preflight and the
    staging, so the activation names a commit and the coordinator compares the
    receipt with the approved candidate before it verifies anything else."""

    coordinator = (PROCEDURE_ROOT / "references" / "coordinator.md").read_text(encoding="utf-8")
    actions = (PROCEDURE_ROOT / "references" / "runtime-actions.md").read_text(encoding="utf-8")

    assert "Every activation is addressed to a commit" in coordinator
    assert "whatever it holds at that instant. The\nlist is what makes that survivable" not in coordinator
    assert "the approved commit per tree" in coordinator
    profile = _profile()
    assert "`git -C <deploy-worktree> checkout --detach <sha>` and `kdcube bundle reload <bundle-id>`" in " ".join(profile.split())
    assert "**Execute** the action as the runtime's profile gives it, at the commit the ref names" in " ".join(coordinator.split())
    assert "**check the receipt against the approved candidate**" in " ".join(coordinator.split())
    assert "A receipt that names another commit is a failed\n   activation: report it as failed, with both commits" in coordinator
    assert coordinator.index("the receipt against the approved candidate") < coordinator.index("6. **Verify the deployed artifact, never the commit.**")
    # The preflight stays, as evidence and explicitly not the guarantee.
    assert "neither is the\n   guarantee" in coordinator
    assert "`git -C <deploy-worktree> checkout --detach <approved-sha>`, then `kdcube bundle reload <bundle-id>`" in profile
    assert "a reload of an app whose path is a working checkout, which stages whatever that checkout holds at that instant" in profile


def test_an_app_deploys_from_its_deploy_worktree_never_from_a_working_checkout():
    """W202, procedure 2026.09.25.11: since the 2026-09-25 23:23Z window every
    app loads from a deploy worktree that nobody edits, checked out at the
    approved commit; web requests, the Data Bus workers and a restart all read
    it. activation.commit is not the guarantee: a restart and the Data Bus
    workers ignore it (W333)."""

    actions = _profile()

    step = actions[actions.index("## Execute (coordinator step 5)"):actions.index("## Verify in the running artifact")]
    assert "The working checkouts are never an app's path" in _words(step)
    assert "a restart and the Data Bus workers ignore it (W333)" in _words(step)
    assert "set `activation.commit" not in step and "--commit <sha>" not in step
    assert step.index("checkout --detach") < step.index("**check")
    assert "`git -C <deploy-worktree> rev-parse HEAD` is the commit on disk" in " ".join(step.split())
    app_row = next(line for line in actions.splitlines() if line.startswith("| An app under `apps/`"))
    assert "**deploy worktree**" in app_row and "is the app's only path" in app_row
    assert "`kdcube bundle <bundle-id> --tenant <t> --project <p> --local-path <container-path>`" in app_row
    assert "which a restart and the Data Bus workers ignore, so it is not the guarantee until W333 lands" in app_row
    assert "--commit" not in app_row
    # Review on #153: a leftover activation block still wins a commitless
    # reload in the web proc, so the entry carries none and the receipt says so.
    assert "carries **no `activation` block** (neither `commit` nor `require_commit`)" in app_row
    assert "`Loaded: mounted tree at head <approved-sha>, clean`" in app_row
    assert "`--local-path`, which keeps an existing `activation` block" in app_row
    flat = " ".join(step.split())
    assert "remove any `activation` block, `commit` or `require_commit`" in flat
    assert flat.index("remove any `activation` block") < flat.index("checkout --detach")
    assert "a `Loaded: snapshot of` line is a failed activation" in flat
    widget_row = next(line for line in actions.splitlines() if line.startswith("| Widget `src/`"))
    assert "--commit" not in widget_row and "the app deploy below" in widget_row


def test_the_worker_host_path_holds_on_a_host_that_has_none_of_it():
    """W147: add-a-worker-host.md is the path a fresh headless host follows,
    so every command must work there without anything a person remembers."""

    from project_board.contract.worker_operation_contract import (
        PROBLEM_BOARD_OPERATIONS,
        required_grants_for_operation,
    )

    text = (OPERATIONAL_PROCEDURE_ROOT / "add-a-worker-host.md").read_text(encoding="utf-8")

    # The install source exists before the install reads it.
    clone = text.index("git clone -q https://github.com/elenaviter/app-ecosystem.git ~/src/app-ecosystem")
    assert clone < text.index("APP_REPOSITORY=/home/<user>/src/app-ecosystem")
    assert "/home/<user>/workspaces/app-ecosystem" not in text
    # Step 6 probes the current environment step 2 installed, never a pipx one.
    assert "pipx" not in text
    assert 'PB_PYTHON="$HOME/.kdcube/client-runtime/tools/problem-board/releases/current/venv/bin/python"' in text
    # The procedure is verified after the source it belongs to is selected.
    step3 = text[text.index("## 3. Configure"):text.index("## 4. Keep")]
    assert step3.index("pb source use-code") < step3.index("pb procedure install") < step3.index("pb procedure verify")
    assert "A stale or unverifiable package is a defect in this\npath" in step3
    # What the install brings and what it does not.
    assert "They do not install Problem Board itself" in text
    # Which step creates the Card.
    assert "only the last creates\na Card" in text
    # Every numbered step names the machine it runs on.
    for number in range(15):
        heading = text.index(f"\n## {number}. ")
        assert text[heading:heading + 300].count("Runs on:") == 1, number
    # The minimum grant names what the contract requires.
    relay = {op for op in PROBLEM_BOARD_OPERATIONS if list(required_grants_for_operation(op)) == ["work:relay"]}
    assert {"worker.heartbeat", "control.pull", "control.worker_settle", "mail.route", "assignment.report"} <= relay
    for op in ("project.plan.item", "plan.notes.list"):
        assert list(required_grants_for_operation(op)) == ["work:observe"], op
    assert "the grant `work:relay`" in text and "`work:observe` to read the plan" in text
    # A worker session never stops on a question nobody watches, started or resumed.
    assert text.count("--disallowedTools AskUserQuestion") >= 2


def test_the_stop_hook_rearms_a_missed_watch() -> None:
    # W182 line 0, 2026-09-24: a missed re-arm costs one turn, independent of the model noticing.
    first_run = _words(_read("references/first-run.md"))
    wake = _words(_read("references/claude-code-wake.md"))

    assert '"command": "pb worker stop-guard"' in first_run
    assert "the hook blocks the stop once and names the exact commands that re-arm the watch" in first_run
    assert "`pb procedure install --target claude-code` adds this hook with the lines above" in first_run
    assert "A missed re-arm then costs one turn" in wake
    assert "A detached or retired worker, any other session on the host, and a failure inside the hook end normally" in wake


def test_the_start_step_reads_the_project_facts_page() -> None:
    # W262, 2026-09-24: the coordinator lost where spark1 runs and how to reach it
    # to a compaction. The standing facts live on one page the context names.
    # Its hosts, agents and client release stay true across sessions and are
    # lost with a compacted context, so the start step reads it first.
    skill = _words(_read("SKILL.md"))
    assert "the project facts page (`project_facts_ref`)" in skill


def test_an_agent_searches_the_plan_and_the_journal_before_acting_on_a_subject() -> None:
    # W305, 2026-09-24: asked to connect the agents on spark1, the coordinator
    # followed a stale runbook and missed the host's own record, although both
    # were in the journal. The search existed, and no step told an agent to run it.
    skill = _words(_read("SKILL.md"))
    assert "Before acting on a named subject (a host, a feature, an item), search for what the project already knows about it" in skill
    assert "`project.plan.search` for the plan and `pb worker journal-search --query <subject>` for the journal of the project you attend" in skill
    assert "For the subject of the task, search the plan and the journal (Choose A Relevant Next Action)." in skill


def test_connecting_agents_on_another_machine_starts_at_the_operator_decisions() -> None:
    # W305, 2026-09-24: the same request skipped step 0, so nobody asked the
    # operator for agent names, repositories or GitHub access.
    skill = _words(_read("SKILL.md"))
    assert "Connecting agents on another machine follows the add-a-worker-host procedure" in skill
    assert "from its step 0, where the operator decides names, repositories and access before anything changes" in skill


def test_an_agent_sets_up_its_workspace_from_the_project_record():
    """W304 finding 39: attending a project brings its record, and the agent clones from it."""

    skill = " ".join((source_package_path() / "SKILL.md").read_text(encoding="utf-8").split())
    reference = " ".join((source_package_path() / "references" / "project-workspace.md").read_text(encoding="utf-8").split())
    assert "whenever you are added to a project, set up its workspace from its record: [project workspace](references/project-workspace.md)" in skill
    assert "pb worker context --project-ref <project>" in reference
    assert "the repository lives at `<workspace>/<alias>`" in reference
    assert "The journal repository (role `journal`) is cloned like any other" in reference
    assert "tell the operator by name, with its alias and URL" in reference


def test_an_assignment_notice_is_settled_after_working_not_after_completion() -> None:
    # W304 finding 51, 2026-09-25: the reaction settled the notice last, after
    # `completed`, but `pb worker renew` refuses a lease held past
    # MAX_MAIL_HOLD_SECONDS (an hour) and redelivers it. claude-ops's W313
    # notice was refused that way mid-work.
    from project_board.client import store

    assert store.MAX_MAIL_HOLD_SECONDS == 3600
    skill = _words(_read("SKILL.md"))
    reaction = skill[skill.index("The reaction, in order:"):skill.index("`working` and `blocked` are progress reports;")]
    assert "2. Report `working` (it sets Working), settle the notice's lease at once (no lease outlives an hour; the row carries the work)" in reaction
    assert "Settle the notice's lease once." not in reaction
    delivery = _words(_read("references/delivery-and-recovery.md"))
    assert "**A lease is held for at most an hour, renewals included** (`MAX_MAIL_HOLD_SECONDS`, 3600)." in delivery
    assert "`field_mail_held_too_long`" in delivery
    assert "An assignment notice is settled right after `working` is accepted" in delivery


def test_project_environment_is_the_final_workspace_setup_step() -> None:
    reference = _words(_read("references/project-workspace.md"))
    assert "read the environment page that the same `pb worker context` result names as `project_environment_ref`" in reference
    assert "build and prove the environment from that page on demand, before the first test or build you run, and before interpreting a test failure" in reference
    assert "The team adds the setup or correction to the project page" in reference


def test_the_coordinator_role_is_handed_over_with_its_note_and_addressed_as_a_role() -> None:
    # W313 step 6: when to hand over, what the note contains, what the
    # successor does first, and the role address.
    coordinator = _words(_read("references/coordinator.md"))
    assert "## Hand the coordinator role over, and take it back" in _read("references/coordinator.md")
    assert "`--recipient coordinator` with `--project-ref`" in coordinator
    assert "pb coordinate project.coordinator.note.write" in coordinator
    for section in (
        "runtime_windows", "merge_queue", "operator_waits", "promised_notifications",
        "blocked_on", "research_owners", "onboarding_checks", "integrators",
    ):
        assert f"`{section}`" in coordinator
    assert "Every section is required, `none` when there is nothing" in coordinator
    assert "press **Make coordinator** on the successor" in coordinator
    assert "The note then says `not_supplied`" in coordinator
    assert "pb coordinate project.coordinator.get --object-ref <project-ref>" in coordinator
    assert "marked `redirected_from`" in coordinator
    assert "presses **Make worker** on you" in coordinator
    assert "A successor that must run a runtime window has to be able to deploy: an agent on the host that runs the runtime" in coordinator
    collaboration = _words(_read("references/collaboration.md"))
    assert "Rule 8 covers work items. The coordinator role itself moves by" in collaboration
    skill = _words(_read("SKILL.md"))
    assert "Mail for whoever coordinates goes to `--recipient coordinator`" in skill


def test_a_routed_item_carries_its_dependencies_from_the_filing_call() -> None:
    # Operator ruling, 2026-09-25: six items went out without dependencies and
    # their order lived only in one coordinator's head.
    coordinator = _words(_read("references/coordinator.md"))
    assert "5. Set the new item's dependencies in the same call. `depends_on` lists the `identity_ref` of every item that must land first" in coordinator
    assert 'goes in the description as "Related:"' in coordinator
    assert "Recheck both directions when you rescope an item or split out a step." in coordinator


def test_the_coordinator_reference_opens_with_what_the_coordinator_is_for() -> None:
    # W313 handover finding 5, 2026-09-25: codex-coord became acting
    # coordinator after reading coordinator.md, did every act correctly and
    # never spoke to the operator. The job lived only in the outgoing
    # coordinator's private memory, so it is written here, first.
    raw = _read("references/coordinator.md")
    body = raw.split("\n---\n", 1)[1]
    headings = [line for line in body.splitlines() if line.startswith("## ")]
    assert headings[0] == "## What the coordinator is for"
    coordinator = _words(raw)
    for phrase in (
        "The coordinator works for the operator.",
        "You speak to the operator; the operator should not have to ask.",
        "The operator is your principal.",
        "A previous coordinator stops directing the operator and answers only mail addressed to it by name.",
        "Keep the operator informed unasked",
        "ask with a notifying kind (`decision`, `question`, `blocked`)",
        "Answer every message the operator sends through the board with a correlated reply before continuing.",
        "situation, then verdict",
        "Name items by key and title, never a bare number.",
        "You drive the team; you do not wait for it.",
        "Ask the worker; do not infer from files.",
        "tell that worker first, then the operator",
        "The runtime is the operator's; the mechanics are yours.",
        "No runtime window (reload, refresh, client switch) without the operator's go.",
        "Do not ask the operator about the mechanics.",
        "[Worker budgets](#worker-budgets)",
    ):
        assert phrase in coordinator, phrase
    # The successor reads it before anything else it inherits.
    assert (
        "0. Read [What the coordinator is for](#what-the-coordinator-is-for) at the "
        "top of this file. From now on you speak to the operator."
    ) in coordinator
    # And the skill sends a role holder there first.
    skill = _words(_read("SKILL.md"))
    assert (
        "When you hold the coordinator role (home or acting), read its first "
        "section, What the coordinator is for, before anything else"
    ) in skill


GENDERED_PRONOUNS = re.compile(r"\b(she|her|hers|herself|he|him|his|himself)\b", re.IGNORECASE)
QUOTED = re.compile(r'"[^"\n]*"|\u201c[^\u201d\n]*\u201d|`[^`\n]*`')


def test_the_procedure_names_no_gender_for_the_operator_or_anyone() -> None:
    # Operator, 2026-09-25: "i hope she is not part of procedure. because
    # operator can be also he." The procedure says "the operator" or
    # "they"; a quoted ruling stays verbatim, so text inside quotation marks
    # and inline code is exempt. The check covers the class, every file of the
    # package, not the lines fixed when it was found.
    package = source_package()
    found = []
    for relative in [package["entrypoint"], *package["references"]]:
        for number, line in enumerate(_read(relative).splitlines(), 1):
            for match in GENDERED_PRONOUNS.finditer(QUOTED.sub("", line)):
                found.append(f"{relative}:{number}: {match.group(0)!r} in {line.strip()[:100]}")
    assert found == [], "\n".join(found)


def test_a_window_that_refreshes_and_moves_an_app_checks_the_app_out_first():
    """W304 U3: the refresh loads the app at startup; a later reload keeps cached submodules."""

    profile = _words(_profile())
    step = profile.split("## Execute (coordinator step 5)", 1)[1].split("## Verify", 1)[0]
    assert "**When one window refreshes the platform and moves an app**" in step
    assert "check the app's deploy worktree out at its approved commit **before** `kdcube refresh --build`, then refresh" in step
    assert "`ImportError: card_delegable_grants`" in step
    assert step.index("**before** `kdcube refresh") < step.index("**check the receipt")


def test_runtime_actions_orders_a_platform_rebuild_before_a_board_that_moves_operations():
    """2026-09-26 03:32-03:36Z: the board would not load after W326.

    A board reload alone met a platform image whose operation contract lacked
    `review.assign`, and the board refuses to load while its handlers and that
    contract differ. The procedure says the order, and its diff check must find
    every operation id in the contract, or a change of format would hide one.
    """

    runtime = _profile()
    assert "Problem Board operation policy and handler table differ" in runtime
    assert "check the approved board commit out in its deploy worktree **without reloading**" in runtime
    assert "a platform rebuild before the board commit is checked out" in runtime
    assert "services/operation_dispatch.py" in runtime
    command = re.search(r"grep -E '(?P<pattern>[^']+)'", runtime.split("## Does This Change Move Board Operations?", 1)[1])
    assert command, "the check command is in the section"
    pattern = command.group("pattern").replace("\\{", "{").replace("[[:space:]]", r"\s")
    pattern = pattern.replace("{", r"\{")

    from project_board.contract import worker_operation_contract as contract

    source = Path(contract.__file__).read_text(encoding="utf-8")
    added = {line.split('"')[1] for line in ("+" + row for row in source.splitlines()) if re.match(pattern, line)}
    assert added == set(contract.PROBLEM_BOARD_OPERATION_POLICIES), sorted(
        added ^ set(contract.PROBLEM_BOARD_OPERATION_POLICIES)
    )


def test_a_project_declares_its_instructions_and_runtimes_and_actions_release_a_ref():
    """W262 lines 1-3, 6: project data, not skill text; every action releases a named ref."""

    skill = " ".join(_read("SKILL.md").split())
    actions = " ".join(_read("references/runtime-actions.md").split())
    coordinator = " ".join(_read("references/coordinator.md").split())
    assert "the project's instructions file (`project_instructions_ref`: what the project is" in skill
    assert "who triggers it and the ref it releases in each repository it loads (`releases`)" in skill
    assert "the commands live in the runtime's profile (`local_profile`), never here, and a project with none has no runtime actions" in skill
    assert "Every action loads, per repository, the commit its ref names, never a working tree, and its result names each repository, ref and commit." in skill
    assert "## Project Runtimes" in _read("references/runtime-actions.md")
    assert "**A runtime action releases its refs.**" in actions
    assert "**Its result names what loaded:** each repository, its ref, and the commit the ref named when it loaded." in actions
    assert "1. **Integrate onto the named ref first.**" in coordinator
    assert coordinator.index("1. **Integrate onto the named ref first.**") < coordinator.index("2. **Read the dashboard first**")
    assert "On one machine this is the same step" in coordinator and "On several machines" in coordinator


KDCUBE_RUNTIME_COMMANDS = (
    "kdcube refresh",
    "bundle reload",
    "bundle config apply",
    "--maintainer-local-python-package",
    "bundles.yaml",
    "bundles.template.yaml",
    "deploy worktree",
    "--local-path",
    "refresh --build",
)


def test_the_generic_worker_procedure_carries_no_kdcube_runtime_command():
    """W262 line 8: a worker on an independent project receives no refresh or
    bundle instruction; the KDCube commands live in the KDCube runtime profile."""

    documents = [PROCEDURE_ROOT / "SKILL.md", *sorted((PROCEDURE_ROOT / "references").glob("*.md"))]
    assert len(documents) > 2
    found = [
        f"{document.relative_to(PROCEDURE_ROOT)}: {command!r}"
        for document in documents
        for command in KDCUBE_RUNTIME_COMMANDS
        if command in " ".join(document.read_text(encoding="utf-8").split())
    ]
    assert found == [], "\n".join(found)


def test_the_kdcube_maintainer_profile_holds_the_runtime_actions():
    """W262 line 4: the profile reproduces refresh with package selectors and bundle reload."""

    profile = _profile()
    words = _words(profile)
    assert "id: kdcube.procedures.runtime-profile-maintainer" in profile
    assert "kdcube refresh" in profile
    assert "--maintainer-local-python-package" in profile
    assert "kdcube bundle reload" in profile
    assert "`bundle config apply`" in profile
    assert "`profile_ref`" in profile and "`local_profile`" in profile
    assert "**Every action releases, in each repository it loads, the commit its ref names**" in words
    assert "**Its receipt names each of those commits**" in words
    assert "A reload is always addressed to a commit" in words
    # The generic package points there and says the commands are the profile's.
    actions = _words(_read("references/runtime-actions.md"))
    assert "(repo:app-ecosystem/products/kdcube/procedures/runtime-profile-maintainer.md)" in actions
    assert "are in that runtime's profile, never on this page" in actions
    assert "A reload without a commit stages the working tree" not in actions

def test_delegation_is_not_free_and_its_reason_is_stated():
    """Operator 2026-09-25: delegate only when net positive; no polling; independent pools first."""

    coordinator = " ".join(_read("references/coordinator.md").split())
    assert "**Delegation is not free** (operator, 2026-09-25)." in coordinator
    assert "only when the parallel work is net positive after the overhead of briefing it, reviewing what comes back and settling it" in coordinator
    assert "It does not wait on a delegate with repeated short empty checks or status polls" in coordinator
    assert "Portable work goes first to an independent, less used quota pool" in coordinator
    assert "Why: a delegation that costs more to brief and check than it saves spends the same shared budget twice" in coordinator


def test_the_kdcube_profile_gives_deploy_worktrees_a_gitdir_the_container_can_read():
    """W262 line 4 proof (dev-main 2026-09-26): an absolute gitdir left the receipt without git evidence."""

    words = " ".join(_profile().split())
    assert "### A deploy worktree the container can read" in _profile()
    assert "`git -C <checkout> worktree add --relative-paths <deploy-worktree> <sha>`" in words
    assert "Before 2.48 (dev-main runs 2.43): after `git worktree add`, rewrite the worktree's `.git` file" in words
    assert "`head -1 <deploy-worktree>/.git` starts with `gitdir: ../`, never `gitdir: /`" in words
    assert "a `Loaded: mounted tree` line with `(no git evidence: ...)` is a failed proof because it names no commit" in words


def test_the_kdcube_profile_verifies_with_the_platform_attestations():
    """W31: verify by version and symbol comparisons, and stop on MISMATCH or UNKNOWN."""

    profile = _profile()
    section = profile[profile.index("## Verify in the running artifact"):profile.index("## Who clears a refresh")]
    words = " ".join(section.split())
    assert "`kdcube info --workdir <workdir>`" in words
    assert "**A descriptor-only apply** (a config change with the commit unchanged) is not proven by `MATCH`" in words
    assert "`kdcube bundle catalog check --workdir <workdir>`" in words
    assert "`kdcube bundle status <bundle-id> --live --json --workdir <workdir>`" in words
    assert "`pb source status`" in words
    assert "**`MATCH`** is the proof; **`MISMATCH`** (the command exits nonzero) or **`UNKNOWN`**" in words
    assert "is a stop" in words
    assert "never timestamps" in words
    before = " ".join(profile[profile.index("### The host `kdcube` CLI is current"):profile.index("## Execute (coordinator step 5)")].split())
    assert "A CLI older than the platform's attestation commits prints no \"Source Attestation\" section" in before
    assert "`git -C <kdcube checkout> rev-parse HEAD` equals it" in before
    assert "`git -C <kdcube checkout> status --porcelain --untracked-files=no` is empty (tracked changes only" in before
    # The hand checks it replaces are gone.
    assert "`dist/` inside the container carries the new source" not in words


def test_a_review_verdict_reaches_the_author_by_board_mail():
    # 2026-09-26: a verdict posted only as a review comment on applications#165
    # reached nobody, and a one-test request sat unseen for 1 h 45 min.
    collaboration = _words(_read("references/collaboration.md"))
    assert "the verdict also goes to the author by board mail (the head, the verdict, the link)" in collaboration
    assert "Mail may point at it and carries the verdict" not in collaboration


def test_the_descriptor_sync_step_ticks_new_operations_on_the_project_control_card():
    # W331 (operator, 2026-09-26): a refresh after new operations reached the
    # catalog did not help until they were ticked on the project Control Card.
    profile = " ".join(_profile().split())
    assert "a project admin ticks the new operations on the project Control Card" in profile
    assert "work_worker_operation_withheld_by_control_card" in profile


def test_a_review_is_routed_in_the_turn_it_arrives():
    # 2026-09-26: W15 waited in Review for hours with the coordinator as default
    # reviewer while its last check needed the operator's browser.
    coordinator = " ".join(_read("references/coordinator.md").split())
    assert "Route a review in the turn it arrives.**" in coordinator
    assert "board mail of kind `decision`, so it reaches their Telegram" in coordinator
    assert "whenever `review.look_at` or `review.could_not_verify` needs a person's browser or account" in coordinator
    assert "tell the operator at once with a notifying kind (`blocked`)" in coordinator
    assert "never mention it only inside a longer list" in coordinator


def test_the_skill_says_which_mail_kinds_reach_the_operators_telegram():
    # 2026-09-26: a Telegram test sent as `reply` never reached Telegram.
    skill = " ".join(_read("SKILL.md").split())
    assert "`progress`, `update`, `reply` and `result` stay on the board" in skill
    assert "only `question`, `decision`, `blocked`, `delivery_failed` reach their Telegram" in skill


def test_cards_are_edited_only_in_connection_hub():
    # Operator ruling 2026-09-26: the board had built a second Card editor that
    # kept its own copy of the decision and overwrote Connection Hub edits.
    collaboration = " ".join(_read("references/collaboration.md").split())
    assert "## Rule 12. Cards are edited only in Connection Hub" in collaboration
    assert "It never builds its own Card editor, and it never keeps a stored copy of the decision" in collaboration
    assert "declared with the operations in the service catalog" in collaboration


def test_new_operations_are_ticked_on_the_project_control_card_before_refresh():
    coordinator = " ".join(_read("references/coordinator.md").split())
    assert "a project admin ticks them on the project Control Card in Connection Hub first, and only then refreshes Cards" in coordinator
    assert "`work_worker_operation_withheld_by_control_card`" in coordinator


def test_knowledge_goes_to_the_journal_or_the_procedure_not_private_memory():
    coordinator = " ".join(_read("references/coordinator.md").split())
    assert "Project state and rulings go in the project journal" in coordinator
    assert "Practice that helps any coordinator or worker goes in this procedure" in coordinator
    assert "Private agent memory holds only that agent's personal preferences" in coordinator



def test_an_agent_attends_one_project_at_a_time():
    # Operator confirmed 2026-09-26: the board refuses a link while the agent attends another project.
    skill = " ".join(_read("SKILL.md").split())
    assert "An agent attends one project at a time" in skill
    assert "several projects" not in skill
    assert "several projects" not in " ".join(_read("references/project-workspace.md").split())



def test_the_reviewer_operator_is_any_person_and_operator_name_is_one():
    coordinator = " ".join(_read("references/coordinator.md").split())
    assert "`operator` is any person in the project" in coordinator
    assert "Use `operator:<user id>` only when a particular person must look" in coordinator



def test_a_board_release_bumps_the_apps_release_record():
    coordinator = " ".join(_read("references/coordinator.md").split())
    assert "**Bump the app's release record.**" in coordinator



def test_a_behaviour_change_carries_its_public_documentation():
    collaboration = " ".join(_read("references/collaboration.md").split())
    assert "## Rule 13. A behaviour change carries its documentation in the same change" in collaboration
    assert "The reviewer checks it and refuses a behaviour change without it" in collaboration



def test_journaling_and_signals_are_opened_from_the_skill():
    skill = " ".join(_read("SKILL.md").split())
    assert "[journaling](references/journaling.md)" in skill
    assert "[signals](references/signals.md)" in skill
    assert "(repo:app-ecosystem/products/project-board/docs/README.md)" in skill


def test_a_second_person_approves_with_the_device_flow_and_the_agent_detects_it():
    # 2026-09-26: a second person's enrollment opened the first person's browser,
    # and the agent waited to be told "done".
    skill = " ".join(_read("SKILL.md").split())
    assert "or when the approving person signs in with a different browser or account" in skill
    assert "confirm the approval yourself with `pb worker inspect`" in skill
    first_run = " ".join(_read("references/first-run.md").split())
    assert "Use `--device` as well whenever the person approving is not the one signed in" in first_run
    assert "do not wait to be told \"done\"" in first_run


def test_the_workspace_comes_from_the_host_root_never_the_session_folder():
    # 2026-09-26: an agent started in a shared checkout was handed that checkout.
    skill = " ".join(_read("SKILL.md").split())
    assert "never the folder this session started in and never a path you choose" in skill
    assert "report it with `pb worker workspace-report`" in skill
    workspace = " ".join(_read("references/project-workspace.md").split())
    assert "else `<agent workspace root>/<your alias>` (`workspace_source: host_root`)" in workspace
    assert "`pb host configure --agent-workspace-root`" in workspace


def test_rehearsing_an_official_flow_gives_no_hints():
    # Operator, 2026-09-26: prototyping is guided, a rehearsal is not.
    coordinator = " ".join(_read("references/coordinator.md").split())
    assert "**Rehearsing an official flow gives no hints.**" in coordinator
    assert "the agent uses only the installed client and skill" in coordinator
    assert "then the agent re-reads the skill and continues from it" in coordinator


def test_the_official_command_starts_an_agent_session():
    # Operator, 2026-09-26: the person-facing enroll page, exactly this simple.
    enroll = " ".join((PROCEDURE_ROOT.parent / "enroll-an-agent.md").read_text(encoding="utf-8").split())
    assert "## 1. Create the agent's workspace, then start it" in enroll
    assert 'cd "$HOME/.kdcube/pb/workspaces/$ALIAS" && claude --add-dir "$HOME/.kdcube" --dangerously-skip-permissions --disallowedTools AskUserQuestion' in enroll
    assert 'codex -C "$HOME/.kdcube/pb/workspaces/$ALIAS" --sandbox danger-full-access --ask-for-approval never --search' in enroll
    assert "## 2. Enroll it to the pool" in enroll and "Prefer `--device`" in enroll
    assert "## 3. Connect it to your project" in enroll
    skill = " ".join(_read("SKILL.md").split())
    assert "enroll-an-agent.md" in skill
    first_run = " ".join(_read("references/first-run.md").split())
    assert "enroll-an-agent.md" in first_run
    host = " ".join((PROCEDURE_ROOT.parent / "add-a-worker-host.md").read_text(encoding="utf-8").split())
    assert "[enroll an agent](./enroll-an-agent.md)" in host


def test_rehearsal_gaps_are_closed():
    # Onboarding rehearsal on .11, 2026-09-26: six places the skill left an agent guessing.
    skill = " ".join(_read("SKILL.md").split())
    assert "What `pb worker context` names is read after you attend a project (step 7)" in skill
    assert "empty means the project has none yet, so read the facts page and ask the coordinator" in skill
    assert "chronicle" not in skill.split("## Receive Addressed Input")[0]
    workspace = " ".join(_read("references/project-workspace.md").split())
    assert "Onboarding does not build it" in workspace


def test_step_5_starts_the_watch_the_guard_can_find():
    # W345, 2026-09-26 16:10Z: a coordinator followed step 5 literally, a
    # background shell running a bare `pb worker watch`. The guard finds
    # watches by the session id in their command line, so it could neither
    # replace nor end that one, and a plain background shell has no cap and
    # posts no end notice.
    skill = _read("SKILL.md")
    step_5 = skill.split("   - **Claude Code:**", 1)[1].split("\n6. ", 1)[0]
    [step_command] = re.findall(r"```bash\n\s*(.+?)\n\s*```", step_5)
    wake = _words(_read("references/claude-code-wake.md"))
    guard = re.search(
        r"start a fresh watch with the Monitor tool \(`(?P<command>[^`]+)`, timeout (?P<timeout>\d+) ms\)",
        wake,
    )
    assert guard, "the guard prompt names its Monitor command and timeout"
    assert step_command == guard["command"]
    words = _words(step_5)
    assert "with the Monitor tool, `timeout_ms` 1800000" in words
    assert guard["timeout"] == "1800000"
    assert "for why a background shell is not the facility" in words
    assert "A plain background shell (`run_in_background`, `&`, `nohup`) is not the facility" in wake
    assert "A bare `pb worker watch` carries no session id in its command line" in wake
    # The guard's pattern, with <id> bound, finds the step-5 watch and not a bare one.
    [pattern] = re.findall(r'pgrep -d, -f "([^"]+)"', wake)
    session = "398cdfe7-d80d-41fe-bc3b-22fb8823335f"
    finds = re.compile(pattern.replace("<id>", re.escape(session)))
    assert finds.search(step_command.replace("<id>", session))
    assert not finds.search("pb worker watch")


def test_a_shared_contract_change_runs_the_other_repositorys_suite():
    # W352: a client change the board app's tests encode was approved on the
    # package suite alone, and eight board tests failed on main.
    collaboration = " ".join(_read("references/collaboration.md").split())
    gate = collaboration[collaboration.index("## Rule 5. The merge gate"):collaboration.index("4. **Runtime import path")]
    assert "A change to a contract another repository's code or tests exercise" in gate
    assert "runs that repository's suite against the change's head too, author and reviewer both, before approval" in gate
    assert "the counts name both repositories" in gate


def test_a_new_agent_is_reachable_before_it_attends_a_project():
    # W304 finding 24 and decision 3: the watch runs before approval, so the
    # approval arrives as its event; and no-project mail reaches an agent that
    # attends none, by its exact stable name.
    skill = " ".join(_read("SKILL.md").split())
    step_4 = skill[skill.index("4. Follow `next`"):skill.index("5. Establish the notification path")]
    assert "Claude Code: start the step-5 watch before this step, so the approval arrives as its `control_plane.connected` event" in step_4
    assert "Without a project, only an agent that attends none can be mailed, by that name (`request`, `reply` or `ping`)" in skill
    wake = " ".join(_read("references/claude-code-wake.md").split())
    assert "Start the watch right after `pb worker listen`, before `pb worker authorize`." in wake
    host = " ".join((PROCEDURE_ROOT.parent / "add-a-worker-host.md").read_text(encoding="utf-8").split())
    assert "while one of them attends no project, anyone who knows its exact stable name (never its alias) may send it a request, reply or ping" in host


def test_the_merger_retargets_a_stacked_change_request_and_proves_the_merged_tree():
    # W354: a stacked change request merged into its base branch after that
    # base landed, so its content never reached main; and a merge on a tested
    # merged tree replaced a rebase round, proven by comparing trees.
    coordinator = " ".join(_read("references/coordinator.md").split())
    merge = coordinator[coordinator.index("### Merge"):coordinator.index("## Release a stalled assignment")]
    assert "Before merging a change request whose base is not the integration branch, retarget it to `main` (`gh pr edit <number> --base main`), or merge it only after its base has merged and it has been retargeted." in merge
    assert "Never merge a stacked change request into its base branch after that base landed" in merge
    assert "When approved heads are behind `main`, the merger may test the exact merged tree instead of asking for a rebase" in merge
    assert "run both repositories' suites on that tree (gate 3)" in merge
    assert "After merging, prove `main`'s tree equals the tested tree." in merge
    assert "git rev-parse origin/main^{tree}" in merge
    collaboration = " ".join(_read("references/collaboration.md").split())
    gate_2 = collaboration[collaboration.index("2. **Base current:**"):collaboration.index("3. **Suites green")]
    assert "The merger may instead test the exact merged tree" in gate_2
    assert "(`coordinator.md`, Merge)" in gate_2
    # The operator's rule of 2026-09-26 20:45Z: two change requests claimed the
    # same revision twice in one day, so the merger sets it, never the author.
    assert "**The merger sets the procedure revision; authors never bump it.**" in merge
    assert "The merger sets the next revision at merge time, in merge order, with one commit on the merged branch" in merge
    gate_3 = collaboration[collaboration.index("3. **Suites green"):collaboration.index("4. **Runtime import path")]
    assert "`test_package_content_is_recorded_for_its_revision` skips on an author's head with the reason \"procedure changed; the merger sets the revision\"" in gate_3
    assert "`PB_REQUIRE_REVISION_RECORDED=1`" in merge
    skill = " ".join(_read("SKILL.md").split())
    assert "leave the revision to the merger" in skill
    assert "advance the package revision" not in skill
    agent_worker = " ".join((PROCEDURE_ROOT.parent / "agent-worker.md").read_text(encoding="utf-8").split())
    assert "The merger sets that revision at merge time, in merge order; an author never bumps it" in agent_worker
    assert "PB_REQUIRE_REVISION_RECORDED=1" in agent_worker


def test_worker_budgets_name_the_usage_field_and_where_the_caps_live() -> None:
    # W351: the coordinator could not find usage figures from the CLI across
    # hosts; the reference now names the field and where the operator's caps are.
    coordinator = _words(_read("references/coordinator.md"))

    assert "`team[].limit_state.windows[]` carries each window's `name`, `used_percent` and `resets_at`" in coordinator
    assert "one `team usage:` line per member" in coordinator
    assert "caps per quota pool" in coordinator
    assert "live on the project's facts page" in coordinator
