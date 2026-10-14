/**
 * `card_committed_policy_pending`: the Card change committed but the invocation policy of an operation it
 * added did not (W727). The answer is recognised so the screen says what is saved and keeps the editor.
 *
 * W755 (operator, 2026-10-11: "WHY ITS NOT A PART"): a Save is one request with the whole Card and the
 * policy of every operation it adds, so there is no separate focused grant to resume; pressing Save again
 * finishes a pending policy.
 */
export const POLICY_PENDING = 'card_committed_policy_pending';

export function isPolicyPending(result: { ok?: boolean; outcome?: string } | null | undefined): boolean {
  return Boolean(result) && result?.ok === false && result?.outcome === POLICY_PENDING;
}
