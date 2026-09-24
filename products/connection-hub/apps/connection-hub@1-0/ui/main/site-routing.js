const DEFAULT_WIDGET_ALIAS = 'connections_settings'

// The standalone shell and the widget are separate documents. Forward only
// the query fields the widget's direct-link contract accepts; unrelated site
// parameters (including credentials) stay outside the iframe URL.
export const WIDGET_QUERY_PARAMS = [
  'tab',
  'view',
  'mode',
  'surface',
  'claim_challenge',
  'claimChallenge',
  'pending_agent_grant',
  'agent_client_id',
  'manual_access_id',
  'control_card_id',
  'access_id',
  'project_ref',
  'target_subject',
  'oauth_consent',
  'resource',
  'claims',
  'namespace',
  'operation',
  'outer_operation',
  'invocation_policy',
  'invocation_change_id',
  'request_bound',
  'request_digest',
  'request_approval_ticket',
  'request_card_revision',
  'request_authority_revision',
  'approval_application_id',
  'account_id',
  'account_claim',
  'provider_id',
  'connector_app_id',
  'agent_resource',
  'agent_claims',
  'provider',
  'tiers',
]

export function buildWidgetUrl({
  origin,
  tenant,
  project,
  applicationId,
  widgetAlias = DEFAULT_WIDGET_ALIAS,
  search = '',
  hash = '',
}) {
  const url = new URL([
    '/api/integrations/bundles',
    encodeURIComponent(String(tenant || '')),
    encodeURIComponent(String(project || '')),
    encodeURIComponent(String(applicationId || '')),
    'widgets',
    encodeURIComponent(String(widgetAlias || DEFAULT_WIDGET_ALIAS)),
  ].join('/'), origin)
  const query = new URLSearchParams(search)
  for (const key of WIDGET_QUERY_PARAMS) {
    const value = query.get(key)
    if (value) url.searchParams.set(key, value)
  }
  const cleanHash = String(hash || '').replace(/^#/, '').trim()
  if (cleanHash && !url.searchParams.has('tab')) url.searchParams.set('tab', cleanHash)
  return url.toString()
}
