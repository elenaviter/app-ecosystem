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
