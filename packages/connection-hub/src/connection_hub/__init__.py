"""Connection Hub: one authority for delegated access.

The identity provider appeared because many applications needed one
authority on who the user is. Delegated access for agents and automations
needs its own authority the same way. Every caller has its own identity
card, the authority lives in one central record, and a guarded boundary
resolves the current authority on every call.

This release claims the name. The boundary/client SDK is being extracted
from the production implementation; see the repository for status:
https://github.com/elenaviter/app-ecosystem
"""

__version__ = "0.0.1"
