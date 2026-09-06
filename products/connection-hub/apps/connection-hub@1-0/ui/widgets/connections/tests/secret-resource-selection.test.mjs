import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildSecretResources,
  isBroadSecretResource,
  secretResourceLabel,
} from '../src/features/delegatedAccess/secretResourceSelection.ts'

const base = {
  tenant: 'tenant-a',
  project: 'project-a',
  scope: 'platform',
  coverage: 'exact',
  target: 'one',
  key: 'services.brave.api_key',
  bundleId: '',
  userId: '',
  userArea: 'direct',
  userBundleId: '',
}

test('platform exact and prefix selectors are canonical', () => {
  const exact = buildSecretResources(base)[0]
  const prefix = buildSecretResources({ ...base, coverage: 'prefix', key: 'platform.services' })[0]
  assert.equal(exact, 'urn:kdcube:management:secret:tenant-a:project-a:platform:_:platform.services.brave.api_key')
  assert.equal(prefix, 'urn:kdcube:management:secret:tenant-a:project-a:platform:_:platform.services.*')
  assert.equal(isBroadSecretResource(exact), false)
  assert.equal(isBroadSecretResource(prefix), true)
})

test('bundle and user selectors retain exact ownership', () => {
  const bundle = buildSecretResources({ ...base, scope: 'bundle', bundleId: 'mail@1-0', key: 'oauth.client_secret' })[0]
  const userBundle = buildSecretResources({
    ...base,
    scope: 'user',
    userId: 'user-17',
    userArea: 'bundle',
    userBundleId: 'mail@1-0',
    coverage: 'all',
  })[0]
  assert.equal(bundle, 'urn:kdcube:management:secret:tenant-a:project-a:bundle:mail@1-0:oauth.client_secret')
  assert.equal(userBundle, 'urn:kdcube:management:secret:tenant-a:project-a:user:user-17~mail@1-0:*')
  assert.match(secretResourceLabel(userBundle), /User user-17~mail@1-0/)
})

test('whole deployment expands to platform, bundle, and user selectors', () => {
  const selectors = buildSecretResources({ ...base, scope: 'deployment' })
  assert.deepEqual(selectors, [
    'urn:kdcube:management:secret:tenant-a:project-a:platform:_:platform.*',
    'urn:kdcube:management:secret:tenant-a:project-a:bundle:*:*',
    'urn:kdcube:management:secret:tenant-a:project-a:user:*:*',
  ])
  assert.ok(selectors.every(isBroadSecretResource))
})

test('free-form wildcards and incomplete owners are rejected', () => {
  assert.throws(() => buildSecretResources({ ...base, key: 'services.*.token' }), /Key is invalid/)
  assert.throws(() => buildSecretResources({ ...base, scope: 'bundle', bundleId: '' }), /Application ID is invalid/)
  assert.throws(() => buildSecretResources({ ...base, scope: 'user', target: 'all', userArea: 'bundle' }), /Choose one user/)
})
