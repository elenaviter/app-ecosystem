// W260: a project's Control Card, opened through the project.
//
// Connection Hub keeps the project Control Card under the person who created
// it, so another admin of the project never finds it in their own list and the
// creator-only `control_card_get` answers "not found". The board's "Manage
// project access" link carries the project (`control_card_id` + `project_ref`,
// no `target_subject`): the widget then reads it through
// `project_control_card_get` and saves through `project_control_card_update`,
// and Connection Hub asks the project whether this person may.

import type { DelegatedAccessRecord, ProjectControlCardAccess } from '../../api/types';

export interface ProjectControlCardTarget {
  controlId: string;
  projectRef: string;
}

/** The project path applies to a Control Card link that names a project and no person or invitation. */
export function projectControlCardFocus(
  focus: { accessId: string; controlOnly: boolean; projectRef?: string; targetSubject?: string; invitationRef?: string } | null | undefined,
): ProjectControlCardTarget | null {
  if (!focus || !focus.controlOnly) return null;
  if (focus.targetSubject || focus.invitationRef) return null;
  const projectRef = (focus.projectRef || '').trim();
  if (!projectRef) return null;
  return { controlId: focus.accessId, projectRef };
}

/** The record the editor opens, carrying how the project let this person reach it. */
export function projectControlCardRecord(access: DelegatedAccessRecord & Partial<ProjectControlCardAccess>): DelegatedAccessRecord {
  const { via, can_edit: canEdit, project_ref: projectRef, ...record } = access;
  return {
    ...(record as DelegatedAccessRecord),
    project_control_card: {
      via: String(via || ''),
      can_edit: Boolean(canEdit),
      project_ref: String(projectRef || ''),
    },
  };
}

/** Where a save of this Card goes: the project path when it was opened there, else null. */
export function projectControlCardUpdateTarget(
  item: DelegatedAccessRecord | null | undefined,
): ProjectControlCardTarget | null {
  const access = item?.project_control_card;
  if (!item || !access || !access.project_ref) return null;
  return { controlId: item.access_id, projectRef: access.project_ref };
}

export function projectControlCardReadOnly(item: DelegatedAccessRecord | null | undefined): boolean {
  return Boolean(item?.project_control_card && !item.project_control_card.can_edit);
}

export const PROJECT_CONTROL_CARD_READ_ONLY_MESSAGE =
  "This project's Control Card is changed by its creator or a project admin; you can read it here.";
