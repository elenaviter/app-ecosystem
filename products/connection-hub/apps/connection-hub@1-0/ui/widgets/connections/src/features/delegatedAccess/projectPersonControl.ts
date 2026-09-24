import type { DelegatedAccessRecord } from '../../api/types';

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
