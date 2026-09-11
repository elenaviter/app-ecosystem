export interface AccessCardFocus {
  accessId: string;
  manualOnly: boolean;
  resource?: string;
  claims: string[];
  outerOperation?: string;
  accountId?: string;
  accountClaim?: string;
}

export function accessCardFocusFromParams(get: (key: string) => string): AccessCardFocus | null {
  const manualAccessId = get('manual_access_id').trim();
  const accessId = manualAccessId || get('access_id').trim();
  if (!accessId) return null;
  const resource = get('resource').trim();
  const claims = get('claims').split(',').map((item) => item.trim()).filter(Boolean);
  const accountId = get('account_id').trim();
  const accountClaim = get('account_claim').trim();
  const outerOperation = get('outer_operation').trim();
  return {
    accessId,
    manualOnly: Boolean(manualAccessId),
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
    && (!focus.manualOnly || candidate.source === 'manual');
}
