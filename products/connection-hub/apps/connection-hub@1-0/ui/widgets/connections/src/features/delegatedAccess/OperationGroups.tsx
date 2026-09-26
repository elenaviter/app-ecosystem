import type { ReactNode } from 'react';
import { groupOperations, type OperationGroupDeclaration } from './operationGroupsModel';

/** The operations under the service's declared groups; a plain list when it declares none (W260). */
export function renderOperationGroups<T>(
  items: readonly T[],
  groupOf: (item: T) => string | undefined,
  declared: readonly OperationGroupDeclaration[] | undefined,
  render: (item: T) => ReactNode,
): ReactNode {
  const groups = groupOperations(items, groupOf, declared);
  if (!groups) return items.map(render);
  // A real group box with a heading, so assistive technology names it.
  return groups.map((group) => (
    <div className="operation-group" key={`group:${group.key || '__other'}`} role="group" aria-label={group.label}>
      <h5 className="operation-group__label">{group.label}</h5>
      {group.items.map(render)}
    </div>
  ));
}
