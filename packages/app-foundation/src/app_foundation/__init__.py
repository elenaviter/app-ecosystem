"""app-foundation: host-neutral foundation for applications in an ecosystem.

Generic principal and service identity, secret-reference resolution,
Postgres/Redis clients, cache and distributed locks, HTTP/CSRF/external-URL
utilities, events and observability primitives. No authority models (those
are `prokura`), no Connection Hub behavior, no KDCube bundle concepts, no
deployment orchestration.

Version 0.0.1 claims the name; the modules are being extracted from the
production implementation. Status: https://github.com/elenaviter/app-ecosystem
"""

__version__ = "0.0.1"
