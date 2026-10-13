/**
 * What a person's Control Card is offered (W360).
 *
 * A service marks an operation `person_card: false` in its catalog when it
 * decides that operation for a person by role alone, never by the person's
 * Card. For Problem Board those are the coordinator levers and the people and
 * project Control Card operations: ticking them on a person's Control Card
 * would change nothing, so a project person's or invitation's Control Card
 * does not offer them, in the editor or in the drift review (changed or newly
 * advertised).
 * Every other operation stays offered; an operation without the mark is
 * offered as before. Other Cards see the whole catalog.
 */
import type {
  DelegatedCatalogDrift,
  DelegatedAccessNamedServiceNamespaceOption,
  DelegatedAccessNamedServiceToolOption,
  DelegatedAccessResourceOption,
} from '../../api/types';
import type { ResourceDriftState } from './resourceEditing';

export function offeredOnPersonCard(entry: { person_card?: boolean } | undefined): boolean {
  return entry?.person_card !== false;
}

function namespaceForPersonCard(
  namespace: DelegatedAccessNamedServiceNamespaceOption,
): DelegatedAccessNamedServiceNamespaceOption {
  if (!namespace.tools) return namespace;
  const tools: Record<string, DelegatedAccessNamedServiceToolOption> = {};
  Object.entries(namespace.tools).forEach(([name, tool]) => {
    if (!offeredOnPersonCard(tool)) return;
    if (!tool.operations) {
      tools[name] = tool;
      return;
    }
    const operations = Object.fromEntries(
      Object.entries(tool.operations).filter(([, operation]) => offeredOnPersonCard(operation)),
    );
    // A tool whose every operation is decided by role offers nothing here.
    if (Object.keys(operations).length) tools[name] = { ...tool, operations };
  });
  return { ...namespace, tools };
}

/** W667 (operator, 2026-10-09: "eveyrthing that is not controlled and cannot be controlled must be hidden!";
 *  managed ones: "not viisble."): when the application declares `person_card_operations`, a person's Card
 *  shows only those it decides; every other operation, managed ones included, is hidden. */
function declaredPersonOperations(option: DelegatedAccessResourceOption): Set<string> | null {
  return Array.isArray(option.person_card_operations) && option.person_card_operations.length
    ? new Set(option.person_card_operations) : null;
}

/** An outer operation a person's Card does not decide: "managed" (shown ticked as the Card holds it,
 *  disabled, "Set by the application"; never changed by the editor or its Save). W739 (operator,
 *  2026-10-11: "we still show this (simply disabled)", "ie.. we must", "because for other permisisons that
 *  also card does not decide we still show them"): it is SHOWN, never hidden. ``person_undecided`` marks it
 *  so Save keeps the grants it needs exactly as the Card holds them. */
function managedUndecided<T extends { name: string }>(
  operation: T, members: Set<string> | null, agents: Set<string> | null = null,
): T & { managed: true; person_undecided: true; in_effect?: boolean; agent_only?: true } {
  // W748 (operator, live 2026-10-11 17:46Z: "read is not ticked btw"): the tick shows whether the operation is
  // IN EFFECT for the person, so a membership read every member holds is ticked whatever the Card holds.
  return { ...operation, managed: true, person_undecided: true,
    ...(members ? { in_effect: members.has(operation.name) } : {}),
    ...(agents?.has(operation.name) ? { agent_only: true as const } : {}) };
}

/** W775 (operator, 2026-10-13: "fold the agent-only tools under \"Agent-only tools (not for people)\" without
 *  the amber INACTIVE warning. << b) - yes"): the operations only an agent's own channel calls, as the
 *  application declares them (`person_agent_operations`); null when it declares none. Never a list kept here. */
function agentOperations(option: DelegatedAccessResourceOption | undefined): Set<string> | null {
  return Array.isArray(option?.person_agent_operations) && option.person_agent_operations.length
    ? new Set(option.person_agent_operations) : null;
}

/** W775: whether an outer operation of this catalog row is agent-only on a person's Card. */
export function agentOnlyOnPersonCard(option: DelegatedAccessResourceOption | undefined, operation: string): boolean {
  return agentOperations(option)?.has(operation) === true;
}

