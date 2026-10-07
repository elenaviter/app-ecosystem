import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const requireDependency = createRequire(import.meta.url)
const { configureStore } = requireDependency('@reduxjs/toolkit')
const ts = requireDependency('typescript')
const slicePath = fileURLToPath(new URL('../src/features/delegatedAccess/delegatedAccessSlice.ts', import.meta.url))
const clientPath = fileURLToPath(new URL('../src/api/client.ts', import.meta.url))

// Execute the authored slice and its local helpers with the real Redux runtime.
// Only the operation client is replaced: no browser, network, or server is used.
function harness(response) {
  const requests = []
  const modules = new Map()
  const client = {
    getOp: async () => { throw new Error('Unexpected read') },
    postOp: async (operation, payload) => {
      requests.push({ operation, payload })
      return typeof response === 'function' ? response() : response
    },
  }
  function load(path) {
    if (path === clientPath) return client
    if (modules.has(path)) return modules.get(path).exports
    const module = { exports: {} }
    modules.set(path, module)
    const { outputText } = ts.transpileModule(readFileSync(path, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
      fileName: path,
    })
    const localRequire = (specifier) => specifier.startsWith('.')
      ? load(resolve(dirname(path), specifier.endsWith('.ts') ? specifier : `${specifier}.ts`))
      : requireDependency(specifier)
    new Function('require', 'module', 'exports', outputText)(localRequire, module, module.exports)
    return module.exports
  }
  const slice = load(slicePath)
  const card = { access_id: 'control-1', card_revision: 7, label: 'Current Card' }
  const state = {
    ...slice.default(undefined, { type: 'test/initial' }),
    busy: false,
    error: 'Earlier notice',
    items: [card, { access_id: 'other-card', card_revision: 3 }],
    focusedCard: card,
    focusedViewer: { can_edit: true },
    issuedAccess: card,
    issuedToken: 'synthetic-token',
    issuedHeader: 'synthetic-header',
  }
  const store = configureStore({ reducer: slice.default, preloadedState: state })
  return { ...slice, store, requests, before: structuredClone(state), card }
}

const person = { kind: 'person', projectRef: 'work:project:example', targetSubject: 'person-1' }
const invitation = { kind: 'invitation', projectRef: 'work:project:example', invitationRef: 'work:invitation:example' }
const refusal = { ok: false, error: 'card_transactions_direct_write_refused', status: 409 }
const updateArgs = {
  accessId: 'control-1', label: 'Draft label',
  resourceGrants: { 'urn:example:resource': ['example:read'] },
  resourceOperations: { 'urn:example:resource': ['example.read'] },
  expectedCardRevision: 7, expectedCatalogVersion: 'catalog-1',
  projectPersonControl: person,
}
const revokeArgs = { accessId: 'control-1', expectedCardRevision: 7, projectPersonControl: person }

function assertPreserved(current, before) {
  for (const key of ['items', 'focusedCard', 'focusedViewer', 'issuedAccess', 'issuedToken', 'issuedHeader']) {
    assert.deepEqual(current[key], before[key], key)
  }
  assert.equal(current.busy, false)
}

for (const action of ['update', 'revoke']) {
  for (const status of [409, 503, undefined]) {
    test(`${action} rejects the named refusal at status ${status ?? 'absent'} and preserves the Card and banner`, async () => {
      let finish
      const response = new Promise((resolveResponse) => { finish = resolveResponse })
      const h = harness(() => response)
      const args = structuredClone(action === 'update' ? updateArgs : revokeArgs)
      const draft = structuredClone(args)
      const thunk = action === 'update' ? h.updateDelegatedAccess : h.revokeDelegatedAccess
      const dispatched = h.store.dispatch(thunk(args))
      assert.equal(h.store.getState().busy, true)
      assert.equal(h.store.getState().error, '')
      finish({ ...refusal, status })
      const result = await dispatched
      assert.equal(result.meta.requestStatus, 'rejected')
      assert.equal(result.meta.rejectedWithValue, true)
      assert.match(result.payload, /Coordinated Card changes are unavailable/)
      assert.doesNotMatch(result.payload, /card_transactions_direct_write_refused|changed while/)
      await assert.rejects(dispatched.unwrap(), (error) => error === result.payload)
      assert.equal(h.store.getState().error, result.payload)
      assertPreserved(h.store.getState(), h.before)
      assert.deepEqual(args, draft)
      assert.equal(h.requests.length, 1, 'the write is never retried')
    })
  }

  test(`${action} displays the server's human refusal message`, async () => {
    const message = 'The project could not coordinate this Card change. Try again when it is available.'
    const h = harness({ ...refusal, message })
    const thunk = action === 'update' ? h.updateDelegatedAccess : h.revokeDelegatedAccess
    const result = await h.store.dispatch(thunk(action === 'update' ? updateArgs : revokeArgs))
    assert.equal(result.meta.requestStatus, 'rejected')
    assert.equal(result.payload, message)
    assert.equal(h.store.getState().error, message)
    assertPreserved(h.store.getState(), h.before)
  })
}

