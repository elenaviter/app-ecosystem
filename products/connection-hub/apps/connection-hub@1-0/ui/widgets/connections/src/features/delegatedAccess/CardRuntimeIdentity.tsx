import type { DelegatedAccessRecord } from '../../api/types';

interface RuntimeAccountIdentity {
  vendor: string;
  accountId: string;
  email: string;
  organization: string;
}

interface RuntimeIdentityFieldsProps {
  clientMetadata?: Record<string, unknown>;
  owner: string;
  ownerTitle?: string;
  layout?: 'card' | 'facts';
}

function publicText(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function vendorLabel(value: string): string {
  if (value === 'claude-code') return 'Claude Code';
  if (value === 'codex') return 'Codex';
  return value || 'Coding runtime';
}

function runtimeAccountIdentity(
  clientMetadata?: Record<string, unknown>,
): RuntimeAccountIdentity | null {
  const metadata = clientMetadata || {};
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

export function cardRuntimeIdentity(item: DelegatedAccessRecord): RuntimeAccountIdentity | null {
  return runtimeAccountIdentity(item.client_metadata);
}

function providerAccountValue(
  clientMetadata?: Record<string, unknown>,
) {
  const identity = runtimeAccountIdentity(clientMetadata);
  const runtimeProvider = publicText((clientMetadata || {}).kdcube_agent_provider);
  if (identity) {
    return <>
      <strong>{identity.vendor} · {identity.email || identity.accountId}</strong>
      {identity.email && identity.email !== identity.accountId
        ? <code title="Vendor account ID">{identity.accountId}</code>
        : null}
      {identity.organization ? <span>{identity.organization}</span> : null}
      <small>Reported by host · identification metadata</small>
    </>;
  }
  if (!runtimeProvider) return null;
  return <>
    <strong>{vendorLabel(runtimeProvider)} · Not reported</strong>
    <small>No provider account was reported by this machine.</small>
  </>;
}

export function RuntimeIdentityFields({
  clientMetadata,
  owner,
  ownerTitle,
  layout = 'card',
}: RuntimeIdentityFieldsProps) {
  const ownerValue = owner || 'Unavailable';
  const providerAccount = providerAccountValue(clientMetadata);
  const title = ownerTitle || owner || 'KDCube owner unavailable';

  if (layout === 'facts') {
    return <>
      <dt>Owner</dt>
      <dd className="card-owner-identity" title={title}>{ownerValue}</dd>
      {providerAccount ? <>
        <dt>Provider account</dt>
        <dd className="card-runtime-identity">{providerAccount}</dd>
      </> : null}
    </>;
  }

  return <>
    <span className="card-field-label">Owner</span>
    <span className="card-field-value card-owner-identity" title={title}>{ownerValue}</span>
    {providerAccount ? <>
      <span className="card-field-label">Provider account</span>
      <span className="card-field-value card-runtime-identity">{providerAccount}</span>
    </> : null}
  </>;
}

export function CardRuntimeIdentityFields({ item, owner }: {
  item: DelegatedAccessRecord;
  owner: string;
}) {
  return (
    <RuntimeIdentityFields clientMetadata={item.client_metadata} owner={owner} />
  );
}
