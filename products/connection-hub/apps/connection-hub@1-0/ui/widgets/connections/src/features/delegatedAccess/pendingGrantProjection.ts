import type {
  DelegatedAccessNamedServiceNamespaceOption,
  DelegatedAccessOperationOption,
  DelegatedAccessResourceOption,
  DelegatedInvocationPolicy,
} from '../../api/types';
import { isSecretResource, secretResourceLabel } from './secretResourceSelection.ts';

export interface PendingServiceRequest {
  resource: string;
  namespace?: string;
  operation?: string;
  outerOperation?: string;
}

export interface NamedServiceOperationProjection {
  operation: string;
  label: string;
  description: string;
  grants: string[];
}

export interface PendingServiceCapability {
  resource: DelegatedAccessResourceOption;
  namespace: DelegatedAccessNamedServiceNamespaceOption;
  operation: NamedServiceOperationProjection;
  requiredDoorGrants: string[];
  accountRequirements: Array<{ providerId: string; claims: string[] }>;
}

export interface PendingOuterCapability {
  resource: DelegatedAccessResourceOption;
  operation: DelegatedAccessOperationOption;
}

export type PendingAccountScope = Record<string, Record<string, string[]>>;

export type PendingSelectionStatus =
  | 'Already granted'
  | 'Used once, approve again'
  | 'Pending - not granted yet'
  | 'Required for this request'
  | null;

export interface PendingConnectedAccount {
  account_id?: string;
  provider_id?: string;
  claims?: string[];
}

/** The Card holds this exact operation under a Once policy whose one use is spent. */
export function onceUsedUp(
  policies: DelegatedInvocationPolicy[] | undefined,
  resource: string,
  operation: string,
): boolean {
  return (policies || []).some((policy) => (
    policy.authority.resource === resource
    && policy.authority.surface === 'outer'
    && policy.authority.operation === operation
    && !policy.authority.account
    && policy.mode === 'once'
    && policy.state === 'consumed'
  ));
}

export function commonOperationGrants(resource: DelegatedAccessResourceOption): string[] {
  const operations = resource.operations || [];
  if (!operations.length) return [];
  const [first, ...rest] = operations.map((operation) => new Set(operation.grants || []));
  return Array.from(first).filter((grant) => rest.every((grants) => grants.has(grant)));
}

function unique(items: string[]): string[] {
  return Array.from(new Set(items.map((item) => item.trim()).filter(Boolean)));
}

/** Describe one control in the focused consent projection. Persisted
 *  authority wins, every other checked value is a pending proposal, and an
 *  unmet prerequisite remains visibly required. A held operation whose Once
 *  is used up asks again (W929, operator 2026-10-23: "Used once, approve again"). */
export function pendingSelectionStatus(
  alreadyGranted: boolean,
  selected: boolean,
  required: boolean,
  onceUsed = false,
): PendingSelectionStatus {
  if (alreadyGranted) return onceUsed ? 'Used once, approve again' : 'Already granted';
  if (selected) return 'Pending - not granted yet';
  if (required) return 'Required for this request';
  return null;
}

export function accountClaimsForOperation(
  namespace: DelegatedAccessNamedServiceNamespaceOption,
  operation: string,
): Array<{ providerId: string; claims: string[] }> {
  return (namespace.connected_accounts || []).flatMap((requirement) => {
    const providerId = String(requirement.provider_id || '').trim();
    if (!providerId) return [];
    const byOperation = requirement.claims_by_operation || {};
    const claims = Object.keys(byOperation).length
      ? unique(Object.entries(byOperation)
        .filter(([key]) => key === operation || key.startsWith(`${operation}.`))
        .flatMap(([, values]) => values || []))
      : unique(requirement.claims || []);
    return claims.length ? [{ providerId, claims }] : [];
  });
}

export function doorGrantsForOperation(
  resource: DelegatedAccessResourceOption,
  namespace: DelegatedAccessNamedServiceNamespaceOption,
  operation: string,
  operationGrants: string[],
): string[] {
  const accountClaims = new Set(
    accountClaimsForOperation(namespace, operation).flatMap((item) => item.claims),
  );
  return unique([
    ...commonOperationGrants(resource),
    ...operationGrants,
  ]).filter((grant) => !accountClaims.has(grant));
}

function selectorMatches(selector: string, value: string): boolean {
  const pattern = selector.split('*').map((part) => part.replace(/[.+?^${}()|[\]\\]/g, '\\$&')).join('.*');
  return new RegExp(`^${pattern}$`).test(value);
}