test('person and invitation operations retain their existing update and revoke payloads', async () => {
  for (const coordinates of [person, invitation]) {
    const identity = coordinates.kind === 'person'
      ? { project_ref: coordinates.projectRef, target_subject: coordinates.targetSubject }
      : { project_ref: coordinates.projectRef, invitation_ref: coordinates.invitationRef, control_id: 'control-1' }
    const h = harness(refusal)
    await h.store.dispatch(h.updateDelegatedAccess({ ...updateArgs, projectPersonControl: coordinates }))
    await h.store.dispatch(h.revokeDelegatedAccess({ ...revokeArgs, projectPersonControl: coordinates }))
    assert.deepEqual(h.requests, [
      {
        operation: 'project_person_control_update',
        payload: {
          ...identity, label: updateArgs.label,
          resource_grants: updateArgs.resourceGrants, resource_operations: updateArgs.resourceOperations,
          expected_card_revision: 7, expected_catalog_version: 'catalog-1',
        },
      },
      { operation: 'project_person_control_revoke', payload: identity },
    ])
  }
})

test('ordinary terminal saves still update the Card and clear busy', async () => {
  const access = { access_id: 'control-1', card_revision: 8, label: 'Saved label' }
  const response = { ok: true, access }
  const h = harness(response)
  assert.deepEqual(await h.store.dispatch(h.updateDelegatedAccess(updateArgs)).unwrap(), response)
  const current = h.store.getState()
  assert.equal(current.busy, false)
  assert.equal(current.error, '')
  assert.deepEqual(current.items, [access, h.before.items[1]])
  assert.deepEqual(current.focusedCard, access)
  assert.deepEqual(current.issuedAccess, access)
  assert.equal(current.issuedToken, h.before.issuedToken)
  assert.equal(current.issuedHeader, h.before.issuedHeader)
})

for (const removed of [true, false]) {
  test(`terminal revoke removed=${removed} keeps successful and idempotent behavior`, async () => {
    const response = { ok: true, removed }
    const h = harness(response)
    assert.deepEqual(await h.store.dispatch(h.revokeDelegatedAccess(revokeArgs)).unwrap(), response)
    const current = h.store.getState()
    assert.equal(current.busy, false)
    assert.equal(current.error, '')
    assert.deepEqual(current.items, [h.before.items[1]])
    assert.equal(current.focusedCard, undefined)
    assert.equal(current.issuedAccess, undefined)
    assert.equal(current.issuedToken, '')
    assert.equal(current.issuedHeader, '')
  })
}

test('existing 409 conflicts retain their fulfilled response for explicit reconciliation', async () => {
  const access = { access_id: 'control-1', card_revision: 8 }
  const conflict = { ok: false, status: 409, error: 'delegated_access_precondition_failed', access }
  const update = harness(conflict)
  assert.deepEqual(await update.store.dispatch(update.updateDelegatedAccess(updateArgs)).unwrap(), conflict)
  assert.deepEqual(update.store.getState().focusedCard, access)
  assert.equal(update.store.getState().busy, false)
  const revokeConflict = { ok: false, status: 409, error: 'delegated_card_revision_conflict' }
  const revoke = harness(revokeConflict)
  assert.deepEqual(await revoke.store.dispatch(revoke.revokeDelegatedAccess(revokeArgs)).unwrap(), revokeConflict)
  assertPreserved(revoke.store.getState(), revoke.before)
  assert.match(revoke.store.getState().error, /confirm again/)
})

test('the existing save catch retains the draft before any success follow-up', () => {
  const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')
  const save = panel.slice(panel.indexOf('const saveEdit = async'), panel.indexOf('// The full delegable catalog'))
  const caught = save.slice(save.indexOf('} catch (error) {', save.indexOf('dispatch(updateDelegatedAccess(')), save.indexOf('if (!updated || updated.ok === false)'))
  assert.match(caught, /setEditActionError\([^]*?\);\s+return;/)
  assert.doesNotMatch(caught, /clearEditState\(|grantAgentAccess\(|loadDelegatedAccess\(/)
  assert.ok(save.indexOf('clearEditState();') > save.indexOf('if (!updated || updated.ok === false)'))
})
