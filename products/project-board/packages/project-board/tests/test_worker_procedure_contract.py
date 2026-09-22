"""The worker procedure package: one assertion per rule that stays.

The skill is condensed knowledge the model reads every time (operator ruling,
2026-09-18). Every rule is here as a short phrase so a rewrite that drops or
softens it fails, and nothing here pins story text, which the skill no longer
carries. Situational procedures live in references opened by one
if-this-read-that line each, and the package manifest ships them.
"""

from __future__ import annotations

import json
from pathlib import Path

from project_board.client.procedures import source_package, source_package_path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROCEDURE_ROOT = source_package_path()


def _read(relative: str) -> str:
    return (PROCEDURE_ROOT / relative).read_text(encoding="utf-8")


def _words(text: str) -> str:
    return " ".join(text.split())


REVISION_LEDGER = PACKAGE_ROOT / "procedure-revisions.json"


def test_package_content_is_recorded_for_its_revision() -> None:
    """A content change under an unchanged revision fails here.

    Why: installed copies compare revisions, so an edit that keeps the
    revision is invisible to every session that already installed it. On
    2026-09-22 the W257 edits to SKILL.md and first-run.md were committed under
    2026.09.22.4, which claude-main had installed minutes earlier, so no worker
    received them. The ledger records the source digest of each revision; a
    changed package needs a new revision and a new ledger line.
    """

    package = source_package()
    ledger = json.loads(REVISION_LEDGER.read_text(encoding="utf-8"))
    revision = package["revision"]
    recorded = ledger.get(revision)
    assert recorded is not None, (
        f"revision {revision} is not in {REVISION_LEDGER.name}; add "
        f'"{revision}": "{package["source_digest"]}"'
    )
    assert recorded == package["source_digest"], (
        f"the package content changed under revision {revision}; bump "
        f"package.json and record the new digest in {REVISION_LEDGER.name}"
    )


def test_package_manifest_ships_every_reference_the_skill_opens() -> None:
    package = json.loads(_read("package.json"))
    skill = _read("SKILL.md")
    assert package["revision"] == "2026.09.22.8"
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
    assert "append `--device`, never callback flags" in skill
    assert "the credential goes to the native store" in skill
    # The identifiers have one home, reached through the installed package's source root.
    assert "What The Setup Coordinates Mean" in first_run
    assert "This section is the owning definition" in first_run
    assert "setup guides point here rather than restating the meanings" in first_run
    assert "`pb` is the console command in the released `project-board` distribution" in first_run
    assert "run it after they approve" in first_run
    # A worker whose credential was refused is not attending (coordinator, 2026-09-21 10:41Z).
    assert "its channel is `pending_authorization`" in first_run
    assert "ask before proposing `pb relay-service start`. Do not reinstall it" in first_run
    assert "This state means the channel works (`active`) and the worker attends a project" in first_run


def test_skill_carries_rules_not_stories() -> None:
    skill = _read("SKILL.md")
    words = _words(skill)
    assert skill.count("\n") < 530, "the skill is condensed knowledge; grow a reference, not the skill"
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


def test_authority_and_next_action_rules() -> None:
    words = _words(_read("SKILL.md"))
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
    assert "ps -o pid=,etime= -p $(pgrep -d, -f \"worker watch.*<id>\")" in wake
    assert "sort -n | awk 'NR>1{print $2}'" in wake
    assert "pgrep -f \"worker watch.*<id>\" | sort -n | sed '$d'" not in wake.replace("(`sort -n | sed '$d'`)", "")
    assert "`ps -o etimes` is Linux only" in wake
    assert "A brief overlap of two watches costs nothing. A gap of one guard interval cost nineteen minutes" in wake
    assert "`stale` once it has stopped" in wake
    assert "`listener.state` does not move when the watch stops" in wake
    assert "queues one direct operator update through the worker's own outbox" in wake
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
    skill = _read("SKILL.md")
    words = _words(skill)
    assert "Commit each coherent piece as you finish it" in words
    assert "Read the shared-write dashboard only when you are about to change shared state" in words
    assert "workspace.shared_write.list" in skill
    assert "workspace.shared_write.publish" in skill
    assert "workspace.shared_write.clear" in skill
    assert "kind=source_in_flight" in skill
    assert "summary says what behavior or area is in flux and why it matters" in words
    assert "a Wn key carries its plain-language identity" in words
    assert "Targets name each concrete path, ref, bundle, or runtime operation" in words
    assert "another change could collide with" in words
    assert "The dashboard grants nothing and blocks nothing" in words
    assert "Do not poll an entry" in words
    assert "Take a bounded turn for a shared Git operation" in words
    assert "Prepare and verify in a private index" in words
    assert "GIT_INDEX_FILE" in skill
    assert "After advancing a ref by hand, refresh the main index" in words
    # The refresh must run outside the private index (two stale shared indexes, 2026-09-21 00:31Z).
    assert "env -u GIT_INDEX_FILE git read-tree HEAD" in skill
    assert "a line naming your paths means the shared index is behind" in words
    assert "Read `git diff --cached --stat` before every commit you make" in words
    assert "Reporting an item complete means its work is committed" in words
    # Documentation is part of the item that changes the behaviour it describes (operator, 2026-09-21 09:39Z).
    assert "when behaviour a doc describes changes, the doc changes in the same item" in words
    assert "because undocumented behaviour is how a diagnosis goes wrong" in words
    assert "One home per concept, one-line pointers elsewhere, and no links to ignored or private paths" in words
    assert "Pushing is the operator's decision, for that exact commit" in words
    assert "Being able to push is not being allowed to" in words
    assert "Clear your dashboard entry when the announced write is finished" in words
    assert "TTL is recovery" in words


def test_operator_runtime_and_conduct_rules() -> None:
    skill = _read("SKILL.md")
    words = _words(skill)
    assert "Mail to a coordinator makes nothing visible to the operator" in words
    assert "before one hour passes during active work without a visible update" in words
    assert "question blocked decision delivery_failed progress reply update result" in words
    assert "work_mail_kind_invalid" in skill
    assert "executed by the coordinator" in words
    assert "A client-source selection and relay restart are host-local: the agents on that host agree, then the coordinator on that host restarts it, or on a host without one the agents pick one of themselves" in words
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
    assert "kdcube refresh --path \"$REPO\" --build" in runtime
    assert "--maintainer-local-python-package DIST=SOURCE" in runtime
    assert "Verify in the running artifact, not in the checkout" in runtime
    assert "the reload returns before that build finishes" in _words(runtime)
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
    assert "A bundle reload returns before the widget build finishes" in coordinator
    assert "Verify the deployed artifact, never the commit" in coordinator
    assert "Say what loaded" in coordinator
    # The four things the first list did not carry (coordinator review, 2026-09-20).
    assert "A `hold` names what releases it" in coordinator
    assert "Running without its answer is allowed only when" in coordinator
    assert "the announcement records the missing answer and that reason" in coordinator
    assert "diff the touched entry against the live `config/bundles.yaml` first" in coordinator
    assert "never by the first match of a block" in coordinator
    assert "a widget has three states after a reload" in coordinator
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
