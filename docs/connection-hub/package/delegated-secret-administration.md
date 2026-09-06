---
id: connection-hub/package/delegated-secret-administration
title: "Delegated Secret Administration"
summary: "Connection Hub Card authority for provider-neutral KDCube secret metadata, read, write, and delete operations, plus the separate human-only whole-descriptor export ceremony."
status: current
tags: ["security", "secrets", "delegation", "connection-hub", "kdcube"]
keywords: ["secret authority", "secret selector", "namespace grant", "whole deployment", "allow once", "allow always", "descriptor export"]
updated_at: 2026-09-06
see_also:
  - ./delegated-authority-and-admission.md
  - ./delegated-cards.md
  - ../macos-user-presence-helper.md
  - https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/service/secrets/secret-management-cli-README.md
  - https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/configuration/secrets-descriptor-README.md
---
# Delegated Secret Administration

Connection Hub lets a KDCube administrator delegate bounded secret
maintenance to a person, agent, or automation without exposing the physical
storage backend. The Card grants logical resources. KDCube resolves those
resources through the deployment-selected secret provider only after live
admission.

```text
person, agent, or automation
        |
        | Card-bound bearer + exact operation + invocation id
        v
Connection Hub admission and invocation policy
        |
        | exact admitted logical target
        v
KDCube management service
        |
        v
descriptor-selected provider
  secrets-file | Host Vault | AWS Secrets Manager | in-memory
```

The delegated operations are:

```text
kdcube.management.secret.metadata.read
kdcube.management.secret.value.read
kdcube.management.secret.value.write
kdcube.management.secret.delete
```

Cards and audit records contain target identities, policies, revisions, and
results. They never contain secret values.

## Logical Targets

Every target is deployment-qualified:

```text
urn:kdcube:management:secret:<tenant>:<project>:<scope>:<scope-id>:<key>
```

| Scope | Scope id | Key syntax | Example |
| --- | --- | --- | --- |
| Platform | `_` | Canonical `platform.` key | `platform.services.brave.api_key` |
| Bundle | exact bundle id | Key relative to that bundle | `connections.oauth_state_secret` |
| User | exact user id | Key relative to that user | `provider.token` |
| User bundle | `USER_ID~BUNDLE_ID` | Key relative to that user's app data | `provider.refresh_token` |

`secrets.yaml` has exactly two top-level namespaces: `platform` and `users`.
Deployment-bundle values remain in `bundles.secrets.yaml`. This logical shape
is stable across storage providers.

## Card Selectors

The Card editor supports three granularities:

| Selection | Resource form | Policy |
| --- | --- | --- |
| Exact key | exact scope id and exact key | `Once` or `Always` |
| Namespace | exact scope id and a trailing `.*` key | `Always` |
| Entire scope | wildcard scope id and/or key | `Always` |

Examples:

```text
one platform service namespace
  ...:platform:_:platform.services.brave.*

all platform secrets
  ...:platform:_:platform.*

all secrets for one bundle
  ...:bundle:connection-hub@1-0:*

all bundle secrets in the deployment
  ...:bundle:*:*

all user and user-bundle secrets
  ...:user:*:*
```

The whole-deployment selection is the explicit union of:

```text
platform / _ / platform.*
bundle   / * / *
user     / * / *
```

Broad selectors are standing authority and therefore require `Always`.
`Once` applies only to one exact key and one exact operation. Consumption is
atomic. Repeating the same invocation id returns the recorded result without
performing the provider effect again.

Every delegated secret operation requires an explicit invocation policy. A
matching Card resource without a stored `Once` or `Always` policy fails closed;
it never inherits the generic admission default.

For a broad grant, admission records both identities:

- the matched selector identifies the Card and invocation-policy authority;
- the exact requested target identifies the provider effect and audit event.

The broad selector is never passed to the provider as a read, write, or
delete target.

## Agent Maintenance Flow

An administrator can give a trusted maintenance agent reusable authority for
one namespace or for the deployment. The agent then uses the KDCube management
API, normally through `kdcube secrets` or `connection-hub secrets host`.

```text
administrator edits agent Card once
        |
        +-- platform.services.* / write / Always
        +-- bundle:*:* / write / Always
        +-- user:*:* / write / Always
        |
agent runs set, import, or delete later
        |
Connection Hub resolves the same live Card on every request
        |
revocation or narrowing applies to the next request
```

Granting metadata, write, or delete does not imply value read. Value
disclosure requires the separate `secret.value.read` operation. The CLI writes
an admitted value to an explicit private file and does not print it.

## Whole Descriptor Export

Plaintext export is intentionally not standing Card authority. It is a fresh,
browser-confirmed, one-use transaction:

```text
administrator starts whole export
        |
KDCube freezes the current provider inventory
        |
unauthenticated start returns only count and request digest
        |
authenticated browser shows the exact frozen target names
        |
administrator confirms through the configured KDCube authority
        |
one PKCE-bound code discloses the inventory and reconstructs exactly:
  secrets.yaml
  bundles.secrets.yaml
```

Whole export includes all current platform, bundle, user, and user-bundle
values. The two files are the normal private descriptor files, placed beside
the ordinary exported descriptors. No alternative archive or secret-package
format is introduced.

The transaction-start response never exposes whole-inventory key names. The
CLI receives them only in the approved exchange and recomputes the frozen
request digest before writing either descriptor.

The ceremony proves a current authenticated KDCube administrator session and
an explicit one-use decision. Deployments can require stronger assurance
through their configured authority adapter, including Cognito fresh login or
WebAuthn user verification. The export permit is not written into a Card and
cannot be reused.

## Import Semantics

Import reads the same literal descriptor pair and performs provider-neutral
upserts through exact management operations. Present values are written;
omitted keys remain unchanged. Deletion remains an explicit operation.

A human can use the OAuth session already held by the Connection Hub CLI:

```bash
connection-hub secrets host import \
  --input-directory ./portable-descriptors \
  --dry-run
connection-hub secrets host import \
  --input-directory ./portable-descriptors \
  --yes
```

The reusable OAuth bearer stays inside the native credential store and CLI
session service. A trusted agent can instead use `kdcube config import` with
the Card-bound bearer the administrator issued to that agent. Both paths
execute exact writes through the same live Card and selected provider.

On a provider-backed target, the target deployment keeps its own backend
configuration and machine identity. A portable full descriptor export resets
machine-specific Host Vault fields to file bootstrap defaults, so a fresh
recipient can initialize from the exported files and later activate its own
Host Vault. Import into an existing Host Vault or cloud deployment preserves
that deployment's selected provider.

KDCube owns the command syntax, migration sequence, file permissions, and
operator procedure in
[Manage KDCube Secrets](https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/service/secrets/secret-management-cli-README.md).

## Security Boundary

The Card is the live authorization boundary. A bearer alone is not sufficient:
Connection Hub resolves the current Card revision, selector, operation, and
invocation policy on every request. KDCube validates the exact deployment and
target again before touching the provider.

This authority does not grant host, Docker, vault-service, or cloud-console
administration. Storage enrollment, Host Vault lifecycle, and cloud IAM remain
deployment-operator responsibilities.
