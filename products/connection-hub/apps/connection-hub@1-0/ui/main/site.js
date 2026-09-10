// Connection Hub standalone site shell.
//
// Served by the platform as this application's main view (ui.main_view), at
// /sites/<alias>/ or at a host mapped to the site. It owns the page chrome and
// the signed-out state, and hosts the connections widget in an iframe served
// from the widget's own bundle URL, so the widget resolves tenant, project and
// application from its route exactly as it does in every other host.

const DEFAULT_PLATFORM_PREFIX = '/platform'
const DEFAULT_WIDGET_ALIAS = 'connections_settings'
const DEFAULT_SIGN_IN_PATH = '/signin/'
const WIDGET_TAB_PARAMS = ['tab', 'view']
// One automatic sign-in attempt per page load: a stale session cookie heals
// without a click, and a genuinely signed-out visitor is not bounced in a loop.
const SIGN_IN_ATTEMPT_KEY = 'kdcube-connection-hub-signin-attempted'

const frame = document.getElementById('connections-frame')
const notice = document.getElementById('hub-notice')
const signinCard = document.getElementById('signin-card')
const signinCardButton = document.getElementById('signin-card-button')
const brandLink = document.getElementById('brand-link')
const scopeLabel = document.getElementById('runtime-scope')
const identityLabel = document.getElementById('identity')
const authButton = document.getElementById('auth-button')
const platformLink = document.getElementById('platform-link')

let platformConfig = null
let siteConfig = null
let profile = null
let route = null
let frameLoaded = false

function asObject(value) {
  return value && typeof value === 'object' ? value : {}
}

function unwrap(payload, key) {
  const record = asObject(payload)
  return asObject(record[key] ?? record)
}

// Tenant, project and application come from the site context the platform
// injects when serving /sites/<alias>/ or a host-mapped site; the static
// application routes are the fallback when the shell is opened directly.
function routeContext() {
  const contextNode = document.getElementById('kdcube-site-context')
  if (contextNode) {
    try {
      const context = asObject(JSON.parse(contextNode.textContent || '{}'))
      if (context.tenant && context.project && context.application_id) {
        return {
          tenant: String(context.tenant),
          project: String(context.project),
          applicationId: String(context.application_id),
        }
      }
    } catch (_error) {}
  }
  const baseUri = String(document.baseURI || '')
  const routePatterns = [
    /\/api\/integrations\/static\/([^/]+)\/([^/]+)\/([^/]+)(?:\/|$)/,
    /\/api\/integrations\/bundles\/([^/]+)\/([^/]+)\/([^/]+)\/public\/static(?:\/|$)/,
  ]
  const match = routePatterns
    .map((pattern) => baseUri.match(pattern))
    .find(Boolean)
  if (!match) return null
  return {
    tenant: decodeURIComponent(match[1]),
    project: decodeURIComponent(match[2]),
    applicationId: decodeURIComponent(match[3]),
  }
}

async function fetchJson(url, options = {}) {
  const { headers = {}, ...requestOptions } = options
  const response = await fetch(url, {
    credentials: 'include',
    cache: 'no-store',
    ...requestOptions,
    headers: { Accept: 'application/json', ...headers },
  })
  if (!response.ok) throw new Error(`${url} returned ${response.status}`)
  return response.json()
}

function setNotice(message, error = false) {
  notice.textContent = message
  notice.classList.toggle('error', error)
  notice.hidden = !message
}

function isAuthenticated(value) {
  const record = asObject(value)
  return Boolean(record.user_id) && String(record.user_type || '').toLowerCase() !== 'anonymous'
}

function platformPath() {
  const prefix = String(platformConfig?.routesPrefix || DEFAULT_PLATFORM_PREFIX).replace(/\/$/, '')
  return `${prefix}/chat`
}

function siteHomePath() {
  const alias = String(siteConfig?.site_alias || '').trim()
  return alias && window.location.pathname.startsWith('/sites/')
    ? `/sites/${encodeURIComponent(alias)}/`
    : '/'
}

