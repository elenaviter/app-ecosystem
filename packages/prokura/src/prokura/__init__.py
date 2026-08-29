"""Prokura: one authority for delegated access.

Prokura is an old commercial-law institution: delegated signing authority
that lives in a register, verified against the register rather than any
carried letter, revocable at the register. This package is that idea,
built for agents and automations: every caller has its own identity card,
the authority lives in one central record, and a guarded boundary resolves
the current authority on every call.

This release claims the name. The boundary/client SDK is being extracted
from the production implementation (the Connection Hub inside KDCube); see
the repository for status: https://github.com/elenaviter/app-ecosystem
"""

__version__ = "0.0.1"
