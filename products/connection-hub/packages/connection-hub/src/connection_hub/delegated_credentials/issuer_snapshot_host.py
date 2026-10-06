"""Server-only delivery confinement; this scope grants no issuer authority."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass
class _OrchestrationScope:
    active: bool = True


_SCOPE: ContextVar[_OrchestrationScope | None] = ContextVar("issuer_snapshot_orchestration", default=None)


@contextmanager
def bind_issuer_snapshot_orchestration() -> Iterator[None]:
    """Confine a server-owned SDK call whose raw result stays inside that app.

    Only trusted server orchestration binds this scope, never a request field
    or header. Actual platform-human identity, runtime scope and both fresh
    issuer decisions remain mandatory. An inherited child context loses this
    capability when the caller exits, even if that child outlives the caller.
    """
    scope = _OrchestrationScope()
    token = _SCOPE.set(scope)
    try:
        yield
    finally:
        scope.active = False
        _SCOPE.reset(token)


def issuer_snapshot_orchestration_is_bound() -> bool:
    scope = _SCOPE.get()
    return type(scope) is _OrchestrationScope and scope.active is True
