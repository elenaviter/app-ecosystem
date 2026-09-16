export const APPLICATION_API_RESOURCE = '*';
export const APPLICATION_OPERATION_POLICY_PROPERTY = 'kdcube.application_operations';
export const APPLICATION_OPERATION_POLICY_SCHEMA_V1 = 'kdcube.application_operations.v1';
export const APPLICATION_OPERATION_POLICY_SCHEMA_V2 = 'kdcube.application_operations.v2';
export const APPLICATION_OPERATION_POLICY_MODE = 'selected';
export const PLATFORM_ROLE_PREFIX = 'kdcube:role:';

export const PLATFORM_ROLES = [
  'kdcube:role:registered',
  'kdcube:role:paid',
  'kdcube:role:privileged',
  'kdcube:role:admin',
  'kdcube:role:super-admin',
] as const;

export type PlatformRole = typeof PLATFORM_ROLES[number];

export interface ApplicationOperationRolePolicy {
  defaultRole: string;
  operationRoles: Record<string, string>;
}

export interface ComposedApplicationOperationRolePolicy {
  operations: string[];
  policy: ApplicationOperationRolePolicy;
}

const ROLE_RANK: Record<PlatformRole, number> = {
  'kdcube:role:registered': 0,
  'kdcube:role:paid': 1,
  'kdcube:role:privileged': 2,
  'kdcube:role:admin': 2,
  'kdcube:role:super-admin': 3,
};

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function clean(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function unique(values: string[]): string[] {
  return Array.from(new Set(values.map((value) => clean(value)).filter(Boolean)));
}

export function isPlatformRole(value: unknown): value is PlatformRole {
  return PLATFORM_ROLES.includes(clean(value) as PlatformRole);
}

export function platformRoleRank(value: unknown): number | null {
  const role = clean(value);
  return isPlatformRole(role) ? ROLE_RANK[role] : null;
}

export function platformRoleLabel(value: string): string {
  const role = clean(value).replace(/^kdcube:role:/, '');
  if (!role) return 'Not selected';
  return role.split('-').map((part) => (
    part ? `${part[0].toUpperCase()}${part.slice(1)}` : ''
  )).join(' ');
}

export function availableApplicationRoles(values: string[] | undefined): string[] {
  return unique(values || [])
    .filter(isPlatformRole)
    .sort((left, right) => (
      (platformRoleRank(left) ?? Number.MAX_SAFE_INTEGER)
      - (platformRoleRank(right) ?? Number.MAX_SAFE_INTEGER)
      || left.localeCompare(right)
    ));
}

export function platformRoleAllowedBy(
  role: string,
  ceilingRoles: string[] | undefined,
): boolean {
  const rank = platformRoleRank(role);
  if (rank === null) return false;
  return availableApplicationRoles(ceilingRoles).some((ceiling) => {
    const ceilingRank = platformRoleRank(ceiling);
    return ceilingRank !== null && rank <= ceilingRank;
  });
}

/** A resource row declares role ceilings. The choices themselves come from
 * the grantor's delegable inventory, bounded by those ceilings. */
export function delegableApplicationRoles(
  delegableRoles: string[] | undefined,
  ceilingRoles: string[] | undefined,
): string[] {
  return availableApplicationRoles(delegableRoles)
    .filter((role) => platformRoleAllowedBy(role, ceilingRoles));
}

export function unavailableApplicationPolicyRoles(
  policy: ApplicationOperationRolePolicy,
  selectedOperations: string[],
  availableRoles: string[],
): string[] {
  const selected = new Set(unique(selectedOperations));
  const available = new Set(availableRoles);
  return unique([
    policy.defaultRole,
    ...Object.entries(policy.operationRoles)
      .filter(([operation]) => selected.has(operation))
      .map(([, role]) => role),
  ]).filter((role) => !available.has(role));
}

export function unselectedApplicationOperationOverrides(
  policy: ApplicationOperationRolePolicy,
  selectedOperations: string[],
): string[] {
  const selected = new Set(unique(selectedOperations));
  return Object.keys(policy.operationRoles)
    .filter((operation) => !selected.has(operation))
    .sort();
}

export function strongestPlatformRole(values: string[] | undefined): string {
  return availableApplicationRoles(values).reduce((strongest, role) => {
    if (!strongest) return role;
    const strongestRank = platformRoleRank(strongest) ?? -1;
    const roleRank = platformRoleRank(role) ?? -1;
    return roleRank > strongestRank || (roleRank === strongestRank && role > strongest)
      ? role
      : strongest;
  }, '');
}

export function projectedRoleAllows(projectedRole: string, requiredRole: string): boolean {
  const projectedRank = platformRoleRank(projectedRole);
  const requiredRank = platformRoleRank(requiredRole);
  if (projectedRank === null || requiredRank === null) return projectedRole === requiredRole;
  return projectedRank >= requiredRank;
}

export function applicationOperationRoleFor(
  policy: ApplicationOperationRolePolicy,
  operationRef: string,
): string {
  return policy.operationRoles[operationRef] || policy.defaultRole;
}

export function applicationOperationRoleIsElevated(
  policy: ApplicationOperationRolePolicy,
  operationRef: string,
): boolean {
  const effectiveRank = platformRoleRank(applicationOperationRoleFor(policy, operationRef));
  const defaultRank = platformRoleRank(policy.defaultRole);
  return effectiveRank !== null && defaultRank !== null && effectiveRank > defaultRank;
}

export function applicationOperationPolicyEnabled(properties: unknown): boolean {
  const policy = record(record(properties)[APPLICATION_OPERATION_POLICY_PROPERTY]);
  return [
    APPLICATION_OPERATION_POLICY_SCHEMA_V1,
    APPLICATION_OPERATION_POLICY_SCHEMA_V2,
  ].includes(clean(policy.schema)) && clean(policy.mode) === APPLICATION_OPERATION_POLICY_MODE;
}

export function applicationOperationPolicyDeclared(properties: unknown): boolean {
  return APPLICATION_OPERATION_POLICY_PROPERTY in record(properties);
}

/** Parse a stored policy. V1 Cards infer their single default from the
 * historical application resource row; every write is normalized to V2. */
export function applicationOperationRolePolicy(
  properties: unknown,
  resourceGrants: Record<string, string[]> | undefined,
): ApplicationOperationRolePolicy | null {
  const policy = record(record(properties)[APPLICATION_OPERATION_POLICY_PROPERTY]);
  if (clean(policy.mode) !== APPLICATION_OPERATION_POLICY_MODE) return null;

  const schema = clean(policy.schema);
  if (schema === APPLICATION_OPERATION_POLICY_SCHEMA_V1) {
    const defaultRole = strongestPlatformRole(resourceGrants?.[APPLICATION_API_RESOURCE]);
    return defaultRole ? { defaultRole, operationRoles: {} } : null;
  }
  if (schema !== APPLICATION_OPERATION_POLICY_SCHEMA_V2) return null;

  const defaultRole = clean(policy.default_role);
  const operationRoles = Object.fromEntries(
    Object.entries(record(policy.operation_roles)).flatMap(([operationRef, value]) => {
      const operation = clean(operationRef);
      const role = clean(value);
      return operation && role && role !== defaultRole
        ? [[operation, role]]
        : [];
    }),
  );
  return { defaultRole, operationRoles };
}

/** Seed an editor from V2, migrate V1 in memory, or expose the strongest
 * historical row role while leaving an undeclared legacy Card unselected. */
export function seedApplicationOperationRolePolicy(
  properties: unknown,
  resourceGrants: Record<string, string[]> | undefined,
): ApplicationOperationRolePolicy {
  return applicationOperationRolePolicy(properties, resourceGrants) || {
    defaultRole: strongestPlatformRole(resourceGrants?.[APPLICATION_API_RESOURCE]),
    operationRoles: {},
  };
}

export function withApplicationOperationPolicy(
  properties: Record<string, unknown> = {},
  policy: ApplicationOperationRolePolicy,
  selectedOperations: string[],
): Record<string, unknown> {
  const selected = new Set(unique(selectedOperations));
  const operationRoles = Object.fromEntries(
    Object.entries(policy.operationRoles)
      .filter(([operationRef, role]) => (
        selected.has(operationRef)
        && isPlatformRole(role)
        && role !== policy.defaultRole
      ))
      .sort(([left], [right]) => left.localeCompare(right)),
  );
  return {
    ...properties,
    [APPLICATION_OPERATION_POLICY_PROPERTY]: {
      schema: APPLICATION_OPERATION_POLICY_SCHEMA_V2,
      mode: APPLICATION_OPERATION_POLICY_MODE,
      default_role: policy.defaultRole,
      operation_roles: operationRoles,
    },
  };
}

export function withoutApplicationOperationPolicy(
  properties: Record<string, unknown> = {},
): Record<string, unknown> {
  const next = { ...properties };
  delete next[APPLICATION_OPERATION_POLICY_PROPERTY];
  return next;
}

export function applicationOperationPropertiesForSelection({
  properties = {},
  policy,
  selectedOperations,
  resourceSelected,
  resourcePreviouslySelected,
}: {
  properties?: Record<string, unknown>;
  policy: ApplicationOperationRolePolicy;
  selectedOperations: string[];
  resourceSelected: boolean;
  resourcePreviouslySelected: boolean;
}): Record<string, unknown> {
  if (resourceSelected) {
    return withApplicationOperationPolicy(properties, policy, selectedOperations);
  }
  return resourcePreviouslySelected
    ? withoutApplicationOperationPolicy(properties)
    : { ...properties };
}

export function changeApplicationDefaultRole(
  policy: ApplicationOperationRolePolicy,
  defaultRole: string,
): ApplicationOperationRolePolicy {
  return {
    defaultRole,
    operationRoles: Object.fromEntries(
      Object.entries(policy.operationRoles).filter(([, role]) => role !== defaultRole),
    ),
  };
}

export function changeApplicationOperationRole(
  policy: ApplicationOperationRolePolicy,
  operationRef: string,
  role: string,
): ApplicationOperationRolePolicy {
  const operationRoles = { ...policy.operationRoles };
  if (!role || role === policy.defaultRole) delete operationRoles[operationRef];
  else operationRoles[operationRef] = role;
  return { ...policy, operationRoles };
}

export function removeApplicationOperationRole(
  policy: ApplicationOperationRolePolicy,
  operationRef: string,
): ApplicationOperationRolePolicy {
  return changeApplicationOperationRole(policy, operationRef, '');
}

function sameTierRole(left: string, right: string): string {
  if (left === right) return left;
  if (new Set([left, right]).size === 2
    && [left, right].includes('kdcube:role:admin')
    && [left, right].includes('kdcube:role:privileged')) {
    return 'kdcube:role:privileged';
  }
  return [left, right].sort()[0];
}

function rankedRole(left: string, right: string, stronger: boolean): string {
  const leftRank = platformRoleRank(left);
  const rightRank = platformRoleRank(right);
  if (leftRank === null || rightRank === null) return '';
  if (leftRank === rightRank) return sameTierRole(left, right);
  if (stronger) return leftRank > rightRank ? left : right;
  return leftRank < rightRank ? left : right;
}

/** Mirror the runtime's operation-to-role composition so the unsaved preview
 * names the same effective role that the next invocation will receive. */
export function composeApplicationOperationRolePolicies(
  caller: ApplicationOperationRolePolicy,
  control: ApplicationOperationRolePolicy,
  callerOperations: string[],
  controlOperations: string[],
  mode: 'and' | 'or',
): ComposedApplicationOperationRolePolicy | null {
  if (!caller.defaultRole || !control.defaultRole) return null;
  const callerSelected = new Set(unique(callerOperations));
  const controlSelected = new Set(unique(controlOperations));
  const operations = (mode === 'or'
    ? unique([...callerSelected, ...controlSelected])
    : [...callerSelected].filter((operation) => controlSelected.has(operation)))
    .sort();
  const defaultRole = rankedRole(caller.defaultRole, control.defaultRole, mode === 'or');
  if (!defaultRole) return null;

  const operationRoles: Record<string, string> = {};
  operations.forEach((operation) => {
    const inCaller = callerSelected.has(operation);
    const inControl = controlSelected.has(operation);
    const role = inCaller && inControl
      ? rankedRole(
          applicationOperationRoleFor(caller, operation),
          applicationOperationRoleFor(control, operation),
          mode === 'or',
        )
      : inCaller
        ? applicationOperationRoleFor(caller, operation)
        : applicationOperationRoleFor(control, operation);
    if (role && role !== defaultRole) operationRoles[operation] = role;
  });
  return { operations, policy: { defaultRole, operationRoles } };
}
