# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Elena Viter

"""Public composition root for project invitation Card lifecycle operations."""

from __future__ import annotations

from typing import Any

from connection_hub.delegated_credentials.project_authorization import (
    ProjectAuthorizationPort,
)
from connection_hub.delegated_credentials.project_invitation_binding import (
    ProjectInvitationBindingResolver,
)
from connection_hub.delegated_credentials.project_invitation_claim import (
    PROJECT_INVITATION_BINDING_PROVENANCE,
)
from connection_hub.delegated_credentials.project_invitation_pending import (
    AuthorityFromRecord,
    ProjectInvitationPendingCards,
    RecordFromAuthority,
)
from connection_hub.delegated_credentials.project_invitation_redemption import (
    ProjectInvitationRedemption,
)


class ProjectInvitationControlLifecycle:
    """Route public operations to their focused lifecycle owner."""

    def __init__(
        self,
        *,
        host: Any,
        authorization_port: ProjectAuthorizationPort | None,
        binding_resolver: ProjectInvitationBindingResolver | None,
        authority_from_record: AuthorityFromRecord,
        record_from_authority: RecordFromAuthority,
    ) -> None:
        self._pending = ProjectInvitationPendingCards(
            host=host,
            authorization_port=authorization_port,
            authority_from_record=authority_from_record,
            record_from_authority=record_from_authority,
        )
        self._redemption = ProjectInvitationRedemption(
            host=host,
            pending_cards=self._pending,
            binding_resolver=binding_resolver,
            authority_from_record=authority_from_record,
            record_from_authority=record_from_authority,
        )

    async def get(self, **kwargs: Any) -> dict[str, Any]:
        return await self._pending.get(**kwargs)

    async def create(self, **kwargs: Any) -> dict[str, Any]:
        return await self._pending.create(**kwargs)

    async def update(self, **kwargs: Any) -> dict[str, Any]:
        return await self._pending.update(**kwargs)

    async def revoke(self, **kwargs: Any) -> dict[str, Any]:
        return await self._pending.revoke(**kwargs)

    async def bind(self, **kwargs: Any) -> dict[str, Any]:
        return await self._redemption.bind(**kwargs)


__all__ = [
    "PROJECT_INVITATION_BINDING_PROVENANCE",
    "ProjectInvitationControlLifecycle",
]
