"""Compatibility alias for the KDCube-owned management transport."""

import sys

from kdcube_cli.management import transport as _implementation

sys.modules[__name__] = _implementation
