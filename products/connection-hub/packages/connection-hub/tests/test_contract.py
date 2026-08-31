# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from connection_hub.contract import (
    CONNECTION_OPERATIONS,
    CatalogEntry,
    Connection,
    ConnectionOperationSpec,
    build_connection_operations,
)


def test_operation_inventory_is_attribute_and_mapping_compatible():
    operations = build_connection_operations(("local", "api"))
    assert tuple(operations) == CONNECTION_OPERATIONS
    for operation, spec in operations.items():
        assert isinstance(spec, ConnectionOperationSpec)
        assert spec.operation == operation
        assert spec.transports == ("local", "api")
        assert spec["transports"] == ["local", "api"]
        assert dict(spec)["operation"] == operation


def test_connection_contract_round_trips_without_host_types():
    connection = Connection.from_dict(
        {
            "provider": "mail",
            "account_id": "acc_1",
            "scope": ["mail:read"],
            "has_token": True,
        }
    )
    entry = CatalogEntry.from_dict(
        {
            "provider": "mail",
            "connected": True,
            "accounts": [connection.to_dict()],
        }
    )
    assert entry.accounts == (connection,)
    assert CatalogEntry.from_dict(entry.to_dict()) == entry
