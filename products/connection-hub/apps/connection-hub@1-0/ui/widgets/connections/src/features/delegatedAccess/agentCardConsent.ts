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

/** A hosted KDCube agent's client id. Operator, 2026-11-07: "W1018 i am sure
 *  all must go via card": every hosted agent's consent opens its Card review. */
export const HOSTED_AGENT_CLIENT_PREFIX = 'kdcube-agent:';

export function isHostedAgentClient(clientId: string): boolean {
  return clientId.trim().startsWith(HOSTED_AGENT_CLIENT_PREFIX);
}

/** Card consent review is selected by the marker, or by the caller: a pending
 *  request from a hosted agent opens it even when an older link carries no
 *  marker. An external client's pending request keeps its pending page. */
export function agentCardConsentFromParams(get: (key: string) => string): AgentCardConsentRequest | null {
  const marked = ['1', 'true'].includes(get('agent_card_consent').trim().toLowerCase());
  const hostedPending = get('pending_agent_grant').trim() === '1' && isHostedAgentClient(get('agent_client_id'));
  if (!marked && !hostedPending) return null;
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
  // The owner's single active Card for this agent first; a supplied access_id
  // only has to be that Card. An inactive named record or one of several active
  // Cards is never picked through the id.
  const cards = items.filter((item) => (item.client_id || '') === request.clientId
    && (item.state || 'active') === 'active');
  if (!cards.length) return { state: 'refused', reason: 'agent_card_not_found' };
  if (cards.length > 1) return { state: 'refused', reason: 'agent_card_ambiguous' };
  if (request.accessId && request.accessId !== cards[0].access_id) {
    return { state: 'refused', reason: 'agent_card_mismatch' };
  }
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
  | { kind: 'named_unavailable'; accountId: string }
  | { kind: 'sole'; accountId: string }
  | { kind: 'choose'; candidates: string[] }
  | { kind: 'none' };

/** One account requirement of the request: an account of `providerId` that
 *  holds every claim. `accountId` is set when the request names the exact
 *  account; then only that account can satisfy it. */
export interface ConsentRequirement {
  providerId: string;
  claims: string[];
  accountId?: string;
}

export function capableAccounts(accounts: readonly ConsentAccount[], requirement: ConsentRequirement): ConsentAccount[] {
  return accounts.filter((account) => account.provider_id === requirement.providerId
    && requirement.claims.every((claim) => (account.claims || []).includes(claim)));
}

/** Which account the consent proposes for one requirement. A named account is
 *  proposed only if it can do every claim; otherwise the request stays
 *  unsatisfiable here (the owner connects or upgrades that account), never
 *  answered by another account. Unnamed: the only capable account, or the
 *  owner's explicit choice among several. */
export function proposeConsentAccount(
  accounts: readonly ConsentAccount[],
  requirement: ConsentRequirement,
): AccountProposal {
  const capable = capableAccounts(accounts, requirement);
  if (requirement.accountId) {
    return capable.some((account) => account.account_id === requirement.accountId)
      ? { kind: 'named', accountId: requirement.accountId }
      : { kind: 'named_unavailable', accountId: requirement.accountId };
  }
  if (capable.length === 1) return { kind: 'sole', accountId: capable[0].account_id };
  if (capable.length > 1) return { kind: 'choose', candidates: capable.map((account) => account.account_id) };
  return { kind: 'none' };
}

/** The account in `scope` that already satisfies the requirement (the named
 *  one only, when it is named), or '' when none does. */
export function accountSatisfying(
  scope: Record<string, Record<string, string[]>>,
  requirement: ConsentRequirement,
): string {
  const held = scope[requirement.providerId] || {};
  const ids = requirement.accountId ? [requirement.accountId] : Object.keys(held);
  return ids.find((accountId) => requirement.claims.every((claim) => (held[accountId] || []).includes(claim))) || '';
}

/** The request's account requirements: the explicit account demand (its
 *  provider, claim and exact account) or every requirement of the requested
 *  operation, each with all its claims. */
export function consentRequirements(
  request: AgentCardConsentRequest,
  operationRequirements: ReadonlyArray<{ providerId: string; claims: string[] }>,
  accounts: readonly ConsentAccount[],
): ConsentRequirement[] {
  if (request.accountClaim) {
    const providerId = consentProviderFor(request.accountClaim, operationRequirements, accounts, request.accountProviderId);
    return providerId ? [{ providerId, claims: [request.accountClaim], accountId: request.accountId }] : [];
  }
  return operationRequirements
    .filter((requirement) => requirement.providerId && requirement.claims.length)
    .map((requirement) => ({ providerId: requirement.providerId, claims: Array.from(new Set(requirement.claims)) }));
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

/** The scope with every claim of one requirement added on one account (never removed). */
export function withAccountClaims(
  scope: Record<string, Record<string, string[]>>,
  providerId: string,
  accountId: string,
  claims: string[],
): Record<string, Record<string, string[]>> {
  return claims.reduce((next, claim) => withProposedAccountClaim(next, providerId, accountId, claim), scope);
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
  if (control.state !== 'active' || control.fail_closed || !control.control_authority) return {};
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
  if (!names.length) return '';
  const truncated = (details as { outside_truncated?: unknown })?.outside_truncated === true;
  return `Not allowed by this agent's Control: ${names.join(', ')}${truncated ? ', and more' : ''}.`;
}

/** Whether an outer (MCP/REST) operation is inside the agent's Control: none
 *  bound allows it, an unavailable Control allows nothing. */
export function outerOperationInControl(
  control: { state?: string; fail_closed?: boolean; control_authority?: DelegatedControlCardAuthority; external_mcp_family?: string } | null | undefined,
  resource: string,
  operation?: string,
): boolean {
  if (!operation) return true;
  if (!control || control.state === 'not_controlled') return true;
  if (control.state !== 'active' || control.fail_closed || !control.control_authority) return false;
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

/** A bound Control that cannot be read (not active, or fail-closed): nothing
 *  can be approved on the Card until it can. */
export function controlUnavailable(control: { state?: string; fail_closed?: boolean } | null | undefined): boolean {
  if (!control || control.state === 'not_controlled') return false;
  return control.state !== 'active' || Boolean(control.fail_closed);
}

/** Door claims the request asks for that the Card does not hold yet. */
export function newRequestedClaims(heldClaims: readonly string[], requested: readonly string[]): string[] {
  return requested.filter((claim) => !heldClaims.includes(claim));
}
