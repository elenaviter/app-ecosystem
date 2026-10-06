import type { DelegatedAccessRecord, DelegatedAccessRevokeResult } from '../../api/types';

/** Freeze the target the owner saw; never replace it after a refusal. Legacy
 *  records without a revision keep the ordinary owner-only wire contract. The
 *  server refuses that contract for issuer-managed Cards. */
export function delegatedAccessRevokePayload(accessId: string, cardRevision?: number) {
  return {
    access_id: accessId,
    ...(cardRevision !== undefined ? {
      expected_access_id: accessId,
      expected_card_revision: cardRevision,
    } : {}),
  };
}

export function isRevokeRevisionConflict(result: DelegatedAccessRevokeResult | undefined): boolean {
  return result?.ok === false
    && result.status === 409
    && result.error === 'delegated_card_revision_conflict';
}

interface RevokeState {
  busy: boolean;
  error: string;
  items: DelegatedAccessRecord[];
  focusedCard?: DelegatedAccessRecord;
  issuedAccess?: DelegatedAccessRecord;
  issuedToken: string;
  issuedHeader: string;
}

/** A refused revoke is not a successful removal. Preserve every local Card
 *  and credential banner until the read refresh supplies the current state. */
export function applyDelegatedAccessRevokeResult(
  state: RevokeState,
  result: DelegatedAccessRevokeResult,
  accessId: string,
  expectedCardRevision?: number,
): void {
  state.busy = false;
  if (result.ok === false) {
    state.error = isRevokeRevisionConflict(result)
      ? `This Card changed since revision ${expectedCardRevision ?? 'unknown'}. Reloading its current state. Review the current revision and confirm again; no revocation was retried.`
      : result.message || result.error || 'Failed to revoke delegated access';
    return;
  }
  state.items = state.items.filter((item) => item.access_id !== accessId);
  if (state.focusedCard?.access_id === accessId) state.focusedCard = undefined;
  if (state.issuedAccess?.access_id === accessId) {
    state.issuedToken = '';
    state.issuedHeader = '';
    state.issuedAccess = undefined;
  }
}
