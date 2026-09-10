/**
 * Permission tokens read as `<service>:<verb>` (`canvas:read`,
 * `slack:files:write`, `named_services:use`). Grouping them by service turns
 * a wall of tokens into one row per service with its verbs, which is how a
 * person thinks about what they granted ("canvas: read and write").
 * Insertion order is kept, so the server's ordering survives.
 */
export interface ClaimGroupEntry {
  token: string;
  verb: string;
}

export interface ClaimGroup {
  service: string;
  claims: ClaimGroupEntry[];
}

export function splitClaim(token: string): ClaimGroupEntry & { service: string } {
  const text = String(token || '').trim();
  const at = text.indexOf(':');
  if (at <= 0) return { token: text, service: text, verb: '' };
  return { token: text, service: text.slice(0, at), verb: text.slice(at + 1) };
}

export function groupClaimsByService(tokens: string[]): ClaimGroup[] {
  const groups: ClaimGroup[] = [];
  const byService = new Map<string, ClaimGroup>();
  const seen = new Set<string>();
  tokens.forEach((raw) => {
    const { token, service, verb } = splitClaim(raw);
    if (!token || seen.has(token)) return;
    seen.add(token);
    let group = byService.get(service);
    if (!group) {
      group = { service, claims: [] };
      byService.set(service, group);
      groups.push(group);
    }
    group.claims.push({ token, verb });
  });
  return groups;
}
