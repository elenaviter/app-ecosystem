"""W502: the read set's ceiling is the product's own (500 people), not W578's 64.

A project-wide step (the zero cutover) reads each person's My Card and Control
Card, so the read set grows with the project. The census already answers up to
MAX_PERSONS people; the read set must hold 2 reads for each of them plus the fixed
reads, stay byte-bounded, and refuse up front, by name, a set the process cannot
hold open at once (each held Card section is one open lock file).
"""

from __future__ import annotations

import contextlib
import os
import resource
import sys
from pathlib import Path

import pytest

from connection_hub.delegated_credentials.cards import card_read_set as rs
from connection_hub.delegated_credentials.cards import service as card_service
from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.census_read import MAX_ANSWER_BYTES, MAX_PERSONS
from service_foundation.coordination.durable_decision_log import DecisionRefused
from test_card_transaction_store import INTENT, NOW, SUBJECT_HASH, Decisions, _authority, _Cache

RS = "f" * 64


class _Held:
    """Counts the PRODUCTION lock's sections as they are entered and left; it never replaces the lock.

    ``peak`` is how many sections were held at the same time, ``held`` how many are held now and
    ``entered`` how many were ever entered (W646 return, codex-infra 2026-10-14: show the sections held
    concurrently and released, and a refusal that acquires nothing).
    """

    def __init__(self, root) -> None:
        self.root = root
        self.held = self.peak = self.entered = 0
        self.expected = 0  # the read set's size: the lock descriptors are counted when that many are held
        self.peak_lock_descriptors = 0  # descriptors open on the store's lock files while all were held


def _path_of(directory: str, name: str) -> str:
    if directory == "/proc/self/fd":
        return os.readlink(f"{directory}/{name}")
    import fcntl

    raw = fcntl.fcntl(int(name), getattr(fcntl, "F_GETPATH", 50), b"\0" * 1024)  # macOS
    return raw.split(b"\0", 1)[0].decode("utf-8", "replace")


def _lock_descriptors(root) -> int:
    """Descriptors this process holds open on files under the Card store's root, the lock files among them.

    W857 (Mint, 2026-10-14): the process-wide count also counts the PG/Redis pool sockets R+P mode opens
    during the prepare, so a whole-process baseline failed there. A socket or pipe has no path under the
    store root; a held Card section's lock file has.
    """

    prefix = str(Path(root).resolve()) + os.sep
    for directory in ("/proc/self/fd", "/dev/fd"):
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        count = 0
        for name in names:
            try:
                path = _path_of(directory, name)
            except (OSError, ValueError):
                continue  # the listing's own descriptor, closed by now, or one without a path
            if path and str(Path(path).resolve()).startswith(prefix):
                count += 1
        return count
    pytest.skip(f"no descriptor listing on {sys.platform}")


async def _real_lock_setup(tmp_path):
    """The filesystem Card store with the PRODUCTION mutation lock: one held flock per section.

    The shared _setup binds a no-op lock, which would not hold any descriptor and so
    could not exercise this ceiling (CodeApp's review, 2026-10-08). Each section here still enters
    the SDK's own ``_kdcube_card_mutation_lock`` (one flock, one descriptor); ``_Held`` only counts it.
    """
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.service import (
        _kdcube_card_mutation_lock,
    )
    from connection_hub.delegated_credentials.cards.service import DelegatedCardService
    from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore

    probe = _Held(tmp_path)

    @contextlib.asynccontextmanager
    async def counted(**kwargs):
        async with _kdcube_card_mutation_lock(**kwargs) as held:
            probe.held += 1
            probe.entered += 1
            probe.peak = max(probe.peak, probe.held)
            if probe.expected and probe.held == probe.expected:
                probe.peak_lock_descriptors = _lock_descriptors(probe.root)
            try:
                yield held
            finally:
                probe.held -= 1

    store = BundleStorageDelegatedCardStore(tmp_path)
    tx.bind_transaction_decisions(store, Decisions())
    service = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=counted)
    await service.commit(_authority(), subject_hash=SUBJECT_HASH, expected_revision=0, now=NOW)
    probe.peak = probe.entered = 0  # count only the read set below, not the setup commit
    return store, service, probe


