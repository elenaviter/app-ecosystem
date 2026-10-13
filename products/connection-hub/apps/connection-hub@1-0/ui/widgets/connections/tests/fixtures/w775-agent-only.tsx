// W775 mounted check: the real DelegatedAccessPanel and store, the Hub answered by a fake bridge with the
// Problem Board catalog of Infra's W731 table plus the board's `person_agent_operations`. Two Cards under an
// active Control whose authority holds only the person-decided operations, so an agent-only operation is
// outside the Control (the old editor showed it amber "inactive"):
//   ?card=my     a person's own My Card (folds the agent-only operations, no Control warning);
//   ?card=agent  an agent's Card (unchanged: the agent-only operations are its own and stay warned).
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'

import { setConnectionsCallOperation } from '../../src/api/client'
import { store } from '../../src/app/store'
import { DelegatedAccessPanel } from '../../src/features/delegatedAccess/DelegatedAccessPanel'
import { loadDelegatedAccess } from '../../src/features/delegatedAccess/delegatedAccessSlice'
import '../../src/styles.css'
import table from './w731-pb-operations.json'

export const AGENT = ['assignment.report', 'attachment.request_upload', 'control.acknowledge', 'control.discard_complete',
  'control.pull', 'control.refuse', 'control.worker_settle', 'event.publish', 'journal.view.fail', 'journal.view.publish',
  'mail.reconciliation.publish', 'mail.route', 'note.view.fail', 'note.view.publish', 'operation.receipt.get',
  'plan.nodes.publish', 'project.coordinator.note.write', 'project.file.edit.result', 'project.github.use',
  'project.role.handover.decide', 'session.resume.fail', 'session.resume.publish', 'worker.heartbeat', 'worker.publish',
  'workspace.shared_write.clear', 'workspace.shared_write.publish']

const card = new URLSearchParams(window.location.search).get('card') || 'my'
const PROJECT_REF = 'work:project:maintenance'
const RES = 'https://board.example.test/mcp/problem_board'
const roleOnly = new Set(table.role_only)
const operations = [...table.declared, ...table.absent, ...table.role_only].sort()

const catalog = {
  resource: RES, label: 'Problem Board', grants: ['work:read', 'work:write', 'work:admin'],
  person_card_operations: table.declared,
  person_member_operations: table.membership,
  person_agent_operations: AGENT,  // W775
  operations: operations.map((name) => ({
    name, group: name.split('.')[0], grants: ['work:read'], ...(roleOnly.has(name) ? { person_card: false } : {}),
  })),
}

// The Control allows only what a person's Card decides: every agent-only operation is outside it.
const controlAuthority = {
  resource_grants: { [RES]: ['work:read'] },
  resource_operations: { [RES]: [...table.declared] },
}
const held = {
  resource_grants: { [RES]: ['work:read'] },
  resource_operations: { [RES]: ['plan.item.update', 'project.control.get', 'worker.publish', 'mail.route'] },
  account_scope: {}, catalog_version: 'catalog-10-13', created_at: 1_760_000_000, expires_at: 0,
  composition_mode: 'and', state: 'active', card_revision: 5,
}
const my = {
  ...held, access_id: 'my-person-1', source: 'project-person', card_kind: 'caller', label: 'My Card',
  client_id: '', grantor_subject: 'person-1', issuer_kind: 'project-person', issuer_ref: PROJECT_REF, properties: {},
  control_card: { state: 'active', composition_mode: 'and', control_authority: controlAuthority,
    binding: { control_id: 'control-person-1', issuer_kind: 'project', issuer_ref: PROJECT_REF, control_revision: 5 } },
}
const agent = {
  ...held, access_id: 'agent-card-1', source: 'agent', card_kind: 'agent', label: 'Agent Card',
  client_id: 'agent-client', grantor_subject: 'person-1', delegate_subject: 'integration:agent-1',
  issuer_kind: 'kdcube_agent_descriptor', issuer_ref: PROJECT_REF, properties: {},
  control_card: { state: 'active', composition_mode: 'and', control_authority: controlAuthority,
    binding: { control_id: 'control-agent-1', issuer_kind: 'kdcube_agent_descriptor', issuer_ref: PROJECT_REF, control_revision: 5 } },
}

setConnectionsCallOperation(async (_method, operation) => {
  if (operation === 'delegated_access_list') {
    return { ok: true, platform_user_id: 'person-1', items: [card === 'agent' ? agent : my], grant_options: [],
      resources: [catalog] } as never
  }
  return { ok: true } as never
})

store.dispatch(loadDelegatedAccess())

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <DelegatedAccessPanel openParams={{ access_id: card === 'agent' ? 'agent-card-1' : 'my-person-1' }} />
  </Provider>,
)
