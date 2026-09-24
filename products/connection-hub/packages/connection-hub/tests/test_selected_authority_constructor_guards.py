from __future__ import annotations

import pytest

from connection_hub.delegated_credentials.automation_access import (
    AutomationAccessService,
)
from connection_hub.delegated_credentials.cards.persistence import (
    DurableCardPersistence,
)
from connection_hub.delegated_credentials.cards.service import (
    CardServingUnavailable,
)
from connection_hub.delegated_credentials.oauth.store import (
    GrantStoreUnavailable,
)


def test_postgresql_automation_service_requires_bound_grant_store() -> None:
    with pytest.raises(
        GrantStoreUnavailable,
        match="selected_authority.automation_grant_store_not_bound",
    ):
        AutomationAccessService(
            redis=object(),
            tenant="tenant-a",
            project="project-a",
            config=None,  # type: ignore[arg-type]
            authority_backend="postgresql",
        )


def test_postgresql_card_persistence_requires_bound_credential_handles() -> None:
    with pytest.raises(
        CardServingUnavailable,
        match="selected_authority.credential_handles_not_bound",
    ):
        DurableCardPersistence(
            redis=object(),
            tenant="tenant-a",
            project="project-a",
            card_store=object(),
            mutation_lock=object(),  # type: ignore[arg-type]
            authority_backend="postgresql",
        )
