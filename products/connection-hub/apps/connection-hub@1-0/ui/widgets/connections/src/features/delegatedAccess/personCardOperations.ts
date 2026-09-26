/**
 * What a person's Control Card is offered (W360).
 *
 * A service marks an operation `person_card: false` in its catalog when it
 * decides that operation for a person by role alone, never by the person's
 * Card. For Problem Board those are the coordinator levers and the people and
 * project Control Card operations: ticking them on a person's Control Card
 * would change nothing, so a project person's or invitation's Control Card
 * does not offer them, in the editor or among newly advertised operations.
 * Every other operation stays offered; an operation without the mark is
 * offered as before. Other Cards see the whole catalog.
 */
import type {
  DelegatedAccessNamedServiceNamespaceOption,
  DelegatedAccessNamedServiceToolOption,
  DelegatedAccessResourceOption,
} from '../../api/types';

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

/** One catalog row as a person's Control Card is offered it. */
export function resourceForPersonCard(option: DelegatedAccessResourceOption): DelegatedAccessResourceOption {
  return {
    ...option,
    ...(option.operations ? { operations: option.operations.filter(offeredOnPersonCard) } : {}),
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
