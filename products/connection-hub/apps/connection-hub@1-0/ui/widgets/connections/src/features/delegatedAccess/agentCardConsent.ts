import type { DelegatedControlCardAuthority } from '../../api/types';
import { controlKeepsCallerOperation } from './controlCardPreview.ts';
import { focusedGrantArgs, type InvocationMode } from './invocationChoice.ts';

/**
 * W1018 A: a hosted agent's consent opens its own Agent Card.
 *
 * Operator, 2026-11-07 (relayed by the coordinator): "W1018 - A". The design B
 * alternative (an account section in the pending pane) was deferred: "this
 * woyld be cleaner but this is one extra hop and can wait".
 *
 * Both entrances (the chat's one-click Grant, passed as `connections.hub.open`
 * params, and the SDK's account-binding link) carry `agent_card_consent=1`
 * with the agent's `agent_client_id` and what the denial asked for. Every field
 * is a proposal: the Card is resolved here from the viewer's own Cards, the
 * account must be a connected account that holds the claim, and the requested
 * operation is offered only inside the agent's Control.
 */

export interface AgentCardConsentRequest {
  clientId: string;
  /** A Card id the link may name; it must be the Card resolved for clientId. */
  accessId?: string;
  resource: string;
  claims: string[];
  namespace?: string;
  operation?: string;
  outerOperation?: string;
  accountId?: string;
  accountClaim?: string;
  /** The provider of an account-only demand (no namespace/operation to read it from). */
  accountProviderId?: string;
  /** The once/always choice of an outer operation, as the pending page reads it:
   *  `choose`, or a preset `once`/`always`. */
  invocationPolicy?: string;
  invocationChangeId?: string;
  requestBound?: boolean;
  requestDigest?: string;
  requestApprovalTicket?: string;
  requestCardRevision?: number;
  requestAuthorityRevision?: string;
  approvalContext?: Record<string, string>;
}

export function agentCardConsentFromParams(get: (key: string) => string): AgentCardConsentRequest | null {
  if (!['1', 'true'].includes(get('agent_card_consent').trim().toLowerCase())) return null;
  const clientId = get('agent_client_id').trim();
  const resource = get('resource').trim();
  if (!clientId || !resource) return null;
  const optional = (key: string) => get(key).trim() || undefined;
  return {
    clientId,
    accessId: optional('access_id'),
    resource,
    claims: get('claims').split(',').map((item) => item.trim()).filter(Boolean),
    namespace: optional('namespace'),
    operation: optional('operation'),
    outerOperation: optional('outer_operation'),
    accountId: optional('account_id'),
    accountClaim: optional('account_claim'),
    accountProviderId: optional('account_provider_id'),
    invocationPolicy: optional('invocation_policy'),
    invocationChangeId: optional('invocation_change_id'),
    requestBound: get('request_bound').trim() === '1' || undefined,
    requestDigest: optional('request_digest'),
    requestApprovalTicket: optional('request_approval_ticket'),
    requestCardRevision: (() => {
      const value = Number.parseInt(get('request_card_revision').trim(), 10);
      return Number.isFinite(value) ? value : undefined;
    })(),
    requestAuthorityRevision: optional('request_authority_revision'),
    approvalContext: optional('approval_application_id')
      ? { application_id: get('approval_application_id').trim() }
      : undefined,
  };
}

/** Props win over the URL, as for the other entrances (an embedded frame may not allow URL mutation). */
export function agentCardConsentRequest(openParams?: Record<string, string>): AgentCardConsentRequest | null {
  if (openParams) {
    const fromProps = agentCardConsentFromParams((key) => String(openParams[key] ?? ''));
    if (fromProps) return fromProps;
  }
  try {
    const params = new URLSearchParams(window.location.search);
    return agentCardConsentFromParams((key) => params.get(key) ?? '');
  } catch {
    return null;
  }
}

