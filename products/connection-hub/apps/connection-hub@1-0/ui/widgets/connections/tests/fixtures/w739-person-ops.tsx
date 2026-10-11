// W739 mounted check: the real DelegatedAccessPanel and store, the Hub answered by a fake bridge with the
// Problem Board catalog of Infra's W731 table (94 outer operations, the object.action aliases). A person's
// Control (?card=control) or My Card (?card=my) is opened; the browser test presses Edit and reads the DOM.
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'

import { setConnectionsCallOperation } from '../../src/api/client'
import { store } from '../../src/app/store'
import { DelegatedAccessPanel } from '../../src/features/delegatedAccess/DelegatedAccessPanel'
import { loadDelegatedAccess } from '../../src/features/delegatedAccess/delegatedAccessSlice'
import '../../src/styles.css'
import table from './w731-pb-operations.json'

const card = new URLSearchParams(window.location.search).get('card') || 'control'
const PROJECT_REF = 'work:project:maintenance'
const RES = 'https://board.example.test/mcp/problem_board'
const roleOnly = new Set(table.role_only)
const aliasRoleOnly = new Set(table.alias_role_only)
const operations = [...table.declared, ...table.absent, ...table.role_only].sort()

const catalog = {
  resource: RES, label: 'Problem Board', grants: ['work:read', 'work:write', 'work:admin'],
  person_card_operations: table.declared,
  person_member_operations: table.membership,  // W748
  operations: operations.map((name) => ({
    name, group: name.split('.')[0], grants: ['work:read'], ...(roleOnly.has(name) ? { person_card: false } : {}),
  })),
  named_services: [{
    namespace: 'work', label: 'Work', description: 'Problem Board objects',
    tools: { 'object.action': { operations: Object.fromEntries(table.aliases.map((name) => [
      name, aliasRoleOnly.has(name) ? { person_card: false } : {}])) } },
  }],
}

const held = {
  resource_grants: { [RES]: ['work:read', 'work:write'] },
  resource_operations: { [RES]: ['plan.item.update', 'project.control.get', 'work.item.save', 'review.assign'] },
  named_service_operations: { [RES]: { work: ['object.action.review.assign', 'object.action.project.role.get'] } },
  account_scope: {}, catalog_version: 'catalog-10-11', created_at: 1_760_000_000, expires_at: 0,
  composition_mode: 'and', state: 'active', card_revision: 5,
}
const control = {
  ...held, access_id: 'control-person-1', source: 'control', card_kind: 'control', label: 'Person Control',
  client_id: '', grantor_subject: 'project:maintenance', issuer_kind: 'project', issuer_ref: PROJECT_REF,
  properties: { 'connection_hub.project_person_control': {
    schema: 'connection_hub.project_person_control.v1', project_ref: PROJECT_REF, target_subject: 'person-1' } },
}
const my = {
  ...held, access_id: 'my-person-1', source: 'project-person', card_kind: 'caller', label: 'My Card',
  client_id: '', grantor_subject: 'person-1', issuer_kind: 'project-person', issuer_ref: PROJECT_REF, properties: {},
  control_card: { state: 'active', composition_mode: 'and',
    binding: { control_id: 'control-person-1', issuer_kind: 'project', issuer_ref: PROJECT_REF, control_revision: 5 } },
}

setConnectionsCallOperation(async (_method, operation) => {
  if (operation === 'project_person_control_get' || operation === 'control_card_get') {
    return { ok: true, access: control, viewer: { can_edit: true, role: 'admin' } } as never
  }
  if (operation === 'delegated_access_list') {
    return { ok: true, platform_user_id: card === 'my' ? 'person-1' : 'admin-1',
      items: card === 'my' ? [my] : [], grant_options: [], resources: [catalog] } as never
  }
  return { ok: true } as never
})

const openParams: Record<string, string> = card === 'my'
  ? { access_id: 'my-person-1' }
  : { control_card_id: 'control-person-1', project_ref: PROJECT_REF, target_subject: 'person-1' }

// The host app loads the list (and with it the catalog) on mount; this fixture does it itself.
store.dispatch(loadDelegatedAccess())

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <DelegatedAccessPanel openParams={openParams} />
  </Provider>,
)
