"""W974: a repeated single-valued option is refused, never silently the last value.

On 24 Oct a `pb worker send` with three `--recipient` flags reached only the
last name: argparse keeps the last value of a repeated option. Every `pb`
parser now stores single-valued options with an action that refuses a second,
different value, and accepts the same value repeated.
"""

from __future__ import annotations

import argparse

import pytest

from project_board.client.cli import _StoreOnce, build_parser

SEND = ["worker", "send", "--kind", "reply", "--subject", "s", "--body", "b", "--idempotency-key", "k"]


def test_a_second_different_recipient_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(SEND + ["--recipient", "A", "--recipient", "B", "--recipient", "C"])

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "argument --recipient: given more than once with different values ('A', then 'B')" in err
    assert "send one message to each" in err


def test_the_same_recipient_repeated_is_one_recipient() -> None:
    args = build_parser().parse_args(SEND + ["--recipient", "A", "--recipient", "A"])

    assert args.recipient == "A"
    assert "_pb_options_given" not in vars(args), "the bookkeeping never reaches a command's arguments"


def test_any_other_single_valued_option_is_refused_too(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(SEND + ["--recipient", "A", "--project-ref", "p1", "--project-ref", "p2"])
    err = capsys.readouterr().err
    assert "argument --project-ref: given more than once" in err
    assert "send one message to each" not in err


def _parsers(parser: argparse.ArgumentParser, path: str = "pb"):
    yield path, parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                yield from _parsers(child, f"{path} {name}")


def test_no_pb_command_keeps_a_plain_last_value_store() -> None:
    # The audit the item asks for: every single-valued option of every pb command, addressing flags
    # (--recipient, --project-ref, --work-ref, --reply-to, --message-ref, --lease-id, --reviewer) included.
    plain = [f"{path} {'/'.join(action.option_strings) or action.dest}"
             for path, parser in _parsers(build_parser())
             for action in parser._actions
             if isinstance(action, argparse._StoreAction) and not isinstance(action, _StoreOnce)]
    assert plain == []
    commands = dict(_parsers(build_parser()))
    send_recipient = next(a for a in commands["pb worker send"]._actions if a.dest == "recipient")
    assert isinstance(send_recipient, _StoreOnce)
