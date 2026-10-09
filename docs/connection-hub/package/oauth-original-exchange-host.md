# Original OAuth exchange hosting

With `connections.card_transactions.enabled`, the productive mount binds the
SDK original-exchange capability before token handling. Unavailable composition
leaves a present, closed binding: there is no legacy consume-and-mint fallback.

The host uses its activated PostgreSQL OAuth authority, actual Card persistence,
configured session authority and the two signing keys. It keeps no issued
bearer in secret custody (operator, 2026-10-09: "i need the stronger version
now"): the SDK re-signs the claims PostgreSQL stores and must match the sealed
digests, so a retry returns the same bearers and a changed key refuses. It
creates no decision protocol. The existing governed bundle-load preparation path prepares
the SDK original-exchange and refresh metadata schemas when Card transactions
and PostgreSQL authority are selected. A token request never installs a schema
or changes provider selection or activation.

The public issuer must be explicitly configured in
`connections.delegated_credentials.oauth.issuer`. A request Host, Referer or
forwarded header cannot supply this production requirement. Refresh signing uses
the protected descriptor named by
`connections.delegated_credentials.oauth.original_exchange.refresh_signing_secret_ref`.
This is a reference, never inline signing material. Binding resolves it once
and stays closed (`original_exchange_signer_unavailable`) when it is missing or
shorter than 32 bytes; this replaced the former custody qualification gate.
The referenced refresh signing secret and the platform session secret
(`platform.services.session_token.secret`) must not change while an original
issuance is being delivered (until its delivery deadline): a change refuses the
retry with `original_exchange_signing_mismatch`, and re-authorization is the
recovery (operator, 2026-10-09). There is no key versioning. Existing provider selection
and activation are unchanged; configuring or qualifying them is a separate
deployment action.

An enabled original-exchange deployment with a missing or invalid configured
issuer closes the entire GET/POST OAuth mount with the finite
`oauth_original_exchange_unavailable` response (503), before discovery, client
registration, consent or code creation. Disabled OAuth retains its existing 404;
the non-Card-transaction adapter retains its existing local/development behavior.

The consumed server record supplies the complete Card candidate input. The host
authenticates the original plan through the Hub's public decision reader, then
checks the complete candidate in the same scoped durable issuance row. Card kind
comes from this frozen candidate, including after restart, not from a request or
the latest Card. The live fence compares the full current original/candidate,
identity, revision, content and database-clock deadlines. An unavailable or
corrupt pointer is not absence.

Consent invocation-policy choices are frozen by the SDK's public
`oauth_issuance_arguments` normalizer and passed unchanged into the Hub's same
issuance decision. An absent or `None` selection is omitted; an explicit empty
mapping remains distinct. The host performs no post-grant policy write.
The owner-visible label uses the same public `oauth_card_label` helper as normal
consent. Only the asserted nested client metadata is persisted on the Card,
matching the existing registry path.

This composition requires the SDK policy-normalizer/full-plan snapshot change
`ba51436ecc75f90eeb4d4acadbc5f78c4a9086da` and the Hub single-issuance policy
effects `af2cb8decdc24bc497910847489b347cc16e36db`, or qualified descendants.
The effect bound remains 32 for one issuance; oversized policy selections must
refuse, never truncate or split. Installed ACTIVE-catalog capacity measurement
and full productive qualification remain separate gates.
Whole normal-consent acceptance, encrypted provider qualification, installed
mount, process/restart recovery and live activation are not established by these
host-composition unit tests.
