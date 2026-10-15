/**
 * How a newly granted outer operation may run, chosen together with the grant.
 *
 * An operation is admitted under an invocation policy: `always` (every call
 * while the card is active) or `once` (the next call only). The policy belongs
 * to ONE operation on ONE card resource. Two rules follow, and this module
 * exists to make them hard to break:
 *
 * 1. The choice is made for the exact operation being granted, never beside a
 *    sibling. A control that sits next to `search` while the request is for
 *    `delete` either looks unusable or changes the wrong policy.
 * 2. Granting and choosing happen in one server transaction, the focused
 *    grant (`delegated_agent_grant_create` with `access_id`,
 *    `invocation_mode`, `invocation_change_id`, and exactly one operation).
 *    Granting first and setting the policy afterwards would leave a window in
 *    which an operation the user wanted once is authorized always.
 */
import type { GrantAgentAccessArgs } from './agentGrantPayload';

export type InvocationMode = 'always' | 'once';

export const INVOCATION_MODE_TEXT: Record<InvocationMode, string> = {
  once: 'the next invocation only',
  always: 'every invocation while the card is active',
};

/** Everything a focused grant must name to commit one operation's policy. */
export interface FocusedGrantIdentity {
  clientId: string;
  accessId: string;
  resource: string;
  operation: string;
  changeId: string;
  requestBound?: boolean;
  requestDigest?: string;
  requestApprovalTicket?: string;
  requestCardRevision?: number;
  requestAuthorityRevision?: string;
  approvalContext?: Record<string, string>;
}

/** The pending request as the recovery link carries it (see the panel's
 *  `PendingAgentGrant`); only the fields the identity needs are read. */
export interface PendingLikeRequest {
  clientId: string;
  accessId?: string;
  resource: string;
  outerOperation?: string;
  invocationPolicy?: string;
  invocationChangeId?: string;
  requestBound?: boolean;
  requestDigest?: string;
  requestApprovalTicket?: string;
  requestCardRevision?: number;
  requestAuthorityRevision?: string;
  approvalContext?: Record<string, string>;
}

/** Whether the request asks the user to pick the policy here. */
export function pendingChoiceRequested(pending: PendingLikeRequest | null | undefined): boolean {
  return pending?.invocationPolicy === 'choose';
}

/** A mode the request already fixed (`invocation_policy=once|always`), or null
 *  when the user is to choose or nothing was asked. */
export function pendingPresetMode(pending: PendingLikeRequest | null | undefined): InvocationMode | null {
  const value = pending?.invocationPolicy;
  return value === 'once' || value === 'always' ? value : null;
}

/** The card a pending request was denied on, for the grant of that request's
 *  own resource only. An OAuth card id is derived from the concrete URL the
 *  client consented at, so the server cannot re-derive it from the declared
 *  resource the link names; other resources picked on the pane carry no id. */
export function pendingGrantAccessId(
  pending: PendingLikeRequest | null | undefined,
  resource: string,
): string | undefined {
  if (!pending?.accessId || resource !== pending.resource) return undefined;
  return pending.accessId;
}

/** The focused-grant identity of a pending outer-operation request, or null
 *  when the link lacks a part the server requires (then no policy can be
 *  committed and the buttons stay disabled rather than submitting a grant
 *  that would be refused). */
export function pendingFocusedIdentity(pending: PendingLikeRequest | null | undefined): FocusedGrantIdentity | null {
  if (!pending?.accessId || !pending.outerOperation || !pending.invocationChangeId) return null;
  return {
    clientId: pending.clientId,
    accessId: pending.accessId,
    resource: pending.resource,
    operation: pending.outerOperation,
    changeId: pending.invocationChangeId,
    requestBound: pending.requestBound,
    requestDigest: pending.requestDigest,
    requestApprovalTicket: pending.requestApprovalTicket,
    requestCardRevision: pending.requestCardRevision,
    requestAuthorityRevision: pending.requestAuthorityRevision,
    approvalContext: pending.approvalContext,
  };
}

/** The thunk arguments of ONE focused grant: this operation, this mode, on
 *  this card. `claims` are the resource's claims to carry (merged server-side,
 *  so passing the existing set is a no-op for the claims). Anything about other
 *  operations is deliberately absent: the server requires exactly one. */
