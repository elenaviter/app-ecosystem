"""Compatibility alias for KDCube-owned private secret output."""

import sys

from kdcube_cli.management import secret_output as _implementation

sys.modules[__name__] = _implementation
