// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Elena Viter

/** A Card's personal MCP servers change only by the Card's owner (W918).
 *
 *  The same rule the Hub enforces on every Save (owner_only_connectors.py): a
 *  viewer who is not the Card's grantor may keep each connector exactly as it is
 *  or remove it, never add one or change one. The editor shows that rule, it does
 *  not decide it. A connector is the Hub's own resource kind, by its stable id. */

export const CONNECTOR_RESOURCE_PREFIX = 'urn:connection-hub:remote-mcp:';

export const CONNECTORS_OWNER_ONLY_NOTE = 'Only the Card\'s owner changes these.';

// The Hub's own connector id (remote_mcp.models: `mcp_` and 24 lower-case letters or digits).
const CONNECTOR_ID = /^mcp_[a-z0-9]{24}$/;

/** Whether a resource is a Hub connector, parsed exactly as the Hub does (connector_id_from_resource):
 *  the trimmed value starts with the prefix and the rest, lower-cased, is a valid connector id. */
export function isConnectorResource(resource: string | undefined): boolean {
  const text = String(resource || '').trim();
  if (!text.startsWith(CONNECTOR_RESOURCE_PREFIX)) return false;
  return CONNECTOR_ID.test(text.slice(CONNECTOR_RESOURCE_PREFIX.length).trim().toLowerCase());
}

/** Whether this viewer may only keep or remove the Card's connectors (not its grantor).
 *
 *  The exact comparison the Hub makes (actor subject against the stored grantor), with no
 *  alias folding; an unknown viewer or grantor counts as not the owner, so the editor never
 *  offers more than the Hub will accept. */
export function connectorsOwnerOnly(grantorSubject: string | undefined, viewerSubject: string | undefined): boolean {
  const grantor = String(grantorSubject || '');
  const viewer = String(viewerSubject || '');
  return !grantor || !viewer || grantor !== viewer;
}