export function focusedGrantArgs(
  identity: FocusedGrantIdentity,
  mode: InvocationMode,
  claims: string[],
  extras: {
    namedServiceOperations?: Record<string, string[]>;
    accountScope?: Record<string, Record<string, string[]>>;
    expectedCardRevision?: number;
  } = {},
): GrantAgentAccessArgs {
  return {
    clientId: identity.clientId,
    resource: identity.resource,
    claims: [...claims],
    accessId: identity.accessId,
    invocationMode: mode,
    invocationChangeId: identity.changeId,
    resourceOperations: [identity.operation],
    ...(identity.requestBound ? {
      requestBound: true,
      requestDigest: identity.requestDigest,
      requestApprovalTicket: identity.requestApprovalTicket,
      requestCardRevision: identity.requestCardRevision,
      requestAuthorityRevision: identity.requestAuthorityRevision,
      approvalContext: identity.approvalContext,
    } : {}),
    ...(extras.namedServiceOperations ? { namedServiceOperations: extras.namedServiceOperations } : {}),
    ...(extras.accountScope ? { accountScope: extras.accountScope } : {}),
    ...(extras.expectedCardRevision !== undefined ? { expectedCardRevision: extras.expectedCardRevision } : {}),
  };
}

/** A change id for a policy chosen in ordinary card editing (no denial gave
 *  one). Printable ASCII, no spaces, at most 256 characters, as the server
 *  validates; distinct per card, operation, and attempt so a retry of the same
 *  attempt replays and a new attempt is a new change. */
export function editChangeId(accessId: string, operation: string, nonce: string): string {
  const clean = (text: string) => text.replace(/[^\x21-\x7e]/g, '-');
  return `edit:${clean(accessId)}:${clean(operation)}:${clean(nonce)}`.slice(0, 256);
}

export function randomNonce(): string {
  const bytes = new Uint8Array(8);
  if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
    crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  }
  return `${Date.now().toString(36)}${Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')}`;
}

export interface EditedOperationSplit {
  /** Operations the card already granted and the editor keeps: saved through
   *  the ordinary card update, their policies untouched. */
  kept: string[];
  /** Operations the editor adds, each with the policy the user chose: granted
   *  one by one through the focused transaction. */
  focused: Array<{ operation: string; mode: InvocationMode }>;
  /** Added operations still without a choice: the save must wait for them. */
  missingChoice: string[];
}

/** Split an edited operation selection for one resource into what the card
 *  update carries and what the focused grants carry. */
export function splitEditedOperations(
  alreadyGranted: string[],
  edited: string[],
  modeOf: (operation: string) => InvocationMode | undefined,
): EditedOperationSplit {
  const granted = new Set(alreadyGranted);
  const kept: string[] = [];
  const focused: EditedOperationSplit['focused'] = [];
  const missingChoice: string[] = [];
  edited.forEach((operation) => {
    if (granted.has(operation)) {
      kept.push(operation);
      return;
    }
    const mode = modeOf(operation);
    if (mode) focused.push({ operation, mode });
    else missingChoice.push(operation);
  });
  return { kept, focused, missingChoice };
}

/** What a newly selected operation is used as until the person chooses (2026-09-28).
 *
 *  The consent draft proposes "Every time" (``always``) for every operation it
 *  selects. An operation the person adds on the page (a resource added, "All",
 *  one operation, an application operation) starts the same way, so Approve
 *  never submits a selected operation without a choice and the platform's
 *  "every selected outer operation requires Once or Always" is never reached. */
export const DEFAULT_INVOCATION_MODE: InvocationMode = 'always';

/** Modes keyed `${resource}:${operation}`, with the default for each operation that has none. */
export function withDefaultInvocationModes(
  current: Record<string, InvocationMode>,
  resource: string,
  operations: string[],
): Record<string, InvocationMode> {
  const next = { ...current };
  for (const operation of operations) {
    const key = `${resource}:${operation}`;
    if (!next[key]) next[key] = DEFAULT_INVOCATION_MODE;
  }
  return next;
}

