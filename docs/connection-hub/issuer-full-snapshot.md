# Issuer full Card snapshot

## 2026-10-06: draft package contract (not independently qualified)

The protected identity read (see
[issuer-managed Card writes](issuer-managed-writes.md), "Protected two-Card
identity read") returns a redacted identity projection. An issuer that must act
on a Card's exact current authority needs the complete original revision
instead. For example, a compare-and-swap update of grants has to bind every
field it preserves. The full snapshot is that second read, authorized
separately.

### What it returns

It returns one person's Control and My Card pair: both, or neither. Each
snapshot has:

- `target`: the exact coordinate object;
- `card_revision`;
- `authority_fingerprint`: the original `CardAuthority.content_hash()`;
- `authority`: the original `CardAuthority.to_dict()`, unredacted.

An inventory calls it once per person. Repeated calls make no claim of one
atomic cut across the inventory: a later write must compare-and-swap on each
Card's revision and full fingerprint.

### Separate from the identity read

The full snapshot has its own:

- protocol, `issuer-snapshot.v1`;
- request digest, `snapshot_digest`;
- signed proof;
- decision, seal and registry (`IssuerSnapshotRegistry`);
- adapter method, `decide_snapshot`.

An identity-read decision, seal or adapter is not accepted. A snapshot proof does
not verify as an identity-read proof, and an identity-read proof does not verify
as a snapshot proof. The capability that enables it, `full_snapshot_operation`,
is configured separately. A configured `read_operation` or write operation does
not imply it.

### Request and ordering

The query is the identity read's strict DTO: `context_ref`, `request_id` and
exactly two targets. The host binds the actual platform human, its
classification, and the runtime tenant and project. The query cannot supply
them.

Ordering:

1. a fresh sealed issuer decision;
2. both production Card fences, through the existing fenced storage read;
3. the raw committed authorities;
4. release both fences;
5. a second fresh sealed decision.

The second decision is bound to the complete snapshots and their full
fingerprints. It keeps the policy version and cannot extend the first expiry.
No peer I/O occurs inside a Card fence. No cache, repair, receipt, revision,
handle or Redis write occurs at all. One 30-second deadline covers the whole
read.

### Credential material refuses the pair

A returned payload is never redacted, so anything that looks like credential
material refuses the whole pair with `issuer_snapshot_credential_material`:

- **A key anywhere in the payload, at any depth, whose name ends with a credential word** (`token`, `secret`, `password`, `credential`, `handle`, `api_key`, `private_key`, `authorization`, `cookie`, `bearer`, and their plurals). Keys are matched by how they end, so standard non-secret metadata such as OAuth's `token_endpoint_auth_method` passes.
- **Any string value in the payload shaped like a secret**: `Bearer ` or `Basic ` followed by a token, a JWT (three dot-separated base64url parts), a complete GitHub, Slack, OpenAI-style or AWS key-id format, or a long unbroken high-entropy run. Values are matched by their complete shape, never by a word, so labels and resource names such as `.../delegated_credentials/...` pass. Opaque identity fields (`access_id`, `client_id`, `grantor_subject`, `delegate_subject`, `issuer_ref`) and lowercase hex fingerprints are exempt from the high-entropy rule only.
- **A `last_four` longer than four characters, or one that is not a string.** A value of four characters or fewer is display metadata. It is returned, because dropping it would break the original fingerprint.
- **An unsafe `manage_url`**: not `https`, or carrying userinfo, a query string or a fragment.

### Confinement

The payload carries personal data. It is an internal authenticated
orchestration result, never a browser or Team listing. Refusals carry a reason
code and a retryable flag only, and the module never logs a payload.

### Not qualified by the author tests

The author tests do not qualify:

- the mounted human authentication and the hosted alias wiring, which belong to the host application;
- the issuer's fresh policy and its durable cross-process nonce consumption;
- the combined candidate.

These remain exact-source activation gates.
