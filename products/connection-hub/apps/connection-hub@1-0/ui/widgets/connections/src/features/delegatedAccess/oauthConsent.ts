import { getPublicOp, postPublicOp } from '../../api/client';
import type {
  DelegatedAccessRecord,
  DelegatedAccessResourceOperations,
  DelegatedAccessStoredNamedServices,
} from '../../api/types';

export interface OAuthConsentRequirementAccount {
  account_id: string;
  label: string;
  held_claims: string[];
}

export interface OAuthConsentProviderRequirement {
  provider_id: string;
  provider_label: string;
  connector_app_id: string;
  needed_claims: string[];
  satisfied_claims: string[];
  missing_claims: string[];
  status: 'not_connected' | 'needs_access' | 'connected';
  connect_url: string;
  accounts: OAuthConsentRequirementAccount[];
}

export interface OAuthConsentChoiceOption {
  provider_id: string;
  provider_label: string;
  connector_app_id: string;
  claims: string[];
  connected: boolean;
  connect_url: string;
}

export interface OAuthConsentDraft {
  ok: boolean;
  draft_id: string;
  catalog_version: string;
  card_revision: number;
  grantor: { subject: string; label: string };
  client: {
    client_id: string;
    client_name?: string;
    client_uri?: string;
    logo_uri?: string;
    registration_kind?: string;
    redirect_uris?: string[];
    client_metadata?: Record<string, unknown>;
  };
  trusted: boolean;
  entry_door: {
    resource: string;
    label: string;
    requested_grants: string[];
    operations: Array<{
      name: string;
      label?: string;
      description?: string;
      grants: string[];
    }>;
  };
  oauth: {
    redirect_uri: string;
    redirect_host: string;
    requested_scopes: string[];
  };
  account_requirements: {
    providers: OAuthConsentProviderRequirement[];
    choices: Array<{ label: string; options: OAuthConsentChoiceOption[] }>;
    unresolved_claims: string[];
    has_gap: boolean;
  };
  catalog_scope: {
    mode: 'full' | 'entry';
    resources: string[];
  };
  selection: {
    label: string;
    resource_grants: Record<string, string[]>;
    resource_operations: DelegatedAccessResourceOperations;
    invocation_policies: Record<string, Record<string, 'always' | 'once'>>;
    named_service_operations: DelegatedAccessStoredNamedServices;
    account_scope: NonNullable<DelegatedAccessRecord['account_scope']>;
    catalog_row_by_resource: Record<string, string>;
  };
  error?: string;
  error_description?: string;
}

export interface OAuthConsentDecision {
  ok: boolean;
  redirect_url?: string;
  error?: string;
  error_description?: string;
  message?: string;
  status?: number;
}

export function loadOAuthConsentDraft(draftId: string): Promise<OAuthConsentDraft> {
  return getPublicOp<OAuthConsentDraft>('oauth/authorize/consent/draft', {
    draft_id: draftId,
  });
}

export interface ApproveOAuthConsentArgs {
  draftId: string;
  label: string;
  resourceGrants: Record<string, string[]>;
  resourceOperations: DelegatedAccessResourceOperations;
  invocationPolicies: Record<string, Record<string, 'always' | 'once'>>;
  namedServiceOperations: DelegatedAccessStoredNamedServices;
  accountScope: NonNullable<DelegatedAccessRecord['account_scope']>;
  expectedCardRevision: number;
  expectedCatalogVersion: string;
}

export function approveOAuthConsent(args: ApproveOAuthConsentArgs): Promise<OAuthConsentDecision> {
  return postPublicOp<OAuthConsentDecision>('oauth/authorize/consent/decision', {
    draft_id: args.draftId,
    decision: 'approve',
    label: args.label,
    resource_grants: args.resourceGrants,
    resource_operations: args.resourceOperations,
    invocation_policies: args.invocationPolicies,
    named_service_operations: args.namedServiceOperations,
    account_scope: args.accountScope,
    expected_card_revision: args.expectedCardRevision,
    expected_catalog_version: args.expectedCatalogVersion,
  });
}

export function denyOAuthConsent(draftId: string): Promise<OAuthConsentDecision> {
  return postPublicOp<OAuthConsentDecision>('oauth/authorize/consent/decision', {
    draft_id: draftId,
    decision: 'deny',
  });
}

export function oauthConsentId(openParams?: Record<string, string>): string {
  const fromProps = String(openParams?.oauth_consent || '').trim();
  if (fromProps) return fromProps;
  try {
    return new URLSearchParams(window.location.search).get('oauth_consent')?.trim() || '';
  } catch {
    return '';
  }
}
