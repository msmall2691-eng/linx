// Every request goes through here, so there is one place that knows about the
// token header and one place that turns a failed response into an Error with a
// message worth showing a person.
//
// Paths are relative: Vite proxies /api to the backend in development, and in
// production the backend serves this build from the same origin.

const TOKEN_KEY = 'linx.token'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

function readErrorDetail(body, status) {
  const detail = body?.detail
  if (typeof detail === 'string') return detail
  // FastAPI validation errors arrive as a list of per-field objects; show the
  // first one rather than "[object Object]".
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0]
    const field = first.loc?.filter((p) => p !== 'body').join('.')
    return field ? `${field}: ${first.msg}` : first.msg
  }
  return `Request failed (${status})`
}

export async function apiFetch(path, { method = 'GET', body, auth = true } = {}) {
  const headers = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'

  const token = getToken()
  if (auth && token) headers.Authorization = `Bearer ${token}`

  let response
  try {
    response = await fetch(`/api${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
  } catch {
    throw new ApiError('Could not reach the server. Check your connection.', 0)
  }

  if (response.status === 204) return null

  let payload = null
  try {
    payload = await response.json()
  } catch {
    payload = null
  }

  if (!response.ok) {
    throw new ApiError(readErrorDetail(payload, response.status), response.status)
  }
  return payload
}
