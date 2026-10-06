"""Real Redis/Cluster UPDATE recovery; dedicated disposable fixtures only.

The source/SDK overlays must be pinned by the caller. No backend is started,
stopped or restarted here; only fixture-owned child processes are SIGKILLed.
External issuer policy and host request context remain synthetic.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid

import pytest

from _issuer_update_recovery_child import STAGES, original_card, update_wire, validate_fixture

CHILD = Path(__file__).with_name("_issuer_update_recovery_child.py")
COMMITTED = {"committed", "projection", "complete"}


def invoke(base, action, *, stage="", killed=False):
    result = subprocess.run([sys.executable, str(CHILD)],
        input=json.dumps({**base, "action": action, "stage": stage}),
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        text=True, capture_output=True, timeout=45)
    assert result.returncode == (-signal.SIGKILL if killed else 0), result.stderr[-2000:]
    return None if killed else json.loads(result.stdout)


@pytest.fixture(params=("standalone", "cluster"))
def backend(request, tmp_path):
    kind = request.param
    variable = "REDIS_URL" if kind == "standalone" else "REDIS_CLUSTER_NODE"
    target = os.environ.get(variable, "")
    if not target:
        pytest.skip(f"{variable} is not set; {kind} UPDATE recovery not verified")
    confirmed = os.environ.get("CONNECTION_HUB_TEST_DISPOSABLE_REDIS", "")
    validate_fixture(kind, target, confirmed)  # before any subprocess or backend connection
    root = tmp_path / "state"
    root.mkdir()
    (root / ".fixture-owned").write_text("issuer-update-test\n")
    base = {"backend": kind, "target": target, "confirmed": confirmed,
            "tenant": "issuer-test-" + uuid.uuid4().hex, "storage_root": str(root)}
    try:
        seed = invoke(base, "seed")
        assert seed["revision"] == seed["cache_revision"] == 1
        assert seed["index_members"] == ["update-fixture-card"]
        yield base, seed
    finally:
        assert invoke(base, "cleanup")["remaining"] == 0


@pytest.mark.parametrize("stage", STAGES)
def test_actual_update_kill_then_fresh_process_recovers_exact_original_outcome(backend, stage):
    base, seed = backend
    invoke(base, "run", stage=stage, killed=True)
    frozen = invoke(base, "snapshot")
    committed = stage in COMMITTED
    assert frozen["revision"] == frozen["history"] == (2 if committed else 1)
    if not committed:
        assert frozen["fingerprint"] == seed["fingerprint"]
    if stage in ("marker", "sidecar", "revision", "pointer", "committed"):
        assert frozen["cache_kind"] == "updating"
    single = invoke(base, "single")
    assert single["single"] == ("committed" if stage == "complete" else "issuer_update_preparation_unresolved")
    started = time.monotonic()
    recovered = invoke(base, "recover")
    assert time.monotonic() - started < 30, "killed child fences stranded recovery"
    assert recovered["state"] == ("committed" if committed else "refused"), recovered
    assert recovered["serving_state"] == "complete" and recovered["decisions"] == 0, recovered
    assert recovered["after_revision"] == (2 if committed else None)
    assert not recovered["active"]
    assert recovered["revision"] == (3 if stage == "complete" else 2 if committed else 1)
    assert recovered["cache_kind"] != "updating"
    if committed:
        assert recovered["cache_revision"] == recovered["revision"]
        assert recovered["cache_fingerprint"] == recovered["fingerprint"]
        assert recovered["index_members"] == ["update-fixture-card"]
    if stage == "complete":
        assert recovered["label"] == "Later legitimate fixture edit"
        assert recovered["fingerprint"] != recovered["after_fingerprint"]
    assert invoke(base, "recover") == recovered, "identical retry changed original outcome"
    changed = invoke(base, "changed_replay")
    assert changed["error"] == "issuer_update_replay_changed" and changed["decisions"] == 0
    assert changed["revision"] == recovered["revision"] and not changed["active"]
    if stage != "complete":
        assert invoke(base, "single")["single"] == "committed"


def test_real_marker_expiry_and_production_read_through_release_refused_intent(backend):
    base, seed = backend
    invoke(base, "run", stage="marker", killed=True)
    restored = invoke(base, "expire_and_read_through")
    assert restored["cache_kind"] == "card" and restored["fingerprint"] == seed["fingerprint"]
    recovered = invoke(base, "recover")
    assert (recovered["state"], recovered["serving_state"], recovered["active"]) == ("refused", "complete", False)
    assert recovered["decisions"] == 0 and recovered["revision"] == 1
    assert invoke(base, "single")["single"] == "committed"


def test_foreign_real_marker_stays_pending_untouched_until_owning_marker_is_restored(backend):
    base, _ = backend
    invoke(base, "run", stage="committed", killed=True)
    foreign = invoke(base, "foreign_marker")
    pending = invoke(base, "recover")
    assert (pending["state"], pending["serving_state"], pending["active"]) == ("committed", "pending", True)
    assert pending["revision"] == 2 and pending["decisions"] == 0
    assert invoke(base, "snapshot") == foreign, "recovery replaced another mutation's marker"
    assert invoke(base, "single")["single"] == "issuer_update_preparation_unresolved"
    invoke(base, "owned_marker")
    completed = invoke(base, "recover")
    assert (completed["state"], completed["serving_state"], completed["active"]) == ("committed", "complete", False)
    assert completed["cache_revision"] == 2 and completed["decisions"] == 0


def test_committed_truth_survives_loss_of_its_real_redis_projection(backend):
    base, _ = backend
    invoke(base, "run", stage="committed", killed=True)
    lost = invoke(base, "drop_projection")
    assert lost["revision"] == 2 and lost["cache_kind"] is None
    assert lost["index_members"] == []
    completed = invoke(base, "recover")
    assert (completed["state"], completed["serving_state"], completed["active"]) == ("committed", "complete", False)
    assert completed["cache_revision"] == 2 and completed["decisions"] == 0
    assert completed["index_members"] == ["update-fixture-card"], "real index was not rebuilt"
    assert invoke(base, "recover") == completed


def test_fixture_authority_and_exact_widening_query_are_valid_without_any_backend():
    from connection_hub.delegated_credentials.issuer_update import IssuerUpdateQuery, build_candidate

    original = original_card()
    query = IssuerUpdateQuery.from_mapping(update_wire())
    candidate = build_candidate(original, query)
    assert (original.card_revision, candidate.card_revision) == (1, 2)
    assert original.content_hash() != candidate.content_hash()
    mutable = {"card_revision", "operations", "resource_operations", "resource_grants"}
    assert {k: v for k, v in original.to_dict().items() if k not in mutable} == {
        k: v for k, v in candidate.to_dict().items() if k not in mutable}


@pytest.mark.parametrize("kind,target", [("standalone", "redis://127.0.0.1:16379/3"),
                                         ("cluster", "127.0.0.1:17001")])
def test_dedicated_fixture_requires_explicit_confirmation(kind, target):
    with pytest.raises(ValueError, match="disposable_fixture_confirmation_required"):
        validate_fixture(kind, target, "")
    assert validate_fixture(kind, target, "1")[0] == "127.0.0.1"


@pytest.mark.parametrize("kind,target", [
    ("standalone", "redis://127.0.0.1:6379/1"),
    ("standalone", "redis://remote.example:16379/1"),
    ("standalone", "redis://actor@127.0.0.1:16379/1"),
    ("standalone", "redis://127.0.0.1:16379/1?unexpected=1"),
    ("standalone", "redis://127.0.0.1:16379/not-a-database"),
    ("cluster", "remote.example:17001"),
    ("cluster", "127.0.0.1:6379"),
    ("cluster", "actor@127.0.0.1:17001"),
    ("cluster", "127.0.0.1:17001/1"),
])
def test_fixture_guard_refuses_ambient_remote_credential_and_malformed_targets(kind, target):
    with pytest.raises(ValueError, match="dedicated_loopback_fixture_target_required"):
        validate_fixture(kind, target, "1")