export type AgentCardResolution<T> =
  | { state: 'loading' }
  | { state: 'resolved'; card: T }
  | { state: 'refused'; reason: 'agent_card_not_found' | 'agent_card_ambiguous' | 'agent_card_mismatch' };

/** The viewer's one Card for this agent. A named access_id must be that Card;
 *  none or several is refused by name, never guessed. */
export function resolveAgentCard<T extends { access_id: string; client_id?: string; state?: string }>(
  items: readonly T[],
  request: AgentCardConsentRequest,
  loading: boolean,
): AgentCardResolution<T> {
  if (loading) return { state: 'loading' };
  const cards = items.filter((item) => (item.client_id || '') === request.clientId
    && (item.state || 'active') === 'active');
  if (request.accessId) {
    const named = items.find((item) => item.access_id === request.accessId);
    if (!named) return { state: 'refused', reason: 'agent_card_not_found' };
    if ((named.client_id || '') !== request.clientId) return { state: 'refused', reason: 'agent_card_mismatch' };
    return { state: 'resolved', card: named };
  }
  if (!cards.length) return { state: 'refused', reason: 'agent_card_not_found' };
  if (cards.length > 1) return { state: 'refused', reason: 'agent_card_ambiguous' };
  return { state: 'resolved', card: cards[0] };
}

export function agentCardResolutionMessage(reason: string, clientId: string): string {
  if (reason === 'agent_card_mismatch') {
    return `The link names a Card that does not belong to agent ${clientId}. Nothing was opened.`;
  }
  if (reason === 'agent_card_ambiguous') {
    return `You hold more than one active Card for agent ${clientId}. Open the one you mean from Granted access.`;
  }
  return `You hold no active Card for agent ${clientId}, so there is nothing to extend here.`;
}

export interface ConsentAccount {
  account_id: string;
  provider_id: string;
  claims?: string[];
  display_name?: string;
  email?: string;
}

export type AccountProposal =
  | { kind: 'named'; accountId: string }
  | { kind: 'sole'; accountId: string }
  | { kind: 'choose'; candidates: string[] }
  | { kind: 'none' };

/** Which account the consent proposes for `claim` on `providerId`: the one the
 *  link names when it can do it, else the only one that can; several capable
 *  accounts are the owner's explicit choice, so none is proposed. */
export function proposeConsentAccount(
  accounts: readonly ConsentAccount[],
  providerId: string,
  claim: string,
  namedAccountId?: string,
): AccountProposal {
  const capable = accounts.filter((account) => account.provider_id === providerId
    && (account.claims || []).includes(claim));
  if (namedAccountId && capable.some((account) => account.account_id === namedAccountId)) {
    return { kind: 'named', accountId: namedAccountId };
  }
  if (capable.length === 1) return { kind: 'sole', accountId: capable[0].account_id };
  if (capable.length > 1) return { kind: 'choose', candidates: capable.map((account) => account.account_id) };
  return { kind: 'none' };
}

/** The provider whose accounts can hold `claim`: the one the request names, else
 *  the capability requirement that names the claim, else the one provider whose
 *  connected accounts hold it. */
export function consentProviderFor(
  claim: string,
  requirements: ReadonlyArray<{ providerId: string; claims: string[] }>,
  accounts: readonly ConsentAccount[],
  namedProviderId?: string,
): string {
  if (namedProviderId) return namedProviderId;
  const required = requirements.find((requirement) => requirement.claims.includes(claim));
  if (required) return required.providerId;
  const providers = Array.from(new Set(accounts
    .filter((account) => (account.claims || []).includes(claim))
    .map((account) => account.provider_id)));
  return providers.length === 1 ? providers[0] : '';
}

/** The Card's account scope with the proposed account+claim added (never removed). */
export function withProposedAccountClaim(
  scope: Record<string, Record<string, string[]>>,
  providerId: string,
  accountId: string,
  claim: string,
): Record<string, Record<string, string[]>> {
  if (!providerId || !accountId || !claim) return scope;
  const held = scope[providerId]?.[accountId] || [];
  if (held.includes(claim)) return scope;
  return {
    ...scope,
    [providerId]: { ...(scope[providerId] || {}), [accountId]: [...held, claim] },
  };
}

