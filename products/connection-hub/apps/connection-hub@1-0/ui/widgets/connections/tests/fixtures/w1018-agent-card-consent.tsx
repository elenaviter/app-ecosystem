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
const SERVICE = 'https://service.example/mcp'
const MEMORIES = '*/api/integrations/bundles/*/*/kdcube-services@1-0/public/mcp/memories*'
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
  resource_grants: { [NAMED]: ['named_services:use', 'slides:read'], [SERVICE]: ['records:read'] },
  resource_operations: { [NAMED]: ['named_services_call'], [SERVICE]: ['records_list'] },
  named_service_operations: { [NAMED]: { slides: ['object.list'] } },
  catalog_row_by_resource: { [NAMED]: NAMED }, account_scope: bound, properties: {}, composition_mode: '',
  control_card: {
    state: 'active', ...(variant === 'fail-closed' ? { fail_closed: true } : {}), binding: { issuer_kind: 'agent_descriptor', issuer_ref: 'workspace', label: 'Descriptor ceiling' },
    // An exact Control snapshot, as the runtime composes it: records_export is inside, records_purge is not.
    control_authority: {
      named_service_operations: { [NAMED]: { slides: ['provider.about', 'object.list', 'object.upsert'], sheets: ['values.update'] } },
      resource_grants: { [NAMED]: ['named_services:use', 'slides:read', 'slides:write'], [SERVICE]: ['records:read'], [MEMORIES]: ['memories:read'] },
      resource_operations: { [NAMED]: ['named_services_call'], [SERVICE]: ['records_list', 'records_export'] },
      account_scope: {},
      properties: { 'connection_hub.control_snapshot': { schema: 'connection_hub.control_snapshot.v1', mode: 'exact', state: 'exact', basis_catalog_version: 'cat-1' } },
    },
  },
})
let bound: Record<string, Record<string, string[]>> = variant === 'named-incapable-bound'
  ? { google: { google_acct_1: ['slides:read'] } } : {}
const resources = [{
  resource: MEMORIES, label: 'Memories', grants: ['memories:read', 'memories:write'], operations: [],
}, {
  resource: SERVICE, label: 'Records service', grants: ['records:read'],
  operations: [{ name: 'records_list', grants: ['records:read'] }, { name: 'records_export', grants: ['records:read'] }, { name: 'records_purge', grants: ['records:read'] }],
}, {
  resource: NAMED, label: 'Named services', grants: ['named_services:use', 'slides:read', 'slides:write'], operations: [],
  named_services: [{
    namespace: 'slides', label: 'Presentations',
    tools: Object.fromEntries(OPS.map((op) => [op, { operation: op, grants: [grantOf(op)] }])),
    connected_accounts: [{ provider_id: 'google', provider_label: 'Google', claims: ['slides:read', 'slides:write'],
      claims_by_operation: Object.fromEntries(OPS.map((op) => [op, [grantOf(op)]])) }],
  }, {
    // A Sheets write needs two claims on one Google account, as the shipped productivity_sheets descriptor declares.
    namespace: 'sheets', label: 'Spreadsheets',
    tools: { 'values.update': { operation: 'values.update', grants: ['sheets:write'] } },
    connected_accounts: [{ provider_id: 'google', provider_label: 'Google', claims: ['sheets:read', 'sheets:write'],
      claims_by_operation: { 'values.update': ['sheets:read', 'sheets:write'] } }],
  }],
}]
const google = (id: string, email: string, claims = ['slides:read', 'slides:write', 'sheets:read', 'sheets:write']) => ({
  account_id: id, provider_id: 'google', display_name: email, email, claims, status: 'active', has_credential: true })
// W34: 'extras-missing' holds slides:read only, so an added object.upsert needs slides:write nobody holds;
// 'extras-none' has no Google account at all.
const accounts = variant === 'extras-none' ? []
  : variant.startsWith('extras-missing') ? [google('google_acct_1', 'owner@example.test', ['slides:read'])]
  : variant === 'choose' ? [google('google_acct_1', 'owner@example.test'), google('google_acct_2', 'work@example.test')]
  : variant === 'named-incapable-bound' ? [google('google_acct_1', 'owner@example.test'), google('google_acct_ro', 'readonly@example.test', ['drive:read'])]
  : variant === 'two-claims' ? [google('google_acct_1', 'owner@example.test'), google('google_acct_ro', 'readonly@example.test', ['sheets:read'])]
  : [google('google_acct_1', 'owner@example.test')]
