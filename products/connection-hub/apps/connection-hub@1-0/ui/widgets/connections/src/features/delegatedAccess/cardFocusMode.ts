import type { AccessCardFocus } from './accessCardFocus';

export type AccessCardOpenMode = 'view' | 'edit';

/** Plain Card links are navigation. Only a link carrying an authority request
 * opens the editor; pending grants and OAuth consent use their own flows. */
export function accessCardOpenMode(focus: AccessCardFocus): AccessCardOpenMode {
  if (focus.resource && focus.outerOperation) return 'edit';
  if (focus.accountClaim || focus.claims.length) return 'edit';
  return 'view';
}
