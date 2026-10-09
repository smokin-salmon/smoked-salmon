const BASE = '/api'

let unauthorizedHandler: (() => void) | null = null

/** Registered by App to flip back to the login screen when a call 401s. */
export function onUnauthorized(cb: () => void) {
  unauthorizedHandler = cb
}

async function handle<T>(res: Response): Promise<T> {
  if (res.status === 401) {
    unauthorizedHandler?.()
    throw new Error('Authentication required.')
  }
  if (!res.ok) throw new Error(await errorDetail(res))
  return res.json()
}

export async function apiGet<T>(path: string): Promise<T> {
  return handle<T>(await fetch(BASE + path))
}

/** Every request but GET and HEAD must be JSON: the server refuses anything else. */
export async function apiPost<T>(path: string, body: unknown = {}): Promise<T> {
  const res = await fetch(BASE + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  return handle<T>(res)
}

async function errorDetail(res: Response): Promise<string> {
  try {
    const data = await res.json()
    if (typeof data.detail === 'string') return data.detail
    return JSON.stringify(data.detail ?? data)
  } catch {
    return `${res.status} ${res.statusText}`
  }
}

/** Whether this browser holds a valid session cookie. */
export async function checkAuth(): Promise<boolean> {
  const res = await fetch(BASE + '/auth')
  return res.ok
}

/** Exchange the token for a session cookie. Returns true on success. */
export async function login(token: string): Promise<boolean> {
  const res = await fetch(BASE + '/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  })
  if (res.ok || res.status === 401) return res.ok
  throw new Error(await errorDetail(res))
}

export async function logout(): Promise<void> {
  await apiPost('/logout')
}

/**
 * The token of a login link (`/#token=...`), removed from the address bar and the history at once.
 * A fragment never reaches the server; this keeps it out of bookmarks and screenshots too.
 */
export function takeTokenFromUrl(): string | null {
  const match = /^#token=(.+)$/.exec(location.hash)
  if (!match) return null
  history.replaceState(null, '', location.pathname + location.search)
  try {
    return decodeURIComponent(match[1])
  } catch {
    // Malformed (#token=%): sent as it is, the login refuses it and says so.
    return match[1]
  }
}

/** The job events websocket, on this page's own host: the server checks its Origin. */
export function wsUrl(): string {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${location.host}${BASE}/ws`
}
