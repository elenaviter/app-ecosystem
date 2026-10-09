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

// W681 (operator, 2026-10-09: "the section with github dissappeared from this card"; after Cancel + Edit
// "for a momemnt i saw section with github"). Opening the editor changes nothing, and Edit never hides
// the My Card's GitHub link or calls an existing account "no accounts yet".
test('opening the editor is not a pending change; a change is', async () => {
  const { callerEditNote } = await import('../src/features/delegatedAccess/cardLabels.ts')
  assert.equal(callerEditNote(false, 'Maintenance Control remains linked.'),
    'Editing the caller Card. Maintenance Control remains linked.')
  assert.equal(callerEditNote(true, 'Maintenance Control remains linked.'),
    'Pending changes to the caller Card. Maintenance Control remains linked.')
})

test('an existing account not chosen for this Card is 0/1, never "no accounts yet"', async () => {
  const { providerAccountsSummary } = await import('../src/features/delegatedAccess/cardLabels.ts')
  assert.equal(providerAccountsSummary(0, 1), '0/1 accounts')
  assert.equal(providerAccountsSummary(1, 2), '1/2 accounts')
  assert.equal(providerAccountsSummary(0, 0), 'no accounts yet')
})

test("the editor shows the My Card's GitHub section and every caller-edit note follows the draft", () => {
  const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')
  const workbench = panel.slice(panel.indexOf('const renderWorkbench'))
  assert.match(workbench.slice(0, workbench.indexOf('className="rename-row"')),
    /isMyCard\(record\)\s*\?\s*<MyCardGithubSection projectRef=\{myCardProjectRef\(record\)\}/,
    'the editor renders the GitHub section for a My Card')
  assert.doesNotMatch(panel, /!editing && isMyCard/, 'no view hides it while editing')
  assert.doesNotMatch(panel, /`Pending changes to the caller Card\./, 'no unconditional "Pending changes" note')
  assert.doesNotMatch(panel, /'no accounts yet'/, 'the provider summary comes from providerAccountsSummary')
})
