import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'

import { setConnectionsCallOperation } from '../../src/api/client'
import { store } from '../../src/app/store'
import { DelegatedAccessPanel } from '../../src/features/delegatedAccess/DelegatedAccessPanel'
import { loadDelegatedAccess } from '../../src/features/delegatedAccess/delegatedAccessSlice'
import '../../src/styles.css'

const answer = new URLSearchParams(window.location.search).get('answer') || 'ok'
const target = 'urn:kdcube:app:tenant-a:project-a:problem-board%401-0:worker'
const namedResource = 'https://example.test/mcp/named-services'
const policy = {
  schema: 'connection_hub.agent_capability_policy.v1',
  resource: target,
  capabilities: { tools: ['tool.old', 'tool.new'] },
}
const card = {
  access_id: 'descriptor-control-1', source: 'control', card_kind: 'control', state: 'active',
  label: 'Worker controls', issuer_kind: 'kdcube_agent_descriptor', issuer_ref: target,
  client_id: 'control:descriptor-control-1', grantor_subject: 'person-1', delegate_subject: 'person-1',
  card_revision: 4, catalog_version: 'cat-1', created_at: 1_760_000_000, expires_at: 1_960_000_000,
  resource_grants: { [namedResource]: ['named_services:use'] }, resource_operations: {},
  named_service_operations: { [namedResource]: { slack: ['object.list'] } }, account_scope: {},
  composition_mode: 'and',
  properties: {
    'kdcube.agent_descriptor_control': {
      schema: 'connection_hub.agent_descriptor_control.v1', resource: target,
    },
    'kdcube.agent_capability_authority': policy,
    'kdcube.agent_capability_defaults': {
      ...policy, capabilities: { tools: ['tool.old'] },
    },
  },
}
const calls: Array<{ operation: string, data: Record<string, unknown> }> = []
;(window as unknown as Record<string, unknown>).__calls = calls
;(window as unknown as Record<string, unknown>).__card = card

setConnectionsCallOperation(async (_method, operation, data) => {
  calls.push({ operation, data: JSON.parse(JSON.stringify(data || {})) })
  if (operation === 'delegated_access_list') {
    return { ok: true, platform_user_id: 'person-1', items: [card], resources: [{
      resource: namedResource, label: 'Named services', grants: ['named_services:use'],
      operations: [], named_services: [{ namespace: 'slack', operations: ['object.list'] }],
    }] } as never
  }
  if (operation === 'control_card_get') {
    return { ok: true, access: card } as never
  }
  if (operation === 'delegated_access_update') {
    if (answer === 'refused') return {
      ok: false, status: 400, error: 'descriptor_selection_refused', message: 'Selection refused',
    } as never
    if (answer === 'stale') return {
      ok: false, status: 409, error: 'delegated_card_revision_conflict', message: 'Card changed',
    } as never
    return { ok: true, card_revision: 5, access: { ...card, card_revision: 5, ...data } } as never
  }
  return { ok: true } as never
})

store.dispatch(loadDelegatedAccess())
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <DelegatedAccessPanel openParams={{ access_id: card.access_id }} />
  </Provider>,
)