def _absent_reads(count: int, *, access_len: int = 0) -> list[dict]:
    return [{"subject_hash": SUBJECT_HASH, "access_id": f"aut_absent_{i:05d}" + "x" * access_len, "revision": 0}
            for i in range(count)]


def _candidate(reads: list[dict]) -> dict:
    return rs.read_set_candidate_value(reads)


def _refused(value) -> str:
    with pytest.raises(DecisionRefused) as exc:
        rs.validate_read_set_candidate(value)
    return str(exc.value)


@contextlib.contextmanager
def _soft_nofile(limit: int):
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
    resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    try:
        yield target
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_the_bound_is_derived_from_the_census_ceiling_not_a_literal():
    assert rs.MAX_READ_SET_READS == 2 * MAX_PERSONS + rs.MAX_READ_SET_CHAIN_READS
    assert rs.MAX_READ_SET_READS >= 2 * 33  # the 33-member zero cutover that 64 refused
    assert rs.MAX_READ_SET_BYTES == MAX_ANSWER_BYTES


@pytest.mark.parametrize("members", [33, MAX_PERSONS])
def test_a_project_wide_read_set_with_a_shared_chain_up_to_the_census_ceiling_is_accepted(members):
    # A shared chain counts once, so the set is My and Control per person; distinct
    # chain Cards beyond MAX_READ_SET_CHAIN_READS are refused by name instead.
    reads = _absent_reads(2 * members)
    assert rs.validate_read_set_candidate(_candidate(reads))["reads"] == _candidate(reads)["reads"]


def test_one_read_past_the_ceiling_is_refused_by_name():
    assert _refused(_candidate(_absent_reads(rs.MAX_READ_SET_READS + 1))) == "card_read_set_too_large"


def test_a_candidate_past_the_byte_bound_is_refused_even_within_the_count():
    # Fewer reads than the count bound, but long ids push the canonical bytes over.
    reads = _absent_reads(600, access_len=900)
    assert len(reads) <= rs.MAX_READ_SET_READS
    assert _refused(_candidate(reads)) == "card_read_set_too_large"


