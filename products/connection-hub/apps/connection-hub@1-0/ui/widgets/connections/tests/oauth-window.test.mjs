import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

// W435 (operator, 2026-10-05): in Safari "Connect GitHub" did nothing. The
// server logged every start as ok; the tab was opened after the start call's
// awaits, where Safari no longer counts the click and blocks the window.

const source = (path) => readFileSync(new URL(`../src/${path}`, import.meta.url), 'utf8')

function fakeWindow({ blockBlank = false } = {}) {
  const calls = []
  const tab = { closed: false, opener: 'connection-hub', replaced: '', location: { replace(url) { tab.replaced = url } }, close() { tab.closed = true } }
  globalThis.window = {
    open(url, target, features) {
      calls.push([url, target, features || ''])
      if (url === 'about:blank') return blockBlank ? null : tab
      return blockBlank ? { closed: false } : null
    },
  }
  return { calls, tab }
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

test('a blocked blank tab falls back to one direct open, and says whether it worked', () => {
  const { calls } = fakeWindow({ blockBlank: true })
  const signIn = openPendingAuthorizationWindow()
  assert.equal(signIn.go('https://github.com/login/oauth/authorize'), true)
  assert.deepEqual(calls[1], ['https://github.com/login/oauth/authorize', '_blank', 'noopener,noreferrer'])
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
