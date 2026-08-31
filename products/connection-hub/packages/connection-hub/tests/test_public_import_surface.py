from __future__ import annotations


def test_connected_account_aggregate_exports_connection_hub_contract() -> None:
    from connection_hub.delegated_to_kdcube import (
        DelegatedToKdcubeStore,
        RedisOAuthStateStore,
        delegated_to_kdcube_config,
        operations_for_user,
        peek_state_payload,
        state_digest,
    )

    assert DelegatedToKdcubeStore.__module__ == "connection_hub.delegated_to_kdcube.store"
    assert RedisOAuthStateStore.__module__ == "connection_hub.delegated_to_kdcube.oauth"
    assert delegated_to_kdcube_config.__module__ == "connection_hub.delegated_to_kdcube.config"
    assert operations_for_user.__module__ == "connection_hub.delegated_to_kdcube.operations"
    assert peek_state_payload.__module__ == "connection_hub.delegated_to_kdcube.oauth"
    assert state_digest.__module__ == "connection_hub.delegated_to_kdcube.oauth"
