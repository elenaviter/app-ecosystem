"""The one-time admin minimum backfill (operator 2026-10-09: "Write directly to data where its is stored";
"All admins must be patched"): only project.cards.manage is added, through the Hub's own Card service,
idempotently, and never over a Card that moved since the check."""
from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import pathlib
import sys
from contextlib import asynccontextmanager

import pytest

HERE = pathlib.Path(__file__).resolve().parent
PACKAGE_TESTS = HERE.parents[2] / "packages" / "connection-hub" / "tests"
sys.path.insert(0, str(PACKAGE_TESTS))

from connection_hub.delegated_credentials.cards import transaction_store as tx  # noqa: E402
from connection_hub.delegated_credentials.cards.service import DelegatedCardService  # noqa: E402
from connection_hub.delegated_credentials.cards.store import (  # noqa: E402
    BundleStorageDelegatedCardStore,
    CardStorageError,
    subject_hash_for,
)
from test_card_service import NOW, _authority, _Cache  # noqa: E402

spec = importlib.util.spec_from_file_location("admin_minimum_backfill", HERE.parent / "scripts" / "admin_minimum_backfill.py")
backfill = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = backfill
spec.loader.exec_module(backfill)

PB = "https://board.example.test/mcp/problem_board"
OTHER = "https://example.test/mcp"


@asynccontextmanager
async def _lock(**kwargs):
    yield


def _card(access_id, *, grantor, operations=("review.accept",), grants=("work:admin", "work:review")):
    return dataclasses.replace(
        _authority(), access_id=access_id, grantor_subject=grantor,
        operations=tuple(sorted(set(operations) | {"messages.search"})),
        resource_operations={PB: tuple(operations), OTHER: ("messages.search",)},
        resource_grants={PB: tuple(grants), OTHER: ("messages:read",)})


async def _world(tmp_path, *cards):
    store = BundleStorageDelegatedCardStore(tmp_path / "bundle")
    service = DelegatedCardService(store=store, cache=_Cache(), mutation_lock=_lock)
    targets = []
    for kind, card in cards:
        await service.commit(card, subject_hash=subject_hash_for(card.grantor_subject), expected_revision=0, now=NOW)
        targets.append(backfill.Target("work:project:x", kind, "", subject_hash_for(card.grantor_subject),
                                       card.access_id))
    return store, service, targets


@pytest.mark.asyncio
async def test_check_then_apply_adds_only_the_operation_and_a_second_check_changes_nothing(tmp_path):
    control = _card("aut_control1", grantor="project:x")
    holder = _card("aut_holder01", grantor="person-1", operations=("review.accept", backfill.OPERATION))
    store, service, targets = await _world(tmp_path, ("control", control), ("my", holder))
    plan = {"schema": backfill.PLAN_SCHEMA, "rows": await backfill.check(store, targets, now=NOW)}
    assert [(row["card"], row["revision"], row["change"], row["admin_grant"]) for row in plan["rows"]] == [
        ("control", 1, True, True), ("my", 1, False, True)]
    rows = await backfill.apply(store, service, targets, json.loads(json.dumps(plan)), now=NOW)
    assert [(row["applied"], row["revision"], row["refused"]) for row in rows] == [(True, 2, ""), (False, 1, "")]
    after = (await store.read_current_authority(subject_hash=targets[0].subject_hash, access_id="aut_control1"))[1]
    assert after.card_revision == 2
    assert set(after.resource_operations[PB]) == {"review.accept", backfill.OPERATION}
    assert backfill.OPERATION in after.operations
    # Nothing else moved: the other resource, every grant, the identity and the acceptance.
    for field in ("resource_grants", "resource_acceptance", "grantor_subject", "card_kind", "properties",
                  "named_service_operations", "account_scope", "control_card", "state", "expires_at"):
        assert getattr(after, field) == getattr(control, field), field
    assert after.resource_operations[OTHER] == control.resource_operations[OTHER]
    again = await backfill.check(store, targets, now=NOW)
    assert [row["change"] for row in again] == [False, False]


@pytest.mark.asyncio
async def test_apply_refuses_a_card_that_moved_since_the_check(tmp_path):
    control = _card("aut_control1", grantor="project:x")
    store, service, targets = await _world(tmp_path, ("control", control))
    plan = {"schema": backfill.PLAN_SCHEMA, "rows": await backfill.check(store, targets, now=NOW)}
    edited = dataclasses.replace(control, card_revision=2, label="edited meanwhile")
    await service.commit(edited, subject_hash=targets[0].subject_hash, expected_revision=1, now=NOW)
    rows = await backfill.apply(store, service, targets, plan, now=NOW)
    assert rows[0]["refused"] == "card_changed_since_check" and rows[0]["applied"] is False
    current = (await store.read_current_authority(subject_hash=targets[0].subject_hash, access_id="aut_control1"))[1]
    assert current.card_revision == 2 and backfill.OPERATION not in current.resource_operations[PB]


