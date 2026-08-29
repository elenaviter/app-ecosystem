"""service-foundation: foundation for standalone runnable services.

Service composition and launcher contracts, browser-session (OIDC) and
service-workload authentication surfaces, opaque-token issuance for the
service's own sessions, configuration, health and readiness, and
migration invocation. No authority models (those are `prokura`), no
application behavior, no KDCube bundle concepts; it stands on
`app-foundation`.

Version 0.0.1 claims the name; the modules arrive with the standalone
host work. Status: https://github.com/elenaviter/app-ecosystem
"""

__version__ = "0.0.1"
