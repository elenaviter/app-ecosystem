import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { buildWidgetUrl } from '../../../main/site-routing.js'
import { accessCardFocusFromParams } from '../src/features/delegatedAccess/accessCardFocus.ts'

const base = {
  origin: 'https://hub.example.test',
  tenant: 'tenant one',
  project: 'project one',
  applicationId: 'connection-hub@1-0',
}

test('standalone site hands an exact delegated Card deep link to the widget', () => {
  const url = new URL(buildWidgetUrl({
    ...base,
    search: '?tab=delegatedAccess&access_id=oauth-fable-card',
  }))

  assert.equal(url.pathname, '/api/integrations/bundles/tenant%20one/project%20one/connection-hub%401-0/widgets/connections_settings')
  assert.equal(url.searchParams.get('tab'), 'delegatedAccess')
  assert.equal(url.searchParams.get('access_id'), 'oauth-fable-card')
})

test('standalone site preserves every exact Card selector accepted by the widget', () => {
  for (const [key, value, manualOnly, controlOnly] of [
    ['access_id', 'oauth-card', false, false],
    ['manual_access_id', 'manual-card', true, false],
    ['control_card_id', 'control-card', false, true],
  ]) {
    const url = new URL(buildWidgetUrl({
      ...base,
      search: `?tab=delegatedAccess&${key}=${value}`,
    }))
    assert.equal(url.searchParams.get(key), value)
    const focus = accessCardFocusFromParams((name) => url.searchParams.get(name) || '')
    assert.equal(focus.accessId, value)
    assert.equal(focus.manualOnly, manualOnly)
    assert.equal(focus.controlOnly, controlOnly)
  }
})

test('standalone site forwards project person control coordinates', () => {
  const url = new URL(buildWidgetUrl({
    ...base,
    search: '?control_card_id=person-control-1&project_ref=work%3Aproject%3Aquickstart&target_subject=platform-user-2',
  }))
  assert.equal(url.searchParams.get('control_card_id'), 'person-control-1')
  assert.equal(url.searchParams.get('project_ref'), 'work:project:quickstart')
  assert.equal(url.searchParams.get('target_subject'), 'platform-user-2')
})

test('standalone site forwards pending invitation control coordinates', () => {
  const url = new URL(buildWidgetUrl({
    ...base,
    search: '?control_card_id=invitation-control-1&project_ref=work%3Aproject%3Aquickstart&invitation_ref=work%3Ainvitation%3Ainv-1',
  }))
  assert.equal(url.searchParams.get('control_card_id'), 'invitation-control-1')
  assert.equal(url.searchParams.get('project_ref'), 'work:project:quickstart')
  assert.equal(url.searchParams.get('invitation_ref'), 'work:invitation:inv-1')
})

test('standalone site forwards guided Card context but excludes unknown fields', () => {
  const url = new URL(buildWidgetUrl({
    ...base,
    search: '?access_id=card-1&resource=door-1&outer_operation=send&account_id=acct-1&bearer_token=secret&next=%2Felsewhere',
    hash: '#delegatedAccess',
  }))

  assert.equal(url.searchParams.get('tab'), 'delegatedAccess')
  assert.equal(url.searchParams.get('resource'), 'door-1')
  assert.equal(url.searchParams.get('outer_operation'), 'send')
  assert.equal(url.searchParams.get('account_id'), 'acct-1')
  assert.equal(url.searchParams.has('bearer_token'), false)
  assert.equal(url.searchParams.has('next'), false)
})

test('the served site delegates its iframe URL to the tested builder', () => {
  const site = readFileSync(new URL('../../../main/site.js', import.meta.url), 'utf8')
  const descriptor = readFileSync(new URL('../../../../config/bundles.template.yaml', import.meta.url), 'utf8')
  assert.match(site, /import \{ buildWidgetUrl \} from '\.\/site-routing\.js'/)
  assert.match(site, /return buildWidgetUrl\(\{/)
  assert.match(descriptor, /cp index\.html site\.js site-routing\.js styles\.css/)
})
