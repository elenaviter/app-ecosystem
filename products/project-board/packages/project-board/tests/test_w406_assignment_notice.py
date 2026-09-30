"""W406: an assignment notice asks for what the item's committed status allows.

An assignee is who the item is with in every status. A Done or Cancelled item's
assignee is informed, a Review item's assignee waits for the review, and only
Todo or Working is work to begin. Every identifier is synthetic.
"""

from __future__ import annotations

import pytest

from project_board.client.assignment_notice import assignment_notice_text

WORK_REF = "work:plan:node:20260930T090000Z:w406:synthetic"
ASSIGNMENT_REF = "work:assignment:assignment-w406"

# The notice every board sent before W406, byte for byte. A board that sends
# no status keeps getting it.
LEGACY_BODY = (
    "You have been assigned work.\n\n"
    f"Work item: {WORK_REF}\n"
    f"Assignment: {ASSIGNMENT_REF}\n"
    "Ownership version: 3\n\n"
    "Read the item, do the work, report against this assignment ref "
    "and this ownership version, and commit what you wrote. This is "
    "work to begin, not a notification to acknowledge.\n\n"
    "  pb coordinate project.plan.item --object-ref <project-ref> "
    "--payload-json '{\"item_key\":\"<Wn>\"}'\n"
    "  pb worker report --assignment-ref <assignment> "
    "--ownership-version <version>\n"
)


def _notice(status):
    return assignment_notice_text(
        status=status, work_ref=WORK_REF, assignment_ref=ASSIGNMENT_REF, ownership_version=3
    )


def test_a_board_that_sends_no_status_gets_the_notice_it_always_got():
    subject, body, reaction = _notice("")
    assert subject == f"Assignment available: {WORK_REF}"
    assert body == LEGACY_BODY
    assert reaction == "begin_work"


@pytest.mark.parametrize("status", ["done", "cancelled"])
def test_a_finished_item_is_information_only(status):
    subject, body, reaction = _notice(status)
    label = status.title()
    assert reaction == "acknowledge_only"
    assert subject == f"Assigned a {label} item, for information: {WORK_REF}"
    assert "This notice is information only." in body
    assert "Do not start implementation, do not report working, and do not reopen the item or change its status" in body
    assert f"Item status: {label}" in body
    assert "a later edit to the item wins over this notice" in body
    assert "work to begin" not in body
    assert "pb worker report" not in body


def test_an_item_in_review_waits_for_the_review():
    subject, body, reaction = _notice("review")
    assert reaction == "await_review"
    assert subject == f"Assigned an item in Review: {WORK_REF}"
    assert "do not report working or restart the implementation" in body
    assert "pb worker report" not in body


@pytest.mark.parametrize("status, label", [("todo", "Todo"), ("working", "Working")])
def test_open_work_is_work_to_begin_and_names_its_status(status, label):
    subject, body, reaction = _notice(status)
    assert reaction == "begin_work"
    assert subject == f"Assignment available: {WORK_REF}"
    assert f"Item status: {label}" in body
    assert "work to begin" in body
    assert "a later edit to the item wins over this notice" in body
