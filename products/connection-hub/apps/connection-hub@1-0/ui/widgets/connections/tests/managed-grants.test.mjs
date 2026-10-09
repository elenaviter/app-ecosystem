import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { MANAGED_GRANT_NOTE, managedGrantsOf, withManagedGrantsAsHeld } from '../src/features/delegatedAccess/managedGrants.ts'

// W661 S5 (operator, 2026-10-09): a permission that only the application's own operation sets is
// disabled in the Card editor and never sent as a change; generic, with a synthetic application here.
const R = 'https://synthetic.example/api/app*'
const managedFor = (resource) => managedGrantsOf(resource === R ? { managed_grants: ['app:steward'] } : {})

test('a resource row declares its managed grants; none is the default', () => {
  assert.deepEqual([...managedGrantsOf({ managed_grants: ['app:steward'] })], ['app:steward'])
  assert.deepEqual([...managedGrantsOf({})], [])
  assert.deepEqual([...managedGrantsOf(undefined)], [])
  assert.equal(MANAGED_GRANT_NOTE, 'Managed by the application')
})

test('an edit keeps each managed grant exactly as the Card holds it; manual grants move freely', () => {
  const held = { [R]: ['app:read', 'app:steward'] }
  // Unticking the managed grant (or dropping the whole resource) keeps it held.
  assert.deepEqual(withManagedGrantsAsHeld({ [R]: ['app:read'] }, held, managedFor), { [R]: ['app:read', 'app:steward'] })
  assert.deepEqual(withManagedGrantsAsHeld({}, held, managedFor), { [R]: ['app:steward'] })
  // Ticking a managed grant the Card does not hold never adds it.
  assert.deepEqual(withManagedGrantsAsHeld({ [R]: ['app:read', 'app:steward'] }, { [R]: ['app:read'] }, managedFor), { [R]: ['app:read'] })
  // A new Card never carries one.
  assert.deepEqual(withManagedGrantsAsHeld({ [R]: ['app:read', 'app:steward'] }, {}, managedFor), { [R]: ['app:read'] })
  // No managed grants on another resource: unchanged.
  assert.deepEqual(withManagedGrantsAsHeld({ other: ['x'] }, {}, managedFor), { other: ['x'] })
})

test('the editor disables managed grants with the generic note and keeps them out of every payload', () => {
  const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')
  // Create: the chip is disabled and unchecked, the payload never carries one.
  assert.match(panel, /disabled=\{scopeBlocked \|\| createManagedFor\(item\.resource\)\.has\(grant\)\}/)
  assert.match(panel, /withManagedGrantsAsHeld\(materializeSelectionRouteGrants\([\s\S]*?\), \{\}, /)
  // Edit: the chip shows what the Card holds, disabled; the saved claims keep it as held.
  assert.match(panel, /disabled=\{stale \|\| editManagedFor\(resource\)\.has\(claim\)\}/)
  assert.match(panel, /const editKeptClaims = [\s\S]*?filter\(\(grant: string\) => managed\.has\(grant\)\)/)
  assert.equal((panel.match(/\{MANAGED_GRANT_NOTE\}/g) || []).length, 2)
  // Generic: no application or grant named in the editor code for this.
  assert.doesNotMatch(readFileSync(new URL('../src/features/delegatedAccess/managedGrants.ts', import.meta.url), 'utf8'), /work:|problem.board/i)
})
