/** W34 C: the MCP bridge tool an agent calls a named-service operation with.
 *  Mirrors Connection Hub's `_NAMED_SERVICE_OUTER_OPERATION`; a provider that
 *  exposes only the generic bridge is called through `named_services_call`. */
const OUTER_OPERATION: Record<string, string> = {
  'provider.about': 'named_services_about',
  'provider.capabilities': 'named_services_capabilities',
  'object.list': 'named_services_list',
  'object.search': 'named_services_search',
  'object.get': 'named_services_get',
  'object.schema': 'named_services_schema',
  'object.upsert': 'named_services_upsert',
  'object.host_file': 'named_services_host_file',
  'object.action': 'named_services_action',
  'object.delete': 'named_services_delete',
};

export function bridgeForOperation(operation: string, offered: readonly string[]): string {
  const family = operation.startsWith('object.action.') ? 'object.action' : operation;
  const bridge = OUTER_OPERATION[family];
  if (bridge && offered.includes(bridge)) return bridge;
  return offered.includes('named_services_call') ? 'named_services_call' : '';
}

/** The bridge to tick with a newly ticked operation, or '' when the Card draft
 *  already has it, the resource does not offer it, or the Control excludes it. */
export function bridgeToAdd(
  operation: string,
  { offered, selected, controlAllowed }: { offered: readonly string[]; selected: readonly string[]; controlAllowed?: readonly string[] },
): string {
  const bridge = bridgeForOperation(operation, offered);
  if (!bridge || selected.includes(bridge)) return '';
  if (controlAllowed && !controlAllowed.includes(bridge)) return '';
  return bridge;
}
