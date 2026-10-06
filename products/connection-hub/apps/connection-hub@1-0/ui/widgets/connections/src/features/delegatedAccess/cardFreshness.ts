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

/** What returning to the tab reads again: the owner list, and the open Control. */
export function tabReturnReads(
  visibility: string,
  focus: AccessCardFocus | null | undefined,
): { list: boolean; control: ControlReadTarget | null } {
  if (visibility !== 'visible') return { list: false, control: null };
  return { list: true, control: controlFocusRead(focus) };
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
  items: readonly T[],
  record: T | null | undefined,
): T[] {
  if (!record) return [...items];
  return items.map((item) => (
    item.access_id === record.access_id && (record.card_revision ?? 0) > (item.card_revision ?? 0)
      ? record
      : item
  ));
}
