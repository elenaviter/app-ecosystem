export interface ApplicationApiSurface {
  alias: string;
  method: string;
  route: string;
  userTypes: string[];
  roles: string[];
  operationId: string;
  operationRef: string;
  operationIdExplicit: boolean;
}

export interface ApplicationApiInventoryEntry {
  id: string;
  label: string;
  description: string;
  apis: ApplicationApiSurface[];
}

export const APPLICATION_OPERATION_POLICY_PROPERTY = 'kdcube.application_operations';
export const APPLICATION_OPERATION_POLICY_SCHEMA = 'kdcube.application_operations.v1';
export const APPLICATION_OPERATION_POLICY_MODE = 'selected';

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function strings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return Array.from(new Set(value.map(text).filter(Boolean)));
}

/** Whether this Card explicitly treats its application-operation row as a
 *  reviewed, default-closed selection. Older wildcard Cards can contain an
 *  empty row for unrelated reasons, so row presence alone is not authority. */
export function applicationOperationPolicyEnabled(properties: unknown): boolean {
  const policy = record(properties)[APPLICATION_OPERATION_POLICY_PROPERTY];
  const value = record(policy);
  return value.schema === APPLICATION_OPERATION_POLICY_SCHEMA
    && value.mode === APPLICATION_OPERATION_POLICY_MODE;
}

/** Preserve every other Card property while opting into selected application
 *  operations. The marker remains meaningful when the selection is empty. */
export function withApplicationOperationPolicy(
  properties: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    ...properties,
    [APPLICATION_OPERATION_POLICY_PROPERTY]: {
      schema: APPLICATION_OPERATION_POLICY_SCHEMA,
      mode: APPLICATION_OPERATION_POLICY_MODE,
    },
  };
}

/** Turn the existing platform bundle catalog into the rows shown beside the
 *  wildcard role grant. Every app stays in the result, including apps with no
 *  declared APIs. */
export function applicationApiInventory(payload: unknown): ApplicationApiInventoryEntry[] {
  const availableBundles = record(record(payload).available_bundles);
  return Object.entries(availableBundles)
    .map(([bundleId, value]) => {
      const bundle = record(value);
      const id = text(bundle.id) || bundleId;
      const apis = (Array.isArray(bundle.apis) ? bundle.apis : [])
        .map((value): ApplicationApiSurface | null => {
          const api = record(value);
          const alias = text(api.alias);
          if (!alias) return null;
          return {
            alias,
            method: text(api.http_method).toUpperCase(),
            route: text(api.route),
            userTypes: strings(api.user_types),
            roles: strings(api.roles),
            operationId: text(api.operation_id),
            operationRef: text(api.operation_ref),
            operationIdExplicit: api.operation_id_explicit === true,
          };
        })
        .filter((api): api is ApplicationApiSurface => api !== null)
        .sort((left, right) => (
          left.alias.localeCompare(right.alias, undefined, { sensitivity: 'base' })
          || left.method.localeCompare(right.method)
          || left.route.localeCompare(right.route)
        ));
      return {
        id,
        label: text(bundle.name) || id,
        description: text(bundle.description),
        apis,
      };
    })
    .sort((left, right) => (
      left.label.localeCompare(right.label, undefined, { sensitivity: 'base' })
      || left.id.localeCompare(right.id)
    ));
}
