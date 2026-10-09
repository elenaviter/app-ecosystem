"""One-time operator backfill: every Problem Board admin's Cards hold ``project.cards.manage``.

Operator, 2026-10-09 (verbatim): "You must patch all cards. All cards that we have in pb";
"For any project and each admin in that project"; "Write directly to data where its is stored";
"I do not care how you make it. Simply make it. It's one time." and "All admins must be patched".

For every Problem Board project: the project Control Card P and, for every member whose COMMITTED role
is owner or admin, their person Control Card and My Card gain the operation ``project.cards.manage`` on
the Problem Board resource. Nothing else changes: the grant ``work:admin`` is already on every one of
these Cards (live read 2026-10-09), so it is only reported when missing, never added.

The write goes through Connection Hub's own Card service (``DelegatedCardService.commit``): the shared
mutation lock, the expected-revision check, the refusal of a Card inside a lifecycle or Card transaction,
the immutable revision, the current pointer, the Redis projection and the index. Never a raw file write.

Run in the chat-proc container as the service user, after a backup of the Hub's delegated-cards folder:

    python admin_minimum_backfill.py check --plan /tmp/backfill-plan.json
    python admin_minimum_backfill.py apply --plan /tmp/backfill-plan.json
    python admin_minimum_backfill.py check --plan /tmp/backfill-verify.json   # every Card: unchanged

Inputs (environment): PB_POSTGRES_DSN, PB_SCHEMA, PB_TENANT, PB_PROJECT, PB_BUNDLE_ID (Problem Board's
committed membership is read, never typed); HUB_STORAGE_ROOT, HUB_TENANT, HUB_PROJECT, REDIS_URL and
HUB_LIFECYCLE_LOCK_SCOPE (as the Hub's own props name them). Output is value-free: project refs, Card id
prefixes, revisions and booleans.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable

OPERATION = "project.cards.manage"
ADMIN_GRANT = "work:admin"
RESOURCE_MARKER = "/mcp/problem_board"
ADMIN_ROLES = ("owner", "admin")
PLAN_SCHEMA = "connection-hub.admin-minimum-backfill-plan.v1"


@dataclass(frozen=True)
class Target:
    """One Card to patch: which project, which Card of it, and where the Hub keeps it."""

    project_ref: str
    kind: str  # "project" (P), "control" (a person's Control) or "my" (their My Card)
    person: str  # "" for P
    subject_hash: str
    access_id: str

    def key(self) -> str:
        return f"{self.subject_hash}/{self.access_id}"


def problem_board_resource(authority: Any) -> str:
    """The one Problem Board resource key this Card names; refuses none or several."""
    keys = sorted({str(key) for key in authority.resource_operations} | {str(key) for key in authority.resource_grants})
    found = [key for key in keys if RESOURCE_MARKER in key]
    if len(found) != 1:
        raise BackfillRefused("problem_board_resource_not_unique")
    return found[0]


class BackfillRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def with_operation(authority: Any, *, now: int) -> tuple[Any, str]:
    """The next revision holding ``project.cards.manage`` on the Problem Board resource, or the Card itself.

    The same delta as Problem Board's ``minimal_bootstrap_candidate`` without the grant (already held):
    the resource's operations and the flat operations gain the one operation; every other field stays.
    Returns (candidate, state): state is "change", "unchanged" (already holds it) or a refusal code.
    """
    if authority.state != "active":
        raise BackfillRefused("card_not_active")
    if authority.expires_at and now >= int(authority.expires_at):
        raise BackfillRefused("card_expired")
    resource = problem_board_resource(authority)
    held = set(authority.resource_operations.get(resource, ()))
    if OPERATION in held:
        return authority, "unchanged"
    operations = dict(authority.resource_operations)
    operations[resource] = tuple(sorted(held | {OPERATION}))
    candidate = dataclasses.replace(
        authority,
        card_revision=int(authority.card_revision) + 1,
        operations=tuple(sorted(set(authority.operations) | {OPERATION})),
        resource_operations=operations,
    )
    return candidate, "change"


def holds_admin_grant(authority: Any) -> bool:
    try:
        resource = problem_board_resource(authority)
    except BackfillRefused:
        return False
    return ADMIN_GRANT in set(authority.resource_grants.get(resource, ()))


async def resolve_project_cards(store: Any, targets: list[Target]) -> list[Target]:
    """Each P's grantor partition, from the Hub store (one partition per Card id; refuses ambiguity)."""
    from connection_hub.delegated_credentials.cards.store import subject_hash_for

    resolved = []
    for target in targets:
        if target.kind == "project" and not target.subject_hash:
            authority = await store.find_current_authority(target.access_id)
            if authority is not None:
                target = dataclasses.replace(target, subject_hash=subject_hash_for(authority.grantor_subject))
        resolved.append(target)
    return resolved


