import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import Module, { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const require = createRequire(import.meta.url)
const ts = require('typescript')
const root = fileURLToPath(new URL('../src/', import.meta.url))

// Run the checked-in TSX and Redux thunks, rather than matching their source.
for (const extension of ['.ts', '.tsx']) {
  Module._extensions[extension] = (mod, filename) => {
    const output = ts.transpileModule(readFileSync(filename, 'utf8'), {
      fileName: filename,
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
        jsx: ts.JsxEmit.ReactJSX,
      },
    })
    mod._compile(output.outputText, filename)
  }
}

globalThis.window = {
  location: {
    origin: 'https://test.example',
    pathname: '/api/integrations/bundles/t/p/b/widgets/connections_settings',
    search: '',
  },
}
globalThis.fetch = async (url) => {
  assert.match(String(url), /\/csrf$/)
  return new Response(JSON.stringify({ csrf_required: false }), { status: 200 })
}

const React = require('react')
const { renderToStaticMarkup } = require('react-dom/server')
const { configureStore } = require('@reduxjs/toolkit')
const { setConnectionsCallOperation } = require(`${root}/api/client.ts`)
const {
  default: reducer,
  loadControlCard,
  updateDelegatedAccess,
} = require(`${root}/features/delegatedAccess/delegatedAccessSlice.ts`)
const { RoleDecidedOperations } = require(`${root}/features/delegatedAccess/RoleDecidedOperations.tsx`)

const catalog = [{
  resource: 'pattern-pb',
  label: 'Problem Board',
  operations: [
    { name: 'project.people.invite', person_card: false },
    { name: 'project.control.update', person_card: false },
  ],
}]

test('authorized Card load renders admin, member, and unknown role without editable operations', async () => {
  for (const role of ['admin', 'member', 'unknown']) {
    const targetRole = role === 'unknown'
      ? { known: false, role: '', administers: false }
      : { known: true, role, administers: role === 'admin' }
    const restore = setConnectionsCallOperation(async (method, operation) => {
      assert.equal(method, 'POST')
      assert.equal(operation, 'project_person_control_get')
      return {
        ok: true,
        access: { access_id: 'card-1', source: 'control', state: 'active' },
        target_role: targetRole,
        role_decided_catalog: catalog,
        role_decided_catalog_available: true,
      }
    })
    try {
      const store = configureStore({ reducer: { delegatedAccess: reducer } })
      await store.dispatch(loadControlCard({
        controlId: 'card-1', projectRef: 'work:project:p', targetSubject: 'person-1',
      })).unwrap()
      const loaded = store.getState().delegatedAccess
      const html = renderToStaticMarkup(React.createElement(RoleDecidedOperations, {
        catalog: loaded.focusedRoleDecidedCatalog,
        catalogAvailable: loaded.focusedRoleDecidedCatalogAvailable,
        targetRole: loaded.focusedTargetRole,
      }))
      const inputs = html.match(/<input[^>]*>/g) || []
      assert.equal(inputs.length, 2)
      assert.ok(inputs.every((input) => input.includes('disabled=""')))
      assert.equal(inputs.filter((input) => input.includes('checked=""')).length,
        role === 'admin' ? 2 : 0)
      if (role === 'unknown') assert.match(html, /role is not known/)
    } finally {
      restore()
    }
  }
})

test('intercepted project-person Save excludes marked operations from its request', async () => {
  const calls = []
  const restore = setConnectionsCallOperation(async (method, operation, payload) => {
    calls.push({ method, operation, payload })
    return { ok: true }
  })
  try {
    const store = configureStore({ reducer: { delegatedAccess: reducer } })
    await store.dispatch(updateDelegatedAccess({
      accessId: 'card-1', label: 'Person Card',
      resourceGrants: { 'concrete-pb': ['work:review'] },
      resourceOperations: {
        'concrete-pb': ['review.assign', 'project.people.invite', 'project.control.update'],
      },
      roleDecidedCatalog: catalog,
      catalogRowByResource: { 'concrete-pb': 'pattern-pb' },
      projectPersonControl: {
        kind: 'person', projectRef: 'work:project:p', targetSubject: 'person-1',
      },
    })).unwrap()
    assert.equal(calls.length, 1)
    assert.equal(calls[0].operation, 'project_person_control_update')
    assert.deepEqual(calls[0].payload.resource_operations, { 'concrete-pb': ['review.assign'] })
    assert.deepEqual(calls[0].payload.resource_grants, { 'concrete-pb': ['work:review'] })
  } finally {
    restore()
  }
})
