/**
 * W34: more access, added on the agent's Card in the same consent.
 *
 * The colleague, 2026-11-11: "огляд лише додає. Повне редагування лишатиметься
 * на редагуванні в самому хабі." The Agent Card consent review offers, beside
 * what the agent asked for, everything the agent's Control allows on every
 * resource and service, and every connected account's provider permissions.
 * It only adds (merge): what the Card holds is shown and never removed; the
 * Card's own Edit stays the place that narrows (`replace`).
 *
 * The same pending page had this before W1018 ("Give this agent more access
 * (optional) - pick from anything delegable here", 2026-07-18); W1018 moved a
 * hosted agent's consent to its Card review, which held only the request.
 *
 * And, "так, варіант 2": a provider permission no connected account holds is
 * requested at that provider by a button that first saves everything selected.
 */
import type {
  DelegatedAccessNamedServiceNamespaceOption,
  DelegatedAccessResourceOption,
  DelegatedControlCardAuthority,
} from '../../api/types';
import {
  controlNamedOperations,
  operationInControl,
  outerOperationInControl,
  type AgentCardConsentRequest,
  type ConsentAccount,
} from './agentCardConsent.ts';
import {
  accountClaimsForOperation,
  doorGrantsForOperation,
  type NamedServiceOperationProjection,
} from './pendingGrantProjection.ts';

type Scope = Record<string, Record<string, string[]>>;

export interface ExtraControl {
  state?: string;
  fail_closed?: boolean;
  control_authority?: DelegatedControlCardAuthority;
  external_mcp_family?: string;
}

export interface ExtraCard {
  access_id: string;
  client_id?: string;
  card_revision?: number;
  resource_grants?: Record<string, string[]>;
  resource_operations?: Record<string, string[]>;
  named_service_operations?: '*' | Record<string, Record<string, string[]>>;
  account_scope?: Scope;
}

/** One offered entry. `held`: the Card has it (shown ticked, never sent as a
 *  change). `inControl`: the Control allows it (otherwise shown disabled). */
export interface ExtraRow {
  key: string;
  label: string;
  held: boolean;
  inControl: boolean;
  /** The door grants this entry brings with it (an operation's grants). */
  grants: string[];
}

export interface ExtraNamespaceOffer {
  namespace: string;
  label: string;
  rows: ExtraRow[];
}

export interface ExtraResourceOffer {
  resource: string;
  label: string;
  grants: ExtraRow[];
  outer: ExtraRow[];
  namespaces: ExtraNamespaceOffer[];
}

/** What the owner ticked beyond the request. */
export interface ExtraPicks {
  grants: Record<string, string[]>;
  outer: Record<string, string[]>;
  named: Record<string, Record<string, string[]>>;
  accounts: Scope;
}

export const NO_EXTRA_PICKS: ExtraPicks = { grants: {}, outer: {}, named: {}, accounts: {} };

function unique(values: Iterable<string>): string[] {
  return Array.from(new Set(Array.from(values).map((value) => value.trim()).filter(Boolean)));
}

function addable(row: ExtraRow): boolean {
  return row.inControl && !row.held;
}

/** The door grants the Control allows on one resource; null when no Control
 *  binds the Card (the catalog is the only bound). */
function controlGrants(control: ExtraControl | null | undefined, resource: string): Set<string> | null {
  if (!control || control.state === 'not_controlled') return null;
  if (control.state !== 'active' || control.fail_closed || !control.control_authority) return new Set();
  const grants = control.control_authority.resource_grants || {};
  return new Set([...(grants[resource] || []), ...(grants['*'] || [])]);
}

function heldNamed(card: ExtraCard, resource: string, namespace: string): '*' | Set<string> {
  const stored = card.named_service_operations;
  if (stored === '*') return '*';
  return new Set(stored?.[resource]?.[namespace] || []);
}

