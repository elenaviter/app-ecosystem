// W587 release gate: the real DelegatedAccessPanel and store, with the Hub
// answered by a counting host bridge. The test reads window.__cardReads to see
// how many times each Card-read operation ran per step.
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'

import { setConnectionsCallOperation } from '../../src/api/client'
import { store } from '../../src/app/store'
import { DelegatedAccessPanel } from '../../src/features/delegatedAccess/DelegatedAccessPanel'
import { loadDelegatedAccess } from '../../src/features/delegatedAccess/delegatedAccessSlice'
import '../../src/styles.css'

const CARD_READS = new Set(['control_card_get', 'project_control_card_get', 'project_person_control_get', 'project_agent_card_get'])
const calls: Record<string, number> = {}
const params = new URLSearchParams(window.location.search)
const scenario = params.get('scenario') || 'project'
const PROJECT_REF = 'work:project:maintenance'

function control(revision: number): Record<string, unknown> {
  const base: Record<string, unknown> = {
    access_id: scenario === 'person' ? 'control-person-1' : 'control-project-1',
    source: 'control', card_kind: 'control', state: 'active', label: 'Maintenance Control',
    client_id: '', grantor_subject: 'project:maintenance', card_revision: revision,
    catalog_version: 'catalog-10-04', created_at: 1_759_700_000, expires_at: 0,
    resource_grants: {}, resource_operations: {}, account_scope: {}, properties: {},
    composition_mode: 'and', issuer_kind: 'project', issuer_ref: PROJECT_REF,
  }
  if (scenario === 'person') {
    base.properties = { 'connection_hub.project_person_control': {
      schema: 'connection_hub.project_person_control.v1', project_ref: PROJECT_REF, target_subject: 'person-1' } }
  } else {
    Object.assign(base, { via: 'project_admin', can_edit: true, project_ref: PROJECT_REF })
  }
  return base
}

setConnectionsCallOperation(async (_method, operation) => {
  calls[operation] = (calls[operation] || 0) + 1
  if (CARD_READS.has(operation)) {
    return { ok: true, access: control(5), viewer: { can_edit: true, role: 'admin' } } as never
  }
  if (operation === 'delegated_access_list') return { ok: true, items: [], grant_options: [], resources: [] } as never
  return { ok: true } as never
})

const w = window as unknown as Record<string, unknown>
w.__calls = calls
w.__cardReads = () => Object.entries(calls).filter(([name]) => CARD_READS.has(name)).reduce((sum, [, n]) => sum + n, 0)
w.__reloadList = () => store.dispatch(loadDelegatedAccess())

const openParams: Record<string, string> = scenario === 'person'
  ? { control_card_id: 'control-person-1', project_ref: PROJECT_REF, target_subject: 'person-1' }
  : { control_card_id: 'control-project-1', project_ref: PROJECT_REF }

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <DelegatedAccessPanel openParams={openParams} />
  </Provider>,
)
