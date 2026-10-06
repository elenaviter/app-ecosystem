// W587 follow-up (EMain 15:13): only a Card or catalog that moved makes an edit stale; every
// other 409 shows the server's own reason and leaves Save open (the operator's accept-only
// save was shown as "changed on the server" although nobody had saved).
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { isStaleEditRefusal, STALE_EDIT_REFUSALS } from '../src/features/delegatedAccess/cardFreshness.ts'

const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')

test('the four moved-Card refusals make an edit stale', () => {
  for (const error of ['delegated_access_precondition_failed', 'delegated_card_save_conflict',
    'delegated_card_revision_conflict', 'consent_catalog_changed']) {
    assert.equal(isStaleEditRefusal({ status: 409, error }), true, error)
  }
  assert.equal(STALE_EDIT_REFUSALS.size, 4)
})

test('any other refusal is shown as itself and never marks the edit stale', () => {
  for (const error of ['project_person_control_audit_changes_empty', 'project_person_control_not_active',
    'project_invitation_control_not_active', 'control_card_not_active', 'delegated_access_not_active',
    'project_person_control_selection_empty', 'issuer_managed_card']) {
    assert.equal(isStaleEditRefusal({ status: 409, error }), false, error)
  }
  assert.equal(isStaleEditRefusal({ status: 503, error: 'delegated_card_save_conflict' }), false)
  assert.equal(isStaleEditRefusal(null), false)
  const save = panel.slice(panel.indexOf('if (isStaleEditRefusal(updated)) {'))
  // W587 C: only a restarting board gets its own words; every other refusal is shown as itself.
  assert.match(save, /return;\s+\}\s+setEditActionError\(isBoardRestarting\(updated\?\.error\)\s+\? `Save was not applied: \$\{BOARD_RESTARTING_MESSAGE\} Your draft is kept\.`\s+: updated\?\.message \|\| `Save was not applied: \$\{updated\?\.error \|\| 'request refused'\}`\);/)
  assert.doesNotMatch(panel, /if \(updated\?\.status === 409\) \{/)
})