/** Everything the review can offer beyond the request: per catalog resource,
 *  its door grants, its outer (MCP/REST) operations and its named-service
 *  operations, each marked held / inside the Control. A resource is listed only
 *  when it has something the owner could still add; within it, entries outside
 *  the Control stay listed (disabled) so the owner sees why. */
export function consentExtrasOffer(
  card: ExtraCard,
  control: ExtraControl | null | undefined,
  resources: readonly DelegatedAccessResourceOption[],
  rowsFor: (namespace: DelegatedAccessNamedServiceNamespaceOption) => Array<NamedServiceOperationProjection & { managed?: boolean }>,
): ExtraResourceOffer[] {
  return resources.flatMap((option) => {
    const resource = option.resource;
    // Selector rows (a secret family, `*` patterns) are chosen in the Card editor, not here.
    if (!resource || option.selector_type || option.admin_only) return [];
    const heldGrants = new Set(card.resource_grants?.[resource] || []);
    const ceilingGrants = controlGrants(control, resource);
    const managed = new Set(option.managed_grants || []);
    const grants: ExtraRow[] = unique(option.grants || [])
      .filter((grant) => !managed.has(grant))
      .map((grant) => ({
        key: grant, label: grant, held: heldGrants.has(grant),
        inControl: ceilingGrants === null || ceilingGrants.has(grant), grants: [grant],
      }));
    const heldOuter = new Set(card.resource_operations?.[resource] || []);
    const outer: ExtraRow[] = (option.operations || []).map((operation) => ({
      key: operation.name,
      label: operation.label || operation.name,
      held: heldOuter.has(operation.name),
      inControl: outerOperationInControl(control, resource, operation.name),
      grants: unique(operation.grants || []),
    }));
    const ceilingNamed = controlNamedOperations(control, resource);
    const namespaces: ExtraNamespaceOffer[] = (option.named_services || []).flatMap((namespace) => {
      const held = heldNamed(card, resource, namespace.namespace);
      const rows: ExtraRow[] = rowsFor(namespace)
        .filter((row) => !row.managed)
        .map((row) => ({
          key: row.operation,
          label: row.label || row.operation,
          held: held === '*' || held.has(row.operation),
          inControl: operationInControl(ceilingNamed, namespace.namespace, row.operation),
          grants: doorGrantsForOperation(option, namespace, row.operation, row.grants),
        }));
      return rows.some((row) => !row.held)
        ? [{ namespace: namespace.namespace, label: namespace.label || namespace.namespace, rows }]
        : [];
    });
    const offersSomething = [...grants, ...outer, ...namespaces.flatMap((item) => item.rows)].some(addable);
    return offersSomething ? [{ resource, label: option.label || resource, grants, outer, namespaces }] : [];
  });
}

/** The picks, kept only where the offer allows an addition (inside the
 *  Control, not held). A pick the offer no longer allows is never sent. */
export function allowedPicks(offer: readonly ExtraResourceOffer[], picks: ExtraPicks): ExtraPicks {
  const out: ExtraPicks = { grants: {}, outer: {}, named: {}, accounts: picks.accounts };
  offer.forEach((item) => {
    const grants = item.grants.filter(addable).map((row) => row.key)
      .filter((key) => (picks.grants[item.resource] || []).includes(key));
    if (grants.length) out.grants[item.resource] = grants;
    const outer = item.outer.filter(addable).map((row) => row.key)
      .filter((key) => (picks.outer[item.resource] || []).includes(key));
    if (outer.length) out.outer[item.resource] = outer;
    item.namespaces.forEach((namespace) => {
      const operations = namespace.rows.filter(addable).map((row) => row.key)
        .filter((key) => (picks.named[item.resource]?.[namespace.namespace] || []).includes(key));
      if (operations.length) {
        out.named[item.resource] = { ...(out.named[item.resource] || {}), [namespace.namespace]: operations };
      }
    });
  });
  return out;
}

