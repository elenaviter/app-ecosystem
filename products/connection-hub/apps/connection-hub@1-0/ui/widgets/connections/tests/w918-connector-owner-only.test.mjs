import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  CONNECTORS_OWNER_ONLY_NOTE, connectorsOwnerOnly, isConnectorResource,
} from '../src/features/delegatedAccess/connectorOwnership.ts'

// W918: a Card's personal MCP servers change only by the Card's owner. The editor shows the Hub's own
// rule (owner_only_connectors.py): anyone else keeps or removes a connector, never adds or changes one.

const CONNECTOR = 'urn:connection-hub:remote-mcp:mcp_aaaaaaaaaaaaaaaaaaaaaaaa'

test('a connector is the Hub resource kind, by its stable id', () => {
  assert.equal(isConnectorResource(CONNECTOR), true)
  // The same parse as the Hub's connector_id_from_resource: trimmed, id lower-cased, exact id shape.
  assert.equal(isConnectorResource(`  ${CONNECTOR}  `), true)
  assert.equal(isConnectorResource('urn:connection-hub:remote-mcp:MCP_AAAAAAAAAAAAAAAAAAAAAAAA'), true)
  for (const other of ['urn:connection-hub:remote-mcp:', 'https://board.example.test/mcp', '*', '', undefined,
    'urn:connection-hub:remote-mcp:mcp_short', 'urn:connection-hub:remote-mcp:mcp_aaaaaaaaaaaaaaaaaaaaaaaa/x',
    'URN:connection-hub:remote-mcp:mcp_aaaaaaaaaaaaaaaaaaaaaaaa', 'urn:connection-hub:remote-mcp:other_aaaaaaaaaaaaaaaaaaaaaaaa']) {
    assert.equal(isConnectorResource(other), false, String(other))
  }
})

test('only a viewer who is not the grantor is limited to keeping or removing', () => {
  assert.equal(connectorsOwnerOnly('user:owner', 'user:owner'), false, 'the owner, exactly')
  assert.equal(connectorsOwnerOnly('owner', 'owner'), false)
  assert.equal(connectorsOwnerOnly('user:owner', 'admin'), true)
  // The Hub compares exact subjects (owner_only_connectors.py): no alias is folded.
  assert.equal(connectorsOwnerOnly('user:owner', 'owner'), true)
  assert.equal(connectorsOwnerOnly('cognito:owner', 'user:owner'), true)
  // Unknown on either side never offers more than the Hub accepts.
  assert.equal(connectorsOwnerOnly('', 'owner'), true)
  assert.equal(connectorsOwnerOnly('user:owner', undefined), true)
})

test('the editor disables a connector body for a non-owner, keeps Remove, and offers no new connector', () => {
  const source = readFileSync(new URL('../src/features/delegatedAccess/DelegatedAccessPanel.tsx', import.meta.url), 'utf8')
  assert.match(source, /disabled=\{isConnectorResource\(resource\) && connectorsOwnerOnly\(item\.grantor_subject, platformUserId\)\}/)
  // Neither picker offers a connector to a non-owner.
  assert.match(source, /childOffers\.filter\(\(offer\) => !isConnectorResource\(offer\.resource\)\)/)
  assert.match(source, /topLevelOffers\.filter\(\(offer\) => !isConnectorResource\(offer\.resource\)\)/)
  // Remove lives in the section head, outside the disabled body.
  const head = source.indexOf('<ResourceSectionHead')
  const body = source.indexOf('className="resource-edit-section__body"')
  assert.ok(head > 0 && head < body)
  assert.equal(CONNECTORS_OWNER_ONLY_NOTE, "Only the Card's owner changes these.")
  // The line is drawn on the connector row itself, not only under a recognised parent route.
  const note = source.indexOf('className="resource-edit-section__owner-only"')
  assert.ok(note > body, 'the note is inside the connector row body')
  assert.doesNotMatch(source, /resource-selection-children__owner-only/)
  // A guided edit link never seeds a change to another person's connector.
  assert.match(source, /const seedsForbiddenConnector = isConnectorResource\(resource\)\s*&& connectorsOwnerOnly\(item\.grantor_subject, platformUserId\)/)
  assert.match(source, /if \(opened && !seedsForbiddenConnector\)/)
})
