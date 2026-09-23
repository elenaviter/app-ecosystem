"""Where an outbox row lives, by its delivery state.

``pending``  queued, not yet claimed by the relay
``leased``   claimed by the relay, delivery in flight
``sent``     the service accepted it (row state ``sent`` or ``ignored``)
``refused``  the service refused it, and the refusal is recorded in the row

A refused row is never filed with sent ones. On 2026-09-23 dev-main held
54,827 rows in ``sent/``, almost all of them refusals, so the folder said
"sent" for work the service never stored (W287 acceptance 1).
"""

from __future__ import annotations


OUTBOX_IN_FLIGHT_FOLDERS: tuple[str, ...] = ("pending", "leased")
OUTBOX_TERMINAL_FOLDERS: tuple[str, ...] = ("sent", "refused")
OUTBOX_FOLDERS: tuple[str, ...] = OUTBOX_IN_FLIGHT_FOLDERS + OUTBOX_TERMINAL_FOLDERS


def terminal_folder(outcome: str) -> str:
    """The folder a settled row moves to."""

    return "refused" if str(outcome or "") == "refused" else "sent"


__all__ = [
    "OUTBOX_FOLDERS",
    "OUTBOX_IN_FLIGHT_FOLDERS",
    "OUTBOX_TERMINAL_FOLDERS",
    "terminal_folder",
]
