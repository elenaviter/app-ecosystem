/**
 * Application-managed grants (W661 S5): a catalog resource row may declare `managed_grants`, permissions
 * that only the owning application sets through its own operations. A Card editor shows them as the Card
 * holds them, never changes them, and never sends a change to them; the Hub also refuses such a client
 * upsert (managed_grant_not_editable). Generic: nothing here names an application or a grant.
 */
export const MANAGED_GRANT_NOTE = 'Managed by the application';
/** W560: an operation the application decides for a person (catalog `person_card: false`), on their Control Card. */
export const MANAGED_OPERATION_NOTE = 'Set by the application';

export function managedGrantsOf(option?: { managed_grants?: string[] } | null): Set<string> {
  return new Set((option?.managed_grants || []).filter((grant) => typeof grant === 'string' && grant));
}

/** The selection with every managed grant as the Card holds it (`held`; `{}` for a new Card). */
export function withManagedGrantsAsHeld(
  selected: Record<string, string[]>,
  held: Record<string, string[] | readonly string[]>,
  managedFor: (resource: string) => Set<string>,
): Record<string, string[]> {
  const next: Record<string, string[]> = {};
  new Set([...Object.keys(selected), ...Object.keys(held)]).forEach((resource) => {
    const managed = managedFor(resource);
    const kept = (selected[resource] || []).filter((grant) => !managed.has(grant));
    const fixed = (held[resource] || []).filter((grant) => managed.has(grant));
    const grants = Array.from(new Set([...kept, ...fixed]));
    if (grants.length) next[resource] = grants;
  });
  return next;
}