/** The catalog row a pending request's resource belongs to.
 *
 *  A request names its exact resource. The catalog offers a concrete secret
 *  only through its `kdcube_secret` selector row
 *  (`urn:kdcube:management:secret:*:*:*:*:*`), so a consent for one secret
 *  found no row by string equality and the page stayed "Loading this
 *  capability" (W929). The selector row's operations apply to the concrete
 *  secret, which keeps its own resource and label, as the card editor builds
 *  it. Other resources still match exactly. */
export function pendingCatalogResource(
  resource: string,
  resources: DelegatedAccessResourceOption[],
): DelegatedAccessResourceOption | null {
  const exact = resources.find((item) => item.resource === resource);
  if (exact) return exact;
  if (!isSecretResource(resource)) return null;
  const row = resources.find((item) => item.selector_type === 'kdcube_secret'
    && selectorMatches(item.resource, resource));
  return row ? { ...row, resource, label: secretResourceLabel(resource) || row.label } : null;
}

/** Project one explicitly requested inner operation through the active
 *  delegated catalog. The operation selection and both claim scopes remain
 *  independent controls; no claim creates or implies an operation. */
export function resolvePendingServiceCapability(
  request: PendingServiceRequest | null,
  resources: DelegatedAccessResourceOption[],
  rowsForNamespace: (
    namespace: DelegatedAccessNamedServiceNamespaceOption,
  ) => NamedServiceOperationProjection[],
): PendingServiceCapability | null {
  if (!request?.namespace || !request.operation) return null;
  const resource = pendingCatalogResource(request.resource, resources);
  if (!resource) return null;
  const namespace = (resource.named_services || [])
    .find((item) => item.namespace === request.namespace);
  if (!namespace) return null;
  const operation = rowsForNamespace(namespace)
    .find((item) => item.operation === request.operation);
  if (!operation) return null;
  const accountRequirements = accountClaimsForOperation(namespace, operation.operation);
  return {
    resource,
    namespace,
    operation,
    requiredDoorGrants: doorGrantsForOperation(
      resource,
      namespace,
      operation.operation,
      operation.grants,
    ),
    accountRequirements,
  };
}

/** Resolve one requested MCP/REST operation on the protected resource. */
export function resolvePendingOuterCapability(
  request: PendingServiceRequest | null,
  resources: DelegatedAccessResourceOption[],
): PendingOuterCapability | null {
  if (!request?.outerOperation) return null;
  const resource = pendingCatalogResource(request.resource, resources);
  if (!resource) return null;
  const operation = (resource.operations || [])
    .find((item) => item.name === request.outerOperation);
  return operation ? { resource, operation } : null;
}


export function pendingOuterApprovalReady(
  capability: PendingOuterCapability | null,
  operationSelected: boolean,
  selectedGrants: string[],
): boolean {
  if (!capability || !operationSelected) return false;
  const selected = new Set(selectedGrants);
  return (capability.operation.grants || []).every((grant) => selected.has(grant));
}

/** One demanded service operation is actionable only when all three explicit
 *  selections are present: the operation, its KDCube door grants, and a
 *  qualifying connected account for every provider requirement. Account
 *  claims satisfy provider prerequisites; they never select the operation. */
export function pendingServiceApprovalReady(
  capability: PendingServiceCapability | null,
  operationSelected: boolean,
  selectedDoorGrants: string[],
  accountScope: PendingAccountScope,
): boolean {
  if (!capability || !operationSelected) return false;
  const selectedGrants = new Set(selectedDoorGrants);
  if (!capability.requiredDoorGrants.every((grant) => selectedGrants.has(grant))) {
    return false;
  }
  return capability.accountRequirements.every((requirement) => Object.values(
    accountScope[requirement.providerId] || {},
  ).some((claims) => requirement.claims.every((claim) => claims.includes(claim))));
}

/** Add one server-identified account/claim pair to the pending proposal. The
 *  active capability and account catalog must both confirm it. An absent
 *  account id is intentionally not guessed, even when only one account is
 *  currently connected. */
export function proposeExactAccountClaim(
  existingScope: PendingAccountScope,
  capability: PendingServiceCapability | null,
  accounts: PendingConnectedAccount[],
  accountId?: string,
  accountClaim?: string,
): PendingAccountScope {
  if (!accountId || !accountClaim || !capability) return existingScope;
  const account = accounts.find((item) => item.account_id === accountId);
  const providerId = String(account?.provider_id || '').trim();
  if (!providerId || !(account?.claims || []).includes(accountClaim)) return existingScope;
  const requested = capability.accountRequirements.some((requirement) => (
    requirement.providerId === providerId && requirement.claims.includes(accountClaim)
  ));
  if (!requested) return existingScope;
  return {
    ...existingScope,
    [providerId]: {
      ...(existingScope[providerId] || {}),
      [accountId]: Array.from(new Set([
        ...(existingScope[providerId]?.[accountId] || []),
        accountClaim,
      ])),
    },
  };
}
