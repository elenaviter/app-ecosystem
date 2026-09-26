import type { ReactNode } from 'react';
import { groupOperations, type OperationGroupDeclaration } from './operationGroups';

/** The operations under the service's declared groups; a plain list when it declares none (W260). */
export function renderOperationGroups<T>(
  items: readonly T[],
  groupOf: (item: T) => string | undefined,
  declared: readonly OperationGroupDeclaration[] | undefined,
  render: (item: T) => ReactNode,
): ReactNode {
  const groups = groupOperations(items, groupOf, declared);
  if (!groups) return items.map(render);
  return groups.map((group) => (
    <div className="operation-group" key={`group:${group.key || '__other'}`} role="group" aria-label={group.label}>
      <div className="operation-group__label">{group.label}</div>
      {group.items.map(render)}
    </div>
  ));
}
