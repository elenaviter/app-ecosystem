"""Compatibility alias for the KDCube-owned secret export protocol."""

import sys

from kdcube_cli.management import secret_export as _implementation

sys.modules[__name__] = _implementation
