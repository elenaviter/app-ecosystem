"""Compatibility import for the KDCube-owned loopback callback."""

from kdcube_cli.management.callback import AuthorizationCallback, LoopbackCallbackServer

__all__ = ["AuthorizationCallback", "LoopbackCallbackServer"]
