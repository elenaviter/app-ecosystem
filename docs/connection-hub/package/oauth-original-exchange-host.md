# Original OAuth exchange hosting

With `connections.card_transactions.enabled`, the productive mount binds the
SDK original-exchange capability before token handling. Unavailable composition
leaves a present, closed binding: there is no legacy consume-and-mint fallback.

The host uses its activated PostgreSQL OAuth authority, actual Card persistence,
configured session authority and qualified issuance custody. It creates no
decision protocol. The existing governed bundle-load preparation path prepares
the SDK original-exchange and refresh metadata schemas when Card transactions
and PostgreSQL authority are selected. A token request never installs a schema
or changes provider selection or activation.

The public issuer must be explicitly configured in
`connections.delegated_credentials.oauth.issuer`. A request Host, Referer or
forwarded header cannot supply this production requirement. Refresh signing uses
the protected descriptor named by
`connections.delegated_credentials.oauth.original_exchange.refresh_signing_secret_ref`.
This is a reference, never inline signing material. Existing provider selection
and activation are unchanged; configuring or qualifying them is a separate
deployment action.

The consumed server record supplies the complete Card candidate input. The host
authenticates the original plan through the Hub's public decision reader, then
checks the complete candidate in the same scoped durable issuance row. Card kind
comes from this frozen candidate, including after restart, not from a request or
the latest Card. The live fence compares the full current original/candidate,
identity, revision, content and database-clock deadlines. An unavailable or
corrupt pointer is not absence.

Current integration limit: nonempty consent invocation-policy choices refuse
until the owning issuance protocol can enlist them under the same decision.
They are never silently dropped or applied through a separate direct write.
Whole normal-consent acceptance, encrypted provider qualification, installed
mount, process/restart recovery and live activation are not established by these
host-composition unit tests.
