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

export {
  APPLICATION_OPERATION_POLICY_MODE,
  APPLICATION_OPERATION_POLICY_PROPERTY,
  APPLICATION_OPERATION_POLICY_SCHEMA_V1,
  APPLICATION_OPERATION_POLICY_SCHEMA_V2,
  applicationOperationPolicyEnabled,
  withApplicationOperationPolicy,
} from './applicationOperationRoles.ts';

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

/** Keep matching applications whole and otherwise retain only matching APIs.
 *  This makes a query such as "telegram" useful across a large catalog while
 *  preserving app-level bulk selection for the rows currently on screen. */
export function filterApplicationApiInventory(
  applications: ApplicationApiInventoryEntry[],
  query: string,
): ApplicationApiInventoryEntry[] {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return applications;

  return applications.flatMap((app) => {
    const appText = [app.id, app.label, app.description]
      .join(' ')
      .toLocaleLowerCase();
    if (appText.includes(needle)) return [app];

    const apis = app.apis.filter((api) => (
      [
        api.alias,
        api.operationId,
        api.method,
        api.route,
        ...api.userTypes,
        ...api.roles,
      ]
        .join(' ')
        .toLocaleLowerCase()
        .includes(needle)
    ));
    return apis.length ? [{ ...app, apis }] : [];
  });
}
