---
id: docs/connection-hub/custom-mcp-connector
title: "Custom MCP Connectors And Governed Invocation"
summary: "What a custom MCP connector is, how Connection Hub proxies it and flattens every connected server into one governed tool list, how an external agent uses it by pointing at one URL, and how a hosted KDCube agent takes it through the descriptor ceiling delegated_resource_families."
status: "active"
tags: ["connection-hub", "custom-mcp-connector", "external-mcp", "governed-proxy", "delegated-card", "resident-agent", "descriptor"]
updated_at: 2026-09-10
see_also:
  - ./quick-start-local.md
  - ./connection-hub-architecture.md
  - ./package/delegated-mcp-gateway.md
  - ./package/delegated-cards.md
  - ./package/delegated-authority-and-admission.md
  - ./configuration-and-capabilities.md
  - ./testing/end-to-end-acceptance.md
  - https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/recipes/connections/custom-mcp-connector-README.md
  - https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/sdk/bundle/surfaces/as-consumer-surfaces-README.md
---

# Custom MCP Connectors And Governed Invocation

A custom MCP connector is an MCP server that a signed-in user registers in
Connection Hub. Nothing about the server is known to the deployment in
advance: not its URL, not its tools, not its credential. The UI calls this
tab **External MCP**; Claude Desktop calls the same thing a custom connector;
the package calls the record a remote MCP connector. This page uses "custom
MCP connector".

The reason this is its own concept: a tool an application declares in its
descriptor is the application's decision, made once by whoever deploys it.
A custom connector is the user's decision, made after deployment, about a
server the platform has never seen. Connection Hub is the one place that
holds that server's credential, discovers its tools, and stands between
every caller and the server, so that a user can hand a coding agent or a
hosted agent exactly two tools of a paid API without handing over the key.

```text
                     the user registers a server once
                                    |
                                    v
                    +---------------------------------+
                    |  Connection Hub                 |
   external agent   |  connector record + credential  |     the user's MCP
   (Claude Desktop, |  discovered descriptor          |     server, any
   Claude Code,     |  one resource on delegated cards| --> streamable-HTTP
   Codex, Hermes,   |  governed MCP proxy             |     endpoint, any
   OpenClaw) -----> |    tools/list  tools/call       |     credential
                    |    every call rechecked         |
   hosted KDCube    |    credential injected here     |
   agent ---------> |                                 |
                    +---------------------------------+
                                    ^
                        each caller holds only its own
                        Connection Hub credential
```

## 1. What Connection Hub does with a connector

