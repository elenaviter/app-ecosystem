// W260 (operator ruling, 2026-09-26): a service declares how its operations are
// grouped (for Problem Board: Review, Work, Plan, People) in its catalog, and
// the Card editor groups by it. No client builds its own grouping.
//
// Each operation may carry `group`; the resource or namespace carries
// `operation_groups`, already sorted by the server ({group, label, order}).
// Operations keep their catalog order inside a group. An operation without a
// declared group, or naming a group the map omits, lands in its own group
// labelled by the key, or in "Other" when it has none.

export interface OperationGroupDeclaration {
  group: string;
  label?: string;
  order?: number;
}

export interface OperationGroup<T> {
  key: string;
  label: string;
  items: T[];
}

export const OTHER_GROUP_LABEL = 'Other';

/** The operations under their declared groups, or null when the service declares none. */
export function groupOperations<T>(
  items: readonly T[],
  groupOf: (item: T) => string | undefined,
  declared: readonly OperationGroupDeclaration[] | undefined,
): OperationGroup<T>[] | null {
  const anyGrouped = items.some((item) => Boolean((groupOf(item) || '').trim()));
  if (!anyGrouped) return null;
  const groups: OperationGroup<T>[] = [];
  const byKey = new Map<string, OperationGroup<T>>();
  (declared || []).forEach((row) => {
    const key = (row.group || '').trim();
    if (!key || byKey.has(key)) return;
    const group = { key, label: (row.label || '').trim() || key, items: [] as T[] };
    byKey.set(key, group);
    groups.push(group);
  });
  const extra: OperationGroup<T>[] = [];
  let other: OperationGroup<T> | null = null;
  items.forEach((item) => {
    const key = (groupOf(item) || '').trim();
    if (!key) {
      other = other || { key: '', label: OTHER_GROUP_LABEL, items: [] };
      other.items.push(item);
      return;
    }
    let group = byKey.get(key);
    if (!group) {
      group = { key, label: key, items: [] };
      byKey.set(key, group);
      extra.push(group);
    }
    group.items.push(item);
  });
  return [...groups, ...extra, ...(other ? [other] : [])].filter((group) => group.items.length > 0);
}
