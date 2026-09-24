import type { DelegatedAccessRecord } from '../../api/types';

export const PROJECT_PERSON_CONTROL_PROPERTY = 'connection_hub.project_person_control';
export const PROJECT_PERSON_CONTROL_SCHEMA = 'connection_hub.project_person_control.v1';

export interface ProjectPersonControlCoordinates {
  projectRef: string;
  targetSubject: string;
}

function clean(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

export function projectPersonControlCoordinates(
  record: Pick<DelegatedAccessRecord, 'properties'>,
): ProjectPersonControlCoordinates | null {
  const marker = record.properties?.[PROJECT_PERSON_CONTROL_PROPERTY];
  if (!marker || typeof marker !== 'object' || Array.isArray(marker)) return null;
  const values = marker as Record<string, unknown>;
  if (clean(values.schema) !== PROJECT_PERSON_CONTROL_SCHEMA) return null;
  const projectRef = clean(values.project_ref);
  const targetSubject = clean(values.target_subject);
  return projectRef && targetSubject ? { projectRef, targetSubject } : null;
}
