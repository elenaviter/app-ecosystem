"""Compatibility alias for KDCube-owned management contracts."""

import sys

from kdcube_cli.management import models as _implementation

sys.modules[__name__] = _implementation
