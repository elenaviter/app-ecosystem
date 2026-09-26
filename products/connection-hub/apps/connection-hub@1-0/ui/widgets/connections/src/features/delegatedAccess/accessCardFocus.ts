export interface AccessCardFocus {
  accessId: string;
  manualOnly: boolean;
  controlOnly: boolean;
  projectRef?: string;
  /** W319 slice 2: the owner shared this agent with the person; no project needed. */
  shared?: boolean;
  targetSubject?: string;
  invitationRef?: string;
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
  const shared = ['1', 'true'].includes(get('shared').trim().toLowerCase());
  const targetSubject = get('target_subject').trim();
  const invitationRef = get('invitation_ref').trim();
  return {
    accessId,
    manualOnly: Boolean(manualAccessId),
    controlOnly: Boolean(controlCardId),
    projectRef: projectRef || undefined,
    shared: shared || undefined,
    targetSubject: targetSubject || undefined,
    invitationRef: invitationRef || undefined,
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

const MY_CARD_PREFIX = 'person-my-card-';

/** Why a requested Card did not open, with Connection Hub's own reason when it
 *  gave one (W260: "Open my Control Card" showed only "does not exist"). */
export function unavailableAccessCardMessage(focus: AccessCardFocus, reason = ''): string {
  const base = `Card ${focus.accessId} does not exist or is not visible to this account.`;
  const why = reason.trim() ? ` Connection Hub answered: ${reason.trim().replace(/\.$/, '')}.` : '';
  // A person's own Card (My Card) is not a Control Card: a link that asks for
  // it as one can never open it.
  const hint = focus.controlOnly && focus.accessId.startsWith(MY_CARD_PREFIX)
    ? ' This is a person\'s own Card (My Card), not a Control Card; it opens with access_id, not control_card_id.'
    : '';
  return `${base}${why}${hint}`;
}

/** The notice on a project-held person Control Card this viewer only reads (W260). */
export function projectPersonControlNotice(
  viewer: { can_edit?: boolean } | undefined,
): string {
  return viewer?.can_edit
    ? ''
    : 'A project admin decides this Control Card. You can read it here; your own Card (My Card) is what you change.';
}
