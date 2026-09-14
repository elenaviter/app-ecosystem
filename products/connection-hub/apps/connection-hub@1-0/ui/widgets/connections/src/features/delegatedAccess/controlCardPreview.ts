import type {
  DelegatedAccessNamedServiceOperations,
  DelegatedControlCardAuthority,
} from '../../api/types';

export type ControlCompositionMode = 'and' | 'or';
export type CompositionRowState = 'normal' | 'added' | 'removed' | 'absent';

function values(items: string[] | undefined): string[] {
  return Array.from(new Set((items || []).filter(Boolean))).sort();
}

function intersectValues(left: string[] | undefined, right: string[] | undefined): string[] {
  const a = values(left);
  const b = values(right);
  if (a.includes('*')) return b;
  if (b.includes('*')) return a;
  const held = new Set(b);
  return a.filter((item) => held.has(item));
}

function unionValues(left: string[] | undefined, right: string[] | undefined): string[] {
  const merged = values([...(left || []), ...(right || [])]);
  return merged.includes('*') ? ['*'] : merged;
}

function namedSelections(
  authority: DelegatedControlCardAuthority,
): DelegatedAccessNamedServiceOperations {
  if (authority.effective_named_service_operations) {
    return authority.effective_named_service_operations;
  }
  const stored = authority.named_service_operations;
  return stored && stored !== '*' ? stored : {};
}

/** Every resource that carries any qualified authority.
 *
 * A resource may intentionally have no direct service claims while still
 * carrying claimless tools. Treating resource_grants as the resource index
 * makes that valid authority disappear from the Effective Card view. */
export function authorityResourceKeys(
  authority: DelegatedControlCardAuthority,
): string[] {
  return Array.from(new Set([
    ...Object.keys(authority.resource_grants || {}),
    ...Object.keys(authority.resource_operations || {}),
    ...Object.keys(namedSelections(authority)),
  ])).sort();
}

export function authorityOuterOperationCount(
  authority: DelegatedControlCardAuthority,
): number {
  if (authority.resource_operations) {
    return Object.values(authority.resource_operations)
      .reduce((total, operations) => total + values(operations).length, 0);
  }
  return values(authority.operations).length;
}

export function authorityNamedOperationCount(
  authority: DelegatedControlCardAuthority,
): number {
  return Object.values(namedSelections(authority)).reduce(
    (total, namespaces) => total + Object.values(namespaces || {})
      .reduce((namespaceTotal, operations) => namespaceTotal + values(operations).length, 0),
    0,
  );
}

export function authorityAccountCount(
  authority: DelegatedControlCardAuthority,
): number {
  return Object.values(authority.account_scope || {})
    .reduce((total, accounts) => total + Object.keys(accounts || {}).length, 0);
}

export function authorityHasAccess(
  authority: DelegatedControlCardAuthority,
): boolean {
  const directClaims = Object.values(authority.resource_grants || {})
    .some((claims) => values(claims).length > 0);
  return directClaims
    || authorityOuterOperationCount(authority) > 0
    || authorityNamedOperationCount(authority) > 0
    || authorityAccountCount(authority) > 0;
}

/** Whether one qualified outer operation survives a Control Card ceiling. */
export function authorityAllowsOuterOperation(
  authority: DelegatedControlCardAuthority,
  resource: string,
  operation: string,
): boolean {
  const selected = values(authority.resource_operations?.[resource]);
  return selected.includes('*') || selected.includes(operation);
}

/** How one Caller/Control selection is represented in the Effective Card. */
export function compositionRowState(
  callerSelected: boolean,
  controlSelected: boolean,
  mode: ControlCompositionMode,
): CompositionRowState {
  if (mode === 'and') {
    if (!callerSelected) return 'absent';
    return controlSelected ? 'normal' : 'removed';
  }
  if (controlSelected && !callerSelected) return 'added';
  return callerSelected ? 'normal' : 'absent';
}

/** Selected Caller Card tools that an AND-linked Control Card keeps inactive. */
export function outerOperationsExcludedByControl(
  caller: DelegatedControlCardAuthority,
  control: DelegatedControlCardAuthority,
): Array<{ resource: string; operation: string }> {
  return Object.entries(caller.resource_operations || {}).flatMap(([resource, operations]) => (
    values(operations)
      .filter((operation) => !authorityAllowsOuterOperation(control, resource, operation))
      .map((operation) => ({ resource, operation }))
  ));
}

