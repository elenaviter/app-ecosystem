"""Alias of ``connection_hub.caller.authorization.session`` (W322).

The caller layer moved to the connection-hub package. This module is that
module, so code importing the old path shares one module object with it.
"""

import sys

from connection_hub.caller.authorization import session as _moved

sys.modules[__name__] = _moved
