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
    "deploy",
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
FENCED_CODE = re.compile(r"(^|\n)(`{3,}|~{3,})[^\n]*\n.*?\n\2[ \t]*(?=\n|$)", re.S)
INLINE_CODE = re.compile(r"`[^`\n]*`")


def _without_code(value: str) -> str:
    """The text with fenced blocks and inline code spans removed."""

    return INLINE_CODE.sub(" ", FENCED_CODE.sub("\n", value))


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
    # Code is quoted, not rendered: a fenced block that lists the slots a
    # reviewer found, a GitHub Actions ${{ secrets.X }} or a Jinja example is
    # text about slots and passes. Only prose is checked.
    found = UNRESOLVED_SLOT.findall(_without_code(value))
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


# A word of three or more letters run straight into a number, outside code:
# "ALLCLEAR22:06", "Apps1204f593", "fresh215625". A ref is not prose: one that
# starts after ":", "/", "@", ".", "#" or "-" (work:mail:..., a path, an
# address, a version) is never matched, and a short ref like W563 has one letter.
GLUED_WORD = re.compile(r"(?<![\w:/@.#-])([A-Za-z]{3,})(\d[\w:.%]*)")
# Real terms that end in digits. Anything else is written with a space, or put
# in backticks when it is a literal.
GLUED_TERMS = frozenset({
    "sha1", "sha224", "sha256", "sha384", "sha512", "base32", "base64", "utf8", "utf16", "utf32",
    "arm64", "amd64", "ipv4", "ipv6", "ext2", "ext3", "ext4", "http2", "http3", "int8", "int16",
    "int32", "int64", "uint8", "uint16", "uint32", "uint64", "float16", "float32", "float64",
    "win32", "win64", "i386", "oauth2", "h264", "h265", "k8s", "i18n", "l10n", "a11y", "python3",
    "pip3", "gzip2", "bzip2", "md5sum", "sha256sum", "x509", "pkcs7", "pkcs8", "pkcs12", "aes128", "aes256",
})
COMMIT_HASH = re.compile(r"[0-9a-f]{7,40}")


def _glued(match: re.Match[str]) -> bool:
    """A word run into a number, and not a commit hash, a host name or a known term.

    Review of 36eeb9b8: a short commit hash that starts with three hex letters
    (cabeb2f6, bef2c40c) and a host name with one trailing digit (spark1,
    Redis7) are not glue. A glued run of two digits or more, or a time
    (ALLCLEAR22:06), still is.
    """

    token = match.group(0)
    if COMMIT_HASH.fullmatch(token):
        return False
    digits = re.match(r"\d+", match.group(2)).group(0)
    if len(digits) < 2 and ":" not in match.group(2):
        return False
    return (token.lower().rstrip(".:%") not in GLUED_TERMS
            and match.group(1).lower() + digits not in GLUED_TERMS)


# A paragraph longer than this reads as a wall in a phone notification.
OPERATOR_PARAGRAPH_MAXIMUM = 900
OPERATOR_RECIPIENTS = frozenset({"operator", "owner"})


def refuse_unreadable_operator_prose(value: Any, *, argument: str) -> Any:
    """Return ``value`` unchanged, or refuse operator mail a person cannot read.

    Operator mail reaches Telegram exactly as written. On 2026-10-05 a decision
    mail arrived as one 1,400-character paragraph with "ALLCLEAR22:06",
    "fresh215625" and "Require64GB" in it; the stored body was already glued,
    so no renderer could have fixed it. Operator, asked whether the procedure
    needs a rule: "yes we need it", then "do not file - fix". Code spans and
    fenced blocks are literals and are not checked.
    """

    if not isinstance(value, str):
        return value
    prose = _without_code(value)
    glued = sorted({match.group(0) for match in GLUED_WORD.finditer(prose) if _glued(match)})
    if glued:
        raise DomainError(
            "problem_board_operator_prose_glued",
            f"{argument} runs words into numbers ({', '.join(glued[:5])}): write them with a space "
            "(\"ALL CLEAR 22:06\", \"Apps 1204f593\"), or put a literal in backticks.",
            details={"argument": argument, "glued": glued[:20], "count": len(glued)},
        )
    longest = max((len(part.strip()) for part in re.split(r"\n\s*\n", prose)), default=0)
    if longest > OPERATOR_PARAGRAPH_MAXIMUM:
        raise DomainError(
            "problem_board_operator_prose_wall",
            f"{argument} has a {longest}-character paragraph: split operator mail into short "
            f"paragraphs or a list (at most {OPERATOR_PARAGRAPH_MAXIMUM} characters each).",
            details={"argument": argument, "longest_paragraph": longest,
                     "maximum": OPERATOR_PARAGRAPH_MAXIMUM},
        )
    return value


# Text people read on the board: the project banner and a work item's fields.
# Operator instruction relayed by the coordinator, 2026-10-05 22:41 UTC: the
# readability standard "applies especially to project banners, operator
# messages, and work-item descriptions, results, review notes, and test
# instructions".
_READ_ON_THE_BOARD = {
    "project.announcement.publish": ("text",),
    "plan.item.create": ("title", "summary", "description", "result", "blocked_reason"),
    "plan.item.update": ("title", "summary", "description", "result", "blocked_reason"),
}
_REVIEW_FIELDS = ("look_at", "could_not_verify")


def refuse_unreadable_board_text(action: str, payload: Mapping[str, Any], *, argument: str) -> None:
    """Refuse a banner or work-item field a person cannot read (W563).

    The same rule as operator mail: no word run into a number, no wall
    paragraph. Only the named prose fields of these operations are checked.
    """

    fields = _READ_ON_THE_BOARD.get(action)
    if not fields:
        return
    values = payload.get("changes") if action == "plan.item.update" else payload
    if not isinstance(values, Mapping):
        return
    for key in fields:
        if isinstance(values.get(key), str):
            refuse_unreadable_operator_prose(values[key], argument=f"{argument} {key}")
    review = values.get("review")
    if isinstance(review, Mapping):
        for key in _REVIEW_FIELDS:
            if isinstance(review.get(key), str):
                refuse_unreadable_operator_prose(review[key], argument=f"{argument} review.{key}")
