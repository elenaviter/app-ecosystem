"""Independent W460 probe (claude-ops@spark1): which locks are held at the
moment a message moves inbox -> leased?

Every other writer of a worker mailbox (ordinary pull_mail, settle, renew,
expiry recovery) guards it with ``<mail root>/.mail.lock``. The selective path
locks ``SharedFieldStore._mail_lock(...)`` and calls pull_mail with
``lock_held=True``. This records the locks the claiming thread holds at the
claim's os.replace, for an ordinary receive (control) and a selective one.
"""

from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path

import pytest

import project_board.client.session as session_module
import project_board.client.store as store_module
from project_board.client.session import pull_worker_input
from test_correlated_receive import PROJECTS, WORKER, _send, field  # noqa: F401

_held = threading.local()


@pytest.fixture
def lock_trace(monkeypatch):
    real_lock = store_module.exclusive_lock
    claims: list[tuple[Path, set[str]]] = []

    @contextlib.contextmanager
    def tracking_lock(path, *args, **kwargs):
        with real_lock(path, *args, **kwargs):
            stack = getattr(_held, "paths", [])
            _held.paths = stack + [str(Path(path))]
            try:
                yield
            finally:
                _held.paths = stack

    real_replace = os.replace

    def tracking_replace(src, dst, *args, **kwargs):
        s, d = Path(src), Path(dst)
        if s.parent.name == "inbox" and d.parent.name == "leased":
            claims.append((s.parent.parent, set(getattr(_held, "paths", []))))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(store_module, "exclusive_lock", tracking_lock)
    monkeypatch.setattr(session_module, "exclusive_lock", tracking_lock)
    monkeypatch.setattr(store_module.os, "replace", tracking_replace)
    return claims


def test_ordinary_receive_claims_under_the_mailbox_lock(field, lock_trace):
    target = _send(field, PROJECTS[1], "ordinary-target")
    result = pull_worker_input(field, worker_name=WORKER)
    assert target["message_ref"] in [i["message"]["message_ref"] for i in result["items"]]
    assert lock_trace, "no inbox->leased claim observed"
    for mailbox, held in lock_trace:
        assert str(mailbox / ".mail.lock") in held


def test_selective_receive_claims_under_the_mailbox_lock(field, lock_trace):
    _send(field, PROJECTS[0], "older")
    target = _send(field, PROJECTS[1], "selected-target")
    result = pull_worker_input(field, worker_name=WORKER, message_ref=target["message_ref"])
    assert [i["message"]["message_ref"] for i in result["items"]] == [target["message_ref"]]
    assert lock_trace, "no inbox->leased claim observed"
    for mailbox, held in lock_trace:
        assert str(mailbox / ".mail.lock") in held, (
            f"selective claim moved {mailbox.name} mail holding only {sorted(Path(p).name for p in held)}"
        )
