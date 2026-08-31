import type {
  DelegatedAccessNamedServiceNamespaceOption,
  DelegatedAccessResourceOption,
} from '../../api/types';

export interface PendingServiceRequest {
  resource: string;
  namespace?: string;
  operation?: string;
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

export type PendingAccountScope = Record<string, Record<string, string[]>>;

export type PendingSelectionStatus =
  | 'Already granted'
  | 'Pending - not granted yet'
  | 'Required for this request'
  | null;

export interface PendingConnectedAccount {
  account_id?: string;
  provider_id?: string;
  claims?: string[];
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
 *  unmet prerequisite remains visibly required. */
export function pendingSelectionStatus(
  alreadyGranted: boolean,
  selected: boolean,
  required: boolean,
): PendingSelectionStatus {
  if (alreadyGranted) return 'Already granted';
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
  const resource = resources.find((item) => item.resource === request.resource);
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
