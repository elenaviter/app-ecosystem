import assert from 'node:assert/strict'
import { readdirSync } from 'node:fs'
import { join, relative } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

// macOS and Windows file systems ignore case, so OperationGroups.tsx and
// operationGroups.ts were one file there: TypeScript resolved the wrong one and
// reported 9 errors on main (W356; the board widget had the same collision with
// onboardingState). No two files in this widget may differ only by case.

const root = fileURLToPath(new URL('..', import.meta.url))
const SKIP = new Set(['node_modules', 'dist', '.vite'])

function files(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    if (SKIP.has(entry.name)) return []
    const path = join(dir, entry.name)
    return entry.isDirectory() ? files(path) : [relative(root, path)]
  })
}

// A code module is imported without its extension, so './OperationGroups' and
// './operationGroups' collide even when one is .tsx and the other .ts.
const CODE = /\.(tsx?|jsx?|mjs|cjs)$/

test('no two files in the widget differ only by case, nor two modules by case alone', () => {
  const byLower = new Map()
  for (const path of files(root)) {
    const key = path.replace(CODE, '').toLowerCase()
    byLower.set(key, [...(byLower.get(key) || []), path])
  }
  const collisions = [...byLower.values()].filter((paths) => paths.length > 1)
  assert.deepEqual(collisions, [])
})
