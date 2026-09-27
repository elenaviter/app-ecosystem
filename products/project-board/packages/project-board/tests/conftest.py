from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_host_output_format(monkeypatch):
    """The host's PB_FORMAT never reaches a test (W349).

    The worker procedure tells every agent to `export PB_FORMAT=brief`, so a
    suite started from such a shell ran `pb` (in-process, or as a child that
    inherits the environment) in brief mode, and a test reading its JSON
    failed only in those runs.
    """

    monkeypatch.delenv("PB_FORMAT", raising=False)
