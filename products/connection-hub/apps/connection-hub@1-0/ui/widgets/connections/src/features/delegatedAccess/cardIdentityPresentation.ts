import type { DelegatedAccessRecord } from '../../api/types';

function metadataText(metadata: Record<string, unknown> | undefined, key: string): string {
  const value = metadata?.[key];
  return value === null || value === undefined ? '' : String(value).trim();
}

export function runtimeProviderLabel(provider: string): string {
  if (provider === 'claude-code') return 'Claude Code';
  if (provider === 'codex') return 'Codex';
  return provider || 'Coding runtime';
}

export function runtimeHostLabel(metadata?: Record<string, unknown>): string {
  return metadataText(metadata, 'kdcube_machine_label')
    || metadataText(metadata, 'kdcube_machine_id')
    || 'this host';
}

export interface AgentCardPresentation {
  title: string;
  subtitle: string;
}

/** Worker metadata is public correlation evidence. Lead with the agent people
 * recognize, and keep runtime software plus the stable session secondary. */
export function agentCardPresentation(
  record: Pick<DelegatedAccessRecord, 'client_metadata'>,
): AgentCardPresentation | null {
  const metadata = record.client_metadata;
  const alias = metadataText(metadata, 'kdcube_worker_alias');
  if (!alias) return null;
  const host = runtimeHostLabel(metadata);
  const provider = metadataText(metadata, 'kdcube_agent_provider');
  const session = metadataText(metadata, 'kdcube_agent_session_id');
  return {
    title: host === 'this host' ? alias : `${alias}@${host}`,
    subtitle: [
      provider ? runtimeProviderLabel(provider) : '',
      session ? `session ${session}` : '',
    ].filter(Boolean).join(' · '),
  };
}
