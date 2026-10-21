// W918 mounted check: the real DelegatedAccessPanel and store, the Hub answered by a fake bridge. An agent Card
// whose grantor is another person holds that person's personal MCP server, which is NOT in this viewer's catalog
// (connectors are owner-scoped offers), so the row renders as a top-level resource:
//   ?viewer=other  the viewer is not the Card's owner (read-only connector, line shown, Remove kept);
//   ?viewer=owner  the viewer is the Card's owner (unchanged editor).
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'

import { setConnectionsCallOperation } from '../../src/api/client'
import { store } from '../../src/app/store'
import { DelegatedAccessPanel } from '../../src/features/delegatedAccess/DelegatedAccessPanel'
import { loadDelegatedAccess } from '../../src/features/delegatedAccess/delegatedAccessSlice'
import '../../src/styles.css'

export const CONNECTOR = 'urn:connection-hub:remote-mcp:mcp_aaaaaaaaaaaaaaaaaaaaaaaa'
const viewer = new URLSearchParams(window.location.search).get('viewer') || 'other'
const RES = 'https://board.example.test/mcp/problem_board'

const catalog = {
  resource: RES, label: 'Problem Board', grants: ['work:read'],
  operations: [{ name: 'plan.item.get', group: 'plan', grants: ['work:read'] }],
}
const agent = {
  access_id: 'agent-card-1', source: 'agent', card_kind: 'agent', label: 'Agent Card', client_id: 'agent-client',
  grantor_subject: 'user:owner', delegate_subject: 'integration:agent-1', properties: {},
  resource_grants: { [RES]: ['work:read'], [CONNECTOR]: ['external_mcp:use'] },
  resource_operations: { [RES]: ['plan.item.get'], [CONNECTOR]: ['search'] },
  account_scope: {}, catalog_version: 'catalog-1', created_at: 1_760_000_000, expires_at: 0, state: 'active',
  card_revision: 4,
}

setConnectionsCallOperation(async (_method, operation) => {
  if (operation === 'delegated_access_list') {
    return { ok: true, platform_user_id: viewer === 'owner' ? 'user:owner' : 'user:admin', items: [agent],
      grant_options: [], resources: [catalog] } as never
  }
  return { ok: true } as never
})

store.dispatch(loadDelegatedAccess())

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <DelegatedAccessPanel openParams={{ access_id: 'agent-card-1' }} />
  </Provider>,
)
