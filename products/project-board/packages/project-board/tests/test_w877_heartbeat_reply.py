"""Pure wire-contract tests; the relay/Board integration gates are separate."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import project_board.contract.heartbeat_reply as heartbeat_contract

from project_board.contract.heartbeat_reply import (
    REPLY_PROTOCOL, MAX_KNOWN_BYTES, canonical_bytes, decode_reply, digest,
    encode_reply, knowledge,
)


SCOPE = {"tenant": "test", "platform_project": "test", "bundle": "test",
         "viewer": "test-worker", "authority": "test-card", "view": "heartbeat.reply",
         "project_ref": "work:project:test"}
STAMP = "2026-10-15T19:00:00.123456Z"
NEXT = "2026-10-15T19:01:00.123456Z"


def snapshot():
    return {"team": [{"worker_name": "synthetic-worker", "runtime_account": {"observed_at": STAMP},
                      "runtime_model": {"model": "test", "observed_at": STAMP},
                      "limit_state": {"kind": "test", "observed_at": STAMP},
                      "store_reads_observed_at": STAMP,
                      "disk_usage": {"host_free_bytes": 123456789012, "sweep": {"observed_at": STAMP}}}],
            "roles": {"keeper": {"state": "held"}}, "mail_recipients": [],
            "assignments": [{"ref": "test", "task": "complete task"}],
            "self": {"store_reads": {"x/y~": {"at": STAMP, "ms": 2}}, "empty": {}},
            "heartbeat_at": STAMP, "updated_at": STAMP}


def board_shaped_snapshot():
    """The real response's volatile paths, with synthetic display values only."""
    value = snapshot()
    disk = {"reported_at": STAMP, "host_free_bytes": 123456789012,
            "host_total_bytes": 999999999999, "workspace_bytes": 2345}
    row = value["team"][0]
    row["disk_usage"].update(disk)
    row["info_text"] = "synthetic display " * 100
    value["team"] = [{**deepcopy(row), "worker_name": f"synthetic-{i}"} for i in range(13)]
    value["disk_usage"] = deepcopy(disk)
    value["self"]["disk_usage"] = deepcopy(disk)
    value["runtime_account_observed_at"] = STAMP
    value["store_reads"] = {"x/y~": {"at": STAMP, "ms": 2, "op": "read"}}
    value["agent_sessions"] = [{"state": "waiting", "last_inbox_check_at": STAMP,
        "relay_heartbeat_at": STAMP, "heartbeat_age_seconds": 1, "relay_heartbeat_age_seconds": 0}]
    return value


