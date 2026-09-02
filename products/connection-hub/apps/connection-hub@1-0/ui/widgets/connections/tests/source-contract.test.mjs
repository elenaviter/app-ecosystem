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
  assert.match(panel, /Service operation/)
  assert.match(panel, /KDCube service permissions/)
  assert.match(panel, /resolvePendingServiceCapability\(pendingGrant, resources, operationRows\)/)
  assert.match(panel, /pendingServiceCapability\.requiredDoorGrants/)
  assert.match(panel, /Connected-account requirements for this capability/)
  assert.match(panel, /Requested changes/)
  assert.match(panel, /focusNextPendingReviewTarget/)
  assert.match(panel, /request-review-section-active/)
  assert.match(panel, /request-review-progress/)
  assert.match(panel, /pendingReviewIndex \+ 1.*pendingReviewTargets\.length/s)
  assert.match(panel, /<PendingStatus status=/)
  assert.match(panel, /checked=\{pendingOperationSelected\}/)
  assert.match(panel, /pendingServiceApprovalReady\(/)
  assert.match(panel, /proposeExactAccountClaim\(/)
  assert.match(panel, /pendingOperationRequested\s*\? !pendingOperationReady/)
  assert.match(panel, /The request is still pending/)
  assert.match(panel, /Connected-account permissions/)
  assert.match(panel, /pendingSelectionStatus\(/)
  assert.match(panel, /existingScope: pendingExistingAccountScope/)
  assert.match(panel, /doorGrantsForOperation\(resourceOption, namespaceOption, operation, grants\)/)

  const projection = source('src/features/delegatedAccess/pendingGrantProjection.ts')
  assert.match(projection, /Already granted/)
  assert.match(projection, /Pending - not granted yet/)
  assert.match(projection, /Required for this request/)

  const catalog = source('src/features/delegatedAccess/DelegatedResourceCatalog.tsx')
  assert.match(catalog, /doorGrantsForOperation\(/)

  const css = source('src/styles.css')
  assert.match(css, /\.request-review-nav \{[\s\S]*position: sticky/)
  assert.match(css, /\.pending-selection-status\[data-state='pending'\][\s\S]*var\(--warn-text\)/)
})

test('an ungranted operation offers one atomic once-or-always grant', () => {
  const app = source('src/App.tsx')
  assert.match(app, /'access_id'/)
  assert.match(app, /'invocation_policy'/)
  assert.match(app, /'invocation_change_id'/)

  const slice = source('src/features/delegatedAccess/delegatedAccessSlice.ts')
  assert.match(slice, /invocation_mode: invocationMode/)
  assert.match(slice, /invocation_change_id: invocationChangeId/)

  const panel = source('src/features/delegatedAccess/DelegatedAccessPanel.tsx')
  assert.match(panel, /pendingGrant\?\.invocationPolicy === 'choose'/)
  assert.match(panel, /grantPending\('once'\)/)
  assert.match(panel, /grantPending\('always'\)/)
  assert.match(panel, />\s*Allow once\s*</)
  assert.match(panel, />\s*Allow always\s*</)
})

test('provider-console OAuth stays transient and issued MCP access is client-ready', () => {
  const remotePanel = source('src/features/remoteMcp/RemoteMcpPanel.tsx')
  assert.match(remotePanel, /Client created in provider console/)
  assert.match(remotePanel, /Redirect URI/)
  assert.match(remotePanel, /requestRemoteMcpOAuth\(args\)/)
  assert.match(remotePanel, /setOAuthClientSecret\(''\)/)
  assert.match(remotePanel, /setReplacementOAuthSecret\(''\)/)

  const remoteSlice = source('src/features/remoteMcp/remoteMcpSlice.ts')
  assert.match(remoteSlice, /without retaining provider client credentials in Redux/)
  assert.match(remoteSlice, /Omit<StartRemoteMcpOAuthArgs, 'oauthClient'>/)
  assert.match(remoteSlice, /token_endpoint_auth_method/)

  const delegatedPanel = source('src/features/delegatedAccess/DelegatedAccessPanel.tsx')
  assert.match(delegatedPanel, /publicMcpUrl\('remote_mcp_proxy'\)/)
  assert.match(delegatedPanel, /Streamable HTTP endpoint/)
  assert.match(delegatedPanel, /<Field label="Header"><code>Authorization<\/code><\/Field>/)
  assert.match(delegatedPanel, /issuedHeader \|\| `Bearer \$\{issuedToken\}`/)
})

test('connections widget announces readiness only after installing its command listener', () => {
  const app = source('src/App.tsx')
  const listener = app.indexOf("window.addEventListener('message', onSurfaceCommand)")
  const ready = app.indexOf('announceConnectionsHubReady()', listener)

  assert.ok(listener >= 0)
  assert.ok(ready > listener)

  const command = source('src/api/surfaceCommand.ts')
  assert.match(command, /SURFACE_READY_MESSAGE_TYPE = 'kdcube\.surface\.ready'/)
  assert.match(command, /target_surfaces: CONNECTIONS_TARGET_SURFACES/)
})

test('the card editor shows a persisted account claim as granted, not pending', () => {
  const panel = source('src/features/delegatedAccess/DelegatedAccessPanel.tsx')

  // Every edit-mode picker must receive the card's persisted scope. Without it
  // `alreadyGranted` is empty, and each ticked-and-saved claim renders as
  // "Pending - not granted yet" beside a card that already grants it.
  const editCalls = panel
    .split('renderAccountScopePicker(')
    .slice(1)
    .filter((call) => call.slice(0, 200).includes('editAccountScope'))
  assert.ok(editCalls.length >= 2)
  for (const call of editCalls) {
    assert.match(call.slice(0, 400), /existingScope: seedAccountScopeFromRecord\(item\)/)
  }

  // The create flow deliberately passes no persisted scope: a card being
  // created grants nothing yet, so "pending" is the truthful label there.
  const createCall = panel
    .split('renderAccountScopePicker(')
    .slice(1)
    .find((call) => call.slice(0, 200).includes('createAccountScope'))
  assert.ok(createCall)
  assert.doesNotMatch(createCall.slice(0, 200), /existingScope/)

  const projection = source('src/features/delegatedAccess/pendingGrantProjection.ts')
  assert.match(projection, /if \(alreadyGranted\) return 'Already granted'/)
})
