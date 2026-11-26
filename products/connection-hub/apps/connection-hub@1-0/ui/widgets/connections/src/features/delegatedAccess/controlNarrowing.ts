import type { DelegatedOutsideControl } from '../../api/types';

export interface ControlNarrowingRow {
  resource: string;
  namespace?: string;
  claim?: string;
  operation?: string;
}

/** Flatten the server's outside-Control block into rows the drift list
 *  renders: named-service operations, tools, then permissions. */
export function controlNarrowingRows(outside?: DelegatedOutsideControl): ControlNarrowingRow[] {
  const rows: ControlNarrowingRow[] = [];
  Object.entries(outside?.named_service_operations || {}).forEach(([resource, namespaces]) => {
    Object.entries(namespaces || {}).forEach(([namespace, operations]) => {
      (operations || []).forEach((operation) => rows.push({ resource, namespace, operation }));
    });
  });
  Object.entries(outside?.outer_operations || {}).forEach(([resource, operations]) => {
    (operations || []).forEach((operation) => rows.push({ resource, operation }));
  });
  Object.entries(outside?.claims || {}).forEach(([resource, claims]) => {
    (claims || []).forEach((claim) => rows.push({ resource, claim }));
  });
  return rows;
}

/** Who narrowed it, in the owner's words. */
export function controlNarrowingSource(outside?: DelegatedOutsideControl): string {
  return outside?.by === 'administrator'
    ? "The administrator's list on this agent's Control"
    : "This agent's descriptor";
}
