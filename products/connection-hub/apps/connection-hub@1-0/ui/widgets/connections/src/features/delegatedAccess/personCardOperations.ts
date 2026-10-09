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

/** One catalog row as a person's Control Card is offered it. W560 (operator, 2026-10-05: "simply selected
 *  and non-editable"): an outer operation the service decides for a person stays LISTED, marked "managed",
 *  shown as the Card holds it and never changed by the editor or its Save. */
export function resourceForPersonCard(option: DelegatedAccessResourceOption): DelegatedAccessResourceOption {
  return {
    ...option,
    ...(option.operations ? { operations: option.operations.map((operation) => (
      offeredOnPersonCard(operation) ? operation : { ...operation, managed: true })) } : {}),
    ...(option.named_services ? { named_services: option.named_services.map(namespaceForPersonCard) } : {}),
  };
}

export function resourcesForPersonCard(options: DelegatedAccessResourceOption[]): DelegatedAccessResourceOption[] {
  return options.map(resourceForPersonCard);
}

/** The outer operations of a catalog row a person's Card is not offered, for the drift review. */
export function notOfferedOnPersonCard(catalogRow: DelegatedAccessResourceOption | undefined): string[] {
  return (catalogRow?.operations || [])
    .filter((operation) => !offeredOnPersonCard(operation))
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
