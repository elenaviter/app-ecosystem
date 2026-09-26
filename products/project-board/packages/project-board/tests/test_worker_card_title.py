"""A worker's Card is titled by who it is (W304 finding 22).

The Card a worker registers in Connection Hub was titled "Connection Hub CLI ·
Problem Board worker · codex:codex-ui:<session>": it led with the tool, and
the person reading the Card list had to find the alias in the middle. It now
leads with the alias and keeps the exact runtime session last.
"""

from __future__ import annotations

from types import SimpleNamespace

from project_board.client.authorization import _worker_client_name

SESSION = "11111111-1111-4111-8111-111111111111"


def _channel(alias: str) -> SimpleNamespace:
    return SimpleNamespace(
        worker_identity=f"codex:{SESSION}",
        worker_alias=alias,
        runtime_kind="codex",
        runtime_session_id=SESSION,
    )


def test_the_title_leads_with_the_alias_and_ends_with_the_session():
    assert _worker_client_name(_channel("codex-ui")) == f"codex-ui · Problem Board worker · codex:{SESSION}"
    assert _worker_client_name(_channel("codex-ui"), coordinator=True) == (
        f"codex-ui · Problem Board coordinator · codex:{SESSION}"
    )


def test_without_an_alias_the_title_names_the_role_and_session():
    assert _worker_client_name(_channel("")) == f"Problem Board worker · codex:{SESSION}"
    assert "Connection Hub CLI" not in _worker_client_name(_channel(""))
