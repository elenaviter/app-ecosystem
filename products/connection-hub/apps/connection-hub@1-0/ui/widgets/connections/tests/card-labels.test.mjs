import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { cardOwnerView, readableCardLabel } from '../src/features/delegatedAccess/cardLabels.ts'

// W304 finding 22 (Connection Hub half): a worker Card's title led with the
// tool that registered it, the owner showed as a raw subject id, and a long
// field label ran into its value.

test('an old worker Card title leads with the alias, keeping the exact session', () => {
  assert.equal(
    readableCardLabel('Connection Hub CLI · Problem Board worker · codex:codex-ui:11111111-1111-4111-8111-111111111111'),
    'codex-ui · Problem Board worker · codex:11111111-1111-4111-8111-111111111111',
  )
  assert.equal(
    readableCardLabel('Connection Hub CLI · Problem Board coordinator · claude-code:claude-app@elena:398cdfe7'),
    'claude-app@elena · Problem Board coordinator · claude-code:398cdfe7',
  )
  // Without an alias, only the tool name goes.
  assert.equal(
    readableCardLabel('Connection Hub CLI · Problem Board worker · codex:11111111'),
    'Problem Board worker · codex:11111111',
  )
  // Any other title is shown as it is.
  assert.equal(readableCardLabel('My laptop CLI'), 'My laptop CLI')
  assert.equal(readableCardLabel(undefined), '')
})

test('the owner reads as a person, with the labelled id on hover', () => {
  assert.deepEqual(cardOwnerView('user-1', 'user-1'), { owner: 'You', ownerTitle: 'Owner ID: user-1' })
  assert.deepEqual(cardOwnerView('user-2', 'user-1'), { owner: 'Another person', ownerTitle: 'Owner ID: user-2' })
  assert.deepEqual(cardOwnerView('', 'user-1'), { owner: 'You', ownerTitle: 'Owner ID: user-1' })
  assert.deepEqual(cardOwnerView('', ''), { owner: '', ownerTitle: '' })
})

const source = (path) => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8')

test('the panel uses them, and the field label wraps', () => {
  const panel = source('src/features/delegatedAccess/DelegatedAccessPanel.tsx')
  assert.doesNotMatch(panel, /owner=\{item\.grantor_subject \|\| platformUserId\}/)
  assert.equal(panel.match(/\{\.\.\.cardOwnerView\(item\.grantor_subject, platformUserId\)\}/g)?.length, 2)
  assert.match(panel, /correlatedCardLabel\(\{ \.\.\.item, label: readableCardLabel\(item\.label\) \}\)/)
  assert.match(source('src/features/delegatedAccess/CardRuntimeIdentity.tsx'), /ownerTitle=\{ownerTitle\}/)
  const css = source('src/styles.css')
  const label = css.slice(css.indexOf('.card-field-label {'), css.indexOf('}', css.indexOf('.card-field-label {')))
  assert.match(label, /white-space: normal;/)
  assert.match(label, /overflow-wrap: anywhere;/)
})
