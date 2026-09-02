"""Host-neutral accounting and economics contracts.

The package will own portable service-usage, attribution, pricing, budget
admission, reservation, and settlement contracts. Delegated authority remains
in ``connection_hub``; storage, locks, configuration, payment integrations,
and user interfaces remain host adapters.

This release reserves the distribution and import names. The production
implementation remains in KDCube while extraction proceeds through
characterized compatibility migrations.
"""

__version__ = "2026.09.02.1559"