function returnPath() {
  return `${window.location.pathname}${window.location.search}` || siteHomePath()
}

// The platform sign-in page: it reuses a live session (refreshing the
// session cookies) or runs the identity provider flow, then returns to
// `next`. The same page the platform bounces a signed-out widget URL to.
function signInUrl() {
  const configured = String(platformConfig?.auth?.loginUrl || siteConfig?.sign_in_url || '').trim()
  const url = new URL(configured || DEFAULT_SIGN_IN_PATH, window.location.origin)
  url.searchParams.set('next', returnPath())
  return url.toString()
}

function signInAttempted() {
  try { return window.sessionStorage.getItem(SIGN_IN_ATTEMPT_KEY) === '1' } catch (_error) { return false }
}

function markSignInAttempt(value) {
  try {
    if (value) window.sessionStorage.setItem(SIGN_IN_ATTEMPT_KEY, '1')
    else window.sessionStorage.removeItem(SIGN_IN_ATTEMPT_KEY)
  } catch (_error) {}
}

function goSignIn() {
  markSignInAttempt(true)
  window.location.assign(signInUrl())
}

// The widget URL: the widget's own bundle route, so its route resolver wins.
// A ?tab= (or #tab) on the site URL is passed through, so a link such as
// /sites/connections/?tab=delegated_by_kdcube opens the intended tab.
function widgetUrl() {
  const tenant = String(siteConfig?.tenant || platformConfig?.tenant || route?.tenant || '')
  const project = String(siteConfig?.project || platformConfig?.project || route?.project || '')
  const applicationId = String(siteConfig?.application_id || route?.applicationId || '')
  const widgetAlias = String(siteConfig?.widget_alias || DEFAULT_WIDGET_ALIAS)
  const url = new URL([
    '/api/integrations/bundles',
    encodeURIComponent(tenant),
    encodeURIComponent(project),
    encodeURIComponent(applicationId),
    'widgets',
    encodeURIComponent(widgetAlias),
  ].join('/'), window.location.origin)
  const query = new URLSearchParams(window.location.search)
  for (const key of WIDGET_TAB_PARAMS) {
    const value = query.get(key)
    if (value) url.searchParams.set(key, value)
  }
  const hash = String(window.location.hash || '').replace(/^#/, '').trim()
  if (hash && !url.searchParams.has('tab')) url.searchParams.set('tab', hash)
  return url.toString()
}

function runtimeConfig() {
  const tenant = String(siteConfig?.tenant || platformConfig?.tenant || route?.tenant || '')
  const project = String(siteConfig?.project || platformConfig?.project || route?.project || '')
  const applicationId = String(siteConfig?.application_id || route?.applicationId || '')
  return {
    configSource: 'website',
    baseUrl: window.location.origin,
    tenant,
    project,
    defaultTenant: tenant,
    defaultProject: project,
    defaultApp: applicationId,
    defaultAppBundleId: applicationId,
    auth: asObject(platformConfig?.auth),
    scene: {
      embedded: true,
      configSource: 'host',
      surface_ref: 'connection-hub.site',
      liveEventsTransport: 'scene',
    },
    liveEventsTransport: 'scene',
  }
}

function notifyWidget(reason) {
  if (!frameLoaded) return
  const authenticated = isAuthenticated(profile)
  const detail = {
    ready: true,
    authenticated,
    reason,
    user: authenticated
      ? {
          sub: profile.user_id || '',
          email: profile.email || '',
          name: profile.username || profile.name || profile.email || profile.user_id || '',
        }
      : null,
  }
  frame.contentWindow?.postMessage({ type: 'kdcube-auth-changed', auth: detail }, window.location.origin)
}

function renderAuth() {
  const authenticated = isAuthenticated(profile)
  identityLabel.textContent = authenticated
    ? String(profile.email || profile.username || profile.user_id || '')
    : ''
  authButton.textContent = authenticated ? 'Sign out' : 'Sign in'
  platformLink.href = platformPath()
}

// The widget is served from an authenticated route. Inside an iframe a
// signed-out request is answered with 403 rather than a sign-in redirect, so
// the shell shows its own sign-in card and mounts the widget only once the
// session is verified.
function renderStage() {
  const authenticated = isAuthenticated(profile)
  signinCard.hidden = authenticated
  if (authenticated) {
    const target = widgetUrl()
    if (frame.getAttribute('src') !== target) {
      frameLoaded = false
      frame.src = target
    }
    frame.hidden = false
  } else {
    frame.hidden = true
    if (frame.getAttribute('src')) {
      frame.removeAttribute('src')
      frameLoaded = false
    }
  }
}

async function refreshProfile(reason = 'profile') {
  const profileUrl = String(siteConfig?.profile_url || platformConfig?.auth?.profileUrl || '/profile')
  try {
    profile = await fetchJson(profileUrl)
  } catch (_error) {
    profile = null
  }
  renderAuth()
  renderStage()
  notifyWidget(reason)
  return profile
}

async function signOut() {
  const logoutUrl = String(platformConfig?.auth?.logoutUrl || '/api/platform/logout')
  try {
    await fetchJson(logoutUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ next: siteHomePath() }).toString(),
    })
  } finally {
    markSignInAttempt(true)
    await refreshProfile('logout')
  }
}

