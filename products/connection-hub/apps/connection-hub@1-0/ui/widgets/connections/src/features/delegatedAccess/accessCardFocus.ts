export interface AccessCardFocus {
  accessId: string;
  manualOnly: boolean;
  controlOnly: boolean;
  projectRef?: string;
  targetSubject?: string;
  resource?: string;
  claims: string[];
  outerOperation?: string;
  accountId?: string;
  accountClaim?: string;
}

export function accessCardFocusFromParams(get: (key: string) => string): AccessCardFocus | null {
  const manualAccessId = get('manual_access_id').trim();
  const controlCardId = get('control_card_id').trim();
  const accessId = manualAccessId || controlCardId || get('access_id').trim();
  if (!accessId) return null;
  const resource = get('resource').trim();
  const claims = get('claims').split(',').map((item) => item.trim()).filter(Boolean);
  const accountId = get('account_id').trim();
  const accountClaim = get('account_claim').trim();
  const outerOperation = get('outer_operation').trim();
  const projectRef = get('project_ref').trim();
  const targetSubject = get('target_subject').trim();
  return {
    accessId,
    manualOnly: Boolean(manualAccessId),
    controlOnly: Boolean(controlCardId),
    projectRef: projectRef || undefined,
    targetSubject: targetSubject || undefined,
    resource: resource || undefined,
    claims,
    outerOperation: outerOperation || undefined,
    accountId: accountId || undefined,
    accountClaim: accountClaim || undefined,
  };
}

export function accessCardFocusRequest(openParams?: Record<string, string>): AccessCardFocus | null {
  if (openParams) {
    const fromProps = accessCardFocusFromParams((key) => String(openParams[key] ?? ''));
    if (fromProps) return fromProps;
  }
  try {
    const params = new URLSearchParams(window.location.search);
    return accessCardFocusFromParams((key) => params.get(key) ?? '');
  } catch {
    return null;
  }
}

export function matchesAccessCardFocus(
  candidate: { access_id: string; source?: string },
  focus: AccessCardFocus,
): boolean {
  return candidate.access_id === focus.accessId
    && (!focus.manualOnly || candidate.source === 'manual')
    && (!focus.controlOnly || candidate.source === 'control');
}

export function findAccessCardFocus<T extends { access_id: string; source?: string }>(
  candidates: readonly T[],
  focus: AccessCardFocus,
): T | undefined {
  return candidates.find((candidate) => matchesAccessCardFocus(candidate, focus));
}

export function unavailableAccessCardMessage(focus: AccessCardFocus): string {
  return `Card ${focus.accessId} does not exist or is not visible to this account.`;
}