/** W775: a person's Card rows split for display: the agent-only ones fold away, the rest render as before. */
export function splitAgentOnly<T extends { agent_only?: boolean }>(operations: T[]): { regular: T[]; agentOnly: T[] } {
  return {
    regular: operations.filter((operation) => operation.agent_only !== true),
    agentOnly: operations.filter((operation) => operation.agent_only === true),
  };
}

/** W748: the operations every member holds by membership, as the application declares them; null when it
 *  declares none (an undecided row's tick then shows what the Card holds, as before). */
function memberOperations(option: DelegatedAccessResourceOption): Set<string> | null {
  return Array.isArray(option.person_member_operations) && option.person_member_operations.length
    ? new Set(option.person_member_operations) : null;
}

/** A named-service alias's outer operation (`object.action.<operation>`), for the membership tick. */
function aliasOperation(name: string): string {
  return name.startsWith('object.action.') ? name.slice('object.action.'.length) : name;
}

/** One catalog row as a person's Card is offered it. An outer operation the service decides for a person
 *  (`person_card: false`, W560) and, with a non-empty `person_card_operations` declaration (W667), every
 *  operation outside the declared set, is managed: shown disabled as "Set by the application" (W739),
 *  never hidden. Showing it never edits authority. */
export function resourceForPersonCard(option: DelegatedAccessResourceOption): DelegatedAccessResourceOption {
  const declared = declaredPersonOperations(option);
  const members = memberOperations(option);
  const agents = agentOperations(option);
  return {
    ...option,
    ...(option.operations ? { operations: option.operations.map((operation) => {
      const decided = offeredOnPersonCard(operation) && (!declared || declared.has(operation.name));
      return decided ? operation : managedUndecided(operation, members, agents);
    }) } : {}),
    ...(option.named_services ? { named_services: option.named_services.map(namespaceForPersonCard) } : {}),
  };
}

/** W667/W739: the held grants a held operation the person's Card does not decide needs; Save keeps them
 *  exactly as the Card holds them, so showing it disabled never removes authority. */
export function hiddenHeldGrants(
  option: DelegatedAccessResourceOption | undefined,
  heldOperations: readonly string[],
  heldGrants: readonly string[],
): string[] {
  const held = new Set(heldOperations);
  const needed = new Set((option?.operations || [])
    .filter((operation) => (operation.hidden === true || operation.person_undecided === true) && held.has(operation.name))
    .flatMap((operation) => operation.grants || []));
  return heldGrants.filter((grant) => needed.has(grant));
}

/** The operations an editor shows: everything but the W667 hidden ones. */
export function visibleOperations<T extends { hidden?: boolean }>(operations: T[] | undefined): T[] {
  return (operations || []).filter((operation) => operation.hidden !== true);
}

/** W739: a namespace as a person's Card SHOWS it: every tool and operation `namespaceForPersonCard` leaves
 *  out is kept and marked ``person_undecided`` (disabled, "Set by the application"). Display only: Save and
 *  the offered set keep reading the narrowed namespace, so what Save sends is unchanged. */
function namespaceShownOnPersonCard(
  namespace: DelegatedAccessNamedServiceNamespaceOption,
  members: Set<string> | null = null,
): DelegatedAccessNamedServiceNamespaceOption {
  if (!namespace.tools) return namespace;
  const effect = (name: string) => (members ? { in_effect: members.has(aliasOperation(name)) } : {});
  const tools = Object.fromEntries(Object.entries(namespace.tools).map(([name, tool]) => {
    const toolDecided = offeredOnPersonCard(tool);
    if (!tool.operations) {
      return [name, toolDecided ? tool : { ...tool, person_undecided: true, ...effect(tool.operation || name) }];
    }
    const operations = Object.fromEntries(Object.entries(tool.operations).map(([operation, entry]) => [
      operation, toolDecided && offeredOnPersonCard(entry) ? entry
        : { ...entry, person_undecided: true, ...effect(operation) },
    ]));
    return [name, { ...tool, operations }];
  }));
  return { ...namespace, tools };
}

/** W739: the named services of one catalog row as a person's Card shows them (C, or a declared My Card),
 *  the ones it does not decide included and marked; display only. */
export function namedServicesShownOnPersonCard(catalogRow: DelegatedAccessResourceOption): DelegatedAccessResourceOption {
  return catalogRow.named_services
    ? { ...catalogRow, named_services: catalogRow.named_services.map(
      (namespace) => namespaceShownOnPersonCard(namespace, memberOperations(catalogRow))) }
    : catalogRow;
}

