"""W502 (EMain 19:02): the real project-person entry points against a Card read fence on the My Card.

``AutomationAccessService`` over ``DurableCardPersistence``, the real Card
store and Redis; only the project authorization port is a stand-in. A
prepared transaction that read the derived My Card id holds it:

- ``card-absent:<sha256(person)>:<my id>``: ``project_person_control_create``,
  the entry point that creates the My Card at that id, is refused until the
  transaction finishes, then admitted at the same derived id;
- ``card:<sha256(person)>:<my id>`` at its revision: ``project_person_my_card_seed``,
  which rewrites an existing untouched My Card, is refused until finish, then
  admitted. (The seed never creates a My Card: with none it is refused
  ``project_identity_edge_missing`` whatever any fence says.)
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

import pytest

from connection_hub.delegated_credentials.cards import transaction_store as tx
from connection_hub.delegated_credentials.cards.store import subject_hash_for
from connection_hub.delegated_credentials.controls.project_person import ProjectPersonControlIdentity
from connection_hub.delegated_credentials.controls.snapshot import materialize_control_snapshot
from connection_hub.delegated_credentials.project_identity_lifecycle import ProjectPersonCardIdentity
from test_card_transaction_store import INTENT, TX
from connection_hub.delegated_credentials.project_authorization import ProjectAuthorizationDecision
from test_project_person_access import ADMIN, PROJECT_REF, TARGET
from test_w580_bound_card_writers import _hub, _manual_card, _memories, redis_client  # noqa: F401 - fixture

GRANT, OPERATION = "memories:read", "search"  # the harness catalog's own resource

USER = {"user_id": ADMIN}


class _Port:
    """The project's admin decision (a stand-in): allows delegating the harness grant."""

    async def authorize_project_person_control(self, request):
        return ProjectAuthorizationDecision.allow(request, delegable_grants=(GRANT,), platform_admin=True,
                                                  evidence={"membership_revision": 7})


async def _service(tmp_path, redis_client):  # noqa: F811
    h = await _hub(tmp_path, redis_client)
    h.service._project_authorization_port = _Port()
    h.service._bind_project_lifecycles()
    return h


async def _hold(h, *, read):
    """A prepared transaction on an unrelated Card that READ the My Card slot (``read``)."""
    card, _ = await _manual_card(h)
    candidate = dataclasses.replace(card, card_revision=card.card_revision + 1, label="staged")
    await tx.stage(h.store, transaction_id=TX, intent_digest=INTENT, participant="project",
                   subject_hash=subject_hash_for(card.grantor_subject), original=card, candidate=candidate,
                   now=datetime.fromtimestamp(h.now, timezone.utc), reads=[read])


async def _release(h):
    h.store._card_transaction_decisions.recorded[TX] = "aborted"
    await tx.decide(h.store, transaction_id=TX, intent_digest=INTENT, decision="aborted")


async def _create(h, request_id):
    return await h.service.project_person_control_create(
        USER, project_ref=PROJECT_REF, target_subject=TARGET, request_id=request_id, label="Quickstart member",
        migration=True)


async def _my_card(h):
    identity = ProjectPersonCardIdentity.build(project_ref=PROJECT_REF, person_subject=TARGET)
    loaded = await h.store.read_current_authority(subject_hash=subject_hash_for(TARGET),
                                                  access_id=identity.my_card_id)
    return identity, (loaded[1] if loaded is not None else None)


@pytest.mark.asyncio
async def test_create_waits_for_an_absent_my_fence_then_creates_at_the_derived_id(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    identity, absent = await _my_card(h)
    assert absent is None
    await _hold(h, read={"subject_hash": subject_hash_for(TARGET), "access_id": identity.my_card_id, "revision": 0})
    refused = await _create(h, "request-create-held")
    # The fence's own reason, as a retryable "not committed" (not a 409 conflict).
    assert refused == {"ok": False, "error": "project_person_control_not_committed",
                       "reason": "card_transaction_unresolved", "retryable": True, "status": 503}
    assert (await _my_card(h))[1] is None  # nothing written at the held slot
    # The lifecycle writes the Control Card first (as for any My-side failure): it stays at r1,
    # and the retry below completes the pair rather than writing a second Control.
    assert (await _control(h)).card_revision == 1
    await _release(h)
    created = await _create(h, "request-create-after")
    assert created["ok"] is True, created
    assert (await _my_card(h))[1].access_id == identity.my_card_id
    assert (await _control(h)).card_revision == 1


@pytest.mark.asyncio
async def test_seed_waits_for_a_my_card_read_then_is_admitted(tmp_path, redis_client):  # noqa: F811
    h = await _service(tmp_path, redis_client)
    assert (await _create(h, "request-create"))["ok"] is True
    await _select_control(h)
    identity, my_card = await _my_card(h)
    await _hold(h, read={"subject_hash": subject_hash_for(TARGET), "access_id": identity.my_card_id,
                         "revision": my_card.card_revision})
    seed = dict(project_ref=PROJECT_REF, target_subject=TARGET, resource_grants={_memories(): [GRANT]},
                resource_operations={_memories(): [OPERATION]})
    refused = await h.service.project_person_my_card_seed(USER, request_id="request-seed-held", **seed)
    assert refused == {"ok": False, "error": "delegated_card_not_committed", "reason": "card_transaction_unresolved",
                       "retryable": True, "status": 503}
    assert (await _my_card(h))[1] == my_card
    await _release(h)
    seeded = await h.service.project_person_my_card_seed(USER, request_id="request-seed-after", **seed)
    assert seeded["ok"] is True and seeded["seeded"] is True, seeded
    assert (await _my_card(h))[1].card_revision == my_card.card_revision + 1


async def _control(h):
    identity = ProjectPersonControlIdentity.build(project_ref=PROJECT_REF, target_subject=TARGET)
    return (await h.store.read_current_authority(subject_hash=subject_hash_for(identity.project_subject),
                                                 access_id=identity.control_id))[1]


async def _select_control(h):
    """The admin's Control selection the seed is capped by (as test_project_person_access does)."""
    identity = ProjectPersonControlIdentity.build(project_ref=PROJECT_REF, target_subject=TARGET)
    current = await _control(h)
    selected = materialize_control_snapshot(
        dataclasses.replace(current, operations=(OPERATION,), resource_grants={_memories(): (GRANT,)},
                            resource_operations={_memories(): (OPERATION,)}),
        basis_catalog_version=current.catalog_version, origin="updated")
    await h.cards.commit(dataclasses.replace(selected, card_revision=current.card_revision + 1),
                         subject_hash=subject_hash_for(identity.project_subject),
                         expected_revision=current.card_revision, now=h.now)
