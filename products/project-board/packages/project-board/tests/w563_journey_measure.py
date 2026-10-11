"""W722: source-level, non-live measurements of one synthetic W563 journey.

The real Project Board CLI dispatch and brief/JSON renderers run here. The field
response is supplied by the fixture, so these numbers are not network, relay,
installed-procedure, model-compaction, or actual-worker measurements.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from project_board.client import cli


TESTS = Path(__file__).resolve().parent
FIXTURE = TESTS / "fixtures" / "w563_sanitized_350_message_journey.json"
PROCEDURE = TESTS.parent / "src" / "project_board" / "procedures" / "problem-board-worker"
BASE_COMMIT = "3343f02a00fff5cb877dbdcf7c5cb46d3cd0f4dc"
PROJECT_REF = "work:project:w563-synthetic-journey"


def load_fixture(path: Path = FIXTURE) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    fixture = json.loads(raw)
    messages = fixture["messages"]
    if (fixture["schema"] != "w563.synthetic-350-message-journey.v1"
            or fixture["code_commit"] != BASE_COMMIT
            or fixture["project_ref"] != PROJECT_REF
            or len(messages) != 350
            or len({m["item_key"] for m in messages}) != 350):
        raise ValueError("W722 fixture identity/count changed")
    return fixture, hashlib.sha256(raw).hexdigest()


def _item(message: dict[str, Any]) -> dict[str, Any]:
    kind = message["kind"]
    status = "review" if kind in {"review_exception", "release_exception"} else "working"
    summary = (
        f"{kind}; reviewer {message['reviewer_availability']}; "
        f"merge {message['merge_state']}; deployment {message['deployment_state']}; "
        f"next actor {message['next_actor']}; event {message['logical_event_ref']}."
    )
    key = message["item_key"]
    return {
        "item_key": key,
        "status": status,
        "title": message["subject"],
        "identity_ref": f"work:plan:synthetic-w563:{key}",
        "item_ref": f"work:plan:synthetic-w563:{key}:revision-{message['freshness_revision']}",
        "revision": message["freshness_revision"],
        "updated_at": message["created_at"],
        "assignee": message["current_owner"],
        "reviewer": "synthetic-reviewer" if status == "review" else "",
        "summary": summary,
        "description": summary + " " + message["body"],
        "acceptance": [
            f"Keep authority {message['authority_ref']} and next actor {message['next_actor']}.",
            "Receipt, custody and settlement are once per logical event, including retries.",
        ],
        "result": "Merged does not mean deployed." if kind == "release_exception" else "",
        "review": {"merged": BASE_COMMIT} if kind == "release_exception" else {},
    }


def _index_row(message: dict[str, Any]) -> dict[str, Any]:
    item = _item(message)
    return {
        "item_key": item["item_key"], "status": item["status"],
        "title": item["title"], "identity_ref": item["identity_ref"],
        "item_ref": item["item_ref"], "revision": item["revision"],
        "updated_at": item["updated_at"], "assignee": item["assignee"],
        "reviewer": item["reviewer"], "summary": item["summary"],
        "message_ref": message["message_ref"],
        "authority_ref": message["authority_ref"],
        "freshness_revision": message["freshness_revision"],
        "next_actor": message["next_actor"],
    }


class FixtureDispatch:
    """Supply immutable synthetic results at the CLI's existing dispatch seam."""

    def __init__(self, fixture: dict[str, Any]) -> None:
        self.fixture = fixture
        self.by_key = {m["item_key"]: m for m in fixture["messages"]}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, args: argparse.Namespace) -> dict[str, Any]:
        payload = json.loads(args.payload_json or "{}")
        action = args.action
        self.calls.append((action, args.payload_json or ""))
        if action == "project.plan.item":
            return {"operation": action, "object": _item(self.by_key[payload["item_key"]])}
        if action == "project.plan.index":
            start = int(payload.get("cursor") or 0)
            limit = min(int(payload.get("limit") or 100), 100)
            messages = self.fixture["messages"]
            chunk = messages[start:start + limit]
            next_cursor = str(start + limit) if start + limit < len(messages) else ""
            return {
                "operation": action,
                "object": {
                    "schema": "problem-board.plan-index.v2",
                    "project_ref": PROJECT_REF,
                    "plan_revision": 350,
                    "item_count": len(messages),
                    "matched_count": len(messages),
                    "count": len(chunk),
                    "page": start // limit + 1,
                    "page_count": (len(messages) + limit - 1) // limit,
                    "generation_token": "synthetic-w563-generation-350",
                    "next_cursor": next_cursor,
                    "state_counts": [
                        {"state": state, "count": count}
                        for state, count in sorted(Counter(_item(m)["status"] for m in messages).items())
                    ],
                    "items": [_index_row(m) for m in chunk],
                },
            }
        raise AssertionError(f"unsupported synthetic action: {action}")


