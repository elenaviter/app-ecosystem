# Configuration and capabilities overview

What the Connection Hub does, seen as one picture, and where each part is
configured. This page routes; the linked documents own the full contracts.

## The topology

```text
                              +---------------------------------------------+
   user (grantor)             |               Connection Hub                |
   -----------------------    |                                             |
   connects provider accounts | connected accounts   (delegated TO the hub) |
   approves and edits cards   | delegated cards      (delegated BY the hub) |
   picks capabilities and     |   one per caller, versioned revisions +     |
   exact accounts at consent  |   a current pointer (access_id)             |
                              | active capability catalog                   |
                              |   the deployment ceiling, versioned         |
                              | authority registry                          |
                              |   which identity realms verify credentials  |
                              +---------+--------------------+--------------+
                                        |                    |
              per-call admission        |                    |  OAuth handshake
                                        |                    |
   caller (agent, automation,           |     external client (e.g. Claude
   connected app) with an               |     Code via its connector):
   opaque bearer                        |     discovery -> registration ->
        |                               |     PKCE authorize -> user consent
        v                               |     with account binding -> opaque
   guarded operation boundary ----------+     bearer + a new "oauth" card
   resolves bearer x current card
   x active catalog, per call;
   allow with bounded authority
   or structured denial
```

Two directions share one house. **Delegated to the hub**: the user connects
provider accounts (Google, Slack, ...) once, and the hub keeps the
credentials; a tool that needs one receives a one-call token after admission,
the caller never holds it. **Delegated by the hub**: every caller gets a
card, and services verify against the card, live.

## Capabilities of the package

| Capability | What it gives | Owned by |
| --- | --- | --- |
| Delegated cards | versioned per-caller authority records, current pointer, drift reconciliation | `connection_hub.delegated_credentials`, [delegated cards](package/delegated-cards.md) |
| Capability catalog | the deployment's delegable ceiling as immutable versions | [architecture](connection-hub-architecture.md) |
| Direct admission | an external backend asks for a live decision per operation, with replay-protected workload proof | `delegated_credentials/admission.py`, [recipe](recipes/direct-protected-service.md) |
| Named-service admission | the same decision at in-host namespace/operation boundaries | `named_service_admission.py`, `named_service_boundary.py` |
| OAuth adapter | RFC 8414/9728 discovery, three client-registration paths, PKCE, consent with account binding, opaque bearers, RFC 7009 revocation | [OAuth protocol](package/oauth-delegated-credential-protocol.md) |
| External MCP proxy | owner-selected MCP connectors, upstream OAuth or direct credentials, accepted descriptor revisions, exact delegated tools, refresh, and revocation | [architecture](connection-hub-architecture.md#user-owned-external-mcp-proxy) |
| Client SDK | `ConnectionsClient`: catalog, status, grant checks, one-call tokens, disconnect, OAuth start | `client.py`, `contract.py` |
| Consent recovery | structured denials that carry the exact missing grant and where to give it | `mcp_consent.py`, `consent_state.py` |
| Authority registry | pluggable identity realms verifying credentials | `authority_registry.py` |

## Where each thing is configured

All configuration lives with the hub's host (in KDCube: the app descriptor of
`connection-hub@1-0`). The shapes below are orientation skeletons; the linked
documents carry the authoritative contracts.

**The delegable catalog** (what exists to delegate):

```yaml
connections:
  delegated_credentials:
    catalog:
      capabilities:
        - grant: customers:search
          label: Search customers
      resources:
        - resource: https://api.example.test/customers
          grants: [customers:search]
          operations:
            - operation: customers.search
              grants: [customers:search]
```

**A protected service registration** (a workload that may ask for
admission): selectors only, no restated operations:

```yaml
connections:
  delegated_credentials:
    admission:
      services:
        crm-api:
          label: CRM API
          secret_ref: secrets.crm_api_signing
          resources: ["https://api.example.test/customers"]
```

**A named-service boundary** (in-host namespaces declaring their guarded
operations): parsed by `NamespaceBoundaryPolicy.from_config`, with per-tool
and per-operation grant vocabularies.

**OAuth clients**: pre-registered entries in the descriptor, client-id URLs
(CIMD), or dynamic registration; consent contract and account-requirement
panels per the [OAuth protocol](package/oauth-delegated-credential-protocol.md).

**External MCP upstream OAuth**: `connections.remote_mcp.oauth` controls the
public client metadata/callback base, state lifetime, and refresh leeway.
`connections.remote_mcp.outbound` controls the endpoint network policy. These
settings govern Connection Hub as an OAuth client of an owner-selected remote
MCP and are independent from the delegated OAuth server configured under
`connections.delegated_credentials.oauth`.

Full walkthroughs: [direct protected-service recipe](recipes/direct-protected-service.md)
for the service lane, the OAuth protocol document for the client lane, and
[delegated authority and admission](package/delegated-authority-and-admission.md)
for the decision mechanics across every surface.
