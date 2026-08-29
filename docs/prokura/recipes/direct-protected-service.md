---
id: prokura-direct-protected-service-recipe
title: Protect An External Backend With Connection Hub
summary: Configures a non-KDCube backend to ask a KDCube-hosted Connection Hub for live delegated operation admission using independent bearer and workload proofs.
tags: [prokura, connection-hub, delegated-access, protected-service, recipe]
keywords: [direct admission, opaque bearer, workload proof, resource server, card, capability catalog]
see_also:
  - ../connection-hub-architecture.md
  - ../../../examples/direct-admission-service/README.md
---

# Protect an external backend with Connection Hub

This recipe connects a backend outside KDCube to a KDCube-hosted Connection
Hub. The backend accepts a user's opaque delegated bearer and asks Connection
Hub whether that bearer currently authorizes one semantic operation.

## 1. Declare the user-grantable capability

The delegated catalog is the authority for operations and their required
grants. Add one grant and one resource/operation to the Connection Hub app's
`connections.delegated_credentials.catalog` configuration:

```yaml
grants:
  - grant: reference_customers:read
    label: Read reference customers
    delegable_roles:
      - kdcube:role:registered
    delegable_permissions:
      - reference_customers:read

resources:
  - resource: https://reference.example.test/customers
    label: Reference customer API
    grants:
      - reference_customers:read
    operations:
      - operation: customers.search
        label: Search customers
        grants:
          - reference_customers:read
```

The approving user selects this authority through the normal delegated-access
card or OAuth consent flow. The resulting bearer remains opaque.

## 2. Register the protected workload

Enable admission and bind the service to resource selectors. This registration
authenticates the workload and limits which catalog resources it may ask about;
it does not restate grants or operations.

```yaml
connections:
  delegated_credentials:
    admission:
      enabled: true
      identity_projection_secret_ref: connections.delegated_credentials.admission.identity_projection_secret
      max_clock_skew_seconds: 300
      nonce_ttl_seconds: 600
      services:
        reference-customers-api:
          label: Reference customer API
          secret_ref: connections.delegated_credentials.admission.services.reference-customers-api.signing_secret
          resources:
            - https://reference.example.test/customers*
```

Put the two referenced secret values in the deployment's app-secret provider.
Each value contains at least 32 random bytes. The service receives only its own
signing secret; it never receives the identity-projection secret.

## 3. Configure the backend

Run the [reference service](../../../examples/direct-admission-service/README.md)
with:

```text
CONNECTION_HUB_ADMISSION_URL = host-specific delegated_admission operation URL
PROKURA_SERVICE_ID           = reference-customers-api
PROKURA_SERVICE_SECRET       = registered workload secret
PROKURA_RESOURCE             = https://reference.example.test/customers
```

For the current KDCube host, the URL is:

```text
/api/integrations/bundles/{tenant}/{project}/connection-hub@1-0/public/delegated_admission
```

This is a host route, not Prokura's permanent product URL. A shorter stable
product alias is postponed until the KDCube app router supports declared route
aliases.

## 4. Evaluate each authority-crossing call

For every protected operation, the backend:

1. keeps the incoming opaque delegated bearer unchanged;
2. creates a fresh timestamp and nonce;
3. signs the service id, timestamp, nonce, bearer hash, and canonical semantic
   request hash with its workload secret;
4. calls Connection Hub with the bearer and `X-Prokura-*` proof headers;
5. performs the domain operation only when the live response says
   `allowed: true`.

Connection Hub authenticates both proofs independently, consumes the nonce,
resolves the current card and active catalog, checks optional connected-account
scope, and returns a pairwise service subject plus bounded authority. Editing,
narrowing, or revoking the card changes the next fresh decision.

## 5. Keep the two authorization layers

Prokura answers whether the delegated caller currently holds the configured
platform authority for the semantic operation. The protected backend remains
responsible for domain rules such as record ownership, field-level policy,
business invariants, and write preconditions.

Do not cache an allow as durable authority. A short transport cache may reduce
duplicate evaluation only if its invalidation and maximum staleness are an
explicit product decision; the live Connection Hub record remains canonical.

## Verification cases

- valid bearer + valid workload proof + current grant -> allow;
- changed body, bearer, or service id under the same signature -> 401;
- replayed nonce -> 409;
- unregistered resource selector -> 403;
- missing, narrowed, or revoked card operation -> structured denial;
- Redis replay guard or authority store unavailable -> retryable 503;
- allow response contains no provider credential or internal platform user id.
