"""Inline prose on the command line is one line, or it comes from a file.

A double-quoted shell removes backticked spans and dollar names before `pb`
starts, so the command receives a well-formed string with a hole in it and
nothing downstream can tell. Three messages lost their identifiers that way
on one day, after the procedure had said to use ``--body-file``. A
rule the sender must remember at the moment of typing does not hold; a
property of the argument does. Every legitimate inline body in use is one
short line (``ready``, ``now``, ``hold``), and every loss was multiline
Markdown, so the line is drawn there: an inline prose argument that spans
lines is refused, naming the file alternative and the mechanism.

What this does not catch, and says so in the refusal: a single line with a
backtick or a dollar name, which the same shell alters the same way, and a
file whose content was already altered when it was written, which is what an
unquoted heredoc (``<<EOF``) does. One of the three losses was that case and
lies outside this guard by construction. Inline JSON (``--payload-json``)
cannot hold a raw newline inside a string, so its prose fields are checked
after parsing: a note or reason that decodes to several lines came through
the shell inline and is refused the same way.
"""

from __future__ import annotations

import argparse
import re
from typing import Any, Mapping

from ..contract.errors import DomainError

MECHANISM = (
    "a double-quoted shell removes backticked spans and dollar names before pb "
    "sees them, so a multiline body typed inline may already have lost text"
)
NOT_CAUGHT = (
    "What this check does not catch: a single line with a backtick or a dollar "
    "name, which the same shell alters the same way, and a file written through "
    "a heredoc whose delimiter is not quoted (<<EOF), which is altered before pb "
    "reads it. Put such a line in single quotes, and write files with <<'EOF'."
)

# argparse dests that carry free text a person wrote for another reader.
PROSE_DESTS = (
    "body",
    "summary",
    "review_look_at",
    "review_could_not_verify",
    "note",
    "reason",
    "text",
)

# Keys inside a --payload-json object that carry prose.
PROSE_PAYLOAD_KEYS = ("text", "reason", "summary", "note", "body")


def _flag(dest: str) -> str:
    return "--" + dest.replace("_", "-")


def _line_count(value: str) -> int:
    return value.count("\n") + 1


def require_single_line(
    value: Any, *, argument: str, file_argument: str | None
) -> Any:
    """Return ``value`` unchanged, or refuse it when it spans lines."""

    if not isinstance(value, str) or ("\n" not in value and "\r" not in value):
        return value
    lines = _line_count(value.replace("\r\n", "\n").replace("\r", "\n"))
    if file_argument:
        remedy = f"write it to a file and pass {file_argument}"
    else:
        remedy = "keep it to one line, or write the longer text where a file is accepted"
    raise DomainError(
        "problem_board_inline_prose_multiline",
        f"{argument} takes one line and this value spans {lines}: {remedy}. "
        f"Refused because {MECHANISM}. {NOT_CAUGHT}",
        details={
            "argument": argument,
            "lines": lines,
            "file_argument": file_argument or "",
            "mechanism": MECHANISM,
            "not_caught": "a single line containing a backtick or a dollar name, and a file written through an unquoted heredoc",
        },
    )


# A slot has a name: {{CUI_P2}}, {{ decision }}. Braces around nothing, JSON
# objects and a lone brace are not slots.
UNRESOLVED_SLOT = re.compile(r"\{\{\s*[A-Za-z_][A-Za-z0-9_.:-]{0,79}\s*\}\}")


def refuse_unresolved_slots(value: Any, *, argument: str) -> Any:
    """Return ``value`` unchanged, or refuse it when a template slot is still in it.

    On 2026-09-23 a consultation result went to the operator and three workers
    with twenty ``{{CUI_P2}}``-style slots where votes and decisions belonged,
    because the mail was built from the template and not from the filled file.
    A slot is never meant for a reader, so the send refuses it and names the
    first one, at the moment of typing and not from a reader's reply.
    """

    if not isinstance(value, str):
        return value
    found = UNRESOLVED_SLOT.findall(value)
    if not found:
        return value
    raise DomainError(
        "problem_board_unresolved_slot",
        f"{argument} still contains {len(found)} template slot(s), the first is "
        f"{found[0]}: fill the template before sending, a reader never gets a slot.",
        details={
            "argument": argument,
            "slots": sorted(set(found))[:20],
            "count": len(found),
        },
    )


def refuse_unresolved_payload_slots(payload: Mapping[str, Any], *, argument: str) -> None:
    """Refuse prose fields of a payload (inline or file) that still carry a slot."""

    for key in PROSE_PAYLOAD_KEYS:
        value = payload.get(key)
        if isinstance(value, str):
            refuse_unresolved_slots(value, argument=f"{argument} field {key!r}")


def guard_inline_prose(args: argparse.Namespace) -> None:
    """Refuse any multiline inline prose argument on a parsed command line.

    Runs before dispatch, so the sender learns at the moment of typing. A dest
    with a sibling ``<dest>_file`` names that file argument as the remedy.
    """

    for dest in PROSE_DESTS:
        if not hasattr(args, dest):
            continue
        file_dest = f"{dest}_file"
        file_argument = _flag(file_dest) if hasattr(args, file_dest) else None
        require_single_line(getattr(args, dest), argument=_flag(dest), file_argument=file_argument)


def guard_inline_payload_prose(payload: Mapping[str, Any], *, argument: str, file_argument: str) -> None:
    """Refuse prose fields of an inline JSON payload that decode to several lines."""

    for key in PROSE_PAYLOAD_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and ("\n" in value or "\r" in value):
            require_single_line(value, argument=f"{argument} field {key!r}", file_argument=file_argument)
