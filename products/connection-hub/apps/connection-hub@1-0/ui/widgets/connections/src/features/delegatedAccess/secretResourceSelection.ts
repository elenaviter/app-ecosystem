export const SECRET_RESOURCE_PREFIX = 'urn:kdcube:management:secret:';

export type SecretSelectorScope = 'platform' | 'bundle' | 'user' | 'deployment';
export type SecretSelectorCoverage = 'exact' | 'prefix' | 'all';
export type SecretSelectorTarget = 'one' | 'all';
export type UserSecretArea = 'direct' | 'bundle';

export interface SecretSelectorDraft {
  tenant: string;
  project: string;
  scope: SecretSelectorScope;
  coverage: SecretSelectorCoverage;
  target: SecretSelectorTarget;
  key: string;
  bundleId: string;
  userId: string;
  userArea: UserSecretArea;
  userBundleId: string;
}

const COMPONENT = /^[A-Za-z0-9_][A-Za-z0-9_.@-]{0,511}$/;

function component(value: string, label: string): string {
  const text = String(value || '').trim();
  if (!COMPONENT.test(text) || text.includes('..') || /[*?\[\]]/.test(text)) {
    throw new Error(`${label} is invalid`);
  }
  return text;
}

function relativeKey(value: string, coverage: SecretSelectorCoverage): string {
  if (coverage === 'all') return '*';
  const text = String(value || '').trim().replace(/^platform\./, '').replace(/\.\*$/, '');
  component(text, coverage === 'prefix' ? 'Namespace' : 'Key');
  return coverage === 'prefix' ? `${text}.*` : text;
}

function resource(
  tenant: string,
  project: string,
  scope: 'platform' | 'bundle' | 'user',
  scopeId: string,
  key: string,
): string {
  const encodeSegment = (value: string) => encodeURIComponent(value).replace(/%40/gi, '@');
  return SECRET_RESOURCE_PREFIX + [tenant, project, scope, scopeId, key]
    .map(encodeSegment)
    .join(':');
}

export function buildSecretResources(draft: SecretSelectorDraft): string[] {
  const tenant = component(draft.tenant, 'Tenant');
  const project = component(draft.project, 'Project');
  if (draft.scope === 'deployment') {
    return [
      resource(tenant, project, 'platform', '_', 'platform.*'),
      resource(tenant, project, 'bundle', '*', '*'),
      resource(tenant, project, 'user', '*', '*'),
    ];
  }

  if (draft.scope === 'platform') {
    const key = draft.coverage === 'all'
      ? 'platform.*'
      : `platform.${relativeKey(draft.key, draft.coverage)}`;
    return [resource(tenant, project, 'platform', '_', key)];
  }

  if (draft.scope === 'bundle') {
    const scopeId = draft.target === 'all' ? '*' : component(draft.bundleId, 'Application ID');
    return [resource(
      tenant,
      project,
      'bundle',
      scopeId,
      relativeKey(draft.key, draft.coverage),
    )];
  }

  const userId = draft.target === 'all' ? '*' : component(draft.userId, 'User ID');
  if (draft.target === 'all' && draft.userArea === 'bundle') {
    throw new Error('Choose one user before selecting one user application');
  }
  const scopeId = draft.userArea === 'bundle'
    ? `${userId}~${component(draft.userBundleId, 'Application ID')}`
    : userId;
  return [resource(
    tenant,
    project,
    'user',
    scopeId,
    relativeKey(draft.key, draft.coverage),
  )];
}

export function isSecretResource(value: string): boolean {
  return String(value || '').startsWith(SECRET_RESOURCE_PREFIX);
}

export function isBroadSecretResource(value: string): boolean {
  if (!isSecretResource(value)) return false;
  const parts = value.slice(SECRET_RESOURCE_PREFIX.length).split(':');
  if (parts.length !== 5) return true;
  try {
    const scopeId = decodeURIComponent(parts[3]);
    const key = decodeURIComponent(parts[4]);
    return scopeId === '*' || key === '*' || key.endsWith('.*');
  } catch {
    return true;
  }
}

export function secretResourceLabel(value: string): string {
  if (!isSecretResource(value)) return '';
  const parts = value.slice(SECRET_RESOURCE_PREFIX.length).split(':');
  if (parts.length !== 5) return 'Secret authority';
  try {
    const scope = decodeURIComponent(parts[2]);
    const scopeId = decodeURIComponent(parts[3]);
    const key = decodeURIComponent(parts[4]);
    if (scope === 'platform') return `Platform secrets: ${key}`;
    if (scope === 'bundle') {
      return scopeId === '*'
        ? `All application secrets: ${key}`
        : `Application ${scopeId}: ${key}`;
    }
    return scopeId === '*'
      ? `All user-owned secrets: ${key}`
      : `User ${scopeId}: ${key}`;
  } catch {
    return 'Secret authority';
  }
}
