// W319: open another person's agent Card through a project the agent attends.
//
// Connection Hub keeps a Card under its owner, so a project's other admins
// never find it in their own Card list. A board link carries the project; the
// widget then asks for the Card through that project, and Connection Hub asks
// the board whether this person is the owner, an admin of a project the agent
// attends now (open and change), or a platform admin (open only).
//
// W319 slice 2: an agent its owner shared with the person opens the same way,
// without a project (the link says `shared=1`); Connection Hub decides the
// share itself: view opens read-only, edit also changes the Card.

import type { AccessCardFocus } from './accessCardFocus';
import type { DelegatedAccessRecord, ProjectAgentCardGetResult } from '../../api/types';

export interface ProjectAgentCardTarget {
  accessId: string;
  projectRef: string;
}

/** The project path applies to a plain agent Card link that names a project or a share. */
export function projectAgentCardFocus(focus: AccessCardFocus | null | undefined): ProjectAgentCardTarget | null {
  if (!focus || focus.controlOnly || focus.manualOnly) return null;
  if (focus.targetSubject || focus.invitationRef) return null;
  const projectRef = (focus.projectRef || '').trim();
  if (!projectRef && !focus.shared) return null;
  return { accessId: focus.accessId, projectRef };
}

export function projectAgentCardGetRequest(target: ProjectAgentCardTarget) {
  return {
    operation: 'project_agent_card_get' as const,
    data: { access_id: target.accessId, project_ref: target.projectRef },
  };
}

/** The record the editor opens, carrying how it was reached. */
export function projectAgentCardRecord(result: ProjectAgentCardGetResult): DelegatedAccessRecord | null {
  if (!result?.item || !result.access) return null;
  return { ...result.item, project_agent_card: { ...result.access } };
}

/** Where a save of this Card goes: the project path, or null for the owner's own path. */
export function projectAgentCardUpdateTarget(item: DelegatedAccessRecord | null | undefined): ProjectAgentCardTarget | null {
  const access = item?.project_agent_card;
  if (!item || !access || access.via === 'owner') return null;
  return { accessId: item.access_id, projectRef: access.project_ref };
}

export function projectAgentCardReadOnly(item: DelegatedAccessRecord | null | undefined): boolean {
  return Boolean(item?.project_agent_card && !item.project_agent_card.can_edit);
}

export function projectAgentCardReadOnlyMessage(item: DelegatedAccessRecord | null | undefined): string {
  const via = item?.project_agent_card?.via;
  if (via === 'platform_admin') {
    return 'A platform admin opens every agent Card but changes one only as an admin of a project the agent attends.';
  }
  if (via === 'shared_view') {
    return 'The owner shares this agent with you to view: you can message it and add it to a project, not change its Card.';
  }
  return 'This Card is open to read only.';
}
