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

test('platform sign-in resolves the authenticator in its referenced authority', () => {
  const pane = source('src/features/authenticators/AuthoritiesPane.tsx')
  assert.match(pane, /selectedAuthenticatorRef\?\.authority_id/)
  assert.match(pane, /row\.authority_id === selectedAuthenticatorAuthorityId/)
  assert.match(pane, /platform\?\.login_authenticator\?\.type/)
  assert.match(pane, /authority\.authority_id === selectedAuthenticatorAuthorityId/)
})

test('every authenticator construct remains an app-defined bundle lookup', () => {
  const constructs = source('src/features/authenticators/authConstructs.ts')
  assert.doesNotMatch(constructs, /authType:\s*'(?:cognito|simple)'/)
  assert.match(constructs, /authType:\s*'bundle'/)
})

test('tab strip remains a single-row overflow carousel', () => {
  const css = source('src/styles.css')
  const tabsWrapBlock = css.slice(
    css.indexOf('.tabs-wrap {'),
    css.indexOf('}', css.indexOf('.tabs-wrap {')),
  )
  const tabsBlock = css.slice(css.indexOf('.tabs {'), css.indexOf('}', css.indexOf('.tabs {')))
  assert.match(tabsWrapBlock, /position: sticky/)
  assert.match(tabsWrapBlock, /top: 0/)
  assert.match(tabsWrapBlock, /flex: 0 0 auto/)
  assert.match(tabsBlock, /flex-wrap: nowrap/)
  assert.match(tabsBlock, /overflow-x: auto/)
  assert.doesNotMatch(css, /@media \(max-width: 479px\)/)
  assert.match(css, /\.tabs-wrap\[data-fade-left\]::before \{ opacity: 1; \}/)
  assert.match(css, /\.tabs-wrap\[data-fade-right\]::after \{ opacity: 1; \}/)

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

test('an ungranted operation offers one atomic once-or-always grant, chosen beside that operation', () => {
  const app = source('src/App.tsx')
  assert.match(app, /'access_id'/)
  assert.match(app, /'invocation_policy'/)
  assert.match(app, /'invocation_change_id'/)

  // The wire payload is built in one pure module the thunk sends verbatim, so
  // tests/invocation-choice.test.mjs can pin what is submitted.
  const slice = source('src/features/delegatedAccess/delegatedAccessSlice.ts')
  assert.match(slice, /agentGrantWirePayload\(args\)/)
  const payload = source('src/features/delegatedAccess/agentGrantPayload.ts')
  assert.match(payload, /invocation_mode: invocationMode/)
  assert.match(payload, /invocation_change_id: invocationChangeId/)

  const panel = source('src/features/delegatedAccess/DelegatedAccessPanel.tsx')
  // The pending choice renders under the requested operation and is submitted
  // with its grant through the focused transaction; nothing sets a sibling's
  // policy on the way.
  assert.match(panel, /pendingChoiceRequested\(pendingGrant\)/)
  assert.match(panel, /<OperationInvocationChoice\n\s*operation=\{pendingOuterCapability\.operation\.name\}/)
  assert.match(panel, /grantPending\(pendingSubmitMode\)/)
  assert.match(panel, /`Allow \$\{pendingGrant\.outerOperation\} \$\{pendingInvocationMode\}`/)
  assert.match(panel, /focusedGrantArgs\(focusedIdentity, invocationMode, claims/)
  assert.doesNotMatch(panel, />\s*Allow once\s*</)
  assert.doesNotMatch(panel, />\s*Allow always\s*</)
  const grantPendingBody = panel.slice(
    panel.indexOf('const grantPending = async'),
    panel.indexOf('// Toggle one claim on one account'),
  )
  assert.ok(grantPendingBody.length > 0)
  assert.doesNotMatch(grantPendingBody, /setDelegatedInvocationPolicy|setOperationInvocationPolicy/)

  // Ordinary editing: a granted operation keeps its labeled live control; an
  // added one gets the choice, the save waits for it, and the card update
  // carries only the operations already granted (each added one goes through
  // the focused grant with its mode).
  assert.match(panel, /<InvocationPolicyControl\n\s*operation=\{operation\.name\}/)
  assert.match(panel, /\) : selected \? \(\n(?:.*\n){3}\s*<OperationInvocationChoice/)
  assert.match(panel, /disabled=\{busy \|\| editSaveProblems\(item\)\.length > 0\}/)
  assert.match(panel, /Object\.entries\(splits\)\.map\(\(\[resource, split\]\) => \[resource, split\.kept\]\)/)
  assert.match(panel, /changeId: editChangeId\(item\.access_id, operation, randomNonce\(\)\)/)

  const controls = source('src/features/delegatedAccess/InvocationControls.tsx')
  assert.match(controls, /aria-label=\{`Invocation policy for \$\{operation\}`\}/)
  assert.match(controls, /aria-label=\{`\$\{operation\}: once`\}/)
  assert.match(controls, /aria-label=\{`\$\{operation\}: always`\}/)

  // An existing grant with no policy runs as always by design, so the control
  // must not report a policy the user never chose: the operator has to tell
  // the default ("every time (default)", no button active) from a chosen
  // "Every time" (button active, no status) to audit which operations carry
  // a decision.
  assert.match(controls, /const mode = policy\?\.mode \|\| null;/)
  assert.doesNotMatch(controls, /const mode = policy\?\.mode \|\| 'always';/)
  assert.match(controls, /\{!mode \? \(\n\s*<span\n\s*className="operation-policy__status"/)
  assert.match(controls, />\s*every time \(default\)\s*</)

  const css = source('src/styles.css')
  for (const cls of ['.outer-operation-editor--policy', '.operation-policy__label', '.pending-operation-policy']) {
    assert.ok(css.includes(cls), `styles.css lacks ${cls}`)
  }
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
  // A new connector names no connector, so it carries no revision precondition.
  assert.match(remoteSlice, /if \(args\.connectorId\) \{\n\s*payload\.connector_id = args\.connectorId;\n\s*payload\.expected_revision = args\.expectedRevision \?\? 0;\n\s*\}/)
  assert.doesNotMatch(remoteSlice, /expected_revision: args\.expectedRevision \|\| 0/)

  const delegatedPanel = source('src/features/delegatedAccess/DelegatedAccessPanel.tsx')
  assert.match(delegatedPanel, /publicMcpUrl\('remote_mcp_proxy'\)/)
  assert.match(delegatedPanel, /Streamable HTTP endpoint/)
  assert.match(delegatedPanel, /<Field label="Header"><code>Authorization<\/code><\/Field>/)
  assert.match(delegatedPanel, /issuedHeader \|\| `Bearer \$\{issuedToken\}`/)

  // Two cards of one program look identical after a reconnect; the issuance
  // stamp is the only field that separates them. It is scoped to OAuth cards,
  // because a manual or agent card never reaches the token endpoint and its
  // zero would read as "never used".
  assert.match(delegatedPanel, /item\.source === 'oauth' && item\.last_issued_at \?/)
  assert.match(delegatedPanel, /not renewed since consent/)
  assert.match(delegatedPanel, /credentials last issued \{formatDate\(item\.last_issued_at\)\}/)
  assert.doesNotMatch(delegatedPanel, /last used|last sign-?in/i)
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
    assert.match(call.slice(0, 400), /existingScope: seedAccountScopeFromRecord\((?:item|record)\)/)
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

test('the resource editor never suggests another card for one resident profile', () => {
  const panel = source('src/features/delegatedAccess/DelegatedAccessPanel.tsx')
  assert.match(panel, /It cannot be added to this card\./)
  assert.doesNotMatch(panel, /grant it on a separate card/i)
})

test('external MCP tab exposes the client endpoint, summons the connector form, and discloses tool schemas', () => {
  const panel = source('src/features/remoteMcp/RemoteMcpPanel.tsx')

  // Requirement 1: the client-facing proxy endpoint is derived from settings
  // through the shared builder and labelled apart from the upstream endpoint.
  assert.match(panel, /const clientEndpoint = publicMcpUrl\('remote_mcp_proxy'\)/)
  assert.match(panel, /Client MCP endpoint/)
  assert.match(panel, /<DoorRef value=\{clientEndpoint\}/)
  assert.doesNotMatch(panel, /localhost|ngrok/)

  // Requirement 2: the creation pane is summoned from the tab action row and
  // leads the tab only while open; direct creation clears and closes it.
  assert.match(panel, /const \[connectOpen, setConnectOpen\] = useState\(false\)/)
  assert.match(panel, /className="tab-actions remote-mcp-head__actions"/)
  assert.match(panel, /onClick=\{\(\) => setConnectOpen\(true\)\}/)
  assert.match(panel, /connectOpen \? \[\{\s*id: 'remote-mcp-connect', title: 'Connect MCP server', content: connectorForm, lead: true,/)
  assert.match(panel, /onClick=\{closeConnectForm\}/)
  assert.match(panel, /resetConnectForm\(\);\s*setConnectOpen\(false\);\s*await refreshCardCatalog\(\)/)
  // The pending upstream OAuth link survives outside the pane.
  assert.match(panel, /remote-mcp-pending-oauth/)
  assert.match(panel, /Open it again/)

  // Requirement 3: accessible disclosures over structured schema data, keyed
  // per connector and tool, with pending drift kept apart from accepted tools.
  assert.match(panel, /aria-expanded=\{open\}/)
  assert.match(panel, /aria-controls=\{bodyId\}/)
  assert.match(panel, /summarizeSchema\(tool\.input_schema\)/)
  assert.match(panel, /summarizeSchema\(tool\.output_schema\)/)
  assert.match(panel, /\$\{pending \? 'pending:' : ''\}\$\{connector\.connector_id\}::\$\{tool\.name\}/)
  assert.match(panel, /renderTools\(connector, connector\.pending_tools, true\)/)
  assert.match(panel, /Proxy name/)
  assert.match(panel, /Raw input schema/)
  assert.doesNotMatch(panel, /input_schema[^\n]*\.split\(/)
  assert.doesNotMatch(panel, /className="claim-chip" key=\{tool\.name\}/)

  const schema = source('src/features/remoteMcp/toolSchema.ts')
  assert.match(schema, /export function summarizeSchema/)
  assert.match(schema, /isObject\(schema\.properties\)/)
  assert.match(schema, /Array\.isArray\(schema\.required\)/)
  assert.doesNotMatch(schema, /\.split\('\\n'\)|\.split\(','\)/)

  // The copy control is shared, not duplicated.
  const shared = source('src/components/CopyControls.tsx')
  assert.match(shared, /export function CopyButton/)
  assert.match(shared, /export function DoorRef/)
  const delegated = source('src/features/delegatedAccess/DelegatedAccessPanel.tsx')
  assert.match(delegated, /import \{ CopyButton, DoorRef \} from '\.\.\/\.\.\/components\/CopyControls'/)
  assert.doesNotMatch(delegated, /^function CopyButton/m)
  assert.doesNotMatch(delegated, /^function DoorRef/m)

  const css = source('src/styles.css')
  assert.match(css, /\.remote-mcp-client-endpoint \{/)
  assert.match(css, /\.tool-disclosure__toggle \{/)
  assert.match(css, /\.tool-raw__json \{[\s\S]*overflow: auto/)
})

test('granted-access cards filter from the action row, by exact rules the explainer describes', () => {
  const rules = source('src/features/delegatedAccess/grantFilter.ts')
  assert.match(rules, /export const EXPIRING_SOON_SECONDS = 7 \* 24 \* 3600/)
  assert.match(rules, /export function recordMatches\(/)
  assert.match(rules, /export function agentGroupMatches\(/)
  assert.match(rules, /export function clientMetadataKeys\(/)
  assert.match(rules, /Object\.prototype\.hasOwnProperty\.call\(metadata, key\)/)
  // A missing timestamp matches only an empty window; it is never guessed.
  assert.match(rules, /if \(from === null && to === null\) return true;\n  if \(!seconds\) return false;/)
  // "Active" keeps a credential that is about to expire.
  assert.match(rules, /filter\.state === 'active' \? state !== 'expired' : state === filter\.state/)
  // Text is plain substring matching over the ticked fields; nothing scores.
  assert.match(rules, /text\.toLowerCase\(\)\.includes\(needle\)/)
  assert.doesNotMatch(rules, /score|rank\(|weight/)

  const bar = source('src/features/delegatedAccess/GrantFilterBar.tsx')
  assert.match(bar, /export function GrantFilterControls\(/)
  assert.match(bar, /export function GrantFilterSettings\(/)
  assert.match(bar, /export function GrantFilterInfo\(/)
  assert.match(bar, /aria-pressed=\{settingsOpen\}/)
  assert.match(bar, /aria-controls="grant-filter-settings"/)
  assert.match(bar, /id="grant-filter-settings"/)
  assert.match(bar, /aria-label="Client metadata key"/)
  assert.match(bar, /aria-label="Client metadata value"/)
  assert.match(bar, /aria-label="How card filtering works"/)
  assert.match(bar, /role="dialog"/)
  // The explainer quotes the implemented expiry window rather than a literal.
  assert.match(bar, /ends within \{EXPIRING_DAYS\} days/)
  // The last ticked field cannot be unticked.
  assert.match(bar, /next\.length \? ALL_SEARCH_FIELDS\.filter/)

  const panel = source('src/features/delegatedAccess/DelegatedAccessPanel.tsx')
  assert.doesNotMatch(panel, /grantQuery|grant-search/)
  assert.match(panel, /className="tab-actions grant-filter-row"/)
  assert.match(panel, /<GrantFilterControls[\s\S]*?onOpenInfo=\{\(\) => setGrantInfoOpen\(true\)\}/)
  assert.match(panel, /items\.length > 0 && grantSettingsOpen \? \(\n\s*<GrantFilterSettings/)
  assert.match(panel, /recordMatches\(item, grantFilter, grantFilterContext\)/)
  assert.match(panel, /agentGroupMatches\(clientId, records, grantFilter, grantFilterContext\)/)
  assert.match(panel, /<ClientMetadataDetails metadata=\{item\.client_metadata\}/)
  assert.match(panel, /Reported by the client; not access authority\./)
  // Any filter change returns to the first page.
  assert.match(panel, /setGrantFilter\(\(current\) => \(\{ \.\.\.current, \.\.\.patch \}\)\);\n\s*setGrantLimit\(GRANT_PAGE_SIZE\);/)

  const css = source('src/styles.css')
  for (const cls of ['.grant-filter-row', '.grant-filter__count', '.grant-filter__settings', '.grant-filter__chip.on', '.grant-filter__formula']) {
    assert.ok(css.includes(cls), `styles.css lacks ${cls}`)
  }
  assert.doesNotMatch(css, /\.grant-search/)
})

test('a card edits several resources under one stable identity: sections, add, remove, acceptance', () => {
  const rules = source('src/features/delegatedAccess/resourceEditing.ts')
  assert.match(rules, /export function editedResourceKeys\(/)
  assert.match(rules, /export function saveProblems\(/)
  assert.match(rules, /'no_resources_left'/)
  assert.match(rules, /export function toggleAccepted\(/)

  const parts = source('src/features/delegatedAccess/ResourceEditorParts.tsx')
  assert.match(parts, /export function ResourceSectionHead\(/)
  assert.match(parts, /export function RemovedResourceStub\(/)
  assert.match(parts, /export function ResourceDriftReview\(/)
  assert.match(parts, /export function ResourceOfferPicker\(/)
  // The review says a changed selected operation is suspended until accepted,
  // and a newly advertised one is not granted.
  assert.match(parts, /Changed, suspended until you accept/)
  assert.match(parts, /Newly advertised, not granted/)
  assert.match(parts, /offerReasonText\(offer\)/)

  const panel = source('src/features/delegatedAccess/DelegatedAccessPanel.tsx')
  // Both edit paths (agent rows and flat cards) render the same per-resource
  // sections; nothing is duplicated.
  assert.equal((panel.match(/renderEditResourceSections\(item\)/g) || []).length, 2)
  assert.match(panel, /const \[editAddedResources, setEditAddedResources\]/)
  assert.match(panel, /const \[editRemovedResources, setEditRemovedResources\]/)
  assert.match(panel, /const \[editAcceptedOperations, setEditAcceptedOperations\]/)
  // An added resource grants no operation yet, whatever equal names other
  // resources carry; the save submits exactly the edited resource set.
  assert.match(panel, /resource in \(item\.resource_grants \|\| \{\}\) \? \(item\.resource_operations\?\.\[resource\] \|\| \[\]\) : \[\]/)
  assert.match(panel, /editResourceKeys\(item\)\.forEach\(\(resource\) => \{\n\s*kept\[resource\] = editKeptClaims\(item, resource\);/)
  assert.match(panel, /acceptedOperations: editAcceptedOperations,/)
  assert.match(panel, /if \(editSaveProblems\(item\)\.length\) return;/)
  // The drift review is per resource and never rendered for a resource being added.
  assert.match(panel, /state=\{item\.catalog_drift\?\.resources\?\.\[resource\]\}/)
  assert.match(panel, /\{!isNew \? \(\n\s*<ResourceDriftReview/)

  const slice = source('src/features/delegatedAccess/delegatedAccessSlice.ts')
  assert.match(slice, /accepted_operations: acceptedOperations/)

  const types = source('src/api/types.ts')
  for (const name of ['resource_acceptance?', 'caller_profile?', 'stable_identity?', 'resource_offers?', 'DelegatedResourceDriftState', 'DelegatedDriftChange']) {
    assert.ok(types.includes(name), `types.ts lacks ${name}`)
  }

  const css = source('src/styles.css')
  for (const cls of ['.resource-section-head', '.resource-removed-stub', '.resource-drift-review', '.resource-offer-picker', '.edit-save-problems']) {
    assert.ok(css.includes(cls), `styles.css lacks ${cls}`)
  }
})
