/**
 * W727: recovering a Save whose focused grant committed the Card but not its
 * invocation policy (`card_committed_policy_pending`).
 *
 * The Card already holds the operation, so a refreshed editor no longer sees
 * it as an addition. Only the exact same grant (same invocation change id,
 * same expected revision) finishes the policy: the project replays its one
 * decision and the Hub then commits the prepared policy change. Retry Save
 * therefore resumes. It sends the base Save again only when the draft's base
 * part changed since it committed, replays the pending grant unchanged, then
 * adds the operations not yet added.
 */
import type { GrantAgentAccessArgs } from './agentGrantPayload';

export const POLICY_PENDING = 'card_committed_policy_pending';

export function isPolicyPending(result: { ok?: boolean; outcome?: string } | null | undefined): boolean {
  return Boolean(result) && result?.ok === false && result?.outcome === POLICY_PENDING;
}

export function grantKey(resource: string, operation: string): string {
  return `${resource}:${operation}`;
}

export interface FocusedGrantResume {
  accessId: string;
  /** The committed base Save's arguments without the revision pin. */
  baseKey: string;
  /** Focused grants already committed with their policy (resource:operation). */
  completed: string[];
  /** The grant whose Card committed and whose policy did not: retried exactly. */
  pending: { key: string; args: GrantAgentAccessArgs } | null;
}

/** What a Save of this Card continues from. Null starts a new Save. */
export function resumeFor(
  resume: FocusedGrantResume | null,
  accessId: string,
  baseKey: string,
): { skipBase: boolean; resume: FocusedGrantResume | null } {
  if (!resume || resume.accessId !== accessId) return { skipBase: false, resume: null };
  return { skipBase: resume.baseKey === baseKey, resume };
}

/** The focused grants still to send, the pending exact retry excluded (it goes first). */
export function remainingAdditions<T extends { resource: string; operation: string }>(
  additions: T[],
  resume: FocusedGrantResume | null,
): T[] {
  const done = new Set([...(resume?.completed || []), ...(resume?.pending ? [resume.pending.key] : [])]);
  return additions.filter(({ resource, operation }) => !done.has(grantKey(resource, operation)));
}

type GrantAnswer = {
  ok?: boolean;
  outcome?: string;
  error?: string;
  message?: string;
  access?: { card_revision?: number } | null;
  managed_card_edit?: { state?: string; card_revision?: number } | null;
};

export type FocusedGrantsOutcome =
  | { state: 'done'; completed: string[] }
  | { state: 'policy_pending'; operation: string; answer: GrantAnswer; completed: string[] }
  | { state: 'refused'; operation: string; error: unknown; completed: string[] };

/**
 * The Save's focused grants, in order: the pending exact retry first, then
 * each addition not yet made, built against the pin the previous write left.
 * `send` rejects on a refusal (nothing written); a policy-pending answer
 * resolves. `progress` is updated in place and is the resume point for the
 * next Save.
 */
export async function runFocusedGrants<T extends { resource: string; operation: string }>(options: {
  progress: FocusedGrantResume;
  additions: T[];
  build: (addition: T, pin: number | null) => GrantAgentAccessArgs;
  send: (args: GrantAgentAccessArgs) => Promise<GrantAnswer>;
  pin: { current: number | null };
  pinAfter: (pin: number | null, answer: GrantAnswer) => number | null;
}): Promise<FocusedGrantsOutcome> {
  const { progress, pin } = options;
  const completed: string[] = [];
  const step = async (key: string, operation: string, args: GrantAgentAccessArgs): Promise<FocusedGrantsOutcome | null> => {
    let answer: GrantAnswer;
    try {
      answer = await options.send(args);
    } catch (error) {
      return { state: 'refused', operation, error, completed };
    }
    // A committed Card moves the pin even when its invocation policy did not commit.
    pin.current = options.pinAfter(pin.current, answer);
    if (isPolicyPending(answer)) {
      progress.pending = { key, args };
      return { state: 'policy_pending', operation, answer, completed };
    }
    if (progress.pending?.key === key) progress.pending = null;
    progress.completed.push(key);
    completed.push(operation);
    return null;
  };
  if (progress.pending) {
    const { key, args } = progress.pending;
    const stopped = await step(key, args.resourceOperations?.[0] || key, args);
    if (stopped) return stopped;
  }
  for (const addition of remainingAdditions(options.additions, progress)) {
    const stopped = await step(grantKey(addition.resource, addition.operation), addition.operation,
      options.build(addition, pin.current));
    if (stopped) return stopped;
  }
  return { state: 'done', completed };
}
