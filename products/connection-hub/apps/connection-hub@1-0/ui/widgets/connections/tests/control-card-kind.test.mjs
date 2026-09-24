import assert from 'node:assert/strict'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import test from 'node:test'

import { controlCardLabel, controlCardNoun } from '../src/features/delegatedAccess/controlCardKind.ts'

// Operator, 2026-09-24: a person's Card per project is a Control Card, "similar
// to project card". One name per Card (W300).

test('every credentialless Card is labelled a control card, whatever its issuer kind', () => {
  for (const kind of ['operator', 'application', 'kdcube_agent_descriptor', undefined]) {
    const card = { source: 'control', issuer_kind: kind }
    assert.equal(controlCardLabel(card), 'control card', String(kind))
    assert.equal(controlCardNoun(card), 'this control card', String(kind))
  }
})

test('no reader-visible "operator card" remains in the widget', () => {
  const files = []
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const path = join(dir, name)
      if (statSync(path).isDirectory()) walk(path)
      else if (/\.(tsx?|css)$/.test(name)) files.push(path)
    }
  }
  walk(new URL('../src', import.meta.url).pathname)
  const offenders = files.filter((path) => /operator card/i.test(readFileSync(path, 'utf8')))
  assert.deepEqual(offenders, [])
})

test('the panel labels credentialless Cards through the kind, and Revoke stays on the Save row', () => {
  const panel = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')
  const styles = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8')
  assert.match(panel, /if \(item\.source === 'control'\) return controlCardLabel\(item\);/)
  assert.match(panel, /if \(item\.source === 'control'\) return controlCardNoun\(item\);/)
  assert.match(styles, /\.form-actions > \.action-row, \.form-actions > \.revoke-confirm \{ width: auto; margin-left: auto; \}/)
})
