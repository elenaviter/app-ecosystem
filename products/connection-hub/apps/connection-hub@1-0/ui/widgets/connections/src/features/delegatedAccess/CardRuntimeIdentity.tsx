import type { DelegatedAccessRecord } from '../../api/types';


interface RuntimeAccountIdentity {
  vendor: string;
  accountId: string;
  email: string;
  organization: string;
}

function publicText(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function vendorLabel(value: string): string {
  if (value === 'claude-code') return 'Claude Code';
  if (value === 'codex') return 'Codex';
  return value || 'Coding runtime';
}

export function cardRuntimeIdentity(item: DelegatedAccessRecord): RuntimeAccountIdentity | null {
  const metadata = item.client_metadata || {};
  const raw = metadata.kdcube_agent_account;
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const account = raw as Record<string, unknown>;
  const accountId = publicText(account.account_id);
  if (!accountId) return null;
  return {
    vendor: vendorLabel(publicText(metadata.kdcube_agent_provider)),
    accountId,
    email: publicText(account.email),
    organization: publicText(account.organization),
  };
}

export function CardRuntimeIdentityFields({ item, owner }: {
  item: DelegatedAccessRecord;
  owner: string;
}) {
  const identity = cardRuntimeIdentity(item);
  const runtimeProvider = publicText((item.client_metadata || {}).kdcube_agent_provider);
  return <>
    <span className="card-field-label">Owner</span>
    <span className="card-field-value card-owner-identity" title={owner || 'KDCube owner unavailable'}>{owner || 'Unavailable'}</span>
    {identity ? <>
      <span className="card-field-label">Provider account</span>
      <span className="card-field-value card-runtime-identity">
        <strong>{identity.vendor} · {identity.email || identity.accountId}</strong>
        {identity.email && identity.email !== identity.accountId
          ? <code title="Vendor account ID">{identity.accountId}</code>
          : null}
        {identity.organization ? <span>{identity.organization}</span> : null}
        <small>Reported by host · identification metadata</small>
      </span>
    </> : runtimeProvider ? <>
      <span className="card-field-label">Provider account</span>
      <span className="card-field-value card-runtime-identity">
        <strong>{vendorLabel(runtimeProvider)} · Not reported</strong>
        <small>No provider account was reported by this machine.</small>
      </span>
    </> : null}
  </>;
}
