/**
 * Filtering of the granted-access cards on the "Delegated by KDCube" tab.
 *
 * Every card is one caller's authority, so the list hides what does not match
 * and never reorders by a guessed relevance: a card that should be revoked must
 * not sit at rank twelve. Matching is plain text over the fields the user
 * ticked, plus exact settings for the caller kind, the expiry state, and two
 * date windows (when the card was granted, when its credential expires).
 *
 * This module is pure so the panel only wires state to it and the rules can be
 * read in one place, together with the explainer that describes them.
 */
import type { DelegatedAccessRecord } from '../../api/types';

export type GrantSearchField = 'name' | 'app' | 'client' | 'door';
export type GrantKind = 'any' | 'agent' | 'oauth' | 'manual';
export type GrantState = 'any' | 'active' | 'expiring' | 'expired';
export type GrantSort = 'newest' | 'expiring' | 'name';

export interface GrantFilter {
  /** Free text, matched case-insensitively as a substring. */
  query: string;
  /** Which card fields the text is matched against. */
  fields: GrantSearchField[];
  kind: GrantKind;
  state: GrantState;
  /** `yyyy-mm-dd` bounds, inclusive whole days in the viewer's time zone. */
  grantedFrom: string;
  grantedTo: string;
  expiresFrom: string;
  expiresTo: string;
  sort: GrantSort;
}

export const ALL_SEARCH_FIELDS: GrantSearchField[] = ['name', 'app', 'client', 'door'];

export const DEFAULT_GRANT_FILTER: GrantFilter = {
  query: '',
  fields: ALL_SEARCH_FIELDS,
  kind: 'any',
  state: 'any',
  grantedFrom: '',
  grantedTo: '',
  expiresFrom: '',
  expiresTo: '',
  sort: 'newest',
};

/** "Expiring" means the credential stops working within this many seconds. */
export const EXPIRING_SOON_SECONDS = 7 * 24 * 3600;

export interface AgentIdentity {
  agent: string;
  app: string;
}

/** What the panel knows that a record does not carry itself. */
export interface GrantFilterContext {
  /** Unix seconds. */
  now: number;
  /** Short alias of a door resource (`.../mcp/productivity` -> `productivity`). */
  doorAlias: (resource: string) => string;
  /** The catalog label of a door resource, or ''. */
  doorLabel: (resource: string) => string;
  /** Human parts of an agent client id, or null for other callers. */
  parseAgent: (clientId: string) => AgentIdentity | null;
}

export type RecordKind = Exclude<GrantKind, 'any'>;
export type RecordState = Exclude<GrantState, 'any'>;

/** Who holds the credential: a hosted agent's grant, an OAuth-connected app,
 *  or a token issued here for the user's own script. Mirrors the badge the
 *  card shows. */
export function recordKind(record: DelegatedAccessRecord): RecordKind {
  if (record.source === 'agent') return 'agent';
  if (record.source === 'oauth') return 'oauth';
  return 'manual';
}

/** The card's expiry read against now. A card with no recorded expiry counts
 *  as active: the UI shows "expires unknown" and cannot claim more. */
export function recordState(record: DelegatedAccessRecord, now: number): RecordState {
  const expires = record.expires_at;
  if (!expires) return 'active';
  if (expires <= now) return 'expired';
  if (expires - now <= EXPIRING_SOON_SECONDS) return 'expiring';
  return 'active';
}

/** Start of a `yyyy-mm-dd` day in the viewer's time zone, in seconds. */
export function dayStartSeconds(iso: string): number | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso.trim());
  if (!match) return null;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
  const seconds = Math.floor(date.getTime() / 1000);
  return Number.isFinite(seconds) ? seconds : null;
}

/** End of that day (last second), so `to` is inclusive. */
export function dayEndSeconds(iso: string): number | null {
  const start = dayStartSeconds(iso);
  return start === null ? null : start + 24 * 3600 - 1;
}

/** Whether a timestamp falls inside an optional window. A missing timestamp
 *  matches only when no bound is set, since nothing can be said about it. */
export function inWindow(seconds: number | undefined, fromIso: string, toIso: string): boolean {
  const from = fromIso ? dayStartSeconds(fromIso) : null;
  const to = toIso ? dayEndSeconds(toIso) : null;
  if (from === null && to === null) return true;
  if (!seconds) return false;
  if (from !== null && seconds < from) return false;
  if (to !== null && seconds > to) return false;
  return true;
}

/** The text of one record, per searchable field. */
export function recordSearchText(
  record: DelegatedAccessRecord,
  ctx: GrantFilterContext,
): Record<GrantSearchField, string[]> {
  const who = record.client_id ? ctx.parseAgent(record.client_id) : null;
  const doors = Object.keys(record.resource_grants || {});
  return {
    name: [record.label || ''],
    app: who ? [who.agent, who.app] : [],
    client: [record.client_id || '', record.access_id],
    door: doors.flatMap((resource) => [resource, ctx.doorAlias(resource), ctx.doorLabel(resource)]),
  };
}