def _call(dispatch: FixtureDispatch, action: str, payload: dict[str, Any], *, full: bool) -> dict[str, Any]:
    args = ["coordinate", action, "--object-ref", PROJECT_REF,
            "--payload-json", json.dumps(payload, sort_keys=True)]
    args += ["--format", "json" if full else "brief"]
    output = io.StringIO()
    previous = cli._coordinate_command
    cli._coordinate_command = dispatch
    try:
        started = time.perf_counter_ns()
        with contextlib.redirect_stdout(output):
            code = cli.main(args)
        elapsed_ns = time.perf_counter_ns() - started
    finally:
        cli._coordinate_command = previous
    if code:
        raise RuntimeError(f"synthetic CLI call failed: {action} {payload}")
    raw = output.getvalue().encode("utf-8")
    return {
        "action": action, "payload": payload, "format": "json" if full else "brief",
        "bytes": len(raw), "elapsed_ns": elapsed_ns,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "text": output.getvalue(),
    }


def _path_run(fixture: dict[str, Any], *, full: bool) -> dict[str, Any]:
    dispatch = FixtureDispatch(fixture)
    calls: list[dict[str, Any]] = []
    start_ns = time.perf_counter_ns()
    if full:
        for message in fixture["messages"]:
            calls.append(_call(dispatch, "project.plan.item", {"item_key": message["item_key"]}, full=True))
    else:
        for offset in range(0, len(fixture["messages"]), 100):
            calls.append(_call(dispatch, "project.plan.index", {"cursor": str(offset), "limit": 100}, full=False))
        for key in fixture["targeted_item_keys"]:
            calls.append(_call(dispatch, "project.plan.item", {"item_key": key}, full=False))
    route_elapsed_ns = time.perf_counter_ns() - start_ns
    digest = hashlib.sha256()
    for call in calls:
        digest.update(call["text"].encode("utf-8"))
    return {
        "bytes": sum(call["bytes"] for call in calls),
        "route_elapsed_ns": route_elapsed_ns,
        "full_history_reads": sum(call["format"] == "json" for call in calls),
        "dispatch_call_count": len(calls),
        "output_sha256": digest.hexdigest(),
        "calls": [{key: value for key, value in call.items() if key != "text"} for call in calls],
        "brief_output": "" if full else "".join(call["text"] for call in calls),
    }


def synthetic_custody(fixture: dict[str, Any]) -> dict[str, Any]:
    """A fixture-only idempotent ledger, never a PB receipt or settlement."""

    events: dict[str, dict[str, Any]] = {}
    delivery_count: Counter[str] = Counter()
    for message in fixture["messages"]:
        event = message["logical_event_ref"]
        delivery_count[event] += 1
        if event not in events:
            events[event] = {
                "receipt_count": 1, "custody_count": 1, "settlement_count": 1,
                "custody_ref": message["custody_ref"],
                "authority_ref": message["authority_ref"],
                "freshness_revision": message["freshness_revision"],
                "next_actor": message["next_actor"],
                "first_message_ref": message["message_ref"],
            }
    return {
        "boundary": "simulated-ledger-only",
        "event_count": len(events), "duplicate_event_count": sum(n > 1 for n in delivery_count.values()),
        "urgent_event_deliveries": delivery_count["synthetic-event-003"],
        "urgent_event": events["synthetic-event-003"],
        "all_once": all(
            record["receipt_count"] == record["custody_count"] == record["settlement_count"] == 1
            for record in events.values()
        ),
    }


class SelectedInstructionCache:
    """Measure complete source text and simulated unchanged-revision reuse."""

    def __init__(self) -> None:
        self.cache: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = {}
        self.file_reads = 0

    def load(self, revision: str, refs: list[str]) -> list[dict[str, Any]]:
        key = (revision, tuple(refs))
        if key in self.cache:
            return self.cache[key]
        loaded = []
        for ref in refs:
            path = (PROCEDURE / ref).resolve()
            if not path.is_relative_to(PROCEDURE.resolve()):
                raise ValueError("instruction path escapes pinned procedure")
            raw = path.read_bytes()
            self.file_reads += 1
            loaded.append({"ref": ref, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                           "text": raw.decode("utf-8")})
        self.cache[key] = loaded
        return loaded


