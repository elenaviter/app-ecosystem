import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const source = (relativePath) =>
  readFileSync(new URL(`../${relativePath}`, import.meta.url), 'utf8')

test('widget bundle identity comes from its served route', () => {
  const settings = source('src/api/settings.ts')
  const start = settings.indexOf('getBundleId()')
  assert.ok(start >= 0)
  const block = settings.slice(
    start,
    settings.indexOf('}', settings.indexOf('return isPlaceholder', start)),
  )
  assert.match(block, /if \(context\.bundleId\) return context\.bundleId/)
})

test('access map is read-only and visible only to platform administrators', () => {
  const slice = source('src/features/accessMap/accessMapSlice.ts')
  assert.match(slice, /getOp<DelegatedAccessMapResult>\('delegated_access_map'\)/)
  assert.doesNotMatch(slice, /postOp/)
  assert.match(slice, /platform_admin_required/)

  const panel = source('src/features/accessMap/AccessMapPanel.tsx')
  assert.doesNotMatch(panel, /postOp|onSubmit|<form/)
  assert.match(panel, /platform administrators only/)

  const app = source('src/App.tsx')
  assert.match(app, /activeTab === 'accessMap' && authenticatorsAllowed/)
})

test('tab strip remains a single-row overflow carousel', () => {
  const css = source('src/styles.css')
  const tabsBlock = css.slice(css.indexOf('.tabs {'), css.indexOf('}', css.indexOf('.tabs {')))
  assert.match(tabsBlock, /flex-wrap: nowrap/)
  assert.match(tabsBlock, /overflow-x: auto/)
  assert.doesNotMatch(css, /@media \(max-width: 479px\)/)
  assert.match(css, /\.tabs-wrap\[data-fade-left\]::before \{ opacity: 1; \}/)
  assert.match(css, /\.tabs-wrap\[data-fade-right\]::after \{ opacity: 1; \}/)
  assert.match(css, /\.tabs-wrap \{ position: relative; margin: 0 0 14px; flex: 0 0 auto; \}/)

  const shell = source('src/components/AppShell.tsx')
  assert.match(shell, /querySelector\('\.tab\.active'\)/)
  assert.match(shell, /scrollIntoView\(\{ block: 'nearest', inline: 'nearest' \}\)/)
  assert.match(shell, /addEventListener\('scroll', updateFade/)
  assert.match(shell, /new ResizeObserver\(updateFade\)/)
  assert.match(shell, /el\.scrollLeft > 1/)
  assert.match(shell, /el\.scrollLeft \+ el\.clientWidth < el\.scrollWidth - 1/)
  assert.match(shell, /data-fade-left=\{fade\.left \|\| undefined\}/)
  assert.match(shell, /data-fade-right=\{fade\.right \|\| undefined\}/)
})

test('viewport-bound widget and access-map panel own their sizing', () => {
  assert.match(source('index.html'), /data-kdcube-resize-reporter/)

  const css = source('src/styles.css')
  const block = css.slice(
    css.indexOf('.access-map-body {'),
    css.indexOf('}', css.indexOf('.access-map-body {')),
  )
  assert.match(block, /overflow-y: auto/)
  assert.match(block, /min-height: 0/)
  assert.match(source('src/features/accessMap/AccessMapPanel.tsx'), /className="access-map-body"/)
})

test('agent consent distinguishes resource requests from existing account permissions', () => {
  const panel = source('src/features/delegatedAccess/DelegatedAccessPanel.tsx')
  assert.match(panel, /Resource permissions requested for this operation/)
  assert.match(panel, /Connected-account permissions/)
  assert.match(panel, /Already granted/)
  assert.match(panel, /existingScope: pendingExistingAccountScope/)
})
