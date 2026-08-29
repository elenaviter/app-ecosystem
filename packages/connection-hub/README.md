# connection-hub

One authority for delegated access.

The identity provider appeared because many applications needed one
authority on who the user is. Delegated access for agents and automations
needs its own authority the same way. The Connection Hub is that
component.

Every caller, an agent, a sub-agent, an automation, has its own identity
card. The authority attached to it lives in one central record: acting for
which user, on which connected accounts, which operations, which claims,
until when. A guarded boundary resolves the current authority on every
call. Calls carry only plain facts, the actor identity and the wanted
grant. Authorization evidence does not travel with calls, and an edit or
revocation of the card applies on the very next call.

**This is a placeholder release claiming the name.** The boundary/client
SDK, the actor and grant reference format, per-call admission, structured
denials a caller can act on, and claim publication for services, is being
extracted from the production implementation that runs inside
[KDCube](https://github.com/kdcube/kdcube) today.

Home: https://github.com/elenaviter/app-ecosystem
