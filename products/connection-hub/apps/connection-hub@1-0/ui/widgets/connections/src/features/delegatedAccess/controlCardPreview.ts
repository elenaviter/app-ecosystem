import type {
  DelegatedAccessNamedServiceOperations,
  DelegatedControlCardAuthority,
} from '../../api/types';

export type ControlCompositionMode = 'and' | 'or';

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
