import type { DelegatedAccessRecord } from '../../api/types';
import { cardAgentCapabilitySelection } from './agentCapabilitySelection.ts';

export interface AgentCapabilityCardLifecycle {
  state: 'active' | 'awaiting_sync';
  badge: 'auto-renews' | 'awaiting sync';
  summary: string;
  hint: string;
}

const AGENT_DESCRIPTOR_ISSUER_KIND = 'kdcube_agent_descriptor';

/** Only descriptor-synchronized app-agent Cards inherit Control defaults.
 * Worker Agent Cards may have a linked Control ceiling, but resetting them to
 * that ceiling would widen the authority their owner actually granted. */
export function isAgentCapabilityCard(record: DelegatedAccessRecord): boolean {
  return record.source === 'agent'
    && record.control_card?.binding?.issuer_kind === AGENT_DESCRIPTOR_ISSUER_KIND
    && cardAgentCapabilitySelection(record.properties) !== null;
}

/** Owner-facing lifecycle language for the auto-renewing capability Card. */
export function agentCapabilityCardLifecycle(
  record: DelegatedAccessRecord,
  now: number,
  leaseEndLabel: string,
): AgentCapabilityCardLifecycle | null {
  if (!isAgentCapabilityCard(record)) return null;
  const lapsed = Boolean(record.expired)
    || Boolean(record.expires_at && record.expires_at <= now);
  const revision = Math.max(0, record.card_revision || 0);
  const timing = leaseEndLabel
    ? `${lapsed ? 'ended' : 'through'} ${leaseEndLabel}`
    : (lapsed ? 'ended' : 'active');
  return {
    state: lapsed ? 'awaiting_sync' : 'active',
    badge: lapsed ? 'awaiting sync' : 'auto-renews',
    summary: `Card revision ${revision} · capability lease ${timing}`,
    hint: lapsed
      ? 'This Card currently grants nothing. The next agent message renews it before tools are projected; the selected capability base and stable Card ID are preserved.'
      : 'This Agent Card credential renews automatically when the agent sends a message. If its inactivity lease lapses, it grants nothing until the next message; that sync preserves the selected capability base and stable Card ID before projecting tools.',
  };
}
