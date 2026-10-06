// W260 (operator ruling, 2026-09-26): Connection Hub is the only Card editor.
// A Card reached through a project is edited here by whoever the project
// allows, and read by everyone else. The editor says so up front, before any
// change is made, the same way on all three project paths: a person's Control
// Card, another person's agent Card, and a project's Control Card.

import type { DelegatedAccessRecord, ProjectPersonControlViewer } from '../../api/types';
import { BOARD_RESTARTING_MESSAGE, projectPersonControlNotice } from './accessCardFocus.ts';
import { projectAgentCardReadOnly, projectAgentCardReadOnlyMessage } from './projectAgentCard.ts';
import { PROJECT_CONTROL_CARD_READ_ONLY_MESSAGE, projectControlCardReadOnly } from './projectControlCard.ts';
import { projectPersonControlCoordinates } from './projectPersonControl.ts';

/** Why this viewer only reads this Card, or '' when they may change it. */
export function cardReadOnlyReason(
  item: DelegatedAccessRecord | null | undefined,
  personControlViewer: ProjectPersonControlViewer | undefined,
): string {
  if (!item) return '';
  if (projectPersonControlCoordinates(item)) {
    // W587 follow-up C: an unanswered permission check is not a refusal.
    if (personControlViewer?.can_edit === null) return '';
    return personControlViewer?.can_edit ? '' : projectPersonControlNotice(personControlViewer);
  }
  if (projectAgentCardReadOnly(item)) return projectAgentCardReadOnlyMessage(item);
  if (projectControlCardReadOnly(item)) return PROJECT_CONTROL_CARD_READ_ONLY_MESSAGE;
  return '';
}

/** W587 follow-up C (EMain 15:54): the project's permission check did not answer
 *  (for example while the board reloaded). It is not a refusal, so the viewer is
 *  not told "a project admin decides this", and it is not a yes either: Save
 *  stays disabled (fail closed) while Edit stays, because pressing Edit reads
 *  the Card again and so checks again. */
export const PERMISSION_UNKNOWN_MESSAGE =
  'Your permission to edit this Card could not be checked just now. Press Edit to check again; saving waits until the check answers.';

/** The same unknown permission, because the board is restarting (W587 C). */
export const BOARD_RESTARTING_EDIT_MESSAGE =
  `${BOARD_RESTARTING_MESSAGE} Press Edit to check again; saving waits until the check answers.`;

export function cardPermissionUnknown(
  item: DelegatedAccessRecord | null | undefined,
  personControlViewer: ProjectPersonControlViewer | undefined,
): string {
  if (!item || !projectPersonControlCoordinates(item)) return '';
  if (personControlViewer?.can_edit !== null) return '';
  return personControlViewer.reason === 'project_board_restarting' ? BOARD_RESTARTING_EDIT_MESSAGE : PERMISSION_UNKNOWN_MESSAGE;
}

/** Why Save is not offered: read-only, or a permission that could not be checked. */
export function cardSaveBlockedReason(
  item: DelegatedAccessRecord | null | undefined,
  personControlViewer: ProjectPersonControlViewer | undefined,
): string {
  return cardReadOnlyReason(item, personControlViewer) || cardPermissionUnknown(item, personControlViewer);
}
