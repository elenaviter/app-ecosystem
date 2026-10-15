"""Pure wire-contract tests; the relay/Board integration gates are separate."""
from copy import deepcopy
import json
import unittest

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
        self.assertNotIn("set", marker)
        self.assertEqual(marker["observations"]["team"]["0"]["/runtime_account/observed_at"], NEXT)

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


if __name__ == "__main__":
    unittest.main()
