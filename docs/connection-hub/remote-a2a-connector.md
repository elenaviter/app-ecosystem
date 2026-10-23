---
id: docs/connection-hub/remote-a2a-connector
title: "Remote A2A Connectors: Asking An Outside Agent"
summary: "How a person registers an outside Agent2Agent (A2A) agent in Connection Hub, how its pinned Agent Card becomes governed operations on a delegated Card, and how an agent sends, reads, cancels and answers tasks through the delegated MCP gateway."
status: "active"
tags: ["connection-hub", "a2a", "agent2agent", "remote-a2a-connector", "delegated-card", "gateway"]
updated_at: 2026-10-23
see_also:
  - ./custom-mcp-connector.md
  - ./package/delegated-mcp-gateway.md
  - ./package/delegated-cards.md
  - ../../packages/app-foundation/README.md
---

# Remote A2A Connectors: Asking An Outside Agent

A remote A2A connector is a person's own registration of one outside agent
that speaks the Agent2Agent protocol 1.0. It works like a
[custom MCP connector](custom-mcp-connector.md). It is a resource of its own,
`urn:connection-hub:remote-a2a:<id>`, with the grant `external_a2a:use`. It is
resolved under the Card's owner, and its credential stays in Connection Hub.

## Registering an agent

The owner gives the agent's Agent Card URL and, if the agent needs one, a
credential: a bearer token or a custom header. Connection Hub then:
- fetches the card through App Foundation's outbound endpoint policy
  (`app_foundation.net`). That means HTTPS, no userinfo, and no private or
  loopback addresses unless the deployment allowlists the host;
- pins its JSON-RPC endpoint for protocol 1.x, its skills, and its declared
  security schemes, under one digest (`app_foundation.a2a`);
- keeps the credential in the owner's secret store, while the connector record
  holds only a reference.

Two kinds of card are refused at registration:
- **An endpoint on another origin.** If the card's endpoint is on a different
  origin than the card URL, it is refused with `card_endpoint_cross_origin`,
  because that endpoint is where the credential would go. A deployment can
  allow it with `connections.remote_a2a.allow_cross_origin_endpoint`.
- **A 0.3-only card**, which is refused with `a2a_version_unsupported`.

Outbound OAuth to an A2A agent is not offered yet.

The operations are `remote_a2a_connectors_list`, `_create`, `_refresh`,
`_accept_descriptor`, `_set_enabled`, `_update_credential` and `_delete`. Each
one is owner-scoped, and the mutations carry an `expected_revision`.

## What a Card can allow

The connector's catalog row offers:
- one operation per pinned skill, `send_<skill id>`;
- `get_task`, `cancel_task` and `reply_task`, which act only on a task the
  same Card sent.

Each operation has its own Once / Every-time policy. A Once on a send is spent
by that send only, so polling the task does not ask again.

A refreshed card whose digest changed is pending, and every operation is
suspended until the owner accepts it again (`operation_descriptor_changed`).
That covers a new endpoint, a skill changing, or the security changing.

## Calling it

An agent reaches the operations through the delegated MCP gateway, for
example with `pb worker services` and `pb worker service-call`. The provider
is `ExternalRemoteA2AProvider`.
- `send_<skill>` takes `text`, plus optional `data` and `context_id`. It
  returns `task_id`, `context_id`, `state` and text or data `artifacts`.
- An input-required task also returns its question as `status_text`.
- A task that was failed, rejected or needs authorization returns one fixed
  code: `remote_task_failed`, `remote_task_rejected` or `remote_auth_required`.
- Every client failure returns `service_unavailable`, or `a2a_task_not_found`
  for a task this Card did not send.

The outside agent's error messages, status texts and links never become part
of a result, a refusal or a consent prompt.

## In a project

On a project chain, a caller's own A2A connectors do not fold through the
project's Controls. The project's A2A switch decides them: the property
`a2a_family` on the project Control, with the deployment family
`user_external_a2a` in `project_resource_families`. It works exactly like the
External MCP switch, and the two are independent. When the switch is absent it
is off. Writing the switch from the project's Card is a later change.
