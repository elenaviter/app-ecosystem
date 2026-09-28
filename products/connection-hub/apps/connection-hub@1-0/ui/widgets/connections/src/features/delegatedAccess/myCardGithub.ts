// The GitHub section of a person's project My Card (W371).
//
// A My Card is the person-owned project Card: source and issuer kind
// "project-person", issued for the project named by issuer_ref. Its GitHub
// section links the person's GitHub connection to the project (the agents
// they own use it while they attend) and holds the commit email those
// agents' commits carry. Pure helpers here; MyCardGithubSection renders.

export const MY_CARD_SOURCE = 'project-person';

export type GithubConnectionState = 'connected' | 'needs_reconnect' | 'not_connected';
export type RepositoryState = 'covered' | 'missing' | 'not_installed' | 'unknown';

export interface GithubConnectHint {
  provider_id: string;
  connector_app_id: string;
  claims: string[];
}

export interface GithubOwnerCoverage {
  owner: string;
  installed: boolean | null;
  install_url?: string;
  repositories: Array<{ repository: string; state: RepositoryState }>;
}

export interface GithubKeyStatus {
  ok?: boolean;
  error?: string;
  message?: string;
  project_ref?: string;
  connection_state?: GithubConnectionState;
  account_id?: string;
  login?: string;
  commit_email?: string;
  refresh_expires_at?: number;
  reason?: string;
  connect?: GithubConnectHint;
  owners?: GithubOwnerCoverage[];
  coverage_error?: string;
  candidates?: Array<{ account_id: string; login?: string }>;
}

export interface OperationRequest {
  operation: string;
  data: Record<string, unknown>;
}

interface CardLike {
  source?: string;
  issuer_kind?: string;
  issuer_ref?: string;
}

export function isMyCard(item: CardLike | null | undefined): boolean {
  return Boolean(
    item
      && item.source === MY_CARD_SOURCE
      && item.issuer_kind === MY_CARD_SOURCE
      && (item.issuer_ref || '').trim(),
  );
}

export function myCardProjectRef(item: CardLike): string {
  return isMyCard(item) ? (item.issuer_ref || '').trim() : '';
}

// Repositories come from the link that opened the Card (the board's Part 2
// step passes the project's GitHub repositories); owner/name only.
export function repositoriesFromSearch(search: string): string[] {
  const raw = new URLSearchParams(search).get('repositories') || '';
  const out: string[] = [];
  for (const part of raw.split(',')) {
    const value = part.trim().replace(/\.git$/i, '').replace(/^\/+|\/+$/g, '');
    if (/^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9._-]{1,100}$/.test(value) && !out.includes(value)) {
      out.push(value);
    }
  }
  return out;
}

export function githubStatusRequest(projectRef: string, repositories: string[] = []): OperationRequest {
  const data: Record<string, unknown> = { project_ref: projectRef };
  if (repositories.length) data.repositories = repositories;
  return { operation: 'project_person_github_key_status', data };
}

export function githubLinkRequest(projectRef: string, accountId = ''): OperationRequest {
  const data: Record<string, unknown> = { project_ref: projectRef };
  if (accountId) data.account_id = accountId;
  return { operation: 'project_person_github_key_link', data };
}

export function githubUnlinkRequest(projectRef: string): OperationRequest {
  return { operation: 'project_person_github_key_unlink', data: { project_ref: projectRef } };
}

export function commitEmailRequest(projectRef: string, email: string): OperationRequest {
  return { operation: 'project_person_commit_email_set', data: { project_ref: projectRef, email: email.trim() } };
}

export function githubStatusLine(status: GithubKeyStatus | null): { tone: 'ok' | 'warn' | 'off'; text: string } {
  const state = status?.connection_state;
  if (state === 'connected') {
    return { tone: 'ok', text: `Connected as ${status?.login || 'your GitHub account'}` };
  }
  if (state === 'needs_reconnect') {
    return { tone: 'warn', text: `GitHub needs reconnecting${status?.login ? ` (${status.login})` : ''}` };
  }
  return { tone: 'off', text: 'Not connected for this project' };
}

export function repositoryStateLabel(state: RepositoryState): string {
  switch (state) {
    case 'covered':
      return 'covered';
    case 'missing':
      return 'the app is not on this repository';
    case 'not_installed':
      return 'the app is not installed on this owner';
    default:
      return 'could not be checked';
  }
}

export function ownerAction(owner: GithubOwnerCoverage): { label: string; url: string } | null {
  if (!owner.install_url) return null;
  if (owner.installed === false) return { label: `Install the app on ${owner.owner}`, url: owner.install_url };
  if (owner.repositories.some((row) => row.state === 'missing')) {
    return { label: `Add the repositories on ${owner.owner}`, url: owner.install_url };
  }
  return null;
}

// The connected GitHub accounts a person could link: those of the provider
// the status names (the deployment's GitHub App provider).
export function linkableAccounts<T extends { provider_id: string }>(
  accounts: T[],
  status: GithubKeyStatus | null,
): T[] {
  const providerId = status?.connect?.provider_id || '';
  return providerId ? accounts.filter((account) => account.provider_id === providerId) : [];
}