/** Whether the requested operation may be offered: only inside the agent's Control.
 *  `controlOperations` is the Control's namespace -> operations ('*' = all);
 *  `null` means no Control binds this Card. */
export function requestedOperationInControl(
  controlOperations: Record<string, string[]> | null,
  namespace?: string,
  operation?: string,
): boolean {
  if (!namespace || !operation) return true;
  if (controlOperations === null) return true;
  const allowed = controlOperations[namespace] || [];
  return allowed.includes('*') || allowed.includes(operation);
}

/** The Control ceiling for one resource's named-service operations, as
 *  namespace -> operations ('*' = every one). `null` when no Control binds the
 *  Card; an unavailable or fail-closed Control offers nothing. */
export function controlNamedOperations(
  control: {
    state?: string;
    fail_closed?: boolean;
    control_authority?: { named_service_operations?: '*' | Record<string, Record<string, string[]>> };
  } | null | undefined,
  resource: string,
): Record<string, string[]> | null {
  if (!control || control.state === 'not_controlled') return null;
  if (control.state !== 'active' || !control.control_authority) return {};
  const named = control.control_authority.named_service_operations;
  if (named === '*') return { '*': ['*'] };
  return { ...(named?.['*'] || {}), ...(named?.[resource] || {}) };
}

/** `requestedOperationInControl` with a namespace-wide '*' row as well. */
export function operationInControl(
  controlOperations: Record<string, string[]> | null,
  namespace?: string,
  operation?: string,
): boolean {
  if (!namespace || !operation || controlOperations === null) return true;
  const allowed = [...(controlOperations[namespace] || []), ...(controlOperations['*'] || [])];
  return allowed.includes('*') || allowed.includes(operation);
}

export interface AgentCardConsentGrantInput {
  card: {
    access_id: string;
    client_id?: string;
    card_revision?: number;
    resource_grants?: Record<string, string[]>;
    named_service_operations?: '*' | Record<string, Record<string, string[]>>;
    account_scope?: Record<string, Record<string, string[]>>;
  };
  request: AgentCardConsentRequest;
  /** The door grants the requested operation needs (empty for an account-only request). */
  requiredDoorGrants: string[];
  /** Whether the owner kept the requested operation ticked (only offered inside the Control). */
  operationSelected: boolean;
  accountScope: Record<string, Record<string, string[]>>;
}

/** The one `delegated_agent_grant_create` the Approve sends: on the resolved
 *  Card, at the revision shown, adding the requested capability and the exact
 *  account scope in the existing Card transaction. It never removes anything
 *  the Card holds. */
export function agentCardConsentGrant(input: AgentCardConsentGrantInput) {
  const { card, request, requiredDoorGrants, operationSelected, accountScope } = input;
  const held = card.resource_grants?.[request.resource] || [];
  const withOperation = operationSelected && request.namespace && request.operation;
  const claims = Array.from(new Set([
    ...held,
    ...request.claims,
    ...(withOperation ? requiredDoorGrants : []),
  ]));
  const storedNamed = card.named_service_operations;
  const heldNamespaces = storedNamed && storedNamed !== '*' ? storedNamed[request.resource] || {} : {};
  const namedServiceOperations: Record<string, string[]> | undefined = withOperation
    ? {
        ...heldNamespaces,
        [request.namespace as string]: Array.from(new Set([
          ...(heldNamespaces[request.namespace as string] || []),
          request.operation as string,
        ])),
      }
    : undefined;
  return {
    clientId: request.clientId,
    accessId: card.access_id,
    resource: request.resource,
    claims,
    ...(namedServiceOperations ? { namedServiceOperations } : {}),
    accountScope,
    ...(typeof card.card_revision === 'number' ? { expectedCardRevision: card.card_revision } : {}),
  };
}