@pytest.mark.asyncio
async def test_a_card_inside_a_card_transaction_is_refused_never_written(tmp_path):
    from datetime import datetime, timezone

    class Decisions:
        async def decision(self, receipt):
            return "undecided"

    control = _card("aut_control1", grantor="project:x")
    store, service, targets = await _world(tmp_path, ("control", control))
    plan = {"schema": backfill.PLAN_SCHEMA, "rows": await backfill.check(store, targets, now=NOW)}
    tx.bind_transaction_decisions(store, Decisions())
    await tx.stage(store, transaction_id="a" * 64, intent_digest="b" * 64, participant="project",
                   subject_hash=targets[0].subject_hash, original=control,
                   candidate=dataclasses.replace(control, card_revision=2, label="staged"),
                   now=datetime.fromtimestamp(NOW, timezone.utc))
    rows = await backfill.apply(store, service, targets, plan, now=NOW)
    assert rows[0]["applied"] is False and rows[0]["refused"]


@pytest.mark.asyncio
async def test_cards_without_a_single_problem_board_resource_or_inactive_are_refused(tmp_path):
    none = dataclasses.replace(_card("aut_nopb0001", grantor="person-2"),
                               resource_operations={OTHER: ("messages.search",)},
                               resource_grants={OTHER: ("messages:read",)})
    revoked = dataclasses.replace(_card("aut_revoked1", grantor="person-3"), state="revoked")
    store, service, targets = await _world(tmp_path, ("my", none), ("my", revoked))
    rows = await backfill.check(store, targets, now=NOW)
    assert [row["refused"] for row in rows] == ["problem_board_resource_not_unique", "card_not_active"]
    assert not any(row["change"] for row in rows)


@pytest.mark.asyncio
async def test_a_project_card_is_found_in_its_partition_by_id(tmp_path):
    p = _card("aut_projectP", grantor="project:creator")
    store, service, _ = await _world(tmp_path, ("project", p))
    resolved = await backfill.resolve_project_cards(store, [backfill.Target("work:project:x", "project", "", "",
                                                                            "aut_projectP")])
    assert resolved[0].subject_hash == subject_hash_for("project:creator")
    rows = await backfill.check(store, resolved, now=NOW)
    assert rows[0]["change"] is True and rows[0]["revision"] == 1


@pytest.mark.asyncio
@pytest.mark.skipif(not os.getenv("PB_TEST_POSTGRES_DSN"), reason="needs a PostgreSQL DSN in PB_TEST_POSTGRES_DSN")
async def test_targets_come_from_problem_boards_committed_admins_project_card_first():
    import asyncpg
    from connection_hub.delegated_credentials.project_identity_lifecycle import ProjectPersonCardIdentity

    connection = await asyncpg.connect(os.environ["PB_TEST_POSTGRES_DSN"])
    schema = "backfill_test"
    try:
        await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE; CREATE SCHEMA {schema}")
        await connection.execute(f"""
            CREATE TABLE {schema}.problem_board_projects (tenant TEXT, project TEXT, bundle_id TEXT, project_ref TEXT);
            CREATE TABLE {schema}.problem_board_project_access (tenant TEXT, project TEXT, bundle_id TEXT,
                project_ref TEXT, principal_key TEXT, role TEXT);
            CREATE TABLE {schema}.problem_board_control_card_links (tenant TEXT, project TEXT, bundle_id TEXT,
                project_ref TEXT, control_id TEXT);
            CREATE TABLE {schema}.problem_board_card_migration_intents (tenant TEXT, project TEXT, bundle_id TEXT,
                state TEXT);""")
        scope = ("t", "p", "problem-board@1-0")
        for ref in ("work:project:a", "work:project:b"):
            await connection.execute(f"INSERT INTO {schema}.problem_board_projects VALUES ($1,$2,$3,$4)", *scope, ref)
        await connection.execute(f"INSERT INTO {schema}.problem_board_control_card_links VALUES ($1,$2,$3,$4,$5)",
                                 *scope, "work:project:a", "control-pa")
        for ref, key, role in (("work:project:a", "user:alice", "owner"), ("work:project:a", "user:bob", "member"),
                               ("work:project:a", "user:carol", "admin"), ("work:project:a", "card:agent1", "admin"),
                               ("work:project:b", "user:dave", "owner")):
            await connection.execute(f"INSERT INTO {schema}.problem_board_project_access VALUES ($1,$2,$3,$4,$5,$6)",
                                     *scope, ref, key, role)
        await connection.execute(f"INSERT INTO {schema}.problem_board_project_access VALUES ($1,$2,$3,$4,$5,$6)",
                                 "other-tenant", "p", "problem-board@1-0", "work:project:a", "user:mallory", "owner")
        targets = await backfill.problem_board_targets(connection, schema=schema, scope=scope)
        assert await backfill.problem_board_in_flight(connection, schema=schema, scope=scope) == {
            "migration_intents_open": 0}
    finally:
        await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await connection.close()
    assert [(t.project_ref, t.kind, t.person) for t in targets] == [
        ("work:project:a", "project", ""),
        ("work:project:a", "control", "alice"), ("work:project:a", "my", "alice"),
        ("work:project:a", "control", "carol"), ("work:project:a", "my", "carol"),
        ("work:project:b", "control", "dave"), ("work:project:b", "my", "dave")]
    alice = ProjectPersonCardIdentity.build(project_ref="work:project:a", person_subject="alice")
    assert (targets[1].access_id, targets[1].subject_hash) == (alice.control_id, subject_hash_for(alice.project_subject))
    assert (targets[2].access_id, targets[2].subject_hash) == (alice.my_card_id, subject_hash_for("alice"))
    assert targets[0].access_id == "control-pa" and targets[0].subject_hash == ""  # resolved from the Hub store
