// W605: one port policy for every listening test server in this widget.
//
// On 6 October a browser fixture shadowed dev-main's live UI: Vite reads
// `server.port: 0` as "unset" and falls back to 5173, the port the KDCube web
// proxy serves. A fixture now asks the operating system for a free loopback
// port, refuses any port a live service uses, and hands Vite that exact port
// with `strictPort`, so a lost race fails instead of moving to another port.
// The reserved list is the one kdcube-docs/procedures/cicd/source-test-environment.md
// names. The Problem Board widget (Applications) and the Connection Hub widget
// (App Ecosystem) keep byte-identical copies of this file, since neither
// repository can import the other; both safe-port tests pin the same sha256.
import { createServer as createNetServer } from 'node:net'

export const LOOPBACK = '127.0.0.1'

/** Ports a live service uses on a dev host; a test server never takes one. */
export const RESERVED_PORTS = Object.freeze([
  443, // web proxy TLS
  5173, // KDCube web proxy (Caddy forwards UI routes to it)
  5432, // PostgreSQL
  6379, // Redis
  7781, // host secrets vault
  8010, // chat ingress
  18080, // Caddy, the tunnel's local entry
  24678, // Vite's fixed HMR websocket port
])

const reserved = new Set(RESERVED_PORTS)

export function isReservedPort(port) {
  return reserved.has(Number(port))
}

function osLoopbackPort() {
  return new Promise((resolve, reject) => {
    const probe = createNetServer()
    probe.once('error', reject)
    probe.listen(0, LOOPBACK, () => {
      const address = probe.address()
      const port = address && typeof address === 'object' ? address.port : 0
      probe.close((error) => error ? reject(error) : resolve(port))
    })
  })
}

/**
 * A free loopback port the OS just handed out that no live service uses.
 * A reserved or invalid answer is retried a bounded number of times, then
 * refused: there is no fallback port.
 */
export async function allocateLoopbackPort({ attempts = 5, allocate = osLoopbackPort } = {}) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const port = await allocate()
    if (Number.isInteger(port) && port > 0 && port < 65536 && !isReservedPort(port)) return port
  }
  throw new Error(`No safe loopback test port after ${attempts} attempts; refusing to fall back to any other port.`)
}

/**
 * Refuse a Vite server config that could land on a live port before it
 * listens: literal port 0 (Vite's 5173 fallback), a reserved port, a missing
 * strictPort, or a host other than loopback. A middleware-mode server must
 * not open Vite's HMR websocket (fixed port 24678 on every interface).
 */
export function assertSafeViteServer(server = {}) {
  if (server.middlewareMode) {
    if (server.hmr !== false || server.ws !== false) {
      throw new Error('A middleware-mode Vite test server must set hmr: false and ws: false; otherwise it listens on 24678 on every interface.')
    }
    return server
  }
  const port = server.port
  if (!Number.isInteger(port) || port <= 0 || port >= 65536) throw new Error(`Refusing Vite test port ${String(port)}: pass an explicit positive port (0 means 5173 to Vite).`)
  if (isReservedPort(port)) throw new Error(`Refusing Vite test port ${port}: a live service uses it.`)
  if (server.strictPort !== true) throw new Error('Refusing a Vite test server without strictPort: it would move to another port on a clash.')
  if (server.host !== LOOPBACK) throw new Error(`Refusing Vite test host ${String(server.host)}: bind ${LOOPBACK} only.`)
  return server
}

/** The `server` block for a listening Vite test server, checked before use. */
export async function safeViteServer(extra = {}, options = {}) {
  const port = await allocateLoopbackPort(options)
  return assertSafeViteServer({ ...extra, port, host: LOOPBACK, strictPort: true })
}

/** The `server` block for an in-process (middleware-mode) Vite test server. */
export function inProcessViteServer(extra = {}) {
  return assertSafeViteServer({ ...extra, middlewareMode: true, hmr: false, ws: false })
}
