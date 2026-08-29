# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

from prokura.connector_app_resolution import (
    resolve_connector_app_id,
    set_service_connector_apps,
)


def test_connector_selection_uses_explicit_portable_context():
    updates = []
    set_service_connector_apps(
        {"google": "gmail", "slack": ""},
        context_updater=updates.append,
    )

    assert updates == [{"connector_apps": {"google": "gmail"}}]
    assert resolve_connector_app_id("google", context=updates[0]) == "gmail"
    assert resolve_connector_app_id("slack", context=updates[0]) == ""
