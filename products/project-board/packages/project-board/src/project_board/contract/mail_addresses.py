from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .errors import DomainError


@dataclass(frozen=True)
class MailRecipient:
    worker_name: str
    worker_alias: str
    pool_status: str
    retired_at: str
    retirement_reason: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MailRecipient":
        return cls(
            worker_name=str(value.get("worker_name") or "").strip().lower(),
            worker_alias=str(value.get("worker_alias") or "").strip(),
            pool_status=str(value.get("pool_status") or "active").strip().lower(),
            retired_at=str(value.get("retired_at") or "").strip(),
            retirement_reason=str(value.get("retirement_reason") or "").strip(),
        )

    def as_mapping(self) -> dict[str, str]:
        return {
            "worker_name": self.worker_name,
            "worker_alias": self.worker_alias,
            "pool_status": self.pool_status,
            "retired_at": self.retired_at,
            "retirement_reason": self.retirement_reason,
        }


def resolve_mail_recipient(
    recipient: Any,
    directory: Sequence[Mapping[str, Any]],
    *,
    error_namespace: str,
    directory_updated_at: str = "",
) -> MailRecipient:
    """Resolve only a stable worker name and explain alias/retirement failures."""

    address = str(recipient or "").strip().lower()
    if not address or len(address.encode("utf-8")) > 160:
        raise DomainError(
            f"{error_namespace}_worker_address_invalid",
            "A worker recipient must be a stable Problem Board worker address.",
            details={"recipient": str(recipient or "")},
        )

    recipients: dict[str, MailRecipient] = {}
    for raw in directory:
        if not isinstance(raw, Mapping):
            continue
        candidate = MailRecipient.from_mapping(raw)
        if candidate.worker_name:
            recipients[candidate.worker_name] = candidate

    resolved = recipients.get(address)
    if resolved is not None:
        if resolved.pool_status == "retired":
            reason = resolved.retirement_reason or "No retirement reason was recorded."
            raise DomainError(
                f"{error_namespace}_worker_retired",
                (
                    f"The addressed worker {resolved.worker_name} was retired. "
                    f"Reason: {reason} Redirect the message to an active worker."
                ),
                status=410,
                details={
                    "recipient": address,
                    "worker_name": resolved.worker_name,
                    "worker_alias": resolved.worker_alias,
                    "retired_at": resolved.retired_at,
                    "retirement_reason": resolved.retirement_reason,
                    "directory_updated_at": directory_updated_at,
                },
            )
        if resolved.pool_status == "not_linked":
            raise DomainError(
                f"{error_namespace}_worker_not_linked",
                (
                    f"The addressed worker {resolved.worker_name} is not linked "
                    "to this project and cannot read its mailbox."
                ),
                status=403,
                details={
                    "recipient": address,
                    "worker_name": resolved.worker_name,
                    "worker_alias": resolved.worker_alias,
                    "directory_updated_at": directory_updated_at,
                },
            )
        return resolved

    alias_matches = sorted(
        {
            candidate.worker_name
            for candidate in recipients.values()
            if candidate.worker_alias
            and candidate.worker_alias.casefold() == address.casefold()
        }
    )
    if alias_matches:
        if len(alias_matches) == 1:
            guidance = f"That alias belongs to {alias_matches[0]}; use that stable address."
        else:
            guidance = (
                "That alias belongs to more than one worker; choose one stable address: "
                + ", ".join(alias_matches)
                + "."
            )
        raise DomainError(
            f"{error_namespace}_worker_alias_not_addressable",
            f"{address} is a display alias, not a worker address. {guidance}",
            status=409,
            details={
                "recipient": address,
                "stable_worker_names": alias_matches,
                "directory_updated_at": directory_updated_at,
            },
        )

    raise DomainError(
        f"{error_namespace}_worker_not_found",
        f"The worker address {address} did not resolve in the project recipient directory.",
        status=404,
        details={
            "recipient": address,
            "directory_updated_at": directory_updated_at,
        },
    )


__all__ = ["MailRecipient", "resolve_mail_recipient"]
