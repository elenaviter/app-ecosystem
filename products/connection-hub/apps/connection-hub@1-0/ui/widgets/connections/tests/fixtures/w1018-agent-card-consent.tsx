// W1018 A mounted check: the real DelegatedAccessPanel and store; the Hub is a recording bridge.
// A hosted agent's Card holds slides object.list; its Control allows provider.about, object.list and object.upsert.
// The variant (URL ?variant=) picks the link and the connected accounts.
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'

import { setConnectionsCallOperation } from '../../src/api/client'
import { store } from '../../src/app/store'
import { DelegatedAccessPanel } from '../../src/features/delegatedAccess/DelegatedAccessPanel'
import { loadDelegatedAccess } from '../../src/features/delegatedAccess/delegatedAccessSlice'
import { loadDelegatedToKdcube } from '../../src/features/delegatedToKdcube/delegatedToKdcubeSlice'
import '../../src/styles.css'

const NAMED = '*/api/integrations/bundles/*/*/kdcube-services@1-0/public/mcp/named_services*'
const CLIENT = 'kdcube-agent:workspace@2026-03-31-13-36:main'
const OPS = ['provider.about', 'provider.capabilities', 'object.list', 'object.upsert']
const grantOf = (op: string) => (op === 'object.upsert' ? 'slides:write' : 'slides:read')
const params = new URLSearchParams(window.location.search)
const variant = params.get('variant') || 'account-only'
let revision = 7
const card = () => ({
  access_id: 'agent-0e150a469bf4edbd', source: 'agent', card_kind: 'agent', state: 'active', label: 'workspace agent',
  client_id: CLIENT, grantor_subject: 'person-1', delegate_subject: '', card_revision: revision, catalog_version: 'cat-1',
  created_at: 1_760_000_000, expires_at: 1_960_000_000,
  resource_grants: { [NAMED]: ['named_services:use', 'slides:read'] }, resource_operations: { [NAMED]: ['named_services_call'] },
  named_service_operations: { [NAMED]: { slides: ['object.list'] } },
  catalog_row_by_resource: { [NAMED]: NAMED }, account_scope: bound, properties: {}, composition_mode: '',
  control_card: {
    state: 'active', binding: { issuer_kind: 'agent_descriptor', issuer_ref: 'workspace', label: 'Descriptor ceiling' },
    control_authority: { named_service_operations: { [NAMED]: { slides: ['provider.about', 'object.list', 'object.upsert'] } }, resource_grants: { [NAMED]: ['named_services:use', 'slides:read', 'slides:write'] } },
  },
})
let bound: Record<string, Record<string, string[]>> = {}
const resources = [{
  resource: NAMED, label: 'Named services', grants: ['named_services:use', 'slides:read', 'slides:write'], operations: [],
  named_services: [{
    namespace: 'slides', label: 'Presentations',
    tools: Object.fromEntries(OPS.map((op) => [op, { operation: op, grants: [grantOf(op)] }])),
    connected_accounts: [{ provider_id: 'google', provider_label: 'Google', claims: ['slides:read', 'slides:write'],
      claims_by_operation: Object.fromEntries(OPS.map((op) => [op, [grantOf(op)]])) }],
  }],
}]
const google = (id: string, email: string) => ({ account_id: id, provider_id: 'google', display_name: email, email,
  claims: ['slides:read', 'slides:write'], status: 'active', has_credential: true })
const accounts = variant === 'choose' ? [google('google_acct_1', 'owner@example.test'), google('google_acct_2', 'work@example.test')]
  : [google('google_acct_1', 'owner@example.test')]
const calls: Array<{ operation: string, data: Record<string, unknown> }> = []
;(window as unknown as Record<string, unknown>).__calls = calls

setConnectionsCallOperation(async (_method, operation, data) => {
  calls.push({ operation, data: JSON.parse(JSON.stringify(data || {})) })
  if (operation === 'delegated_access_list') {
    return { ok: true, platform_user_id: 'person-1', items: [card()],
      grant_options: [{ grant: 'named_services:use' }, { grant: 'slides:read' }, { grant: 'slides:write' }], resources } as never
  }
  if (operation === 'delegated_to_kdcube_catalog') {
    return { ok: true, providers: { google: { label: 'Google', claims: ['slides:read', 'slides:write'] } }, accounts } as never
  }
  if (operation === 'delegated_agent_grant_create') {
    const payload = data as Record<string, unknown>
    if (variant === 'refused') {
      return { ok: false, error: 'agent_capability_selection_outside_control', status: 403,
        details: { outside: [{ resource: NAMED, namespace: 'slides', operation: 'object.upsert' }] } } as never
    }
    bound = (payload.account_scope as typeof bound) || bound
    revision += 1
    return { ok: true, access_id: card().access_id, access: card() } as never
  }
  return { ok: true } as never
})

store.dispatch(loadDelegatedAccess())
store.dispatch(loadDelegatedToKdcube())
const link: Record<string, string> = { agent_card_consent: '1', pending_agent_grant: '1', agent_client_id: CLIENT, resource: NAMED, claims: '', account_claim: 'slides:read' }
if (variant === 'operation' || variant === 'refused') Object.assign(link, { namespace: 'slides', operation: 'object.upsert', account_claim: 'slides:write' })
if (variant === 'outside') Object.assign(link, { namespace: 'slides', operation: 'provider.capabilities' })
if (variant === 'mismatch') link.access_id = 'agent-someone-else'
// The old pending pane (no Card-review marker) for an operation outside the agent's Control.
if (variant === 'pending-outside') { delete link.agent_card_consent; Object.assign(link, { namespace: 'slides', operation: 'provider.capabilities' }) }
if (variant === 'provider') link.account_provider_id = 'google'
// variant=url: the SDK account-binding link opens the page itself; the request is in the URL, not in props.
createRoot(document.getElementById('root')!).render(
  <Provider store={store}><DelegatedAccessPanel openParams={variant === 'url' ? undefined : link} /></Provider>,
)
