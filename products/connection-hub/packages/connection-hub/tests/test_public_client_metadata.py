# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.oauth.clients import (
    PublicClient,
    client_from_record,
    normalize_public_client_metadata,
)


def test_public_client_metadata_is_json_safe_and_round_trips() -> None:
    metadata = {
        "kdcube_agent_id": "codex:session-1",
        "kdcube_machine": {"id": "machine-1", "labels": ["dev", "arm64"]},
        "interactive": True,
    }

    client = client_from_record(
        {
            "client_id": "dcr-worker",
            "redirect_uris": ["http://127.0.0.1/callback"],
            "metadata": {"client_name": "Worker", "client_metadata": metadata},
        }
    )

    assert client.client_metadata == metadata
    assert client.snapshot()["client_metadata"] == metadata
    assert PublicClient(
        client_id="dcr-worker",
        redirect_uris=("http://127.0.0.1/callback",),
        client_metadata=metadata,
    ).snapshot_digest() == PublicClient(
        client_id="dcr-worker",
        redirect_uris=("http://127.0.0.1/callback",),
        client_metadata=metadata,
    ).snapshot_digest()


@pytest.mark.parametrize(
    "metadata",
    [
        {"api_token": "secret"},
        {"nested": {"refresh_token": "secret"}},
        {"label": "line one\nline two"},
        {"bad key": "value"},
        {"depth": {"one": {"two": {"three": {"four": True}}}}},
    ],
)
def test_public_client_metadata_rejects_sensitive_or_unbounded_values(metadata) -> None:
    with pytest.raises(ValueError):
        normalize_public_client_metadata(metadata)
