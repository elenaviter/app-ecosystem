from __future__ import annotations

from dataclasses import dataclass


MAX_WORKER_INPUT_BYTES = 64 * 1024
# Room kept around one message in a receive: the envelope (session echo,
# worker, projects, delivery, leases) plus the session's 16 KiB finalization
# reserve, which every receive holds back. A message whose own serialized item
# does not fit beside this reserve can never be delivered and is stubbed. An
# empty receive measured 5.9 KB on 2026-09-21 after the session echo was
# trimmed, and the largest legal one (projects and assignments at their echo
# caps) 9.9 KB. The reserve was first 12 KiB, which forgot the finalization
# reserve and would have told the sender of a 45 KB message to resend it
# unchanged. A test keeps the largest legal envelope inside this reserve.
RECEIVE_ENVELOPE_RESERVE_BYTES = 28 * 1024
MAX_RECEIVABLE_MESSAGE_BYTES = MAX_WORKER_INPUT_BYTES - RECEIVE_ENVELOPE_RESERVE_BYTES


@dataclass(slots=True)
class MailPullBudget:
    """One response-wide budget shared by every mailbox in a receive."""

    maximum_bytes: int = MAX_WORKER_INPUT_BYTES
    response_bytes: int = 0
    claimed_messages: int = 0
    remaining_messages: int = 0
    limited_by: str = ""
    deferred_message_ref: str = ""
    required_response_bytes: int = 0

    def can_claim(self, response_bytes: int) -> bool:
        return int(response_bytes) <= int(self.maximum_bytes)

    def record_claim(self, response_bytes: int) -> None:
        self.response_bytes = int(response_bytes)
        self.claimed_messages += 1

    def defer(self, *, message_ref: str, response_bytes: int) -> None:
        if not self.deferred_message_ref:
            self.deferred_message_ref = str(message_ref)
            self.required_response_bytes = int(response_bytes)

    def is_empty(self) -> bool:
        """No message has been claimed into this response yet."""
        return self.claimed_messages == 0


    def record_mailbox(self, *, remaining: int, limited_by: str = "") -> None:
        self.remaining_messages += max(0, int(remaining))
        if limited_by and not self.limited_by:
            self.limited_by = str(limited_by)


__all__ = [
    "MAX_RECEIVABLE_MESSAGE_BYTES",
    "MAX_WORKER_INPUT_BYTES",
    "RECEIVE_ENVELOPE_RESERVE_BYTES",
    "MailPullBudget",
]
