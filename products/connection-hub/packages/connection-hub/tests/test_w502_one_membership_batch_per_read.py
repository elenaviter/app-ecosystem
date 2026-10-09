# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter
"""W502 (C): one Control Card read asks the project host once per person.

Operator, 2026-10-09: "waiting 10 seconds for card retrieval is unacceptable";
"Checking 1000 operations must take same time as checking 2". A read asks the
policy port two questions (the read, and the viewer's edit question). Each
resolves the actor's membership from the project host, and the host computes
the acting person's delegable grants (one batch of every catalog operation)
for that answer. Within one read the membership answer is now shared, so the
batch is paid once. Nothing is kept across reads: the scope is the read.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from connection_hub.delegated_credentials.project_authorization import (
    PROJECT_PERSON_CONTROL_READ,
    PROJECT_PERSON_CONTROL_UPDATE,
    ProjectAuthorizationRequest,
    ProjectMembershipEvidence,
    ResolverBackedProjectAuthorizationPort,
    shared_membership_scope,
)
from test_project_person_access import ADMIN, PROJECT_REF, TARGET, _create, _Host, _lifecycle, _Port

ROLES = {ADMIN: "admin", TARGET: "member"}


class _CountingResolver:
    """The project host: one call here is one membership answer, with its grants batch."""

    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.calls: Counter[str] = Counter()
        self.gate = gate

    async def resolve_project_membership(self, *, project_ref: str, subject: str):
        self.calls[subject] += 1
        if self.gate is not None:
            await self.gate.wait()
        return ProjectMembershipEvidence.build(
            project_ref=project_ref, subject=subject, role=ROLES[subject],
            delegable_grants=("work:admin",) if ROLES[subject] == "admin" else (),
        )


def _port(resolver: _CountingResolver) -> ResolverBackedProjectAuthorizationPort:
    return ResolverBackedProjectAuthorizationPort(resolver=resolver, administrative_roles=("admin",))


async def _read(resolver: _CountingResolver, *, actor: str):
    host = _Host()
    await _create(_lifecycle(host, _Port()))
    return await _lifecycle(host, _port(resolver)).get(
        actor_subject=actor, project_ref=PROJECT_REF, target_subject=TARGET, request_id="request-read",
    )


@pytest.mark.asyncio
async def test_an_admin_reading_a_persons_card_asks_the_host_once_per_person() -> None:
    resolver = _CountingResolver()
    view = await _read(resolver, actor=ADMIN)
    assert view["ok"] is True
    assert view["viewer"]["can_edit"] is True
    # Before: ADMIN twice (the read and the edit question), so the grants batch twice.
    assert resolver.calls == Counter({ADMIN: 1, TARGET: 1})


@pytest.mark.asyncio
async def test_a_member_reading_their_own_card_asks_the_host_once() -> None:
    resolver = _CountingResolver()
    view = await _read(resolver, actor=TARGET)
    assert view["ok"] is True
    assert view["viewer"] == {"can_edit": False, "reason": "project_person_control_decided_by_admin"}
    assert resolver.calls == Counter({TARGET: 1})


@pytest.mark.asyncio
async def test_nothing_is_kept_across_reads() -> None:
    """Distributed system, no module-level caches (operator): the next read asks again."""

    resolver = _CountingResolver()
    host = _Host()
    await _create(_lifecycle(host, _Port()))
    lifecycle = _lifecycle(host, _port(resolver))
    for request_id in ("read-1", "read-2"):
        view = await lifecycle.get(actor_subject=ADMIN, project_ref=PROJECT_REF, target_subject=TARGET,
                                   request_id=request_id)
        assert view["ok"] is True
    assert resolver.calls == Counter({ADMIN: 2, TARGET: 2})


def _request(operation: str, *, actor: str = ADMIN, request_id: str = "r") -> ProjectAuthorizationRequest:
    return ProjectAuthorizationRequest.build(
        actor_subject=actor, project_ref=PROJECT_REF, target_subject=TARGET,
        operation=operation, request_id=request_id,
    )


@pytest.mark.asyncio
async def test_outside_a_read_every_question_asks_the_host() -> None:
    """A save or any other question outside the read's scope is unchanged."""

    resolver = _CountingResolver()
    port = _port(resolver)
    await port.authorize_project_person_control(_request(PROJECT_PERSON_CONTROL_UPDATE))
    await port.authorize_project_person_control(_request(PROJECT_PERSON_CONTROL_UPDATE))
    assert resolver.calls == Counter({ADMIN: 2, TARGET: 2})


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [ADMIN, TARGET])
async def test_the_shared_answer_decides_exactly_as_separate_answers(actor: str) -> None:
    """Authorization semantics are unchanged: same allow/deny, reason and grants."""

    def public(decision):
        return (decision.allowed, decision.reason, decision.delegable_grants)

    questions = [_request(operation, actor=actor) for operation in
                 (PROJECT_PERSON_CONTROL_READ, PROJECT_PERSON_CONTROL_UPDATE)]
    separate = [public(await _port(_CountingResolver()).authorize_project_person_control(q)) for q in questions]
    port = _port(_CountingResolver())
    with shared_membership_scope():
        shared = [public(d) for d in await asyncio.gather(
            *(port.authorize_project_person_control(q) for q in questions))]
    assert shared == separate


@pytest.mark.asyncio
async def test_retiring_one_question_never_cancels_the_shared_answer() -> None:
    """The read retires the viewer's edit question; the read still gets its answer."""

    resolver = _CountingResolver(gate=asyncio.Event())
    port = _port(resolver)
    with shared_membership_scope():
        edit = asyncio.ensure_future(port.authorize_project_person_control(
            _request(PROJECT_PERSON_CONTROL_UPDATE, request_id="r:viewer")))
        read = asyncio.ensure_future(port.authorize_project_person_control(_request(PROJECT_PERSON_CONTROL_READ)))
        while not resolver.calls:
            await asyncio.sleep(0)
        for _ in range(3):  # both questions now wait on the one shared answer
            await asyncio.sleep(0)
        assert resolver.calls == Counter({ADMIN: 1})
        edit.cancel()
        for _ in range(3):
            await asyncio.sleep(0)
        resolver.gate.set()
        decision = await read
    assert edit.cancelled()
    assert decision.allowed is True
    assert resolver.calls == Counter({ADMIN: 1, TARGET: 1})