/** The bounded refusal rows (`details.outside`) as one sentence; no raw text. */
export function outsideControlText(
  details: unknown,
  resourceLabel: (resource: string) => string,
): string {
  const rows = Array.isArray((details as { outside?: unknown })?.outside)
    ? ((details as { outside: unknown[] }).outside)
    : [];
  const names = rows.flatMap((row) => {
    if (!row || typeof row !== 'object') return [];
    const { resource, namespace, operation, grant } = row as Record<string, unknown>;
    const safe = (value: unknown) => (typeof value === 'string' && /^[\w.:*@/\-]{1,200}$/.test(value) ? value : '');
    const what = safe(namespace) && safe(operation)
      ? `${safe(namespace)}.${safe(operation)}`
      : safe(operation) || safe(grant);
    if (!what) return [];
    return [safe(resource) ? `${what} on ${resourceLabel(safe(resource))}` : what];
  });
  return names.length ? `Not allowed by this agent's Control: ${names.join(', ')}.` : '';
}

/** Whether an outer (MCP/REST) operation is inside the agent's Control: none
 *  bound allows it, an unavailable Control allows nothing. */
export function outerOperationInControl(
  control: { state?: string; control_authority?: DelegatedControlCardAuthority; external_mcp_family?: string } | null | undefined,
  resource: string,
  operation?: string,
): boolean {
  if (!operation) return true;
  if (!control || control.state === 'not_controlled') return true;
  if (control.state !== 'active' || !control.control_authority) return false;
  return controlKeepsCallerOperation(control.control_authority, resource, operation, control.external_mcp_family);
}

/** What the outer operation's once/always choice needs before Approve:
 *  `none` (no policy asked), `choose` (the owner picks), `preset` (the link
 *  fixed it), or `missing` (a policy was asked but the denial gave no change
 *  id, so no policy can be committed and Approve stays disabled). */
export function outerPolicyState(request: AgentCardConsentRequest): 'none' | 'choose' | 'preset' | 'missing' {
  if (!request.outerOperation || !request.invocationPolicy) return 'none';
  if (!request.invocationChangeId) return 'missing';
  return request.invocationPolicy === 'choose' ? 'choose' : 'preset';
}

/** The one grant for a request that names an outer operation. With a policy,
 *  it is the existing focused grant (exactly that operation, its mode, change id
 *  and any request-bound proof) on the resolved Card, with the account scope
 *  in the same transaction; without one, the operation joins the Card's held
 *  operations. */
export function agentCardOuterGrant(
  input: AgentCardConsentGrantInput & {
    card: AgentCardConsentGrantInput['card'] & { resource_operations?: Record<string, string[]> };
    mode: InvocationMode | null;
  },
) {
  const { card, request, accountScope, mode, operationSelected } = input;
  const base = agentCardConsentGrant({ ...input, operationSelected: false });
  if (!request.outerOperation || !operationSelected) return base;
  if (request.invocationPolicy && request.invocationChangeId && mode) {
    return focusedGrantArgs({
      clientId: request.clientId,
      accessId: card.access_id,
      resource: request.resource,
      operation: request.outerOperation,
      changeId: request.invocationChangeId,
      requestBound: request.requestBound,
      requestDigest: request.requestDigest,
      requestApprovalTicket: request.requestApprovalTicket,
      requestCardRevision: request.requestCardRevision,
      requestAuthorityRevision: request.requestAuthorityRevision,
      approvalContext: request.approvalContext,
    }, mode, base.claims, {
      accountScope,
      ...(typeof card.card_revision === 'number' ? { expectedCardRevision: card.card_revision } : {}),
    });
  }
  const held = card.resource_operations?.[request.resource] || [];
  return { ...base, resourceOperations: Array.from(new Set([...held, request.outerOperation])) };
}
