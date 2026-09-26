// W260 (operator ruling, 2026-09-26): Connection Hub is the only Card editor.
// A Card reached through a project is edited here by whoever the project
// allows, and read by everyone else. The editor says so up front, before any
// change is made, the same way on all three project paths: a person's Control
// Card, another person's agent Card, and a project's Control Card.

import type { DelegatedAccessRecord, ProjectPersonControlViewer } from '../../api/types';
import { projectPersonControlNotice } from './accessCardFocus.ts';
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
    return personControlViewer?.can_edit ? '' : projectPersonControlNotice(personControlViewer);
  }
  if (projectAgentCardReadOnly(item)) return projectAgentCardReadOnlyMessage(item);
  if (projectControlCardReadOnly(item)) return PROJECT_CONTROL_CARD_READ_ONLY_MESSAGE;
  return '';
}
