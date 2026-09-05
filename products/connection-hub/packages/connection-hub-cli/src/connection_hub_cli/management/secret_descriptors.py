"""Compatibility alias for KDCube-owned secret descriptor export."""

import sys

from kdcube_cli.management import secret_descriptors as _implementation

sys.modules[__name__] = _implementation