function textMatches(haystacks: string[], needle: string): boolean {
  return haystacks.some((text) => text.toLowerCase().includes(needle));
}

/** The exact settings a single record must pass (text aside). */
function recordPassesSettings(record: DelegatedAccessRecord, filter: GrantFilter, ctx: GrantFilterContext): boolean {
  if (filter.kind !== 'any' && recordKind(record) !== filter.kind) return false;
  if (filter.state !== 'any') {
    const state = recordState(record, ctx.now);
    // "Active" includes a credential that is about to expire: it still works.
    const passes = filter.state === 'active' ? state !== 'expired' : state === filter.state;
    if (!passes) return false;
  }
  if (!inWindow(record.created_at, filter.grantedFrom, filter.grantedTo)) return false;
  if (!inWindow(record.expires_at, filter.expiresFrom, filter.expiresTo)) return false;
  return true;
}

function fieldsOf(filter: GrantFilter): GrantSearchField[] {
  return filter.fields.length ? filter.fields : ALL_SEARCH_FIELDS;
}

/** One flat card (connected app or manual token). */
export function recordMatches(record: DelegatedAccessRecord, filter: GrantFilter, ctx: GrantFilterContext): boolean {
  if (!recordPassesSettings(record, filter, ctx)) return false;
  const needle = filter.query.trim().toLowerCase();
  if (!needle) return true;
  const text = recordSearchText(record, ctx);
  return textMatches(fieldsOf(filter).flatMap((field) => text[field]), needle);
}

/** An agent card groups every permission row of one agent. It stays visible
 *  when any row matches, and the panel shows all of its rows, so what an agent
 *  can do keeps reading in one place. */
export function agentGroupMatches(
  clientId: string,
  records: DelegatedAccessRecord[],
  filter: GrantFilter,
  ctx: GrantFilterContext,
): boolean {
  if (filter.kind !== 'any' && filter.kind !== 'agent') return false;
  const anyRecordPasses = records.some((record) => recordPassesSettings(record, filter, ctx));
  if (!anyRecordPasses) return false;
  const needle = filter.query.trim().toLowerCase();
  if (!needle) return true;
  const who = ctx.parseAgent(clientId);
  const fields = fieldsOf(filter);
  const own: Record<GrantSearchField, string[]> = {
    name: [],
    app: who ? [who.agent, who.app] : [],
    client: [clientId],
    door: [],
  };
  const haystacks = fields.flatMap((field) => [
    ...own[field],
    ...records.flatMap((record) => recordSearchText(record, ctx)[field]),
  ]);
  return textMatches(haystacks, needle);
}

function newestOf(records: DelegatedAccessRecord[]): number {
  return records.reduce((max, record) => Math.max(max, record.created_at || 0), 0);
}

/** Soonest expiry of a group; Infinity when none is recorded, so those sort last. */
function soonestExpiryOf(records: DelegatedAccessRecord[]): number {
  return records.reduce((min, record) => (record.expires_at ? Math.min(min, record.expires_at) : min), Infinity);
}

export function compareRecords(sort: GrantSort, a: DelegatedAccessRecord, b: DelegatedAccessRecord): number {
  if (sort === 'expiring') return soonestExpiryOf([a]) - soonestExpiryOf([b]);
  if (sort === 'name') return (a.label || a.access_id).localeCompare(b.label || b.access_id, undefined, { sensitivity: 'base' });
  return (b.created_at || 0) - (a.created_at || 0);
}

export function compareAgentGroups(
  sort: GrantSort,
  a: [string, DelegatedAccessRecord[]],
  b: [string, DelegatedAccessRecord[]],
  ctx: GrantFilterContext,
): number {
  if (sort === 'expiring') return soonestExpiryOf(a[1]) - soonestExpiryOf(b[1]);
  if (sort === 'name') {
    const nameOf = (clientId: string) => {
      const who = ctx.parseAgent(clientId);
      return who ? `${who.agent} · ${who.app}` : clientId;
    };
    return nameOf(a[0]).localeCompare(nameOf(b[0]), undefined, { sensitivity: 'base' });
  }
  return newestOf(b[1]) - newestOf(a[1]);
}

/** How many settings differ from the defaults, text and sort aside. Shown as
 *  the count on the settings toggle so a narrowed list is never mistaken for
 *  the whole list. */
export function activeSettingCount(filter: GrantFilter): number {
  let count = 0;
  if (fieldsOf(filter).length !== ALL_SEARCH_FIELDS.length) count += 1;
  if (filter.kind !== 'any') count += 1;
  if (filter.state !== 'any') count += 1;
  if (filter.grantedFrom || filter.grantedTo) count += 1;
  if (filter.expiresFrom || filter.expiresTo) count += 1;
  return count;
}

/** True when nothing narrows the list (sort may still differ). */
export function isUnfiltered(filter: GrantFilter): boolean {
  return !filter.query.trim() && activeSettingCount(filter) === 0;
}
