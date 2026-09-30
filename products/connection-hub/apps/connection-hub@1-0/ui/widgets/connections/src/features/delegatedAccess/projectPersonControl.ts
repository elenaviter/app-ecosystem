import type { DelegatedAccessRecord, DelegatedControlCardBinding } from '../../api/types';
import { isMyCard, myCardProjectRef } from './myCardGithub.ts';

export const PROJECT_PERSON_CONTROL_PROPERTY = 'connection_hub.project_person_control';
export const PROJECT_PERSON_CONTROL_SCHEMA = 'connection_hub.project_person_control.v1';
export const PROJECT_INVITATION_CONTROL_PROPERTY = 'connection_hub.project_invitation_control';
export const PROJECT_INVITATION_CONTROL_SCHEMA = 'connection_hub.project_invitation_control.v1';

export interface ProjectPersonControlCoordinates {
  kind: 'person';
  projectRef: string;
  targetSubject: string;
}

export interface ProjectInvitationControlCoordinates {
  kind: 'invitation';
  projectRef: string;
  invitationRef: string;
}

export type ProjectControlCoordinates =
  | ProjectPersonControlCoordinates
  | ProjectInvitationControlCoordinates;

export interface ControlCardGetRequest {
  operation: 'project_person_control_get' | 'project_control_card_get' | 'control_card_get';
  data: Record<string, string>;
}

export function controlCardGetRequest({
  controlId,
  projectRef,
  targetSubject,
  invitationRef,
}: {
  controlId: string;
  projectRef?: string;
  targetSubject?: string;
  invitationRef?: string;
}): ControlCardGetRequest {
  if (projectRef && invitationRef) {
    return {
      operation: 'project_person_control_get',
      data: {
        project_ref: projectRef,
        invitation_ref: invitationRef,
        control_id: controlId,
      },
    };
  }
  if (projectRef && targetSubject) {
    return {
      operation: 'project_person_control_get',
      data: {
        project_ref: projectRef,
        target_subject: targetSubject,
      },
    };
  }
  if (projectRef) {
    // W260: a project's Control Card, through its project; another admin of
    // the project is not its creator, so `control_card_get` would not find it.
    return {
      operation: 'project_control_card_get',
      data: { control_id: controlId, project_ref: projectRef },
    };
  }
  return {
    operation: 'control_card_get',
    data: { control_id: controlId },
  };
}

function clean(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

export function projectPersonControlCoordinates(
  record: Pick<DelegatedAccessRecord, 'properties'>,
): ProjectControlCoordinates | null {
  const marker = record.properties?.[PROJECT_PERSON_CONTROL_PROPERTY];
  if (marker && typeof marker === 'object' && !Array.isArray(marker)) {
    const values = marker as Record<string, unknown>;
    const projectRef = clean(values.project_ref);
    const targetSubject = clean(values.target_subject);
    if (
      clean(values.schema) === PROJECT_PERSON_CONTROL_SCHEMA
      && projectRef
      && targetSubject
    ) {
      return { kind: 'person', projectRef, targetSubject };
    }
  }
  const invitationMarker = record.properties?.[PROJECT_INVITATION_CONTROL_PROPERTY];
  if (
    !invitationMarker
    || typeof invitationMarker !== 'object'
    || Array.isArray(invitationMarker)
  ) return null;
  const values = invitationMarker as Record<string, unknown>;
  const projectRef = clean(values.project_ref);
  const invitationRef = clean(values.invitation_ref);
  return clean(values.schema) === PROJECT_INVITATION_CONTROL_SCHEMA
    && projectRef
    && invitationRef
    ? { kind: 'invitation', projectRef, invitationRef }
    : null;
}

export interface LinkedControlOpenTarget {
  controlId: string;
  projectRef?: string;
  targetSubject?: string;
}

/** W424: where the "Open <Control Card>" button on a composed Card reads the
 *  linked Control Card from. The binding's control_id is the trusted key, and
 *  the route follows who holds the Card:
 *
 *  - a person's My Card in a project is capped by that person's project
 *    Control Card, which the project holds: read it through the project and
 *    the person (`project_person_control_get`), never as the signed-in
 *    person's own Card (`control_card_get` answers control_card_not_found);
 *  - any other project-issued link is the project's Control Card
 *    (`project_control_card_get`);
 *  - otherwise the Card is the signed-in person's own (`control_card_get`).
 *
 *  A backend `control_kind` on the binding, when present, decides first. The
 *  displayed label or UUID never selects the Card. */
export function linkedControlOpenTarget(
  item: Pick<DelegatedAccessRecord, 'source' | 'issuer_kind' | 'issuer_ref' | 'grantor_subject'>,
  binding: Pick<DelegatedControlCardBinding, 'control_id' | 'issuer_ref' | 'issuer_kind'> & { control_kind?: string },
): LinkedControlOpenTarget | null {
  const controlId = clean(binding.control_id);
  if (!controlId) return null;
  const kind = clean(binding.control_kind);
  const projectRef = clean(binding.issuer_ref) || myCardProjectRef(item);
  const targetSubject = clean(item.grantor_subject);
  if ((kind === 'project_person' || (!kind && isMyCard(item))) && projectRef && targetSubject) {
    return { controlId, projectRef, targetSubject };
  }
  if ((kind === 'project' || (!kind && clean(binding.issuer_kind) === 'project')) && projectRef) {
    return { controlId, projectRef };
  }
  return { controlId };
}

/** The Card a linked read returned is the one the binding names, or not at all. */
export function isLinkedControlCard(
  loaded: Pick<DelegatedAccessRecord, 'access_id'> | null | undefined,
  target: LinkedControlOpenTarget,
): boolean {
  return Boolean(loaded) && clean(loaded?.access_id) === target.controlId;
}
