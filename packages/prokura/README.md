# prokura

One authority for delegated access.

Prokura is an old commercial-law institution: delegated signing authority
that lives in a register. Third parties verify against the register, not
against any letter the holder carries, and revocation takes effect at the
register. This package is that idea, built for agents and automations.

The identity provider appeared because many applications needed one
authority on who the user is. Delegated access needs its own authority the
same way. Every caller, an agent, a sub-agent, an automation, has its own
identity card. The authority attached to it lives in one central record:
acting for which user, on which connected accounts, which operations,
which claims, until when. A guarded boundary resolves the current
authority on every call. Calls carry only plain facts, the actor identity
and the wanted grant. Authorization evidence does not travel with calls,
and an edit or revocation of the card applies on the very next call.

**This is a placeholder release claiming the name.** The boundary/client
SDK, the actor and grant reference format, per-call admission, structured
denials a caller can act on, and claim publication for services, is being
extracted from the production implementation: the Connection Hub that runs
inside [KDCube](https://github.com/kdcube/kdcube) today. Prokura is its
packaged, standalone form.

Home: https://github.com/elenaviter/app-ecosystem
