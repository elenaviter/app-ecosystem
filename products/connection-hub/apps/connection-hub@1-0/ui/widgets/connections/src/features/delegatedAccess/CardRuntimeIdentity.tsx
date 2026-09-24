import type { ReactNode } from 'react';
import type { DelegatedAccessRecord } from '../../api/types';
import { runtimeHostLabel, runtimeProviderLabel } from './cardIdentityPresentation';

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

interface IdentityRow {
  label: string;
  value: ReactNode;
  valueClassName?: string;
}

function publicText(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
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
    vendor: runtimeProviderLabel(publicText(metadata.kdcube_agent_provider)),
    accountId,
    email: publicText(account.email),
    organization: publicText(account.organization),
  };
}

function providerAccountLabel(
  providerLabel: string,
  identity: RuntimeAccountIdentity | null,
): string {
  if (identity?.email) return `${providerLabel} · ${identity.email}`;
  if (identity) return providerLabel;
  return `${providerLabel} · Not reported`;
}

export function cardRuntimeIdentity(item: DelegatedAccessRecord): RuntimeAccountIdentity | null {
  return runtimeAccountIdentity(item.client_metadata);
}

function runtimeIdentityRows(
  clientMetadata: Record<string, unknown> | undefined,
  owner: string,
  ownerTitle: string,
): IdentityRow[] {
  const identity = runtimeAccountIdentity(clientMetadata);
  const provider = publicText((clientMetadata || {}).kdcube_agent_provider);
  const rows: IdentityRow[] = [{
    label: 'Owner',
    value: <span title={ownerTitle}>{owner || 'Unavailable'}</span>,
    valueClassName: 'card-owner-identity',
  }];
  if (!provider) return rows;

  const providerLabel = identity?.vendor || runtimeProviderLabel(provider);
  const host = runtimeHostLabel(clientMetadata);
  rows.push({
    label: 'Provider account',
    value: <>
      <strong>{providerAccountLabel(providerLabel, identity)}</strong>
      <small>
        {identity
          ? <>Read from the {providerLabel} login on {host}. It identifies the agent&apos;s provider account. Access comes from the owner&apos;s Card.</>
          : <>No provider account was reported by {host}.</>}
      </small>
    </>,
    valueClassName: 'card-runtime-identity',
  });
  if (identity?.accountId) {
    rows.push({
      label: 'Account ID',
      value: <code title={identity.accountId}>{identity.accountId}</code>,
      valueClassName: 'card-runtime-identity-id',
    });
  }
  if (identity?.organization) {
    rows.push({
      label: 'Organization ID',
      value: <code title={identity.organization}>{identity.organization}</code>,
      valueClassName: 'card-runtime-identity-id',
    });
  }
  return rows;
}

function IdentityField({ row, layout }: { row: IdentityRow; layout: 'card' | 'facts' }) {
  if (layout === 'facts') {
    return <>
      <dt>{row.label}</dt>
      <dd className={row.valueClassName}>{row.value}</dd>
    </>;
  }
  return <>
    <span className="card-field-label">{row.label}</span>
    <span className={`card-field-value ${row.valueClassName || ''}`.trim()}>{row.value}</span>
  </>;
}

export function RuntimeIdentityFields({
  clientMetadata,
  owner,
  ownerTitle,
  layout = 'card',
}: RuntimeIdentityFieldsProps) {
  const ownerValue = owner || 'Unavailable';
  const title = ownerTitle || owner || 'KDCube owner unavailable';
  const rows = runtimeIdentityRows(clientMetadata, ownerValue, title);
  return <>{rows.map((row) => <IdentityField key={row.label} row={row} layout={layout} />)}</>;
}

export function CardRuntimeIdentityFields({ item, owner, ownerTitle }: {
  item: DelegatedAccessRecord;
  owner: string;
  ownerTitle?: string;
}) {
  return (
    <RuntimeIdentityFields
      clientMetadata={item.client_metadata}
      owner={owner}
      ownerTitle={ownerTitle}
    />
  );
}