async function bootstrap() {
  try {
    route = routeContext()
    if (!route) throw new Error('Application route context is unavailable')

    const configUrl = [
      '/api/integrations/bundles',
      encodeURIComponent(route.tenant),
      encodeURIComponent(route.project),
      encodeURIComponent(route.applicationId),
      'public/site_config',
    ].join('/')
    siteConfig = unwrap(await fetchJson(configUrl), 'site_config')
    platformConfig = await fetchJson(
      String(siteConfig.platform_config_url || '/api/cp-frontend-config'),
    )

    const tenant = String(siteConfig.tenant || platformConfig.tenant || route.tenant)
    const project = String(siteConfig.project || platformConfig.project || route.project)

    document.title = String(siteConfig.title || 'Connection Hub')
    brandLink.href = siteHomePath()
    scopeLabel.textContent = `${tenant} / ${project}`
    platformLink.href = platformPath()
    frame.addEventListener('load', () => {
      frameLoaded = true
      setNotice('')
      notifyWidget('widget-load')
    })
    const current = await refreshProfile('initial')
    if (isAuthenticated(current)) {
      markSignInAttempt(false)
    } else if (!signInAttempted()) {
      // Expired or missing session cookie: let the sign-in page try to reuse
      // the live session before showing anyone a sign-in card.
      setNotice('Checking your session...')
      goSignIn()
      return
    }
    setNotice('')
  } catch (error) {
    setNotice(error instanceof Error ? error.message : String(error), true)
    scopeLabel.textContent = 'Runtime unavailable'
  }
}

authButton.addEventListener('click', () => {
  if (isAuthenticated(profile)) void signOut()
  else goSignIn()
})
signinCardButton.addEventListener('click', () => goSignIn())

// Another tab or the platform login page reports an auth change: re-probe.
window.addEventListener('kdcube-auth-changed', () => { void refreshProfile('auth-changed') })

window.addEventListener('message', (event) => {
  if (event.origin !== window.location.origin || event.source !== frame.contentWindow) return
  const message = asObject(event.data)
  if (message.type === 'kdcube-auth-required') {
    goSignIn()
    return
  }
  if (message.type !== 'CONFIG_REQUEST') return
  const data = asObject(message.data)
  const identity = String(data.identity || message.identity || '').trim()
  if (!identity) return
  frame.contentWindow?.postMessage({
    type: 'CONFIG_RESPONSE',
    identity,
    config: runtimeConfig(),
  }, window.location.origin)
})

void bootstrap()
