"""The OAuth path: a re-consent never leaves a selection without its tree.

On 2026-09-28 an agent-grant write stored named_services: {} while the Card
still selected named-service operations, and every composition with the
Control failed (control_card_authority_invalid). The hosted-agent path was
fixed and checked live on 2026-11-18. The OAuth grant path rebuilds the tree
under a submitted selection too, and a connected client that declares
kdcube_credential_use=multi_resource holds several resources on one Card. This
pins, for every re-consent shape on such a Card, that the stored namespace tree
is exactly the stored selection.
"""

from __future__ import annotations

import pytest

import test_oauth_card_extension as fixture
from test_resident_profile_cards import GRANTOR, _Harness


def _selected_namespaces(card):
    stored = card.named_service_operations.to_stored()
    return {namespace for namespaces in stored.values() for namespace in namespaces}


def _tree_namespaces(card):
    return set(((card.named_services or {}).get("namespaces") or {}))


RECONSENTS = {
    "nothing submitted": {},
    "provider scopes only": {"scopes": ["named_services:use"]},
    "one resource's selection only": {
        "named_service_operations": {fixture.NAMED: {"slides": fixture.SLIDES}},
    },
    "one resource's whole authority": {
        "resource_grants": {fixture.NAMED: fixture.GRANTS},
        "resource_operations": {fixture.NAMED: ["named_services_call"]},
        "named_service_operations": {fixture.NAMED: {"slides": fixture.SLIDES}},
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(RECONSENTS))
async def test_a_reconsent_on_a_two_resource_card_keeps_the_tree_equal_to_the_selection(tmp_path, shape):
    harness = _Harness(tmp_path, connections=fixture._named_connections())
    record = await fixture._named_card(harness)
    assert _tree_namespaces(record) == _selected_namespaces(record) == {"slides", "notes"}

    await harness.service.record_oauth_grant(
        grantor_subject=GRANTOR,
        client_id=fixture.CLIENT,
        resource=fixture.NAMED,
        access_token="at-reconsent",
        refresh_token="rt-reconsent",
        **RECONSENTS[shape],
    )

    card = await harness.card(record.access_id)
    assert _selected_namespaces(card), shape
    assert _tree_namespaces(card) == _selected_namespaces(card), shape


@pytest.mark.asyncio
async def test_a_reconsent_that_submits_nothing_keeps_the_other_resource(tmp_path):
    harness = _Harness(tmp_path, connections=fixture._named_connections())
    record = await fixture._named_card(harness)

    await harness.service.record_oauth_grant(
        grantor_subject=GRANTOR, client_id=fixture.CLIENT, resource=fixture.NAMED,
        access_token="at-reconsent", refresh_token="rt-reconsent",
    )

    card = await harness.card(record.access_id)
    assert set(card.resource_grants) == {fixture.NAMED, fixture.OTHER_NAMED}
    assert card.named_service_operations.to_stored() == fixture._held_selection()