export function hasExtraPicks(picks: ExtraPicks): boolean {
  return Object.keys(picks.grants).length > 0 || Object.keys(picks.outer).length > 0
    || Object.keys(picks.named).length > 0
    || Object.values(picks.accounts).some((accounts) => Object.values(accounts).some((claims) => claims.length > 0));
}

/** The account scope with the picked account permissions added, only claims
 *  the account holds at its provider (never removed). */
export function withExtraAccounts(scope: Scope, picks: Scope, accounts: readonly ConsentAccount[]): Scope {
  let next = scope;
  Object.entries(picks).forEach(([providerId, byAccount]) => {
    Object.entries(byAccount).forEach(([accountId, claims]) => {
      const account = accounts.find((item) => item.account_id === accountId && item.provider_id === providerId);
      if (!account) return;
      const holdable = claims.filter((claim) => (account.claims || []).includes(claim));
      const held = next[providerId]?.[accountId] || [];
      const added = holdable.filter((claim) => !held.includes(claim));
      if (!added.length) return;
      next = { ...next, [providerId]: { ...(next[providerId] || {}), [accountId]: [...held, ...added] } };
    });
  });
  return next;
}

export interface ExtraGrant {
  clientId: string;
  accessId: string;
  resource: string;
  claims: string[];
  resourceOperations?: string[];
  namedServiceOperations?: Record<string, string[]>;
}

/** One merge grant per resource the picks touch, each a superset of what the
 *  Card holds there: its held door grants, the picked grants, and the door
 *  grants every picked operation needs. Account permissions are Card-wide and
 *  travel with the first write (see `consentWrites`). */
export function consentExtraGrants(
  card: ExtraCard,
  request: AgentCardConsentRequest,
  offer: readonly ExtraResourceOffer[],
  rawPicks: ExtraPicks,
): ExtraGrant[] {
  const picks = allowedPicks(offer, rawPicks);
  const touched = unique([...Object.keys(picks.grants), ...Object.keys(picks.outer), ...Object.keys(picks.named)]);
  return touched.map((resource) => {
    const item = offer.find((entry) => entry.resource === resource) as ExtraResourceOffer;
    const outerPicked = picks.outer[resource] || [];
    const namedPicked = picks.named[resource] || {};
    const claims = unique([
      ...(card.resource_grants?.[resource] || []),
      ...(picks.grants[resource] || []),
      ...item.outer.filter((row) => outerPicked.includes(row.key)).flatMap((row) => row.grants),
      ...item.namespaces.flatMap((namespace) => namespace.rows
        .filter((row) => (namedPicked[namespace.namespace] || []).includes(row.key))
        .flatMap((row) => row.grants)),
    ]);
    const grant: ExtraGrant = { clientId: request.clientId, accessId: card.access_id, resource, claims };
    if (outerPicked.length) {
      grant.resourceOperations = unique([...(card.resource_operations?.[resource] || []), ...outerPicked]);
    }
    if (Object.keys(namedPicked).length) {
      const stored = card.named_service_operations;
      const held = stored && stored !== '*' ? stored[resource] || {} : {};
      grant.namedServiceOperations = Object.fromEntries(
        unique([...Object.keys(held), ...Object.keys(namedPicked)]).map((namespace) => [
          namespace, unique([...(held[namespace] || []), ...(namedPicked[namespace] || [])]),
        ]),
      );
    }
    return grant;
  });
}

/** A provider permission the Card's operations (held or picked) need. */
export interface NeededProviderClaims {
  providerId: string;
  connectorAppId: string;
  /** The resource whose operations need it (the hand-off's agent resource). */
  resource: string;
  claims: string[];
}

/** Provider permissions the held and picked named-service operations need,
 *  per provider: `bindable` some connected account holds but the Card does not
 *  bind yet; `missing` no connected account of that provider holds. */
