import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { controlCardLabel, controlCardNoun, isOperatorCard } from '../src/features/delegatedAccess/controlCardKind.ts'

// Operator, 2026-09-24: "AHH this is control card! everything is mixed. then
// how i can look on MY operator card, not a control one?"

test('a credentialless Card a person operates an application with reads as an operator card', () => {
  const operator = { source: 'control', issuer_kind: 'operator' }
  assert.equal(isOperatorCard(operator), true)
  assert.equal(controlCardLabel(operator), 'operator card')
  assert.equal(controlCardNoun(operator), 'this operator card')
})

test('a Control Card that governs linked Cards keeps its name', () => {
  for (const kind of ['application', 'kdcube_agent_descriptor', undefined]) {
    const control = { source: 'control', issuer_kind: kind }
    assert.equal(isOperatorCard(control), false)
    assert.equal(controlCardLabel(control), 'control card')
  }
  assert.equal(isOperatorCard({ source: 'agent', issuer_kind: 'operator' }), false)
})

test('the panel labels credentialless Cards through the kind, and Revoke stays on the Save row', () => {
  const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')
  const styles = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8')
  assert.match(panel, /if \(item\.source === 'control'\) return controlCardLabel\(item\);/)
  assert.match(panel, /if \(item\.source === 'control'\) return controlCardNoun\(item\);/)
  assert.doesNotMatch(panel, /return 'control card';/)
  assert.match(styles, /\.form-actions > \.action-row, \.form-actions > \.revoke-confirm \{ width: auto; margin-left: auto; \}/)
})
