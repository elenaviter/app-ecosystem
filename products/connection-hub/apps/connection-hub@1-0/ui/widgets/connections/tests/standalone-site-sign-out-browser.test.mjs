// The standalone site's Sign out (W446): the real shell (main/index.html and
// site.js) in a browser, with the platform answered by fixtures. Signing out
// ends the platform session and then the identity provider's own sign-in, the
// same order as the platform chat, the board and "Switch account". Leaving the
// provider signed in would let the next sign-in return the same account
// without offering a choice.

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { launchBrowser } from './browser-fixture.mjs'

const ORIGIN = 'https://hub.example.test'
const SITE = `${ORIGIN}/api/integrations/static/tenant-one/project-one/connection-hub@1-0/`
const SITE_CONFIG = `${ORIGIN}/api/integrations/bundles/tenant-one/project-one/connection-hub%401-0/public/site_config`
const UPSTREAM = 'https://idp.example.test/logout?client_id=hub-client&logout_uri=https%3A%2F%2Fhub.example.test%2Fapi%2Fplatform%2Fsession%2Fsigned-out'
const MAIN = new URL('../../../main/', import.meta.url)

const files = {
  '': ['text/html', readFileSync(new URL('index.html', MAIN), 'utf8')],
  'site.js': ['text/javascript', readFileSync(new URL('site.js', MAIN), 'utf8')],
  'site-routing.js': ['text/javascript', readFileSync(new URL('site-routing.js', MAIN), 'utf8')],
  'styles.css': ['text/css', readFileSync(new URL('styles.css', MAIN), 'utf8')],
}

const browser = await launchBrowser()
const skip = browser ? false : 'no Chromium on this machine (Playwright cache or PLAYWRIGHT_CHROMIUM_EXECUTABLE)'
test.after(async () => { await browser?.close() })

// One signed-in visit; `upstreamLogoutUrl` is what the platform logout answers.
async function signedInSite(upstreamLogoutUrl) {
  const page = await browser.newPage()
  const seen = []
  let signedIn = true
  await page.route('**/*', async (route) => {
    const request = route.request()
    const url = request.url()
    const path = url.split('?')[0]
    seen.push(`${request.method()} ${path}`)
    if (path.startsWith(SITE)) {
      const file = files[path.slice(SITE.length)]
      return file ? route.fulfill({ contentType: file[0], body: file[1] }) : route.fulfill({ status: 404, body: '' })
    }
    if (path === SITE_CONFIG) {
      return route.fulfill({ json: { site_config: { tenant: 'tenant-one', project: 'project-one', application_id: 'connection-hub@1-0' } } })
    }
    if (path === `${ORIGIN}/api/cp-frontend-config`) {
      return route.fulfill({ json: { auth: { logoutUrl: '/api/platform/logout', profileUrl: '/profile', loginUrl: '/signin/' } } })
    }
    if (path === `${ORIGIN}/profile`) {
      return route.fulfill({ json: signedIn ? { user_id: 'user-1', email: 'person@example.test' } : { user_type: 'anonymous' } })
    }
    if (path === `${ORIGIN}/api/platform/logout`) {
      signedIn = false
      return route.fulfill({ json: { ok: true, invalidated: true, upstreamLogoutUrl } })
    }
    return route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>elsewhere</title>' })
  })
  await page.goto(SITE)
  await page.locator('#identity', { hasText: 'person@example.test' }).waitFor()
  return { page, seen }
}

test('Sign out ends the platform session, then follows the provider sign-out', { skip }, async () => {
  const { page, seen } = await signedInSite(UPSTREAM)
  try {
    await page.locator('#auth-button').click()
    await page.waitForURL(UPSTREAM, { timeout: 5000 })
    const logout = seen.indexOf(`POST ${ORIGIN}/api/platform/logout`)
    const provider = seen.indexOf(`GET ${UPSTREAM.split('?')[0]}`)
    assert.ok(logout >= 0, 'the platform logout was posted')
    assert.ok(provider > logout, 'the provider sign-out follows the platform logout')
    assert.ok(!seen.some((entry) => entry.endsWith('/signin/')), 'no sign-in starts on the way out')
  } finally {
    await page.close()
  }
})

test('Sign out without a provider sign-out shows the signed-out site and does not sign in again', { skip }, async () => {
  for (const upstreamLogoutUrl of ['', 'javascript:alert(1)', 'data:text/html,signed-out']) {
    const { page, seen } = await signedInSite(upstreamLogoutUrl)
    try {
      await page.locator('#auth-button').click()
      await page.locator('#auth-button', { hasText: 'Sign in' }).waitFor()
      assert.equal(new URL(page.url()).href, SITE, `${upstreamLogoutUrl || 'no address'}: the page stays on the site`)
      assert.equal(await page.locator('#signin-card').isVisible(), true)
      assert.ok(!seen.some((entry) => entry.endsWith('/signin/')), 'no automatic sign-in after signing out')
    } finally {
      await page.close()
    }
  }
})
