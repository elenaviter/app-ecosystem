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

**Version 0.0.1 is a development release.** The package contains the portable
authority implementation: authority and authenticator contracts, durable
identity-card and capability-catalog histories, delegated OAuth and connected-
account lifecycle, identity-edge resolution, per-call named-service and
managed-surface and direct protected-service admission, structured denial contracts, and explicit ports for
host storage, dispatch, identity, and live-delivery capabilities. KDCube
consumes these modules through thin host adapters, and the Connection Hub app
in this repository is the first frontend over the package.

Home: https://github.com/elenaviter/app-ecosystem

Architecture and contracts:

- [Connection Hub architecture and semantic requirements](../../docs/prokura/connection-hub-architecture.md)
- [Package boundary and extraction](../../docs/prokura/package/extraction-architecture.md)
- [Delegated authority and admission](../../docs/prokura/package/delegated-authority-and-admission.md)
- [Delegated access cards](../../docs/prokura/package/delegated-cards.md)
- [OAuth delegated credential protocol](../../docs/prokura/package/oauth-delegated-credential-protocol.md)