class HeartbeatReplyContractTests(unittest.TestCase):
    def full(self, value):
        response = encode_reply(value, protocol=REPLY_PROTOCOL, scope=SCOPE)
        result, scope = decode_reply(response)
        self.assertEqual(result, value)
        return result, scope

    def delta(self, previous, current):
        saved, scope = self.full(previous)
        response = encode_reply(current, protocol=REPLY_PROTOCOL, scope=SCOPE, known=knowledge(saved, scope))
        result, restored_scope = decode_reply(response, baseline=saved, scope=scope)
        self.assertEqual(result, current)
        self.assertEqual(restored_scope, scope)
        return response["heartbeat_reply"]

    def test_canonical_golden_vectors(self):
        self.assertEqual(canonical_bytes({"a": 1}), b'{"a":1}')
        self.assertEqual(digest({"a": 1}), "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862")
        self.assertEqual(digest({"a": 1}, short=True), "AVq9f1zFei3ZS3WQ8ErYCA")
        self.assertEqual(canonical_bytes({"é": "☃", "z": [1.25, None, {}]}),
                         br'{"z":[1.25,null,{}],"\u00e9":"\u2603"}')
        self.assertEqual(digest({"x": 1, "y": 2}), digest({"y": 2, "x": 1}))
        for invalid in (float("nan"), float("inf"), -float("inf")):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                canonical_bytes(invalid)

    def test_cold_full_round_trip(self):
        self.full(snapshot())

    def test_unchanged_is_ack_only(self):
        marker = self.delta(snapshot(), snapshot())
        self.assertEqual(set(marker), {"protocol", "scope", "revision", "base", "state"})
        self.assertEqual(marker["state"], "delta")

    def test_observation_only_preserves_every_value(self):
        before, after = snapshot(), snapshot()
        after["team"][0]["runtime_account"]["observed_at"] = NEXT
        after["self"]["store_reads"]["x/y~"]["at"] = NEXT
        after["heartbeat_at"] = NEXT
        marker = self.delta(before, after)
        self.assertFalse("set" in marker, sorted(marker.get("set", {})))
        self.assertEqual(marker["observations"]["team"]["0"]["/runtime_account/observed_at"], NEXT)

    def test_real_board_observation_churn_has_no_sets_and_fits_reply_budget(self):
        before = board_shaped_snapshot()
        after = deepcopy(before)
        after["heartbeat_at"] = after["updated_at"] = NEXT
        after["runtime_account_observed_at"] = NEXT
        after["store_reads"]["x/y~"]["at"] = NEXT
        for disk in [after["disk_usage"], after["self"]["disk_usage"],
                     *[row["disk_usage"] for row in after["team"]]]:
            disk["reported_at"] = NEXT
            disk["host_free_bytes"] -= 12345
        for row in after["team"]:
            row["runtime_account"]["observed_at"] = NEXT
        after["agent_sessions"][0].update(last_inbox_check_at=NEXT, relay_heartbeat_at=NEXT,
                                         heartbeat_age_seconds=31, relay_heartbeat_age_seconds=1)
        marker = self.delta(before, after)
        self.assertFalse("set" in marker, sorted(marker.get("set", {})))
        self.assertEqual(marker["observations"]["store_reads"][""], {
            "/x~1y~0/at": NEXT, "/x~1y~0/ms": 2, "/x~1y~0/op": "read"})
        frame = {"operation": "worker.heartbeat", "state": "ok", "object": {"heartbeat_reply": marker}}
        size = len(json.dumps(frame, ensure_ascii=False, sort_keys=True).encode())
        # This original 13-member fixture is not a bound for all telemetry.
        # Keep complete observation groups intact under an explicit 7 KB
        # synthetic-frame budget; the live SDK envelope is a separate gate.
        self.assertLess(size, 7000)
        print(f"W877 real-shaped synthetic SDK-spaced reply_frame={size} budget=7000")

    def test_each_new_observation_is_lossless_and_does_not_replace_its_section(self):
        for name, container, key, changed in (
            ("disk_usage", (), "reported_at", NEXT),
            ("runtime_account_observed_at", (), None, NEXT),
            ("store_reads", ("x/y~",), "at", NEXT),
            ("agent_sessions", (0,), "last_inbox_check_at", NEXT),
            ("agent_sessions", (0,), "relay_heartbeat_at", NEXT),
            ("agent_sessions", (0,), "heartbeat_age_seconds", 31),
            ("agent_sessions", (0,), "relay_heartbeat_age_seconds", 1),
        ):
            with self.subTest(section=name, key=key):
                before = board_shaped_snapshot()
                after = deepcopy(before)
                if key is None:
                    after[name] = changed
                else:
                    node = after[name]
                    for component in container:
                        node = node[component]
                    node[key] = changed
                marker = self.delta(before, after)
                self.assertFalse("set" in marker, sorted(marker.get("set", {})))
                self.assertIn(name, marker["observations"])

    def test_new_observation_classification_keeps_semantic_changes_as_whole_sets(self):
        for name, container, key, changed in (
            ("team", (0, "limit_state"), "resets_at", NEXT),
            ("agent_sessions", (0,), "state", "detached"),
            ("disk_usage", (), "workspace_bytes", 4567),
            ("store_reads", ("x/y~",), "records", 3),
        ):
            with self.subTest(section=name, key=key):
                before = board_shaped_snapshot()
                after = deepcopy(before)
                node = after[name]
                for component in container:
                    node = node[component]
                node[key] = changed
                self.assertEqual(self.delta(before, after)["set"][name], after[name])

    def test_observation_null_empty_and_absent(self):
        for replacement in (None, "", "absent"):
            with self.subTest(replacement=replacement):
                before, after = snapshot(), snapshot()
                if replacement == "absent":
                    del after["team"][0]["runtime_account"]["observed_at"]
                else:
                    after["team"][0]["runtime_account"]["observed_at"] = replacement
                marker = self.delta(before, after)
                self.assertEqual("set" in marker, replacement == "absent")

    def test_added_observation_changes_layout(self):
        before, after = snapshot(), snapshot()
        after["team"][0]["new"] = {"observed_at": NEXT}
        self.assertIn("team", self.delta(before, after)["set"])

    def test_full_team_change_reorder_remove_empty(self):
        before = snapshot()
        before["team"].append({"worker_name": "second", "observed_at": STAMP})
        for team in (before["team"][::-1], before["team"][:1], []):
            with self.subTest(team=team):
                after = deepcopy(before)
                after["team"] = team
                self.assertEqual(self.delta(before, after)["set"]["team"], team)

    def test_unknown_fields_are_not_discarded(self):
        before, after = snapshot(), snapshot()
        after["new"] = {"value": [None, {}, [], ""]}
        self.assertEqual(self.delta(before, after)["set"]["new"], after["new"])

    def test_section_removal_is_explicit(self):
        before, after = snapshot(), snapshot()
        del after["roles"]
        self.assertEqual(self.delta(before, after)["remove"], ["roles"])

    def test_large_assignment_changes_are_complete(self):
        before, after = snapshot(), snapshot()
        after["assignments"][0]["task"] = "x" * 123_000
        self.assertEqual(self.delta(before, after)["set"]["assignments"], after["assignments"])

    def test_invalid_or_foreign_echo_recovers_full(self):
        value, scope = self.full(snapshot())
        known = knowledge(value, scope)
        bad = deepcopy(known)
        bad["scope"] = "f" * 64
        huge = deepcopy(known)
        huge["sections"] = {"x" * MAX_KNOWN_BYTES: {"b": digest({}, short=True), "o": {}}}
        for echo in (None, [], {}, bad, huge, {**known, "revision": "unknown"}):
            with self.subTest(echo_type=type(echo).__name__):
                response = encode_reply(value, protocol=REPLY_PROTOCOL, scope=SCOPE, known=echo)
                self.assertEqual(response["heartbeat_reply"]["state"], "full")

    def test_context_scope_dimensions_do_not_reuse(self):
        saved, scope = self.full(snapshot())
        for field in SCOPE:
            with self.subTest(field=field):
                response = encode_reply(saved, protocol=REPLY_PROTOCOL,
                                        scope={**SCOPE, field: "different"}, known=knowledge(saved, scope))
                self.assertEqual(response["heartbeat_reply"]["state"], "full")

    def test_baseline_and_integrity_are_required(self):
        saved, scope = self.full(snapshot())
        response = encode_reply(saved, protocol=REPLY_PROTOCOL, scope=SCOPE, known=knowledge(saved, scope))
        for base, baseline_scope in ((None, scope), ({}, scope), (saved, "other")):
            with self.subTest(base=base is None, scope=baseline_scope), self.assertRaises(ValueError):
                decode_reply(response, baseline=base, scope=baseline_scope)
        changed = deepcopy(response)
        changed["heartbeat_reply"]["revision"] = "f" * 64
        with self.assertRaises(ValueError):
            decode_reply(changed, baseline=saved, scope=scope)

    def test_malformed_delta_does_not_mutate_saved_snapshot(self):
        saved, scope = self.full(snapshot())
        original = deepcopy(saved)
        response = encode_reply(saved, protocol=REPLY_PROTOCOL, scope=SCOPE, known=knowledge(saved, scope))
        response["heartbeat_reply"]["observations"] = {"team": {"0": {"/not-in-layout": "bad"}}}
        with self.assertRaises(ValueError):
            decode_reply(response, baseline=saved, scope=scope)
        self.assertEqual(saved, original)

    def test_caller_mutation_is_separate(self):
        value = snapshot()
        restored, _ = self.full(value)
        restored["team"].clear()
        self.assertEqual(len(value["team"]), 1)

    def test_legacy_and_unsupported_protocols_stay_full(self):
        value = snapshot()
        for protocol in (None, "unknown"):
            self.assertIs(encode_reply(value, protocol=protocol, scope=SCOPE), value)
        self.assertEqual(decode_reply(value), (value, ""))
        self.assertIs(encode_reply(value, protocol=REPLY_PROTOCOL, scope={}), value)

    def test_thirteen_member_realistic_churn_budget(self):
        before = snapshot()
        before["team"] = [{**deepcopy(before["team"][0]), "worker_name": f"synthetic-{i}",
                           "info_text": "x" * 3000} for i in range(13)]
        after = deepcopy(before)
        for row in after["team"]:
            row["runtime_account"]["observed_at"] = NEXT
            row["disk_usage"]["host_free_bytes"] -= 12345
        after["heartbeat_at"] = after["updated_at"] = NEXT
        saved, scope = self.full(before)
        echo = knowledge(saved, scope)
        response = encode_reply(after, protocol=REPLY_PROTOCOL, scope=SCOPE, known=echo)
        restored, _ = decode_reply(response, baseline=saved, scope=scope)
        self.assertEqual(restored, after)
        # Mirrors SDK _json_dump (spaced, sorted, UTF-8), including a synthetic
        # outer response frame. Live bytes and CPU remain separate gates.
        size = lambda value: len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode())
        frame = {"operation": "worker.heartbeat", "state": "ok", "object": response}
        self.assertLess(size(frame), 6000)
        self.assertLess(size(echo) + size(frame), size(before) // 2)
        self.assertNotIn("team", response["heartbeat_reply"].get("set", {}))
        print(f"W877 synthetic bytes: full={size(before)} request_echo={size(echo)} reply_frame={size(frame)}")


class StoreReadObservationTests(unittest.TestCase):
    """Normalized summaries only; counts, keys and off-path names stay semantic."""

    full = HeartbeatReplyContractTests.full
    delta = HeartbeatReplyContractTests.delta

    def value(self):
        entry = {"at": STAMP, "ms": 2, "op": "read", "records": 3,
                 "partitions": 1, "range": "current", "key": "synthetic-key"}
        reads = {"x/y~": entry}
        return {"store_reads": deepcopy(reads), "self": {"store_reads": deepcopy(reads)},
                "team": [{"worker_name": "synthetic", "store_reads": deepcopy(reads)}]}

    def entry(self, value, section):
        node = value[section]
        if section == "team":
            node = node[0]
        return (node if section == "store_reads" else node["store_reads"])["x/y~"]

    def test_store_read_ms_and_op_are_exact_lossless_observations(self):
        for section in ("store_reads", "self", "team"):
            for key, replacement in (("ms", 3), ("ms", .25), ("ms", None), ("ms", ""),
                                     ("op", "write"), ("op", None), ("op", "")):
                with self.subTest(section=section, key=key, replacement=replacement):
                    before = self.value()
                    after = deepcopy(before)
                    self.entry(after, section)[key] = replacement
                    marker = self.delta(before, after)
                    self.assertNotIn(section, marker.get("set", {}))
                    group = "0" if section == "team" else ""
                    prefix = "" if section == "store_reads" else "/store_reads"
                    self.assertEqual(marker["observations"][section][group], {
                        prefix + "/x~1y~0/at": STAMP,
                        prefix + "/x~1y~0/ms": self.entry(after, section)["ms"],
                        prefix + "/x~1y~0/op": self.entry(after, section)["op"]})

    def test_counts_keys_ranges_and_unknown_fields_replace_whole_sections(self):
        for section in ("store_reads", "self", "team"):
            for key, replacement in (("records", 4), ("partitions", 2), ("range", "changed"),
                                     ("key", "different-key"), ("future", "unknown")):
                with self.subTest(section=section, key=key):
                    before = self.value()
                    after = deepcopy(before)
                    self.entry(after, section)[key] = replacement
                    self.assertEqual(self.delta(before, after)["set"][section], after[section])

    def test_ms_op_names_outside_exact_summary_paths_remain_semantic(self):
        for value in ({"ms": 1, "op": "read"}, {"store_reads": {"ms": 1, "op": "read"}},
                      {"nested": {"store_reads": {"x": {"ms": 1, "op": "read"}}}},
                      {"store_reads": {"x": {"nested": {"ms": 1, "op": "read"}}}},
                      {"store_reads": [{"ms": 1, "op": "read"}]}):
            for section in ("self", "team", "unknown"):
                with self.subTest(section=section, value=value):
                    before = {section: [deepcopy(value)] if section == "team" else deepcopy(value)}
                    after = deepcopy(before)
                    def change(node):
                        if isinstance(node, dict):
                            for key, child in node.items():
                                if key == "ms":
                                    node[key] = 2
                                elif key == "op":
                                    node[key] = "write"
                                else:
                                    change(child)
                        elif isinstance(node, list):
                            for child in node:
                                change(child)
                    change(after[section])
                    self.assertEqual(self.delta(before, after)["set"][section], after[section])
        for value in ({"ms": 1}, {"op": "read"}):
            after = {"store_reads": {key: "changed" for key in value}}
            self.assertEqual(self.delta({"store_reads": value}, after)["set"], after)

    def test_structured_absent_and_added_ms_op_replace_section(self):
        for section in ("store_reads", "self", "team"):
            for key in ("ms", "op"):
                for replacement in ({"nested": 4}, [4], "absent", "added"):
                    with self.subTest(section=section, key=key, replacement=replacement):
                        before = self.value()
                        after = deepcopy(before)
                        if replacement == "absent":
                            del self.entry(after, section)[key]
                        elif replacement == "added":
                            del self.entry(before, section)[key]
                        else:
                            self.entry(after, section)[key] = replacement
                        self.assertEqual(self.delta(before, after)["set"][section], after[section])

    def test_mixed_classifiers_fall_back_to_whole_sections_in_both_directions(self):
        before = self.value()
        after = deepcopy(before)
        for section in before:
            self.entry(after, section).update(ms=4, op="write", at=NEXT)
        saved, scope = self.full(before)
        for old_sender in (False, True):
            with self.subTest(old_sender=old_sender):
                if old_sender:
                    known = knowledge(saved, scope)
                    with patch.object(heartbeat_contract, "_STORE_READ_TELEMETRY", frozenset()):
                        response = encode_reply(after, protocol=REPLY_PROTOCOL, scope=SCOPE, known=known)
                else:
                    with patch.object(heartbeat_contract, "_STORE_READ_TELEMETRY", frozenset()):
                        known = knowledge(saved, scope)
                    response = encode_reply(after, protocol=REPLY_PROTOCOL, scope=SCOPE, known=known)
                self.assertEqual(response["heartbeat_reply"]["set"], after)
                self.assertEqual(decode_reply(response, baseline=saved, scope=scope)[0], after)
                self.assertEqual(saved, before)

    def test_thirteen_rows_all_store_read_groups_refresh_losslessly(self):
        before = self.value()
        row = before["team"][0]
        row["info_text"] = "synthetic display " * 150
        entry = row["store_reads"]["x/y~"]
        row["store_reads"] = {f"synthetic-{i}": deepcopy(entry) for i in range(5)}
        before["team"] = [{**deepcopy(row), "worker_name": f"synthetic-{i}"} for i in range(13)]
        after = deepcopy(before)
        for member in after["team"]:
            for summary in member["store_reads"].values():
                summary.update(at=NEXT, ms=4, op="lookup")
        for section in ("store_reads", "self"):
            self.entry(after, section).update(at=NEXT, ms=4, op="lookup")
        marker = self.delta(before, after)
        self.assertNotIn("set", marker)
        self.assertEqual(set(marker["observations"]["team"]), {str(i) for i in range(13)})
        self.assertTrue(all(len(group) == 15 for group in marker["observations"]["team"].values()))
        # Compare the exact previous classifier, not a rewritten encoder.
        with patch.object(heartbeat_contract, "_STORE_READ_TELEMETRY", frozenset()):
            old = self.delta(before, after)
        frame = lambda value: len(json.dumps({"operation": "worker.heartbeat", "state": "ok",
            "object": {"heartbeat_reply": value}}, ensure_ascii=False, sort_keys=True).encode())
        self.assertLess(frame(marker), frame(old))
        print(f"W877 13-row five-store synthetic frame: old={frame(old)} narrow={frame(marker)}; not a live bound")


class HeartbeatReplySnapshotTests(unittest.TestCase):
    context = ("synthetic-client", 1, "synthetic-worker", "profile", "partition")

    def setUp(self):
        # The client package has the project's normal dependency closure.
        # Keep pure contract selection runnable without claiming a client gate.
        from project_board.client.heartbeat_reply import ReplySnapshots
        self.ReplySnapshots = ReplySnapshots

    def seed(self):
        owner = self.ReplySnapshots()
        request = owner.begin("p", self.context)
        full = encode_reply(snapshot(), protocol=REPLY_PROTOCOL, scope=SCOPE)
        owner.apply(request, full, context=self.context, materialize=lambda value: None)
        return owner

    def test_snapshot_and_echo_are_retained_together(self):
        owner = self.seed()
        request = owner.begin("p", self.context)
        self.assertIn("known_reply", request.payload())
        next_value = snapshot()
        next_value["team"][0]["runtime_account"]["observed_at"] = NEXT
        wire = encode_reply(next_value, protocol=REPLY_PROTOCOL, scope=SCOPE, known=request.payload()["known_reply"])
        seen = []
        self.assertEqual(owner.apply(request, wire, context=self.context, materialize=seen.append), next_value)
        self.assertEqual(seen, [next_value])

    def test_late_request_cannot_materialize(self):
        owner = self.seed()
        older = owner.begin("p", self.context)
        newer = owner.begin("p", self.context)
        full = encode_reply(snapshot(), protocol=REPLY_PROTOCOL, scope=SCOPE)
        self.assertIsNone(owner.apply(older, full, context=self.context, materialize=lambda value: self.fail("late")))
        self.assertEqual(owner.apply(newer, full, context=self.context, materialize=lambda value: None), snapshot())

    def test_reconnect_and_changed_context_require_full(self):
        for context in (("synthetic-client", 2), ("replacement-client", 1), None):
            with self.subTest(context=context):
                owner = self.seed()
                old = owner.begin("p", self.context)
                self.assertNotIn("known_reply", owner.begin("p", context).payload())
                self.assertIsNone(owner.apply(old, snapshot(), context=context, materialize=lambda value: self.fail("old context")))

    def test_materialization_failure_drops_knowledge_and_keeps_error(self):
        owner = self.seed()
        request = owner.begin("p", self.context)
        error = RuntimeError("synthetic materializer failure")
        def fail(value):
            raise error
        with self.assertRaises(RuntimeError) as raised:
            owner.apply(request, encode_reply(snapshot(), protocol=REPLY_PROTOCOL, scope=SCOPE),
                        context=self.context, materialize=fail)
        self.assertIs(raised.exception, error)
        self.assertNotIn("known_reply", owner.begin("p", self.context).payload())

    def test_consumer_mutation_cannot_corrupt_retained_pair(self):
        owner = self.ReplySnapshots()
        request = owner.begin("p", self.context)
        result = owner.apply(request, encode_reply(snapshot(), protocol=REPLY_PROTOCOL, scope=SCOPE),
                             context=self.context, materialize=lambda value: value.clear())
        result.clear()
        self.assertEqual(owner.begin("p", self.context).baseline, snapshot())

    def test_corruption_forgets_snapshot(self):
        owner = self.seed()
        owner._saved["p"].snapshot.clear()
        self.assertNotIn("known_reply", owner.begin("p", self.context).payload())

    def test_legacy_reply_clears_conditional_knowledge(self):
        owner = self.seed()
        request = owner.begin("p", self.context)
        owner.apply(request, snapshot(), context=self.context, materialize=lambda value: None)
        self.assertNotIn("known_reply", owner.begin("p", self.context).payload())

    def test_unlink_during_materialization_does_not_revive_snapshot(self):
        owner = self.seed()
        request = owner.begin("p", self.context)
        owner.apply(request, encode_reply(snapshot(), protocol=REPLY_PROTOCOL, scope=SCOPE),
                    context=self.context, materialize=lambda value: owner.retain_projects(set()))
        self.assertNotIn("known_reply", owner.begin("p", self.context).payload())


class HeartbeatReplyRelayTests(unittest.IsolatedAsyncioTestCase):
    """Real request/reconstruction/republish methods with synthetic I/O edges."""

    async def asyncSetUp(self):
        from project_board.client.relay import ProblemBoardHostRelayAdapter
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.client = SimpleNamespace(connected=True, connection_generation=1, partition_ref="test-partition")
        config = SimpleNamespace(project_id="attendance", worker_name="test-worker", worker_identity="test-viewer",
                                 connection_hub_profile="test-profile", journal_workspace=lambda: None)
        self.adapter = ProblemBoardHostRelayAdapter(config=config, field=SimpleNamespace(), client=self.client,
                                                   store_executor=self.executor)
        self.calls, self.seen = [], []
        self.value = {"attendances": [], "self": {"worker_name": "test-worker"}, "heartbeat_at": STAMP}

        async def action(**kwargs):
            self.calls.append(deepcopy(kwargs))
            payload = kwargs["payload"]
            return {"object": encode_reply(deepcopy(self.value), protocol=payload.get("reply_protocol"),
                                           known=payload.get("known_reply"), scope=SCOPE)}

        async def account_action(payload, call):
            return await call()

        async def no_account(payload):
            pass

        self.client.action = action
        self.adapter._account_action = account_action
        self.adapter._add_runtime_account = no_account

    async def asyncTearDown(self):
        self.executor.shutdown(wait=True)

    async def beat(self, materialize=None):
        return await self.adapter._heartbeat_with_republish(
            {"availability": "available"}, materialize=materialize or self.seen.append)

    async def test_discovery_wire_is_reconstructed_before_materialization(self):
        first, registration = await self.beat()
        self.assertEqual(first, self.value)
        self.assertIsNone(registration)
        self.assertNotIn("known_reply", self.calls[0]["payload"])
        self.value["heartbeat_at"] = NEXT
        second, _ = await self.beat()
        self.assertEqual(second, self.value)
        self.assertEqual(self.seen, [first, second])
        self.assertIn("known_reply", self.calls[1]["payload"])
        self.assertNotIn("heartbeat_reply", second)

    async def test_full_and_delta_alias_results_reach_the_real_acknowledger(self):
        recorded = []
        self.adapter.field = SimpleNamespace(record_worker_alias_result=lambda *args: recorded.append(args))
        request = {"alias": "synthetic-alias", "requested_at": STAMP}
        for state in ("applied", "superseded", "refused"):
            self.value["worker_alias_result"] = {"state": state, "alias": "synthetic-alias", "requested_at": STAMP}
            result, _ = await self.beat(
                lambda snapshot: self.adapter._acknowledge_alias_request(request, snapshot))
            self.assertEqual(recorded[-1], ("test-worker", STAMP, self.value["worker_alias_result"]))
            self.assertEqual(result, self.value)
        self.assertNotIn("known_reply", self.calls[0]["payload"])
        self.assertTrue(all("known_reply" in call["payload"] for call in self.calls[1:]))
        self.assertEqual(len(recorded), 3)

    async def test_discovery_reconnect_and_rebuilt_adapter_require_coherent_pair(self):
        await self.beat()
        from project_board.client.relay import ProblemBoardHostRelayAdapter
        child = ProblemBoardHostRelayAdapter(config=self.adapter.config, field=self.adapter.field,
                                             client=self.client, store_executor=self.executor,
                                             reply_snapshots=self.adapter._heartbeat_replies())
        request, payload = child._prepare_reply_request("", {})
        self.assertIn("known_reply", payload)
        self.client.connection_generation += 1
        await self.beat()
        self.assertNotIn("known_reply", self.calls[-1]["payload"])
        with self.assertRaises(Exception) as raised:
            child._materialize_heartbeat_reply(request, {"heartbeat_at": NEXT}, self.seen.append)
        self.assertEqual(raised.exception.code, "work_heartbeat_reply_obsolete")

    async def test_failed_materialization_forces_full_and_preserves_exception(self):
        await self.beat()
        error = OSError("synthetic field failure")
        def fail(value):
            raise error
        with self.assertRaises(OSError) as raised:
            await self.beat(fail)
        self.assertIs(raised.exception, error)
        await self.beat()
        self.assertNotIn("known_reply", self.calls[-1]["payload"])

    async def test_invalid_delta_requires_full_without_materializing(self):
        await self.beat()
        self.value["heartbeat_at"] = NEXT
        original = self.client.action
        async def corrupt(**kwargs):
            result = await original(**kwargs)
            result["object"]["heartbeat_reply"]["revision"] = "f" * 64
            return result
        self.client.action = corrupt
        with self.assertRaises(Exception) as raised:
            await self.beat()
        self.assertEqual(raised.exception.code, "work_heartbeat_reply_full_required")
        self.assertEqual(len(self.seen), 1)
        self.client.action = original
        await self.beat()
        self.assertNotIn("known_reply", self.calls[-1]["payload"])

    async def test_republish_drops_project_knowledge_and_retries_discovery_full(self):
        from project_board.contract.errors import DomainError
        await self.beat()
        project_request = self.adapter._heartbeat_replies().begin("p", self.adapter._team_context())
        self.adapter._materialize_heartbeat_reply(project_request,
            encode_reply(snapshot(), protocol=REPLY_PROTOCOL, scope=SCOPE), lambda value: None)
        original = self.client.action
        refused = False
        async def missing(**kwargs):
            nonlocal refused
            if not refused:
                refused = True
                raise DomainError("work_worker_unavailable", "Synthetic missing registration")
            return await original(**kwargs)
        published = []
        async def publish(**kwargs):
            published.append(kwargs)
            return {"remote": {"pool_status": "active"}}
        self.client.action = missing
        self.adapter._published_interval = 30
        self.adapter._publish_worker = publish
        value, registration = await self.beat()
        self.assertEqual(value, self.value)
        self.assertEqual(registration, {"remote": {"pool_status": "active"}})
        self.assertEqual(published, [{"reconcile_ceiling_seconds": 30}])
        self.assertNotIn("known_reply", self.calls[-1]["payload"])
        self.assertNotIn("known_reply", self.adapter._heartbeat_replies().begin("p", self.adapter._team_context()).payload())

    async def test_legacy_server_rollback_is_materialized_and_forgets_echo(self):
        await self.beat()
        async def legacy(**kwargs):
            return {"object": deepcopy(self.value)}
        self.client.action = legacy
        value, _ = await self.beat()
        self.assertEqual(value, self.value)
        self.assertEqual(self.seen[-1], self.value)
        self.assertNotIn("known_reply", self.adapter._prepare_reply_request("", {})[1])

    async def test_project_poll_consumers_receive_full_values_on_full_and_delta(self):
        from contextlib import nullcontext
        import asyncio
        adapter = self.adapter
        adapter.config.project_id = "test"
        self.value = {**snapshot(), "assignments": [{"task": "complete synthetic task"}],
                      "mail_recipients": [{"worker_name": "test-recipient"}]}
        for name, value in {"_session_report_delta": (None, "s"), "_assignment_files_delta": (None, "f"),
                            "_store_reads_delta": (None, "r"), "_worker_info": ({}, False),
                            "_alias_request": ({}, False), "_workspace_report": ({}, False)}.items():
            setattr(adapter, name, lambda *args, _value=value, **kwargs: _value)
        for name in ("_add_worker_info", "_add_alias_request", "_add_project_record", "_add_workspace_report",
                     "_record_project_heartbeat", "_record_session_report", "_acknowledge_worker_info",
                     "_acknowledge_alias_request", "_acknowledge_workspace_report", "_record_attendance_observation",
                     "_materialize_attended_project", "_report_dead_notification_path"):
            setattr(adapter, name, lambda *args, **kwargs: None)
        consumers = {}
        def record(name, value):
            consumers.setdefault(name, []).append(deepcopy(value))
        adapter.field.sync_project_team = lambda project, rows: record("team", rows)
        adapter.field.sync_project_coordinator = lambda project, value: record("coordinator", value)
        adapter.field.sync_project_roles = lambda project, value: record("roles", value)
        async def journal(value):
            record("journal", value)
            return {"state": "unchanged"}
        async def recipients(value):
            record("recipients", value)
            return {"state": "ok"}
        def assignments(value):
            record("assignments", value["assignments"])
            return 0, []
        async def no_controls(*args, **kwargs):
            return {}
        adapter._add_disk_usage = adapter._add_runtime_account
        adapter._trace_stage = lambda *args, **kwargs: nullcontext()
        adapter._reconcile_journal_binding_background = journal
        adapter._run_startup_recovery = recipients
        adapter._reconcile_assignments = assignments
        adapter._pull_controls = adapter._flush_outbox = no_controls
        lock = asyncio.Lock()
        adapter._pull_lock = lambda: lock
        adapter._next_project_heartbeat_seconds = lambda **kwargs: 30
        for stamp in (STAMP, NEXT):
            self.value["team"][0]["runtime_account"]["observed_at"] = stamp
            result = await adapter._poll_project_once(agent_sessions=[], force_heartbeat=True)
            self.assertTrue(result["heartbeat_sent"])
            self.assertEqual(consumers["team"][-1], self.value["team"])
            self.assertEqual(consumers["assignments"][-1], self.value["assignments"])
            self.assertEqual(consumers["recipients"][-1], self.value["mail_recipients"])
            self.assertEqual(consumers["journal"][-1], self.value)
        self.assertIn("known_reply", self.calls[-1]["payload"])
        # Later consumers are part of materialization too, not just TEAM.
        error = OSError("synthetic journal failure")
        async def failed_journal(value):
            raise error
        adapter._reconcile_journal_binding_background = failed_journal
        with self.assertRaises(OSError) as raised:
            await adapter._poll_project_once(agent_sessions=[], force_heartbeat=True)
        self.assertIs(raised.exception, error)
        self.assertNotIn("known_reply", adapter._prepare_reply_request("work:project:test", {})[1])
        adapter._reconcile_journal_binding_background = journal
        from project_board.contract.errors import DomainError
        async def failed_directory(value):
            raise DomainError("project_not_found", "Synthetic directory race")
        adapter._run_startup_recovery = failed_directory
        await adapter._poll_project_once(agent_sessions=[], force_heartbeat=True)
        self.assertNotIn("known_reply", adapter._prepare_reply_request("work:project:test", {})[1])
        adapter._run_startup_recovery = recipients
        adapter._reconcile_assignments = lambda value: (0, [{"code": "synthetic_assignment_failure"}])
        await adapter._poll_project_once(agent_sessions=[], force_heartbeat=True)
        self.assertNotIn("known_reply", adapter._prepare_reply_request("work:project:test", {})[1])


if __name__ == "__main__":
    unittest.main()
