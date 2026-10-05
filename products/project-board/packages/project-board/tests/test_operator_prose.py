"""Operator mail is refused when a person cannot read it (W563).

Why: operator mail reaches Telegram exactly as written. On 2026-10-05 a
decision mail arrived as one 1,400-character paragraph with "ALLCLEAR22:06",
"fresh215625" and "Require64GB" in it; the stored body was already glued.
Operator: "yes we need it", then "do not file - fix".
"""

from __future__ import annotations

import pytest

from project_board.client.prose_arguments import (
    OPERATOR_PARAGRAPH_MAXIMUM,
    refuse_unreadable_operator_prose,
)
from project_board.contract.errors import DomainError


THE_2026_10_05_MAIL = (
    "Bundle deployment is complete: coordinator ALLCLEAR22:06, source MATCH and independent live checks "
    "PASS. Require64GB logical Docker image plus measured host data. Coordinator owns isolated restore "
    "of fresh215625 backup before any cold window."
)


def test_the_glued_mail_of_2026_10_05_is_refused_naming_each_glued_word():
    with pytest.raises(DomainError) as refused:
        refuse_unreadable_operator_prose(THE_2026_10_05_MAIL, argument="--body-file")
    assert refused.value.code == "problem_board_operator_prose_glued"
    assert refused.value.details["glued"] == ["ALLCLEAR22:06", "Require64GB", "fresh215625"]


@pytest.mark.parametrize("text", [
    "ALL CLEAR 22:06 for Apps 1204f593 at W563, revision 5.",
    "Ref work:mail:20261005T220600Z:mail_c793f8f0:all-clear and path ~/.kdcube/logs/relay2.log.",
    "Digest sha256 109d8760, an ext4 disk, utf8 text, arm64 host, k8s and i18n.",
    "The literal `Apps1204f593` and a block:\n\n```\nALLCLEAR22:06 fresh215625\n```\n",
    "claude-code-0050e243 and codex-01a0d896 sent v2.1 to gpt-6.1-sol; W563 r54; 5h 98%.",
    # Review of 36eeb9b8: real heads from 2026-10-05 and our host name.
    "Merged as cabeb2f6 on main. AE bef2c40c returned. Runs on spark1 now, with Redis7.",
    "Python3.11, PG16, W563/PR544, IPv6, mp4, release 2026.10.05.9, dc2de69c and f09efbba.",
])
def test_readable_text_refs_terms_and_code_pass(text):
    assert refuse_unreadable_operator_prose(text, argument="--body") == text


def test_one_wall_paragraph_is_refused_and_short_paragraphs_pass():
    wall = "Word " * (OPERATOR_PARAGRAPH_MAXIMUM // 5 + 10)
    with pytest.raises(DomainError) as refused:
        refuse_unreadable_operator_prose(wall, argument="--body-file")
    assert refused.value.code == "problem_board_operator_prose_wall"
    paragraphs = "\n\n".join(["Word " * 100] * 5)
    assert refuse_unreadable_operator_prose(paragraphs, argument="--body-file") == paragraphs


def test_the_send_command_checks_only_operator_mail():
    from project_board.client import cli
    source = cli.__file__
    text = open(source, encoding="utf-8").read()
    assert "if direct_address in OPERATOR_RECIPIENTS:" in text
    assert 'refuse_unreadable_operator_prose(args.subject, argument="--subject")' in text


@pytest.mark.parametrize("glued", ["ALLCLEAR22:06", "fresh215625", "Apps1204f593", "Require64GB"])
def test_each_real_glue_of_2026_10_05_is_still_refused(glued):
    with pytest.raises(DomainError) as refused:
        refuse_unreadable_operator_prose(f"Status: {glued} is done.", argument="--body")
    assert refused.value.details["glued"] == [glued]
