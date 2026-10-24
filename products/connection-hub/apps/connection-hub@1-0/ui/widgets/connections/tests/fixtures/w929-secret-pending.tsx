// W929 mounted check: the real DelegatedAccessPanel and store; the Hub is a recording bridge. The CLI's Card holds
// only the deployment resource. The catalog offers deployment management and, through its kdcube_secret selector
// row, every secret. The pending grant (the admission's resource_not_consented consent) names one concrete secret.
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'

import { setConnectionsCallOperation } from '../../src/api/client'
import { store } from '../../src/app/store'
import { DelegatedAccessPanel } from '../../src/features/delegatedAccess/DelegatedAccessPanel'
import { loadDelegatedAccess } from '../../src/features/delegatedAccess/delegatedAccessSlice'
import '../../src/styles.css'

const DEPLOYMENT = 'urn:kdcube:management:deployment:*:*'
const SECRET_SELECTOR = 'urn:kdcube:management:secret:*:*:*:*:*'
const SECRET = 'urn:kdcube:management:secret:demo-tenant:demo-project:bundle:connection-hub@1-0:connections.signing_key'
const WRITE = 'kdcube.management.secret.value.write'
const card = {
  access_id: 'oauth-cli', source: 'oauth', card_kind: 'connector', state: 'active', label: 'Connection Hub CLI',
  client_id: 'dcr-cli', grantor_subject: 'person-1', delegate_subject: '', card_revision: 1,
  catalog_version: 'cat-1', created_at: 1_760_000_000, expires_at: 1_960_000_000,
  resource_grants: { [DEPLOYMENT]: ['kdcube.management.inspect'] },
  resource_operations: { [DEPLOYMENT]: ['kdcube.management.deployment.inspect'] },
  catalog_row_by_resource: { [DEPLOYMENT]: DEPLOYMENT }, account_scope: {}, properties: {}, composition_mode: '',
}
const resources = [
  { resource: DEPLOYMENT, label: 'KDCube deployment', grants: ['kdcube.management.inspect'],
    operations: [{ name: 'kdcube.management.deployment.inspect', grants: ['kdcube.management.inspect'] }] },
  { resource: SECRET_SELECTOR, label: 'KDCube secrets', selector_type: 'kdcube_secret',
    grants: ['kdcube.management.secret.write'],
    operations: [{ name: WRITE, label: 'Write a secret value', grants: ['kdcube.management.secret.write'] }] },
]
const calls: Array<{ operation: string, data: Record<string, unknown> }> = []
;(window as unknown as Record<string, unknown>).__calls = calls

setConnectionsCallOperation(async (_method, operation, data) => {
  calls.push({ operation, data: JSON.parse(JSON.stringify(data || {})) })
  if (operation === 'delegated_access_list') {
    return { ok: true, platform_user_id: 'person-1', items: [card],
      grant_options: [{ grant: 'kdcube.management.inspect' }, { grant: 'kdcube.management.secret.write' }],
      resources } as never
  }
  if (operation === 'delegated_agent_grant_create') {
    return { ok: true, access_id: 'oauth-cli', access: { ...card, card_revision: 2 },
      invocation_policy: { mode: 'once' } } as never
  }
  return { ok: true } as never
})

store.dispatch(loadDelegatedAccess())
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <DelegatedAccessPanel openParams={{
      pending_agent_grant: '1', agent_client_id: 'dcr-cli', access_id: 'oauth-cli', resource: SECRET,
      claims: 'kdcube.management.secret.write', outer_operation: WRITE, invocation_policy: 'choose',
      invocation_change_id: 'consent-1',
    }} />
  </Provider>,
)
