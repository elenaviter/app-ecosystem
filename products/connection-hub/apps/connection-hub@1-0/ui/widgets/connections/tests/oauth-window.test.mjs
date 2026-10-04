import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

// W435 (operator, 2026-10-05): in Safari "Connect GitHub" did nothing. The
// server logged every start as ok; the tab was opened after the start call's
// awaits, where Safari no longer counts the click and blocks the window.

const source = (path) => readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8')

// Browser-faithful: a refused open returns null, and so does an open with the
// noopener feature even when the tab opened (W435 review: a Chromium probe
// opened the provider tab while the helper reported it blocked).
function fakeWindow({ blockBlank = false, blockDirect = false } = {}) {
  const calls = []
  const opened = []
  const tab = { closed: false, opener: 'connection-hub', replaced: '', location: { replace(url) { tab.replaced = url } }, close() { tab.closed = true } }
  globalThis.window = {
    open(url, target, features) {
      calls.push([url, target, features || ''])
      const refused = url === 'about:blank' ? blockBlank : blockDirect
      if (refused) return null
      const handle = url === 'about:blank' ? tab : { closed: false, opener: 'connection-hub', url }
      opened.push(handle)
      return String(features || '').includes('noopener') ? null : handle
    },
  }
  return { calls, tab, opened }
}

const { openPendingAuthorizationWindow } = await import('../src/features/oauthWindow.ts')

test('the sign-in tab is opened blank at once, then sent to the provider', () => {
  const { calls, tab } = fakeWindow()
  const signIn = openPendingAuthorizationWindow()
  assert.deepEqual(calls, [['about:blank', '_blank', '']], 'opened inside the click, before any await')
  assert.equal(tab.opener, null, 'the provider page cannot reach back')
  assert.equal(signIn.go('https://github.com/login/oauth/authorize?x=1'), true)
  assert.equal(tab.replaced, 'https://github.com/login/oauth/authorize?x=1')
  assert.equal(calls.length, 1, 'no second window')
})

test('a failed start closes the blank tab', () => {
  const { tab } = fakeWindow()
  openPendingAuthorizationWindow().cancel()
  assert.equal(tab.closed, true)
})

test('a blocked blank tab falls back to one direct open that reports success truthfully and cuts the opener', () => {
  const { calls, opened } = fakeWindow({ blockBlank: true })
  const signIn = openPendingAuthorizationWindow()
  assert.equal(signIn.go('https://github.com/login/oauth/authorize'), true, 'an opened tab is never reported as blocked')
  assert.deepEqual(calls[1], ['https://github.com/login/oauth/authorize', '_blank', ''])
  assert.equal(opened.length, 1)
  assert.equal(opened[0].opener, null, 'the provider page cannot reach back')
})

test('only a really refused fallback reports blocked', () => {
  const { opened } = fakeWindow({ blockBlank: true, blockDirect: true })
  assert.equal(openPendingAuthorizationWindow().go('https://github.com/login/oauth/authorize'), false)
  assert.equal(opened.length, 0)
})

test('every Connect opens its tab before the start call and never after it', () => {
  for (const path of [
    'features/delegatedAccess/MyCardGithubSection.tsx',
    'features/delegatedToKdcube/DelegatedToKdcubePanel.tsx',
    'features/providerConnections/ProviderConnectCard.tsx',
  ]) {
    const text = source(path)
    const opened = text.indexOf('const signIn = openPendingAuthorizationWindow();')
    const started = text.indexOf('await dispatch(start', opened)
    assert.ok(opened > 0 && started > opened, `${path} opens the tab before awaiting the start`)
    assert.doesNotMatch(text, /window\.open\(result\.authorize_url/, `${path} opens nothing after the await`)
    assert.match(text, /signIn\.go\(result\.authorize_url\)/)
    assert.match(text, /signIn\.cancel\(\)/)
  }
})