/** The invocation policies a create or consent form submits: one mode for every selected operation.
 *
 *  Application operations (resource ``*``) have no Once/Always control on the
 *  page (the role decides what they run as), but the platform still wants a
 *  policy for each selected one, so they carry their recorded mode or the
 *  default. Until 2026-09-28 they were sent as ``{}``, which the consent
 *  route refused whenever an application operation was selected. */
export function submittedInvocationModes(
  selectedResources: string[],
  resourceOperations: Record<string, string[]>,
  modes: Record<string, InvocationMode | undefined>,
): Record<string, Record<string, InvocationMode>> {
  return Object.fromEntries(selectedResources.map((resource) => [
    resource,
    Object.fromEntries((resourceOperations[resource] || []).map((operation) => [
      operation,
      modes[`${resource}:${operation}`] || DEFAULT_INVOCATION_MODE,
    ])),
  ]));
}


/** W872 (operator, 2026-10-15: "when i click through the card during edit THIS MUST NOT CAUSE NO ANY CALLS! i must
 *  simply be able normally pick what i need! only when i press save someting can be asked"): what the switch of an
 *  operation the Card already holds shows and allows. A click only records the draft mode; Save sends it. */
export function policySwitchView(
  policy: { mode?: InvocationMode | null; remaining?: number | null } | null | undefined,
  draftMode: InvocationMode | undefined,
  busy: boolean,
  onceBlocked = false,
): { shown: InvocationMode; changed: boolean; alwaysDisabled: boolean; onceDisabled: boolean; used: boolean } {
  const stored: InvocationMode = policy?.mode || 'always';
  const used = policy?.mode === 'once' && policy?.remaining === 0;
  const shown: InvocationMode = draftMode || stored;
  // A used Once is re-armed by choosing Once again; an armed one is already what Once means.
  const onceIsCurrent = shown === 'once' && !(used && !draftMode);
  return {
    shown,
    changed: Boolean(draftMode) && (draftMode !== stored || (draftMode === 'once' && used)),
    alwaysDisabled: busy || shown === 'always',
    onceDisabled: busy || onceBlocked || onceIsCurrent,
    used: used && !draftMode,
  };
}

/** W872: the held operations whose mode the draft changes, to send with Save and to list before it. A draft equal to
 *  the stored mode is no change, except Once on a used Once (a re-arm). */
export function heldModeChanges(
  policyOf: (resource: string, operation: string) => { mode?: InvocationMode | null; remaining?: number | null } | undefined,
  held: Record<string, string[]>,
  draft: Record<string, InvocationMode>,
): Array<{ resource: string; operation: string; from: InvocationMode; to: InvocationMode }> {
  const changes: Array<{ resource: string; operation: string; from: InvocationMode; to: InvocationMode }> = [];
  Object.entries(held).forEach(([resource, operations]) => {
    operations.forEach((operation) => {
      const to = draft[`${resource}:${operation}`];
      if (!to) return;
      const policy = policyOf(resource, operation);
      const from: InvocationMode = policy?.mode || 'always';
      const rearm = to === 'once' && policy?.mode === 'once' && policy?.remaining === 0;
      if (to !== from || rearm) changes.push({ resource, operation, from, to });
    });
  });
  return changes;
}

/** W872: a written policy replaces its row on every copy of the Card the page holds: the listed one and the
 *  focused read. The editor shows the focused read when the revisions are equal, and a policy write does not
 *  move card_revision. */
export function withInvocationPolicy<T extends { access_id: string; invocation_policies?: P[] },
  P extends { policy_id: string; authority: { access_id: string } }>(item: T, policy: P): T {
  if (item.access_id !== policy.authority.access_id) return item;
  const policies = (item.invocation_policies || []).filter((existing) => existing.policy_id !== policy.policy_id);
  return { ...item, invocation_policies: [...policies, policy] };
}


/** W872 (live, 2026-10-15): the Hub refused the Save because an earlier change to a policy is not settled yet. */
export function undecidedPolicyMessage(operations: string[]): string {
  const named = operations.length ? operations.join(', ') : 'this permission';
  return `An earlier change to ${named} is not settled yet; nothing was saved. Your draft is kept; try Save again in a moment.`;
}
