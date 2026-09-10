/**
 * Grouping of access cards by a trait the reader chooses. A trait is
 * something every card has: who holds it (kind), which door it enters,
 * whether it still works (state). Groups are ordered as they appear, so the
 * list's own sort order survives inside and across groups.
 */
import type { DelegatedAccessRecord } from '../../api/types';
import type { RecordState } from './grantFilter';

export type CardGroupBy = 'kind' | 'door' | 'state';

export const CARD_GROUP_OPTIONS: Array<{ id: CardGroupBy; label: string }> = [
  { id: 'kind', label: 'kind' },
  { id: 'door', label: 'door' },
  { id: 'state', label: 'state' },
];

export interface CardGroup {
  key: string;
  label: string;
  records: DelegatedAccessRecord[];
}

const KIND_LABEL: Record<string, string> = {
  agent: 'Agents',
  oauth: 'Connected apps',
  manual: 'Manual tokens',
};

const STATE_LABEL: Record<RecordState, string> = {
  active: 'Active',
  expiring: 'Expiring soon',
  expired: 'Expired',
};

export function groupCards(
  records: DelegatedAccessRecord[],
  by: CardGroupBy,
  ctx: {
    stateOf: (record: DelegatedAccessRecord) => RecordState;
    doorLabel: (record: DelegatedAccessRecord) => string;
  },
): CardGroup[] {
  const groups = new Map<string, CardGroup>();
  const add = (key: string, label: string, record: DelegatedAccessRecord) => {
    let group = groups.get(key);
    if (!group) {
      group = { key, label, records: [] };
      groups.set(key, group);
    }
    group.records.push(record);
  };
  records.forEach((record) => {
    if (by === 'kind') {
      const kind = record.source || 'manual';
      add(kind, KIND_LABEL[kind] || kind, record);
    } else if (by === 'state') {
      const state = ctx.stateOf(record);
      add(state, STATE_LABEL[state], record);
    } else {
      const door = ctx.doorLabel(record) || 'no door';
      add(door, door, record);
    }
  });
  return Array.from(groups.values());
}