const calls: Array<{ operation: string, data: Record<string, unknown> }> = []
;(window as unknown as Record<string, unknown>).__calls = calls
// W34: a save-then-request leaves this page; the calls made before it are kept for the test to read.
try {
  ;(window as unknown as Record<string, unknown>).__previous = JSON.parse(sessionStorage.getItem('w34-calls') || '[]')
  sessionStorage.removeItem('w34-calls')
} catch { /* no storage: the test reads nothing */ }
const remember = () => { try { sessionStorage.setItem('w34-calls', JSON.stringify(calls)) } catch { /* ignore */ } }

setConnectionsCallOperation(async (_method, operation, data) => {
  calls.push({ operation, data: JSON.parse(JSON.stringify(data || {})) })
  remember()
  if (operation === 'delegated_access_list') {
    const old = { ...card(), access_id: 'agent-old-revoked', state: 'revoked' }
    return { ok: true, platform_user_id: 'person-1', items: variant === 'inactive-id' ? [old, card()] : [card()],
      grant_options: [{ grant: 'named_services:use' }, { grant: 'slides:read' }, { grant: 'slides:write' }], resources } as never
  }
  if (operation === 'delegated_to_kdcube_catalog') {
    return { ok: true, providers: { google: { label: 'Google', claims: ['slides:read', 'slides:write'] } }, accounts } as never
  }
  if (operation === 'delegated_agent_grant_create') {
    const payload = data as Record<string, unknown>
    if (variant === 'refused' || variant.endsWith('-refused')) {
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
if (variant === 'inactive-id') link.access_id = 'agent-old-revoked'
if (variant === 'claims-only') Object.assign(link, { resource: MEMORIES, claims: 'memories:read', account_claim: '' })
if (variant === 'named-unavailable') Object.assign(link, { account_id: 'google_acct_9', account_provider_id: 'google' })
if (variant === 'named-incapable-bound') Object.assign(link, { account_id: 'google_acct_ro', account_provider_id: 'google' })
if (variant === 'two-claims') Object.assign(link, { account_claim: '', namespace: 'sheets', operation: 'values.update' })
// An older hosted link without the Card-review marker, for an operation outside the agent's Control:
// a hosted caller always gets Card review (operator: "W1018 i am sure all must go via card").
if (variant === 'pending-outside' || variant === 'markerless') delete link.agent_card_consent
if (variant === 'pending-outside') Object.assign(link, { namespace: 'slides', operation: 'provider.capabilities' })
// An external client's pending request keeps the legacy pending page.
if (variant === 'external') { delete link.agent_card_consent; Object.assign(link, { agent_client_id: 'dcr-cli', resource: SERVICE, claims: 'records:read', account_claim: '' }) }
if (variant === 'provider') link.account_provider_id = 'google'
// Outer (MCP/REST) operations keep the pending page's once/always contract inside the Card approval.
if (variant === 'outer-choose') Object.assign(link, { resource: SERVICE, account_claim: '', outer_operation: 'records_export', invocation_policy: 'choose', invocation_change_id: 'consent-9' })
if (variant === 'outer-bound') Object.assign(link, { resource: SERVICE, account_claim: '', outer_operation: 'records_export', invocation_policy: 'once', invocation_change_id: 'consent-10',
  request_bound: '1', request_digest: 'd1', request_approval_ticket: 'ticket-1', request_card_revision: '7', request_authority_revision: 'r3', approval_application_id: 'app-1' })
if (variant === 'outer-outside') Object.assign(link, { resource: SERVICE, account_claim: '', outer_operation: 'records_purge', invocation_policy: 'choose', invocation_change_id: 'consent-11' })
// variant=url: the SDK account-binding link opens the page itself; the request is in the URL, not in props.
createRoot(document.getElementById('root')!).render(
  <Provider store={store}><DelegatedAccessPanel openParams={variant === 'url' ? undefined : link} /></Provider>,
)
