import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { controlIssuerLabel, isPersonIssuer } from '../src/features/delegatedAccess/cardLabels.ts'
import { accessCardFocusRequest } from '../src/features/delegatedAccess/accessCardFocus.ts'

// Operator, 2026-09-26: on a person's Control Card and My Card the composition
// panel read "Card composed with cognito:<id> (AND)" and "Open cognito:<id>".
// A person is named, never by raw id (the board's rule, #188 and #191).

const PERSON = { issuer_ref: 'cognito:02657414-aaaa-4bbb-8ccc-111111111111', issuer_kind: 'operator', control_id: 'ctl_1' }

test('the viewer\'s own Control Card is "your Control Card"', () => {
  assert.equal(controlIssuerLabel(PERSON, { viewerSubject: '02657414-aaaa-4bbb-8ccc-111111111111' }), 'your Control Card')
  assert.equal(controlIssuerLabel(PERSON, { viewerSubject: 'cognito:02657414-aaaa-4bbb-8ccc-111111111111' }), 'your Control Card')
})

test('a person the board named is named', () => {
  assert.equal(
    controlIssuerLabel(PERSON, { viewerSubject: 'someone-else', targetSubject: 'cognito:02657414-aaaa-4bbb-8ccc-111111111111', targetLabel: 'Dana' }),
    "Dana's Control Card",
  )
})

test('an unnamed person is "another person", never the raw id', () => {
  const label = controlIssuerLabel(PERSON, { viewerSubject: 'someone-else' })
  assert.equal(label, "another person's Control Card")
  assert.doesNotMatch(label, /cognito:/)
  // A name given for a different person is not borrowed.
  assert.equal(controlIssuerLabel(PERSON, { targetSubject: 'cognito:other', targetLabel: 'Robin' }), "another person's Control Card")
})

test('a stored label wins, and an application issuer keeps its ref', () => {
  assert.equal(controlIssuerLabel({ ...PERSON, issuer_label: 'Operations desk' }), 'Operations desk')
  const app = { issuer_ref: 'problem-board@1-0:project:quickstart', issuer_kind: 'application', control_id: 'ctl_2' }
  assert.equal(isPersonIssuer(app), false)
  assert.equal(controlIssuerLabel(app), 'problem-board@1-0:project:quickstart')
})

test('a link passes the person\'s name through target_label', () => {
  const focus = accessCardFocusRequest({ control_card_id: 'ctl_1', project_ref: 'work:project:p', target_subject: 'cognito:x', target_label: 'Dana' })
  assert.equal(focus.targetLabel, 'Dana')
})

test('every place that names a Control Card by its issuer uses controlIssuerLabel', () => {
  const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')
  assert.doesNotMatch(panel, /issuer_label \|\| binding\.issuer_ref/)
  assert.doesNotMatch(panel, /binding\?\.issuer_label\s*\|\|\s*linkedControl\?\.binding\?\.issuer_ref/)
  assert.equal(panel.match(/controlIssuerLabel\(/g)?.length, 4)
  assert.match(panel, /record\.issuer_ref && !isPersonIssuer\(record\) \?/)
})
