"""A completed report names who reviews it, and for a person the integration evidence (W326).

In review, the assignee who must act is the reviewer. The worker may name an
agent or the operator; with none named the acting coordinator reviews. A
person is named only once the work is merged and deployed (or needs no
deploy). The fields ride the report's review, so an older board keeps them
as extra review fields and changes nothing.
"""

from __future__ import annotations

import pytest

from project_board.client.cli import _review_routing, build_parser
from project_board.client.store import _review_routing_fields

REPORT = [
    "worker", "report", "--project-ref", "work:project:p", "--assignment-ref", "work:assignment:a",
    "--ownership-version", "1", "--state", "completed", "--summary", "done",
    "--source-event-ref", "work:mail:e", "--review-look-at", "run the tests",
    "--review-could-not-verify", "None",
]


def test_the_report_parser_takes_the_reviewer_and_the_evidence():
    args = build_parser().parse_args([
        *REPORT, "--reviewer", "operator", "--merged", "abc123,def456", "--deploy", "03:10Z window: board loads",
    ])
    assert _review_routing(args) == {
        "review_reviewer": "operator",
        "review_merged": "abc123,def456",
        "review_deploy": "03:10Z window: board loads",
        "review_nothing_to_deploy": False,
    }
    bare = build_parser().parse_args(REPORT)
    assert _review_routing(bare)["review_reviewer"] is None


def test_deploy_and_nothing_to_deploy_exclude_each_other():
    with pytest.raises(SystemExit):
        build_parser().parse_args([*REPORT, "--deploy", "w: c", "--nothing-to-deploy"])


def test_the_routing_fields_ride_the_review_in_their_stored_shape():
    assert _review_routing_fields(
        reviewer="operator", merged="abc123, def456", deploy="", nothing_to_deploy=True
    ) == {
        "reviewer": "operator",
        "integration": {"merged": ["abc123", "def456"], "nothing_to_deploy": True},
    }
    # Nothing named: nothing added, so the report is unchanged.
    assert _review_routing_fields(reviewer=None, merged=None, deploy=None, nothing_to_deploy=False) == {}
