// W755 mounted check: the real DelegatedAccessPanel and store; the Hub is a recording bridge. One caller Card
// (a manual token) holds `read` and `write`; the catalog offers read, write and delete. ?answer= picks the Save answer:
// ok (default), refused, unchanged, mode_required (invocation_mode_required), stale (a revision conflict),
// catalog (the catalog version is not the newest),
// lost (the first answer never arrives; the Hub had written r5, so Save pressed again is refused as stale).
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'

import { setConnectionsCallOperation } from '../../src/api/client'
import { store } from '../../src/app/store'
import { DelegatedAccessPanel } from '../../src/features/delegatedAccess/DelegatedAccessPanel'
import { loadDelegatedAccess } from '../../src/features/delegatedAccess/delegatedAccessSlice'
import '../../src/styles.css'

const answer = new URLSearchParams(window.location.search).get('answer') || 'ok'
const RES = 'https://svc.example.test/mcp'
const card = {
  access_id: 'card-1', source: 'manual', card_kind: 'automation', state: 'active', label: 'Build token',
  client_id: 'manual:card-1', grantor_subject: 'person-1', delegate_subject: 'person-1', card_revision: 4,
  catalog_version: 'cat-1', created_at: 1_760_000_000, expires_at: 1_960_000_000,
  resource_grants: { [RES]: ['svc:read'] }, resource_operations: { [RES]: ['read', 'write'] }, account_scope: {},
  properties: {}, composition_mode: '',
}
const catalog = {
  resource: RES, label: 'Service', grants: ['svc:read'],
  operations: ['read', 'write', 'delete'].map((name) => ({ name, grants: ['svc:read'] })),
}
const calls: Array<{ operation: string, data: Record<string, unknown> }> = []
;(window as unknown as Record<string, unknown>).__calls = calls

setConnectionsCallOperation(async (_method, operation, data) => {
  calls.push({ operation, data: JSON.parse(JSON.stringify(data || {})) })
  if (operation === 'delegated_access_list') {
    return { ok: true, platform_user_id: 'person-1', items: [card], grant_options: [{ grant: 'svc:read' }],
      resources: [catalog] } as never
  }
  if (operation === 'delegated_access_update') {
    const saves = calls.filter((call) => call.operation === 'delegated_access_update').length
    if (answer === 'refused') return { ok: false, status: 409, error: 'card_plan_selection_invalid', message: 'Refused by the project: card_plan_selection_invalid.' } as never
    if (answer === 'unchanged') return { ok: true, unchanged: true, card_revision: 4, access: card } as never
    if (answer === 'mode_required') return { ok: false, status: 409, error: 'invocation_mode_required', operations: [{ resource: RES, operation: 'write' }] } as never
    if (answer === 'catalog') return { ok: false, status: 409, error: 'consent_catalog_changed', message: 'catalog moved' } as never
    if (answer === 'stale') return { ok: false, status: 409, error: 'delegated_card_revision_conflict', message: 'stale' } as never
    if (answer === 'lost' && saves === 1) throw new Error('Failed to fetch')
    if (answer === 'lost') return { ok: false, status: 409, error: 'delegated_card_revision_conflict', message: 'stale' } as never
    return { ok: true, card_revision: 5, access: { ...card, card_revision: 5 } } as never
  }
  return { ok: true } as never
})

store.dispatch(loadDelegatedAccess())
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <DelegatedAccessPanel openParams={{ access_id: 'card-1' }} />
  </Provider>,
)