def instruction_and_handoff(fixture: dict[str, Any]) -> dict[str, Any]:
    manifest = json.loads((PROCEDURE / "package.json").read_text())
    revision = manifest["revision"]
    if revision != fixture["procedure_revision"]:
        raise ValueError("fixture/procedure revision mismatch")
    refs = fixture["selected_instruction_refs"]
    if any(ref != "SKILL.md" and ref not in manifest["references"] for ref in refs):
        raise ValueError("selected instruction absent from pinned manifest")
    loader = SelectedInstructionCache()
    first = loader.load(revision, refs)
    before_reuse = loader.file_reads
    second = loader.load(revision, refs)
    handoff = fixture["handoff"]
    handoff_text = "\n".join([
        "W563 synthetic active handoff; source-level baseline only.",
        f"Current owner: {handoff['current_owner']}; next actor: {handoff['next_actor']}.",
        f"Messages: {len(fixture['messages'])}; targeted reads: {', '.join(fixture['targeted_item_keys'])}.",
        *handoff["active_constraints"],
    ])
    return {
        "boundary": "source-files-complete; reuse-cache-simulated; handoff-proposed",
        "revision": revision,
        "selected": [{key: value for key, value in entry.items() if key != "text"} for entry in first],
        "all_selected_bytes_loaded": (
            len(first) == len(refs)
            and all(entry["ref"] == ref and len(entry["text"].encode("utf-8")) == entry["bytes"]
                    for entry, ref in zip(first, refs))
        ),
        "same_revision_reused": first is second and loader.file_reads == before_reuse,
        "file_reads": loader.file_reads,
        "handoff_words": len(handoff_text.split()),
        "handoff_bytes": len(handoff_text.encode("utf-8")),
        "handoff_sha256": hashlib.sha256(handoff_text.encode("utf-8")).hexdigest(),
        "handoff_text": handoff_text,
    }


def measure(fixture_path: Path = FIXTURE, *, samples: int = 3) -> dict[str, Any]:
    if samples < 2:
        raise ValueError("repeat each dispatch path at least twice")
    fixture, fixture_sha = load_fixture(fixture_path)
    full_runs = [_path_run(fixture, full=True) for _ in range(samples)]
    compact_runs = [_path_run(fixture, full=False) for _ in range(samples)]
    full_bytes = [run["bytes"] for run in full_runs]
    compact_bytes = [run["bytes"] for run in compact_runs]
    reductions = [1 - compact / full for compact, full in zip(compact_bytes, full_bytes)]
    custody = synthetic_custody(fixture)
    instructions = instruction_and_handoff(fixture)
    return {
        "schema": "w563.synthetic-source-measurement.v1",
        "boundary": "real CLI dispatch and render; fixture-supplied field result; no network or actual worker",
        "code_commit": BASE_COMMIT, "procedure_revision": fixture["procedure_revision"],
        "fixture_sha256": fixture_sha, "message_count": len(fixture["messages"]),
        "sample_count_per_path": samples,
        "full_history_read_definition": "one project.plan.item --format json per fixture message",
        "full_read": [{key: value for key, value in run.items() if key != "brief_output"} for run in full_runs],
        "compact_read": [{key: value for key, value in run.items() if key != "brief_output"} for run in compact_runs],
        "payload_reduction": reductions,
        "payload_reduction_target_met": all(value >= 0.70 for value in reductions),
        "zero_full_history_happy_path": all(run["full_history_reads"] == 0 for run in compact_runs),
        "custody": custody, "instructions": instructions,
        "targeted_evidence_visible": all(
            token in compact_runs[0]["brief_output"]
            for token in ("URGENT", "reviewer unavailable", "merged but not deployed")
        ),
        "targeted_case_refs_visible": all(
            all(token in compact_runs[0]["brief_output"] for token in (
                f"item: {key}", f"work:plan:synthetic-w563:{key}",
                message["authority_ref"], f"next actor {message['next_actor']}",
            ))
            for key, message in ((key, next(m for m in fixture["messages"] if m["item_key"] == key))
                                 for key in fixture["targeted_item_keys"])
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = measure(args.fixture, samples=args.samples)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "output": str(args.output), "fixture_sha256": result["fixture_sha256"],
        "full_bytes": [run["bytes"] for run in result["full_read"]],
        "compact_bytes": [run["bytes"] for run in result["compact_read"]],
        "payload_reduction": result["payload_reduction"],
        "reduction_target_met": result["payload_reduction_target_met"],
        "zero_full_history_happy_path": result["zero_full_history_happy_path"],
        "synthetic_custody_once": result["custody"]["all_once"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
