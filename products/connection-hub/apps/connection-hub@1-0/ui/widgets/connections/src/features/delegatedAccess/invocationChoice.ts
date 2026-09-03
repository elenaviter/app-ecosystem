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
  extras: { namedServiceOperations?: Record<string, string[]>; accountScope?: Record<string, Record<string, string[]>> } = {},
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
