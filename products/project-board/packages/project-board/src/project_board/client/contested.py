"""Who was right, who was wrong, and the coordinator counted alongside everyone.

A team of agents disagrees constantly, and almost none of it is recorded. The
disagreement is resolved in a message thread, the loser's reasoning disappears,
and the next person to face the same question starts from nothing. Worse, no one
can see whether a particular voice is usually right, which is the only thing
that should earn it more weight.

So each contested call is written down when it resolves: what was claimed, who
claimed it, what turned out to be true, and what settled it. The verdict is
about the claim, never about the agent.

Two rules make this a record rather than a scoreboard.

The coordinator is counted like everyone else. A tally that exempts whoever
keeps it is worthless, and the coordinator is the one voice nobody else is
positioned to contradict.

And a call is only recorded once something settled it. "I think X" and "I think
not X" is not a contested call; it becomes one when evidence arrives, and the
evidence is written down with it so a reader can disagree with the verdict.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

CONTESTED_SCHEMA = "problem-board.contested-call.v1"

# "open" is the verdict a position carries before anyone knows. It is the
# normal state of a live disagreement and the reason this can be written down
# while the argument is still running rather than reconstructed afterwards.
VERDICTS = ("open", "right", "wrong", "partly")


def normalize_positions(positions: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """One position per participant, each with its verdict.

    A position is kept even when it was wrong, with what was claimed. A tally
    showing only that someone was wrong four times, without what they argued,
    invites the wrong conclusion: being wrong while reasoning from the evidence
    available is not the same as being careless, and the record should let a
    reader tell those apart.
    """

    out: list[dict[str, str]] = []
    for row in positions:
        if not isinstance(row, Mapping):
            continue
        who = str(row.get("who") or "").strip()
        claim = str(row.get("claim") or "").strip()
        verdict = str(row.get("verdict") or "").strip().lower()
        if not who or not claim:
            continue
        if verdict not in VERDICTS:
            verdict = "open"
        out.append(
            {
                "who": who,
                "claim": claim,
                "verdict": verdict,
                "role": str(row.get("role") or "").strip(),
            }
        )
    return out


def tally(calls: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Counts per participant, ordered by how often they were contested.

    Ordered by participation rather than by score, so the list does not read as
    a ranking. Someone who is right twice out of two has not earned more trust
    than someone right eight times out of twelve, and sorting by ratio would
    claim otherwise.
    """

    right: Counter[str] = Counter()
    wrong: Counter[str] = Counter()
    partly: Counter[str] = Counter()
    roles: dict[str, str] = {}

    for call in calls:
        # An unresolved call counts for nobody. Its positions are recorded and
        # readable, but nothing is scored until something settled it, so a live
        # argument cannot quietly move a tally.
        if not call.get("resolved_at"):
            for position in call.get("positions") or []:
                if isinstance(position, Mapping) and position.get("who"):
                    roles.setdefault(str(position["who"]), str(position.get("role") or ""))
            continue
        for position in call.get("positions") or []:
            if not isinstance(position, Mapping):
                continue
            who = str(position.get("who") or "")
            if not who:
                continue
            if position.get("role"):
                roles[who] = str(position["role"])
            verdict = str(position.get("verdict") or "")
            if verdict == "right":
                right[who] += 1
            elif verdict == "wrong":
                wrong[who] += 1
            else:
                partly[who] += 1

    everyone = set(right) | set(wrong) | set(partly)
    rows = [
        {
            "who": who,
            "role": roles.get(who, ""),
            "right": right[who],
            "wrong": wrong[who],
            "partly": partly[who],
            "contested": right[who] + wrong[who] + partly[who],
        }
        for who in everyone
    ]
    rows.sort(key=lambda row: (-row["contested"], row["who"]))
    return rows


def matrix(calls: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The whole record: the counts, and every call behind them.

    The calls travel with the counts on purpose. A count without the call it
    came from cannot be argued with, and the point of writing this down is that
    a later reader can look at a verdict and decide it was wrong.
    """

    ordered = sorted(
        calls,
        key=lambda call: str(call.get("resolved_at") or call.get("opened_at") or ""),
    )
    open_calls = [call for call in ordered if not call.get("resolved_at")]
    return {
        "schema": CONTESTED_SCHEMA,
        "tally": tally(ordered),
        "calls": [dict(call) for call in ordered],
        "call_count": len(ordered),
        # Surfaced separately because an open call is a question waiting for
        # evidence, not a result. A reader should see what is still contested.
        "open_count": len(open_calls),
    }


def record_for(calls: Sequence[Mapping[str, Any]], who: str) -> dict[str, Any]:
    """One participant's record, readable without reading everything else.

    The whole matrix is the honest view and it is also the unreadable one: by
    the time a team has argued for a week, asking "how has this agent been
    doing" means scrolling past every call it was not in. This answers that
    question directly, and it carries the same calls rather than a summary, so
    the reader can still disagree with any verdict.

    The other side of each call travels with it. A count of who was right,
    without what the other position claimed, invites the wrong conclusion: being
    wrong while reasoning from the evidence available is not carelessness.
    """

    subject = str(who or "").strip()
    mine: list[dict[str, Any]] = []
    for call in calls:
        positions = [
            position
            for position in call.get("positions") or []
            if isinstance(position, Mapping)
        ]
        held = next(
            (p for p in positions if str(p.get("who") or "") == subject), None
        )
        if held is None:
            continue
        mine.append(
            {
                **{k: v for k, v in call.items() if k != "positions"},
                "position": dict(held),
                "others": [dict(p) for p in positions if p is not held],
            }
        )
    # Settled calls in the order they resolved, then whatever is still open.
    # Sorting on a missing timestamp alone put an unresolved call, which has
    # neither, at the very top, so the first thing a reader saw was the one
    # thing that had not been decided.
    mine.sort(
        key=lambda call: (
            1 if not call.get("resolved_at") else 0,
            str(call.get("resolved_at") or call.get("opened_at") or ""),
        )
    )
    counts = next(
        (row for row in tally(calls) if row["who"] == subject),
        {"who": subject, "role": "", "right": 0, "wrong": 0, "partly": 0, "contested": 0},
    )
    return {
        "schema": CONTESTED_SCHEMA,
        "who": subject,
        "counts": counts,
        "calls": mine,
        "call_count": len(mine),
        "open_count": sum(1 for call in mine if not call.get("resolved_at")),
    }


__all__ = [
    "CONTESTED_SCHEMA",
    "VERDICTS",
    "matrix",
    "normalize_positions",
    "record_for",
    "tally",
]