/** A My Card narrows its named services only with a declaration (W667); without one they are shown as before. */
export function namedServicesShownOnPersonMyCard(catalogRow: DelegatedAccessResourceOption): DelegatedAccessResourceOption {
  return declaredPersonOperations(catalogRow) ? namedServicesShownOnPersonCard(catalogRow) : catalogRow;
}

export function resourcesForPersonCard(options: DelegatedAccessResourceOption[]): DelegatedAccessResourceOption[] {
  return options.map(resourceForPersonCard);
}

/** A person's own My Card shows what it does not decide as managed and disabled (W739). With a declaration
 *  (W667) it is treated exactly as the person's Control Card. Without one (W560) only the outer
 *  `person_card: false` operations are managed; every other operation and the named services are as before. */
export function resourcesForPersonMyCard(options: DelegatedAccessResourceOption[]): DelegatedAccessResourceOption[] {
  return options.map((option) => {
    if (declaredPersonOperations(option)) return resourceForPersonCard(option);
    if (!option.operations?.some((operation) => !offeredOnPersonCard(operation))) return option;
    const members = memberOperations(option);
    const agents = agentOperations(option);
    return {
      ...option,
      operations: option.operations.map((operation) => (
        offeredOnPersonCard(operation) ? operation : managedUndecided(operation, members, agents))),
    };
  });
}

/** The outer operations of a catalog row a person's Card is not offered, for the drift review. */
export function notOfferedOnPersonCard(catalogRow: DelegatedAccessResourceOption | undefined): string[] {
  const declared = catalogRow ? declaredPersonOperations(catalogRow) : null;
  return (catalogRow?.operations || [])
    .filter((operation) => !offeredOnPersonCard(operation) || (declared !== null && !declared.has(operation.name)))
    .map((operation) => operation.name);
}

/** The named-service operations of a catalog row a person's Card is not offered, as `namespace operation`. */
export function notOfferedNamedOnPersonCard(catalogRow: DelegatedAccessResourceOption | undefined): Set<string> {
  const out = new Set<string>();
  (catalogRow?.named_services || []).forEach((namespace) => {
    Object.values(namespace.tools || {}).forEach((tool) => {
      Object.entries(tool.operations || {}).forEach(([operation, entry]) => {
        if (!offeredOnPersonCard(tool) || !offeredOnPersonCard(entry)) out.add(`${namespace.namespace} ${operation}`);
      });
    });
  });
  return out;
}

/**
 * A resource's descriptor review as a person's Control Card shows it.
 *
 * Operator, 2026-09-27: after the board's catalog changed, a person's Card
 * listed `project.people.invite` and `set_role` as "changed, suspended until
 * you accept", although both are marked role-only. Such an operation gives the
 * person nothing whether accepted or not, so the review lists it neither as
 * changed nor as newly advertised. Removed operations stay listed: the
 * service no longer offers them and Save removes them from the Card.
 */
export function driftForPersonCard(
  state: ResourceDriftState | undefined,
  notOffered: string[] | undefined,
): ResourceDriftState | undefined {
  if (!state || !notOffered?.length) return state;
  const hidden = new Set(notOffered);
  const keep = (operations?: string[]) => operations?.filter((operation) => !hidden.has(operation));
  return {
    ...state,
    ...(state.changed_operations ? { changed_operations: keep(state.changed_operations) } : {}),
    ...(state.added_operations ? { added_operations: keep(state.added_operations) } : {}),
  };
}

/** The Card-level notice of what the catalog added, without what a person's Card is not offered. */
export function catalogDriftForPersonCard(
  drift: DelegatedCatalogDrift | undefined,
  rowFor: (resource: string) => DelegatedAccessResourceOption | undefined,
): DelegatedCatalogDrift | undefined {
  if (!drift?.added) return drift;
  const outer = drift.added.outer_operations?.filter(
    (row) => !row.operation || !notOfferedOnPersonCard(rowFor(row.resource)).includes(row.operation),
  );
  const named = drift.added.named_service_operations?.filter(
    (row) => !row.operation || !notOfferedNamedOnPersonCard(rowFor(row.resource)).has(`${row.namespace} ${row.operation}`),
  );
  return {
    ...drift,
    added: {
      ...drift.added,
      ...(outer ? { outer_operations: outer } : {}),
      ...(named ? { named_service_operations: named } : {}),
    },
  };
}
