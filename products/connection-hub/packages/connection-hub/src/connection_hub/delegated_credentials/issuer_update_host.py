"""Internal update delivery scope; never grants issuer or mutation authority."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass
class _UpdateScope:
    active: bool = True


_SCOPE: ContextVar[_UpdateScope | None] = ContextVar("issuer_update_orchestration", default=None)


@contextmanager
def bind_issuer_update_orchestration() -> Iterator[None]:
    """Trusted server call only; no request header or field can create it.

    The raw updated Card stays inside the orchestrating server. Inherited
    tasks lose this scope when the caller exits. This does not approve a write:
    the actual human and fresh configured issuer decision are still required.
    """
    scope = _UpdateScope()
    token = _SCOPE.set(scope)
    try:
        yield
    finally:
        scope.active = False
        _SCOPE.reset(token)


def issuer_update_orchestration_is_bound() -> bool:
    scope = _SCOPE.get()
    return type(scope) is _UpdateScope and scope.active is True
