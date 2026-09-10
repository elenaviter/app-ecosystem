/**
 * The platform-auth constructs the resolver supports, as YAML templates the
 * editor opens with. Each is one provider block for the platform authority;
 * the mixes are the same blocks pointing at each other (a session login on
 * a Cognito provider that trusts several pools). Values known from the
 * current platform are filled in; the rest are placeholders in angle
 * brackets the administrator replaces. Secret-bearing entries are never
 * invented here: the comment says where to copy them from.
 */
import type { AuthoritiesDescribeResult } from '../../api/types';

export interface AuthConstruct {
  id: string;
  title: string;
  /** One sentence on when this is the right shape. */
  summary: string;
  /** What assembly.yaml's auth.type says when this provider is the platform's sign-in. */
  authType: 'cognito' | 'bundle' | 'simple';
  yaml: (facts: ConstructFacts) => string;
  defaultProviderId: string;
}

export interface ConstructFacts {
  authorityId: string;
  region: string;
  userPoolId: string;
  appClientId: string;
  hostedUiDomain: string;
  cognitoProviderId: string;
}

export function constructFacts(described: AuthoritiesDescribeResult | null): ConstructFacts {
  const authority = described?.authorities?.find((row) => row.platform) || described?.authorities?.[0];
  const cognito = authority?.providers.find((row) => (row.type || '').includes('cognito'));
  const auth = cognito?.authenticator || {};
  return {
    authorityId: authority?.authority_id || 'kdcube.platform',
    region: auth.region || '<region>',
    userPoolId: auth.user_pool_id || '<user pool id>',
    appClientId: auth.app_client_id || '<app client id>',
    hostedUiDomain: auth.hosted_ui_domain || 'https://<hosted ui domain>',
    cognitoProviderId: cognito?.provider_id || 'cognito',
  };
}

const cognitoBlock = (f: ConstructFacts, pools: boolean) => `type: ${pools ? 'multi_cognito' : 'cognito'}
enabled: true
label: <what this sign-in is called in the widget>
authenticator:
  type: cognito_id_token
  region: ${f.region}
  user_pool_id: ${f.userPoolId}
  app_client_id: ${f.appClientId}
  hosted_ui_domain: ${f.hostedUiDomain}
  jwks_cache_ttl_seconds: 86400
  # id_token and cookie: copy these two entries from the existing ${f.cognitoProviderId} provider;
  # they name the token transport and are never shown here.
${pools ? `  trusted_providers:
  - alias: ${f.userPoolId === '<user pool id>' ? '<alias of the first pool>' : 'primary'}
    kind: cognito
    region: ${f.region}
    user_pool_id: ${f.userPoolId}
    app_client_id: ${f.appClientId}
    hosted_ui_domain: ${f.hostedUiDomain}
  - alias: <alias of another pool>
    kind: cognito
    region: <region>
    user_pool_id: <user pool id>
    app_client_id: <app client id>
    hosted_ui_domain: https://<hosted ui domain>
` : ''}`;

export const AUTH_CONSTRUCTS: AuthConstruct[] = [
  {
    id: 'cognito',
    title: 'Cognito, one pool',
    summary: 'The browser runs the OIDC client against one Cognito user pool; the platform verifies the tokens it presents.',
    authType: 'cognito',
    defaultProviderId: 'cognito',
    yaml: (f) => cognitoBlock(f, false),
  },
  {
    id: 'multi-cognito',
    title: 'Cognito, several pools (mixed mode)',
    summary: 'Tokens from every pool listed under trusted_providers are accepted; the sign-in itself uses the primary pool.',
    authType: 'cognito',
    defaultProviderId: 'cognito',
    yaml: (f) => cognitoBlock(f, true),
  },
  {
    id: 'session-cognito',
    title: 'Server-side login on Cognito',
    summary: 'The platform runs the sign-in itself against a Cognito provider of this authority and keeps the session server-side; sites and clients hold no tokens.',
    authType: 'bundle',
    defaultProviderId: 'browser_session',
    yaml: (f) => `type: bundle_session_login
enabled: true
label: Platform browser session (server-held, Cognito upstream)
input:
  authenticator_ref:
    authority_id: ${f.authorityId}
    provider_id: ${f.cognitoProviderId}
  scopes:
  - openid
  - email
  - profile
  groups_claim: cognito:groups
issuer:
  type: kdcube_session_token
  ttl_seconds: 43200
  max_ttl_seconds: 604800
  return_origins:
  - https://<site origin that may send people here and take them back>
`,
  },
  {
    id: 'session-oidc',
    title: 'Server-side login on an OIDC issuer',
    summary: 'The same server-held session, signing in through any OIDC issuer declared as a provider of this authority.',
    authType: 'bundle',
    defaultProviderId: 'browser_session',
    yaml: (f) => `# Two blocks: the OIDC upstream provider, and the session login that points at it.
# Paste the first as its own provider (for example "oidc"), then this one.
#
# oidc:
#   type: oidc
#   enabled: true
#   label: <issuer name>
#   authenticator:
#     issuer: https://<issuer url>
#     client_id: <client id>
#     client_secret_ref: <bundle secret reference>
type: bundle_session_login
enabled: true
label: Platform browser session (server-held, OIDC upstream)
input:
  authenticator_ref:
    authority_id: ${f.authorityId}
    provider_id: oidc
  scopes:
  - openid
  - email
  - profile
issuer:
  type: kdcube_session_token
  ttl_seconds: 43200
  max_ttl_seconds: 604800
  return_origins:
  - https://<site origin that may send people here and take them back>
`,
  },
  {
    id: 'simple',
    title: 'Simple IDP (development only)',
    summary: 'The built-in identity store for a laptop runtime; never for a public deployment.',
    authType: 'simple',
    defaultProviderId: 'simple',
    yaml: () => `type: simple_idp
enabled: true
label: Simple IDP (development)
`,
  },
];
