"""W563 command scope (ballot 11 Oct 08:05 UTC, P1/P2/P7 base direction; narrowed Source phase 08:20 UTC).

What this pins is the text a worker reads: the CLI help of the two commands it confuses, Rule 16's availability
step, and the delivery reference's transport wording. It does not prove that a worker routes correctly; that needs
the separately authorized behaviour rehearsal on W563.
"""

from __future__ import annotations

import re

from project_board.client import cli
from project_board.client.procedures import source_package_path

RULE_16 = "references/collaboration/rule-16-every-task-has-a-living-route-and-each-actor-knows-i.md"


def _worker_help(name: str) -> str:
    worker = cli.build_parser()._subparsers._group_actions[0].choices["worker"]
    for action in worker._subparsers._group_actions:
        for choice in getattr(action, "_choices_actions", []):
            if choice.dest == name:
                return " ".join(str(choice.help).split())
    raise AssertionError(f"pb worker {name} has no help entry")


def _availability_step() -> str:
    text = (source_package_path() / RULE_16).read_text(encoding="utf-8")
    match = re.search(r"^3\. \*\*Read availability now.*?(?=^4\. )", text, flags=re.MULTILINE | re.DOTALL)
    assert match, "Rule 16 step 3 is missing"
    return " ".join(match.group(0).split())


def test_worker_context_help_names_the_cross_host_team_view():
    text = _worker_help("context")
    assert "across hosts" in text
    assert "--routing" in text and "--member" in text


def test_worker_list_help_says_it_is_host_local_not_availability():
    text = _worker_help("list")
    assert "host-local only" in text
    assert "not team availability" in text


def test_rule_16_reads_availability_from_the_project_view_not_the_host_list():
    step = _availability_step()
    assert "pb worker context --project-ref <project> --routing" in step
    assert "--member <stable name>" in step
    # The host list is named only as diagnostics, never as an availability source.
    assert "`pb worker list` is this host's relay and watch diagnostics only" in step
    assert "(`pb worker context`, `pb worker list`)" not in step
    # An online row does not lift an operator's instruction; unknown is neither available nor limited.
    assert "does not lift an operator's OFF or paused instruction" in step
    assert "neither available nor limited" in step
    assert "Identity and native ownership do not expire" in step


def test_delivery_reference_separates_transport_from_reading():
    text = " ".join((source_package_path() / "references/delivery-and-recovery.md").read_text(encoding="utf-8").split())
    assert "neither means the recipient read it" in text
    assert "base64 text and state the SHA-256 of the decoded bundle" in text
    assert "Transport never rewrites the author's Source" in text