async def read_card(store: Any, target: Target) -> Any | None:
    if not target.subject_hash:
        return None
    loaded = await store.read_current_authority(subject_hash=target.subject_hash, access_id=target.access_id)
    return None if loaded is None else loaded[1]


async def check(store: Any, targets: Iterable[Target], *, now: int) -> list[dict[str, Any]]:
    """Per Card: what apply would do. Writes nothing."""
    rows = []
    for target in targets:
        row = {"project": target.project_ref, "card": target.kind, "access": target.access_id[:18],
               "key": target.key(), "revision": None, "change": False, "admin_grant": None, "refused": ""}
        try:
            authority = await read_card(store, target)
            if authority is None:
                raise BackfillRefused("card_absent")
            row["revision"] = int(authority.card_revision)
            row["content_hash"] = authority.content_hash()
            row["admin_grant"] = holds_admin_grant(authority)
            _, state = with_operation(authority, now=now)
            row["change"] = state == "change"
        except BackfillRefused as exc:
            row["refused"] = exc.code
        except Exception as exc:  # noqa: BLE001 - one unreadable Card is reported, never a crash mid-run
            row["refused"] = f"unreadable:{type(exc).__name__}"
        rows.append(row)
    return rows


async def apply(store: Any, service: Any, targets: Iterable[Target], plan: dict[str, Any], *,
                now: int) -> list[dict[str, Any]]:
    """Commit each planned change, only when the Card is still exactly what check saw."""
    planned = {row["key"]: row for row in plan["rows"]}
    rows = []
    for target in targets:
        row = {"project": target.project_ref, "card": target.kind, "access": target.access_id[:18],
               "revision": None, "applied": False, "refused": ""}
        seen = planned.get(target.key())
        try:
            if seen is None:
                raise BackfillRefused("not_in_plan")
            if not seen["change"]:
                row["revision"] = seen["revision"]
                rows.append(row)
                continue
            authority = await read_card(store, target)
            if authority is None:
                raise BackfillRefused("card_absent")
            if int(authority.card_revision) != seen["revision"] or authority.content_hash() != seen["content_hash"]:
                raise BackfillRefused("card_changed_since_check")
            candidate, state = with_operation(authority, now=now)
            if state != "change":
                raise BackfillRefused("card_changed_since_check")
            await service.commit(candidate, subject_hash=target.subject_hash,
                                 expected_revision=int(authority.card_revision), now=now)
            row["revision"] = int(candidate.card_revision)
            row["applied"] = True
        except BackfillRefused as exc:
            row["refused"] = exc.code
        except Exception as exc:  # noqa: BLE001 - reported per Card; the Hub service refused or failed
            row["refused"] = f"commit_failed:{type(exc).__name__}:{getattr(exc, 'reason', '') or ''}"
        rows.append(row)
    return rows


def print_rows(rows: list[dict[str, Any]], *, mode: str) -> None:
    for row in rows:
        fields = [f"project={row['project']}", f"card={row['card']}", f"access={row['access']}",
                  f"revision={row['revision']}"]
        if mode == "check":
            fields += [f"would_change={'yes' if row['change'] else 'no'}",
                       f"admin_grant={row['admin_grant']}"]
        else:
            fields += [f"applied={'yes' if row['applied'] else 'no'}"]
        if row["refused"]:
            fields.append(f"refused={row['refused']}")
        print(" ".join(fields))
    key = "change" if mode == "check" else "applied"
    print(f"total cards={len(rows)} {'would_change' if mode == 'check' else 'applied'}="
          f"{sum(1 for row in rows if row[key])} refused={sum(1 for row in rows if row['refused'])}")


# --- Problem Board's committed targets ------------------------------------------------------------

