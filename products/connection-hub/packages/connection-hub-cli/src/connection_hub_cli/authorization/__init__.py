"""OAuth authorization primitives, now in ``connection_hub.caller.authorization`` (W322).

Each submodule here is an alias of the moved one; this package re-exports the
same names so existing imports keep working.
"""

from connection_hub.caller.authorization import *  # noqa: F403
from connection_hub.caller.authorization import __all__  # noqa: F401
