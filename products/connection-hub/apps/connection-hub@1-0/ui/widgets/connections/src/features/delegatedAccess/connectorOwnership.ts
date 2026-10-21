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

const PERSON_SUBJECT_PREFIXES = ['cognito:', 'user:'];

function bareSubject(value: string | undefined): string {
  const text = String(value || '').trim();
  const prefix = PERSON_SUBJECT_PREFIXES.find((candidate) => text.startsWith(candidate));
  return prefix ? text.slice(prefix.length) : text;
}

export function isConnectorResource(resource: string | undefined): boolean {
  const text = String(resource || '');
  return text.startsWith(CONNECTOR_RESOURCE_PREFIX) && text.length > CONNECTOR_RESOURCE_PREFIX.length;
}

/** Whether this viewer may only keep or remove the Card's connectors (not its grantor).
 *
 *  An unknown viewer or grantor counts as not the owner: the editor never offers more
 *  than the Hub will accept. */
export function connectorsOwnerOnly(grantorSubject: string | undefined, viewerSubject: string | undefined): boolean {
  const grantor = bareSubject(grantorSubject);
  const viewer = bareSubject(viewerSubject);
  return !grantor || !viewer || grantor !== viewer;
}