export function neededProviderClaims(
  card: ExtraCard,
  resources: readonly DelegatedAccessResourceOption[],
  picks: ExtraPicks,
  accounts: readonly ConsentAccount[],
  scope: Scope,
  rowsFor: (namespace: DelegatedAccessNamedServiceNamespaceOption) => Array<{ operation: string }>,
): { bindable: Record<string, string[]>; missing: NeededProviderClaims[] } {
  const needed = new Map<string, NeededProviderClaims>();
  resources.forEach((option) => {
    (option.named_services || []).forEach((namespace) => {
      const held = heldNamed(card, option.resource, namespace.namespace);
      const picked = picks.named[option.resource]?.[namespace.namespace] || [];
      const operations = held === '*'
        ? rowsFor(namespace).map((row) => row.operation)
        : unique([...held, ...picked]);
      operations.forEach((operation) => {
        accountClaimsForOperation(namespace, operation).forEach(({ providerId, claims }) => {
          const requirement = (namespace.connected_accounts || [])
            .find((item) => String(item.provider_id || '').trim() === providerId);
          const connectorAppId = String(requirement?.connector_app_id || '').trim();
          const key = `${providerId}\u0000${connectorAppId}`;
          const entry = needed.get(key) || { providerId, connectorAppId, resource: option.resource, claims: [] };
          entry.claims = unique([...entry.claims, ...claims]);
          needed.set(key, entry);
        });
      });
    });
  });
  const bindable: Record<string, string[]> = {};
  const missing: NeededProviderClaims[] = [];
  needed.forEach((entry) => {
    const bound = new Set(Object.values(scope[entry.providerId] || {}).flat());
    const providerAccounts = accounts.filter((account) => account.provider_id === entry.providerId);
    const holdable = entry.claims.filter((claim) => providerAccounts.some((account) => (account.claims || []).includes(claim)));
    const open = holdable.filter((claim) => !bound.has(claim));
    if (open.length) bindable[entry.providerId] = unique([...(bindable[entry.providerId] || []), ...open]);
    const absent = entry.claims.filter((claim) => !holdable.includes(claim));
    if (absent.length) missing.push({ ...entry, claims: absent });
  });
  return { bindable, missing };
}

// The Card review's own link params; the provider step must read a clean state.
const CARD_REVIEW_PARAMS = [
  'agent_card_consent', 'pending_agent_grant', 'access_id', 'account_claim', 'account_provider_id', 'namespace',
  'operation', 'outer_operation', 'invocation_policy', 'invocation_change_id', 'request_bound', 'request_digest',
  'request_approval_ticket', 'request_card_revision', 'request_authority_revision', 'approval_application_id',
  'agent_claims', 'connector_app_id', 'account_id',
];

/** The provider consent link (Accounts tab) for permissions no connected
 *  account holds, with the agent hand-off: once approved there, "Continue -
 *  grant it to <agent>" returns to this agent's Card review. */
export function providerUpgradeHref(
  baseUrl: string,
  needed: NeededProviderClaims,
  clientId: string,
  accounts: readonly ConsentAccount[],
): string {
  try {
    const url = new URL(baseUrl);
    CARD_REVIEW_PARAMS.forEach((key) => url.searchParams.delete(key));
    url.searchParams.set('tab', 'delegated_to_kdcube');
    url.searchParams.set('provider_id', needed.providerId);
    if (needed.connectorAppId) url.searchParams.set('connector_app_id', needed.connectorAppId);
    url.searchParams.set('claims', needed.claims.join(','));
    // One connected account of the provider is the one to extend; several: the owner picks there.
    const providerAccounts = accounts.filter((account) => account.provider_id === needed.providerId);
    if (providerAccounts.length === 1) url.searchParams.set('account_id', providerAccounts[0].account_id);
    url.searchParams.set('agent_client_id', clientId);
    url.searchParams.set('agent_resource', needed.resource);
    return url.toString();
  } catch {
    return '';
  }
}