function composeNamedServices(
  caller: DelegatedControlCardAuthority,
  control: DelegatedControlCardAuthority,
  resources: string[],
  mode: ControlCompositionMode,
): DelegatedAccessNamedServiceOperations {
  const callerSelection = namedSelections(caller);
  const controlSelection = namedSelections(control);
  const composed: DelegatedAccessNamedServiceOperations = {};
  resources.forEach((resource) => {
    const callerNamespaces = callerSelection[resource] || {};
    const controlNamespaces = controlSelection[resource] || {};
    const namespaces = mode === 'or'
      ? new Set([...Object.keys(callerNamespaces), ...Object.keys(controlNamespaces)])
      : new Set(Object.keys(callerNamespaces).filter((namespace) => namespace in controlNamespaces));
    namespaces.forEach((namespace) => {
      const operations = mode === 'or'
        ? unionValues(callerNamespaces[namespace], controlNamespaces[namespace])
        : intersectValues(callerNamespaces[namespace], controlNamespaces[namespace]);
      if (operations.length) {
        composed[resource] ||= {};
        composed[resource][namespace] = operations;
      }
    });
  });
  return composed;
}

function composeAccountScope(
  callerScope: Record<string, Record<string, string[]>>,
  controlScope: Record<string, Record<string, string[]>>,
  mode: ControlCompositionMode,
): Record<string, Record<string, string[]>> {
  if (mode === 'or') {
    const result = structuredClone(callerScope);
    Object.entries(controlScope).forEach(([provider, accounts]) => {
      const target = result[provider] ||= {};
      Object.entries(accounts || {}).forEach(([accountId, claims]) => {
        target[accountId] = unionValues(target[accountId], claims);
      });
    });
    return result;
  }

  const result: Record<string, Record<string, string[]>> = {};
  Object.entries(callerScope).forEach(([provider, callerAccounts]) => {
    const controlAccounts = controlScope[provider] || controlScope['*'];
    if (!controlAccounts) return;
    Object.entries(callerAccounts || {}).forEach(([accountId, callerClaims]) => {
      const candidates = accountId === '*' && !controlAccounts['*']
        ? Object.entries(controlAccounts)
        : [[accountId, controlAccounts[accountId] || controlAccounts['*']]] as Array<[
            string,
            string[] | undefined,
          ]>;
      candidates.forEach(([effectiveAccount, controlClaims]) => {
        if (!controlClaims) return;
        const claims = intersectValues(callerClaims, controlClaims);
        if (claims.length) {
          result[provider] ||= {};
          result[provider][effectiveAccount] = claims;
        }
      });
    });
  });
  return result;
}

/** Compose an unsaved caller draft with the linked Control Card authority.
 *
 * The list response also carries the effective authority for the saved caller,
 * but that value is lossy: if the caller draft adds something the Control Card
 * already allows, the saved intersection cannot prove it. Preview therefore
 * requires the raw credentialless Control Card authority.
 */
export function composeControlCardAuthority(
  caller: DelegatedControlCardAuthority,
  control: DelegatedControlCardAuthority,
  mode: ControlCompositionMode,
): DelegatedControlCardAuthority {
  const callerGrants = caller.resource_grants || {};
  const controlGrants = control.resource_grants || {};
  const resources = mode === 'or'
    ? Array.from(new Set([...Object.keys(callerGrants), ...Object.keys(controlGrants)])).sort()
    : Object.keys(callerGrants).filter((resource) => resource in controlGrants).sort();
  const resourceGrants = Object.fromEntries(resources.map((resource) => [
    resource,
    mode === 'or'
      ? unionValues(callerGrants[resource], controlGrants[resource])
      : intersectValues(callerGrants[resource], controlGrants[resource]),
  ]));
  const callerOperations = caller.resource_operations || {};
  const controlOperations = control.resource_operations || {};
  const resourceOperations = Object.fromEntries(resources.map((resource) => [
    resource,
    mode === 'or'
      ? unionValues(callerOperations[resource], controlOperations[resource])
      : intersectValues(callerOperations[resource], controlOperations[resource]),
  ]));
  const named = composeNamedServices(caller, control, resources, mode);

  return {
    operations: [],
    resource_grants: resourceGrants,
    resource_operations: resourceOperations,
    named_service_operations: named,
    effective_named_service_operations: named,
    account_scope: composeAccountScope(
      caller.account_scope || {},
      control.account_scope || {},
      mode,
    ),
  };
}
