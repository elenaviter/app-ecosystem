import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { personControlCardHolder, personControlCardTitle } from '../src/features/delegatedAccess/cardLabels.ts'

// Operator, 2026-09-26 (W360): the Card list showed a person's Control Card as
// "42d5a4e4-…", the account id Connection Hub stored when the board created
// the Card without a name. A person's Card is named by the person.

const TARGET = '42d5a4e4-aaaa-4bbb-8ccc-111111111111'

test('a Card stored under the account id is never titled by it', () => {
  for (const label of [TARGET, `cognito:${TARGET}`, '', undefined]) {
    const title = personControlCardTitle(label, TARGET, { viewerSubject: 'someone-else' })
    assert.equal(title, "Another person's Control Card")
    assert.doesNotMatch(title, /42d5a4e4/)
  }
})

test('the viewer\'s own Card, a stored name, and the name the board\'s link gave', () => {
  assert.equal(personControlCardTitle(TARGET, TARGET, { viewerSubject: `cognito:${TARGET}` }), 'Your Control Card')
  assert.equal(personControlCardTitle('dana@example.test', TARGET, { viewerSubject: 'x' }), "dana@example.test's Control Card")
  assert.equal(
    personControlCardTitle(TARGET, TARGET, { viewerSubject: 'x', targetSubject: TARGET, targetLabel: 'Dana' }),
    "Dana's Control Card",
  )
  // A name the link gave for someone else is not borrowed.
  assert.equal(
    personControlCardTitle(TARGET, TARGET, { viewerSubject: 'x', targetSubject: 'other', targetLabel: 'Robin' }),
    "Another person's Control Card",
  )
})

test('the Card header names whom the Card is for the same way', () => {
  assert.equal(personControlCardHolder(TARGET, TARGET, { viewerSubject: TARGET }), 'You')
  assert.equal(personControlCardHolder('Dana', TARGET), 'Dana')
  assert.equal(personControlCardHolder(TARGET, TARGET), 'Another person')
})

test('the Card list and the Card header use the person rule', () => {
  const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')
  const title = panel.slice(panel.indexOf('const cardTitle = '), panel.indexOf('const cardBadge = '))
  assert.match(title, /personControlCardTitle\(item\.label, personControl\.targetSubject, issuerViewer\)/)
  assert.match(panel, /personControlCardHolder\(record\.issuer_label, projectPersonControl\.targetSubject, issuerViewer\)/)
})