def test_the_lock_budget_counts_descriptors_in_use_and_infinity_never_refuses(monkeypatch):
    monkeypatch.setattr(resource, "getrlimit", lambda _which: (300, 300))
    monkeypatch.setattr(card_service, "_open_descriptors", lambda: 40)
    budget = 300 - 40 - card_service.LOCK_FD_RESERVE
    card_service._require_lock_budget(budget)  # exactly at the budget
    with pytest.raises(tx.CardTransactionRefused, match="card_read_set_lock_budget_exceeded"):
        card_service._require_lock_budget(budget + 1)
    # Descriptors already in use shrink the budget.
    monkeypatch.setattr(card_service, "_open_descriptors", lambda: 200)
    with pytest.raises(tx.CardTransactionRefused, match="card_read_set_lock_budget_exceeded"):
        card_service._require_lock_budget(budget)
    monkeypatch.setattr(resource, "getrlimit", lambda _which: (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
    card_service._require_lock_budget(10_000)


def test_the_process_descriptor_count_is_read_from_the_os():
    assert card_service._open_descriptors() >= 3  # stdin, stdout, stderr at least


@pytest.mark.asyncio
async def test_a_read_set_of_a_full_project_prepares_on_the_real_store_when_the_limit_allows(tmp_path):
    reads = _absent_reads(2 * MAX_PERSONS)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    needed = len(reads) + card_service.LOCK_FD_RESERVE + 64
    if hard != resource.RLIM_INFINITY and hard < needed:
        pytest.skip(f"the hard open-file limit {hard} is below the {needed} this case needs")
    store, service, probe = await _real_lock_setup(tmp_path)
    probe.expected = len(reads)
    locks_before = _lock_descriptors(tmp_path)
    with _soft_nofile(max(needed, soft)):
        receipt = await service.stage_read_set_transaction(transaction_id=RS, intent_digest=INTENT,
                                                           participant="project", reads=reads, catalog="")
    assert receipt["state"] == "prepared" and len(receipt["reads"]) == len(reads)
    # Every read's production section was held at the same time, then all were released: no section and
    # no lock descriptor outlives the prepare (the flocks are closed, not leaked). Only descriptors on the
    # store's files are counted: R+P mode's PG/Redis pool sockets are not lock descriptors (W857).
    assert probe.peak >= len(reads) and probe.held == 0
    assert probe.peak_lock_descriptors - locks_before >= len(reads), "each held section holds a lock descriptor"
    assert _lock_descriptors(tmp_path) <= locks_before
    for read in reads[:: len(reads) // 10]:  # every read is fenced for this transaction
        owner = await tx._live_read_fence(store, subject_hash=read["subject_hash"], access_id=read["access_id"])
        assert owner is not None and owner["transaction_id"] == RS


@pytest.mark.asyncio
async def test_a_read_set_over_the_process_budget_is_refused_and_leaves_nothing(tmp_path):
    reads = _absent_reads(400)
    store, service, probe = await _real_lock_setup(tmp_path)
    with _soft_nofile(256):
        with pytest.raises(tx.CardTransactionRefused, match="card_read_set_lock_budget_exceeded"):
            await service.stage_read_set_transaction(transaction_id=RS, intent_digest=INTENT,
                                                     participant="project", reads=reads, catalog="")
    assert probe.entered == 0 and probe.held == 0, "refused before acquiring any production section"
    assert await tx.read_receipt(store, RS) is None
    owner = await tx._live_read_fence(store, subject_hash=reads[0]["subject_hash"], access_id=reads[0]["access_id"])
    assert owner is None


@pytest.mark.asyncio
async def test_without_the_preflight_the_real_locks_exhaust_descriptors_mid_stack(tmp_path, monkeypatch):
    # Proves the sections really hold one descriptor each: with the budget check
    # bypassed, the production lock stack runs out of descriptors part-way through.
    reads = _absent_reads(400)
    store, service, probe = await _real_lock_setup(tmp_path)
    monkeypatch.setattr(card_service, "_require_lock_budget", lambda _sections: None)
    with _soft_nofile(256):
        with pytest.raises(OSError) as exc:
            await service.stage_read_set_transaction(transaction_id=RS, intent_digest=INTENT,
                                                     participant="project", reads=reads, catalog="")
    assert exc.value.errno == 24  # EMFILE: too many open files
    assert await tx.read_receipt(store, RS) is None
    # Part-way: some production sections were held at once, fewer than the set, and every one was released.
    assert 0 < probe.peak < len(reads) and probe.held == 0


@pytest.mark.asyncio
async def test_the_lock_descriptor_count_sees_a_leaked_lock_and_not_a_socket(tmp_path):
    # W857: the count the full-project case relies on fails on a leaked lock file and ignores a socket,
    # which is what R+P mode's PG/Redis pools add during the prepare.
    import socket

    reads = _absent_reads(8)
    store, service, probe = await _real_lock_setup(tmp_path)
    locks_before = _lock_descriptors(tmp_path)
    leaked = []
    real_counted = service._mutation_lock if hasattr(service, "_mutation_lock") else None

    @contextlib.asynccontextmanager
    async def leaking(**kwargs):
        async with real_counted(**kwargs) as held:
            lock_path = Path(tmp_path) / f"leaked-{len(leaked)}.lock"
            leaked.append(open(lock_path, "a"))  # a section that forgets to close its lock file
            yield held

    assert real_counted is not None, "the service keeps the lock it was given"
    service._mutation_lock = leaking
    pool = [socket.socket() for _ in range(5)]
    try:
        await service.stage_read_set_transaction(transaction_id=RS, intent_digest=INTENT,
                                                 participant="project", reads=reads, catalog="")
        assert _lock_descriptors(tmp_path) - locks_before == len(reads), "every leaked lock file is seen"
    finally:
        for handle in leaked:
            handle.close()
        for sock in pool:
            sock.close()
    assert _lock_descriptors(tmp_path) <= locks_before
