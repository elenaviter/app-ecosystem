// W587, the operator's rule (2026-10-06 13:43): the Hub editor loads a Card
// when it is opened and again on Edit; an edit made on a version the server
// has since replaced cannot be saved until the person reloads the Card. The
// decisions live here, as pure functions, so they are tested by behaviour.

import type { AccessCardFocus } from './accessCardFocus';

export interface ControlReadTarget {
  controlId: string;
  projectRef?: string;
  targetSubject?: string;
  invitationRef?: string;
}

/** The server read a Control link needs. Always a read: a copy cached in this
 *  tab is never a reason to skip it (another admin may have saved since). */
export function controlFocusRead(focus: AccessCardFocus | null | undefined): ControlReadTarget | null {
  if (!focus?.controlOnly) return null;
  return {
    controlId: focus.accessId,
    projectRef: focus.projectRef,
    targetSubject: focus.targetSubject,
    invitationRef: focus.invitationRef,
  };
}

/** The pin an edit starts with: the revision of the Card it was seeded from. */
export function pinAtStart(record: { card_revision?: number }): number | null {
  return record.card_revision ?? null;
}

/** A refused save (409) keeps the pin: the edit stays on the version it was
 *  made on, and only a reload into the editor (pinAtStart) moves it. */
export function pinAfterRefusal(pin: number | null): number | null {
  return pin;
}

/** A successful save moves the pin to the revision the server wrote. */
export function pinAfterSave(pin: number | null, saved: { card_revision?: number } | null | undefined): number | null {
  return saved?.card_revision ?? pin;
}

/** A Card read replaces the owner list's copy when it is newer, so the rows
 *  and a row's Edit never start from an older copy than the tab has read. */
export function withNewerCard<T extends { access_id: string; card_revision?: number }>(
  items: T[],
  record: T | null | undefined,
): T[] {
  // The SAME array when nothing is newer: a read must never change the list's
  // identity for nothing, or anything keyed on the list fires again (W587 loop).
  if (!record) return items;
  const index = items.findIndex((item) => item.access_id === record.access_id);
  if (index < 0 || (record.card_revision ?? 0) <= (items[index].card_revision ?? 0)) return items;
  const next = items.slice();
  next[index] = record;
  return next;
}

/** The catalog an edit starts on, pinned with the revision: a catalog change
 *  while editing is refused by the server (409), never adopted silently. */
export function catalogPinAtStart(record: {
  catalog_version?: string; catalog_drift?: { current_version?: string } | null;
}): string | null {
  return record.catalog_drift?.current_version || record.catalog_version || null;
}

/** A link that asks to grant one operation adds it to the draft AFTER the
 *  draft was seeded from the server read; seeding never drops it. */
export function withLinkedOperation(
  operations: Record<string, string[]>,
  resource: string | undefined,
  operation: string | undefined,
): Record<string, string[]> {
  if (!resource || !operation) return operations;
  return { ...operations, [resource]: Array.from(new Set([...(operations[resource] || []), operation])) };
}