**The record.** The user names the server, gives its streamable-HTTP
endpoint, and chooses how Connection Hub authenticates to it: no credential,
a bearer token, a custom header, or an OAuth browser login (automatic client
registration, or a client the user created in the provider console). The
credential goes once into the user's server-side secret store. The durable
connector record keeps an opaque reference and a presence marker, never the
value. See the [quick start](quick-start-local.md#2-connect-the-remote-mcp)
for the form itself and the [architecture](connection-hub-architecture.md#user-owned-external-mcp-proxy)
for the OAuth chain.

**Discovery.** Connection Hub runs `initialize` and `tools/list` against the
endpoint through the same network boundary later calls use, and stores the
tool descriptors with a digest and a revision. Discovered tools are
descriptions, not permissions: nothing is granted by discovery. When a later
discovery finds a changed descriptor, the affected tool is suspended until
the user accepts the change.

**The resource.** A connector becomes one stable resource with the id

```text
urn:connection-hub:remote-mcp:<connector-id>
```

independent of its URL, name, or label. It is the unit a delegated card can
grant, with an exact subset of the discovered tool names as its operations.
Two connectors advertising a tool with the same name stay distinct, because a
grant names the resource and the tool together.

**The doors.** Every caller reaches connectors through one of two MCP
endpoints Connection Hub serves. Both authenticate the caller, resolve the
caller's live card, and answer only what that card grants.

| Door | Lists | Tool names |
| --- | --- | --- |
| `public/mcp/remote_mcp_proxy` | custom connectors only | `<connector-id>__<tool-slug>_<10 hex of the tool name>` |
| `public/mcp/delegated_mcp_gateway` | everything on the card: custom connectors (`remote_mcp`) and managed KDCube MCP surfaces (`managed_kdcube_mcp`) | `ch_<kind>_<16 hex of the resource id>__<operation-slug>_<16 hex of the route>` |

This is the flattening: however many servers the user connected and however
many the card grants, the caller sees one top-level tool list, each entry
routing to exactly one resource and operation. Names derive from stable
identity, so a renamed label or a moved endpoint does not rename a tool, and
a hash collision is refused rather than resolved by guessing.

**Every call.** `tools/call` does not trust an earlier `tools/list`. It
re-reads the card, re-resolves the connector and its accepted descriptor,
verifies the resource-qualified grant, checks connector state and credential
readiness, consumes the invocation policy, injects the upstream credential,
dispatches, and records provenance. A card edit, a revocation, a disabled
connector, a drifted descriptor, or a lost credential therefore changes the
next call, for every caller and on every worker.

**Invocation policy.** Per operation, the card carries `always` (reusable) or
`once` (one invocation, consumed atomically even under concurrent calls).
A denied call returns a structured refusal with a recovery link to the exact
card screen; it never dispatches silently.

## 2. Using a connector from an external agent

An external agent needs one thing: the door URL. For a deployment at
`https://<host>` with tenant `<tenant>` and project `<project>`:

```text
https://<host>/api/integrations/bundles/<tenant>/<project>/connection-hub@1-0/public/mcp/remote_mcp_proxy
```

Claude Desktop takes it in Settings, Connectors, Add custom connector. Claude
Code takes it with `claude mcp add --transport http <name> <url>` followed by
`claude mcp login <name>`. Codex, Hermes and OpenClaw follow the same shape
through the [local client helper](local-client-helper.md).

The client's first connection runs OAuth against Connection Hub. The user
signs in, sees only their own connectors, picks the connector and the exact
tools, chooses `once` or `always` per tool, and approves. That approval
creates one delegated card for that client, with its own `access_id` and its
own credential. Nothing the client stores can reach the upstream server.

```text
Claude Desktop ---OAuth---> Connection Hub ---> card "Claude Desktop"
                                                  resource: urn:...:remote-mcp:abc
                                                  operations: search (always)
tools/list  -> [ abc__search_1f3e9a7c2d ]
tools/call  -> card ok, connector ok, policy ok, credential injected, dispatched
```

Afterwards the user edits or revokes the card in **Delegated by KDCube**
without the client logging in again. The next call sees the change. A second
client of the same kind (two Claude installs) gets a second card, never a
shared one.

## 3. Using a connector from a hosted KDCube agent

A hosted agent is a caller too, and it goes through the same doors, cards,
policy and proxy. What differs is who says the agent may take user
connectors at all. An external client is the user's own program; the user
answers for it at consent. A hosted agent belongs to an application someone
else deployed, so the application must opt in first, and bound what it opts
into. That bound is the descriptor ceiling.

### 3.1 The resident caller profile and its card

Inside Connection Hub a hosted agent is a **resident caller profile**: one
agent of one application, acting for one user. Its identity is

```text
kdcube-agent:<application>:<agent>
```

and it has exactly one card per user, whose `access_id` stays stable while
resources are added and removed. Everything bound to the card, the reusable
bearer, the invocation policies, the recovery links, the audit trail, keys on
that id. Managed KDCube resources the application declares and custom
connectors the user grants sit on the same card.

The user grants a connector to a hosted agent in two ways: by editing the
agent's card under Delegated by KDCube, or from the chat, when the agent
attempts a tool it does not hold and the consent banner opens the prefilled
card screen for exactly that resource and tool.

### 3.2 The descriptor ceiling: `delegated_resource_families`

**Where it lives.** In the KDCube application descriptor (`bundles.yaml`),
under the agent that may consume user-owned resources:

```yaml
surfaces:
  as_consumer:
    agents:
      <agent_id>:
        delegated_resource_families:
        - id: user_external_mcp
          resource_kinds: [remote_mcp]
          authority_sources: [delegated_card]
          transports: [streamable-http]
          resource_patterns:
          - "urn:connection-hub:remote-mcp:*"
          allowed_tools: ["*"]
          max_resources: 8
          max_tools_per_resource: 64
```

**What it is.** A family is a class of user-owned resources this agent may
consume, described by kind, transport, resource id pattern and tool pattern,
with size limits. It is a ceiling: the most the agent can ever be given. It
grants nothing by itself. The user still has to grant each connector on the
agent's card, and each grant is intersected with the ceiling.

**Whose concept it is.** It is a KDCube descriptor concept, on the same level
as `mcp.services` and `agents.<id>.tools`: the application's outbound
contract. Connection Hub has no field of that name and receives nothing
from it. Connection Hub knows cards, resources, grants, policies, and doors.
Its caller model carries an optional `resource_ceiling`, a pattern list that
would narrow requestable discovery for a caller that presents one, and the
hosted door does not fill it today. So the offers the user sees at consent
come from the card and the connector list alone, and the ceiling is applied
when KDCube binds the resources: a grant outside it is not silently dropped,
it shows in the Extensions list as refused, with the reason.

**Who reads it and where it is enforced.** KDCube reads it, in two places,
both on the KDCube side of the boundary:

1. When the capabilities view for a conversation is built (the Extensions
   list the user sees), and
2. At the start of every turn, when the agent's tools are bound.

Both run one resolver over the same inputs, so the UI cannot show a tool the
turn would refuse. Connection Hub enforces its own layer on every call
regardless: the card, the connector state, the policy. A resource the ceiling
rejects never reaches Connection Hub from that agent, and a call Connection
Hub refuses never reaches the server.

**The effective set.** For one agent, one user, one conversation:

```text
   application ceiling          delegated_resource_families on the agent
          ∩
   the user's card              resources and tools granted to kdcube-agent:<app>:<agent>
          ∩
   readiness                    connector enabled, credential present, descriptor accepted
          ∩
   conversation narrowing       what the user unticked for this conversation
          =
   the tools bound this turn, and the Extensions list
```

Each layer can only remove. Nothing in a lower layer widens what a higher
one allowed.

### 3.3 The fields of one family

Each entry of `delegated_resource_families` is one family. An agent may list
several. Values marked as globs use shell-style matching (`*`, `?`, `[...]`),
case-sensitive.

| Field | Required | Values | Meaning |
| --- | --- | --- | --- |
| `id` | yes | an identifier unique within the agent | Names the family in capability views and refusal reasons. |
| `resource_kinds` | yes | `remote_mcp` for custom connectors; `managed_kdcube_mcp` for managed KDCube MCP surfaces the application declares | Only resources of a listed kind are admitted. |
| `authority_sources` | no, default `delegated_card` | `delegated_card`, `application`, `connected_account` | Who authorized the resource: a delegated card the user granted (custom connectors), the application's own descriptor, or a connected-account claim. |
| `transports` | yes | `streamable-http` (the only transport a custom connector can have today). Values are lower-cased and `_` becomes `-`. | Transports the agent may bind. |
| `resource_patterns` | yes | globs over the stable resource id: `urn:connection-hub:remote-mcp:*` for every custom connector, or the exact id of one | Which resources the family admits. |
| `allowed_tools` | no, default `*` | globs over the tool name as the server advertises it: `search`, `read_*` | Which tools of an admitted resource the agent may take. |
| `endpoint_schemes` | no | lower-case globs over the endpoint scheme: `https` | A further bound on where the resource may point. |
| `endpoint_hosts` | no | lower-case globs over the endpoint host: `*.example.com` | Same, by host. |
| `max_resources` | no, default 8 | positive integer | At most this many resources of the family bind in one turn. |
| `max_tools_per_resource` | no, default 64 | positive integer | At most this many tools per resource. |

A family missing a required field fails descriptor parsing with
`resource_family_id_required`, `resource_family_kinds_required:<id>`,
`resource_family_transports_required:<id>` or
`resource_family_patterns_required:<id>`, and an unlisted authority source
with `unknown_resource_authority_source:<value>`. The agent then binds no
user-owned resource until the descriptor is fixed. The agent id is matched
as written, then with dots and dashes folded to underscores.

### 3.4 What the user sees

In Connection Hub, the agent's card under Delegated by KDCube lists its
resources and granted tools with their policies, as for any caller. In the
chat, the Extensions list shows each granted connector and tool, and, for
anything the ceiling or the card refused, the reason:

| Reason | Meaning |
| --- | --- |
| `resource_outside_ceiling`, `resource_kind_mismatch`, `tool_outside_ceiling` | The card grants it, the application's ceiling does not admit it. |
| `resource_limit_exceeded`, `tool_limit_exceeded` | Over `max_resources` or `max_tools_per_resource`. |
| `resource_not_granted`, `operation_not_granted` | Within the ceiling, not on the card. |
| `connector_disabled`, `credential_missing`, `operation_descriptor_changed`, `operation_removed` | The connector is not ready, or its descriptor drifted and awaits acceptance. |
| `once_exhausted` | The one-invocation policy was consumed. |
| `conversation_disabled` | The user narrowed it away for this conversation. |
| `card_missing`, `card_revoked`, `card_expired` | The agent has no live card for this user. |

The user narrows per conversation in the capability picker; a narrowing can
only remove, and it never touches the card.

### 3.5 Why this is not "just connect a tool"

`mcp.services` plus `agents.<id>.tools` declares a server the application
knows, authenticated as the application, the same for every user. A custom
connector is unknown to the application, belongs to one user, is
authenticated with that user's credential held by Connection Hub, and is
granted per agent per user with per-tool policy that the user can withdraw
at any time. The ceiling is the application's only say in that arrangement:
which kinds, which servers, which tools, how many.

## 4. Layers, from descriptor to server

```text
 who              what they control                       when it is applied
 ---------------  -------------------------------------   --------------------------
 app deployer     delegated_resource_families (ceiling)   descriptor load; every turn
 user             connector record + credential           connect, reconnect, delete
 user             card grant per caller, per tool,        card edit, consent banner
                  once/always
 user             conversation narrowing                  the picker, per conversation
 KDCube           effective-set resolver                  capabilities view; turn start
 Connection Hub   card, connector state, descriptor,      every tools/list, tools/call
                  policy, credential injection, audit
 remote server    its own authorization                   receives the injected credential
```

## 5. Operator configuration

The deployment enables and bounds connectors on the Connection Hub
application, not per user:

- `connections.remote_mcp.outbound`: the network policy for connector
  endpoints (`allow_http`, `allow_private_networks`, `allowed_hosts`).
  Loopback, private, link-local and metadata targets are refused by default;
  a local server reached as `host.docker.internal` needs an explicit entry.
- `connections.remote_mcp.oauth`: the public base URL the OAuth callback and
  client metadata document are served from, state lifetime, refresh leeway.
- `connections.remote_mcp.read_timeout_seconds` and `max_result_bytes`.

The full mapping is in
[configuration and capabilities](configuration-and-capabilities.md). The
step-by-step for connecting a server and wiring it to hosted and external
agents is the KDCube recipe
[Use A Custom MCP Server From KDCube Agents And External Clients](https://github.com/kdcube/kdcube/blob/main/app/ai-app/docs/recipes/connections/custom-mcp-connector-README.md).