async def problem_board_targets(connection: Any, *, schema: str, scope: tuple[str, str, str]) -> list[Target]:
    """Every project's P, then each committed owner/admin's Control and My Card (ancestors first)."""
    from connection_hub.delegated_credentials.cards.store import subject_hash_for
    from connection_hub.delegated_credentials.project_identity_lifecycle import ProjectPersonCardIdentity

    tenant, project, bundle_id = scope
    projects = await connection.fetch(
        f"""SELECT project_ref FROM {schema}.problem_board_projects
             WHERE tenant=$1 AND project=$2 AND bundle_id=$3 ORDER BY project_ref""", tenant, project, bundle_id)
    targets: list[Target] = []
    for row in projects:
        project_ref = str(row["project_ref"])
        link = await connection.fetchrow(
            f"""SELECT control_id FROM {schema}.problem_board_control_card_links
                 WHERE tenant=$1 AND project=$2 AND bundle_id=$3 AND project_ref=$4""",
            tenant, project, bundle_id, project_ref)
        if link is not None:
            # P's grantor partition is resolved from the Hub store itself (resolve_project_cards): an older
            # link row may not record its creator subject.
            targets.append(Target(project_ref, "project", "", "", str(link["control_id"])))
        admins = await connection.fetch(
            f"""SELECT principal_key FROM {schema}.problem_board_project_access
                 WHERE tenant=$1 AND project=$2 AND bundle_id=$3 AND project_ref=$4
                   AND role = ANY($5::text[]) AND principal_key LIKE 'user:%'
                 ORDER BY principal_key""", tenant, project, bundle_id, project_ref, list(ADMIN_ROLES))
        for admin in admins:
            person = str(admin["principal_key"])[len("user:"):]
            identity = ProjectPersonCardIdentity.build(project_ref=project_ref, person_subject=person)
            targets.append(Target(project_ref, "control", person, subject_hash_for(identity.project_subject),
                                  identity.control_id))
            targets.append(Target(project_ref, "my", person, subject_hash_for(person), identity.my_card_id))
    return targets


async def problem_board_in_flight(connection: Any, *, schema: str, scope: tuple[str, str, str]) -> dict[str, int]:
    """Problem Board work that names Card revisions and must not be in flight during the write."""
    tenant, project, bundle_id = scope
    intents = await connection.fetchval(
        f"""SELECT count(*) FROM {schema}.problem_board_card_migration_intents
             WHERE tenant=$1 AND project=$2 AND bundle_id=$3 AND state IN ('pending','partial')""",
        tenant, project, bundle_id)
    return {"migration_intents_open": int(intents or 0)}


async def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("check", "apply"))
    parser.add_argument("--plan", required=True, help="check writes it; apply reads it")
    args = parser.parse_args(argv)
    env = os.environ
    import asyncpg
    import redis.asyncio as redis_asyncio
    from connection_hub.delegated_credentials.cards.store import BundleStorageDelegatedCardStore
    from kdcube_ai_app.apps.chat.sdk.integrations.connection_hub.delegated_credentials.cards.persistence import (
        DurableCardPersistence,
    )

    scope = (env["PB_TENANT"], env["PB_PROJECT"], env["PB_BUNDLE_ID"])
    connection = await asyncpg.connect(env["PB_POSTGRES_DSN"])
    try:
        targets = await problem_board_targets(connection, schema=env["PB_SCHEMA"], scope=scope)
        in_flight = await problem_board_in_flight(connection, schema=env["PB_SCHEMA"], scope=scope)
    finally:
        await connection.close()
    print(" ".join(f"{name}={count}" for name, count in in_flight.items()))
    if args.mode == "apply" and any(in_flight.values()):
        print("refused: Problem Board Card work is in flight; finish or abort it first", file=sys.stderr)
        return 2
    store = BundleStorageDelegatedCardStore(env["HUB_STORAGE_ROOT"],
                                            lifecycle_lock_scope=env.get("HUB_LIFECYCLE_LOCK_SCOPE", ""))
    targets = await resolve_project_cards(store, targets)
    now = int(time.time())
    if args.mode == "check":
        rows = await check(store, targets, now=now)
        with open(args.plan, "w", encoding="utf-8") as handle:
            json.dump({"schema": PLAN_SCHEMA, "rows": rows}, handle, sort_keys=True)
        print_rows(rows, mode="check")
        return 0
    with open(args.plan, encoding="utf-8") as handle:
        plan = json.load(handle)
    if plan.get("schema") != PLAN_SCHEMA:
        print("refused: plan schema", file=sys.stderr)
        return 2
    redis = redis_asyncio.from_url(env["REDIS_URL"])
    try:
        persistence = DurableCardPersistence(redis=redis, tenant=env["HUB_TENANT"], project=env["HUB_PROJECT"],
                                             card_store=store)
        rows = await apply(persistence.card_store, persistence.card_service, targets, plan, now=now)
    finally:
        await redis.aclose()
    print_rows(rows, mode="apply")
    return 1 if any(row["refused"] for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
