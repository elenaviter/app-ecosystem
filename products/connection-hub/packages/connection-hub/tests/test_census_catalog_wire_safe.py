"""Live 2026-10-09: "Fit to role" failed with participant_unavailable. Every census answer carries the active
catalog, and every catalog since 2026-09-02 held the Hub's own default
``connections.delegated_credentials.oauth.client_id_metadata_documents.fetch_timeout_seconds: 5.0``. The signed
participant wire carries integers only, so signing refused every census.

The catalog now stores only wire-carriable values: an integral float is its integer, any other float refuses
publication naming its path. A census answer carries such a catalog and signs.
"""
from __future__ import annotations

import copy
import logging
import math

import pytest

from connection_hub.delegated_credentials.catalog.models import (
    CatalogDocument,
    CatalogNotWireSafe,
    wire_safe_connections,
)
from connection_hub.delegated_credentials.catalog.publisher import CatalogPublicationError
from connection_hub.delegated_credentials.catalog.hashing import connections_content_hash
from service_foundation.coordination.durable_wire import WireRefused, canonical_json_bytes
import test_card_census_read as census
from test_catalog_publisher import CONNECTIONS
from test_w502_catalog_reservation import _publish

TIMEOUT_PATH = "connections.delegated_credentials.oauth.client_id_metadata_documents.fetch_timeout_seconds"


def _with_timeout(value):
    connections = copy.deepcopy(CONNECTIONS)
    connections["delegated_credentials"] = {"oauth": {"client_id_metadata_documents": {
        "enabled": True, "fetch_timeout_seconds": value, "allowed_hosts": ["a.example", "b.example"]}}}
    return connections


def test_an_integral_float_is_stored_as_its_integer_and_other_values_are_kept_exactly():
    cleaned = wire_safe_connections(_with_timeout(5.0))
    stored = cleaned["delegated_credentials"]["oauth"]["client_id_metadata_documents"]
    assert stored["fetch_timeout_seconds"] == 5 and type(stored["fetch_timeout_seconds"]) is int
    assert stored["enabled"] is True and stored["allowed_hosts"] == ["a.example", "b.example"]
    canonical_json_bytes(cleaned)  # the wire carries it


@pytest.mark.parametrize("value", [2.5, 0.1, math.inf, math.nan])
def test_any_other_float_refuses_naming_its_path_only(value):
    with pytest.raises(CatalogNotWireSafe) as refused:
        wire_safe_connections(_with_timeout(value))
    assert refused.value.path == TIMEOUT_PATH and refused.value.reason == "connections_not_wire_safe"
    assert str(value) not in str(refused.value)


def test_a_non_string_key_refuses():
    with pytest.raises(CatalogNotWireSafe):
        wire_safe_connections({"service": {1: "one"}})


def test_a_built_document_holds_the_integer_and_an_older_stored_document_still_verifies():
    built = CatalogDocument.build(_with_timeout(5.0))
    assert type(built.connections["delegated_credentials"]["oauth"]["client_id_metadata_documents"]
                ["fetch_timeout_seconds"]) is int
    # A version published before this fix still reads: its hash is verified over what it stored.
    old = _with_timeout(5.0)
    stored = {"schema": "connection_hub.delegated_catalog.v1", "version": "delegated_catalog_old",
              "content_hash": connections_content_hash(old), "created_at": "", "connections": old}
    assert CatalogDocument.from_mapping(stored).connections == old


@pytest.mark.asyncio
async def test_publication_refuses_a_fractional_float_and_logs_only_its_path(tmp_path, caplog):
    from connection_hub.delegated_credentials.catalog.store import BundleStorageDelegatedCatalogStore

    caplog.set_level(logging.ERROR)
    store = BundleStorageDelegatedCatalogStore(tmp_path / "bundle")
    with pytest.raises(CatalogPublicationError, match="connections_not_wire_safe"):
        await _publish(store, _with_timeout(2.5))
    assert TIMEOUT_PATH in caplog.text and "2.5" not in caplog.text
    assert await store.read_active() is None  # nothing published


@pytest.mark.asyncio
async def test_the_census_carries_a_catalog_published_with_the_hubs_default_and_signs(tmp_path, monkeypatch):
    monkeypatch.setattr(census, "CONNECTIONS", _with_timeout(5.0))
    operation, store, control, identity, catalog_store = await census._world(tmp_path)
    request = census._request([])
    result = census._verified(await operation.answer(request), request)
    active = await catalog_store.read_active()
    assert result["catalog"]["content_hash"] == active.content_hash
    assert result["catalog"]["document"]["connections"]["delegated_credentials"]["oauth"][
        "client_id_metadata_documents"]["fetch_timeout_seconds"] == 5


@pytest.mark.asyncio
async def test_a_census_over_a_catalog_stored_before_the_fix_names_the_path(tmp_path, monkeypatch, caplog):
    # Until the next deploy republishes the catalog, the stored active version still holds 5.0: the
    # refusal now names where.
    operation, store, control, identity, catalog_store = await census._world(tmp_path)
    old = _with_timeout(5.0)
    document = CatalogDocument(version="delegated_catalog_2026-09-02-00-00-00-000_aaaaaaaaaaaa",
                               content_hash=connections_content_hash(old), created_at="", connections=old)
    await catalog_store.publish_active(document)
    caplog.set_level(logging.ERROR)
    with pytest.raises(WireRefused):
        await operation.answer(census._request([]))
    assert f"answer.result.catalog.document.{TIMEOUT_PATH}" in caplog.text and "5.0" not in caplog.text
