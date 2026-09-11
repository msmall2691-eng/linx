import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'

import { apiFetch, getToken, setToken } from './api.js'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  // Starts true so a refresh on a protected route waits for /auth/me instead of
  // bouncing the user to the login screen before the token has been checked.
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false

    async function restoreSession() {
      if (!getToken()) {
        setLoading(false)
        return
      }
      try {
        const me = await apiFetch('/auth/me')
        if (!cancelled) setUser(me)
      } catch {
        // Expired or tampered token — drop it rather than leaving a dead
        // credential in storage to fail every later request.
        setToken(null)
        if (!cancelled) setUser(null)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    restoreSession()
    return () => {
      cancelled = true
    }
  }, [])

  const acceptToken = useCallback((response) => {
    setToken(response.access_token)
    setUser(response.user)
    return response.user
  }, [])

  const login = useCallback(
    async (email, password) =>
      acceptToken(
        await apiFetch('/auth/login', {
          method: 'POST',
          auth: false,
          body: { email, password },
        }),
      ),
    [acceptToken],
  )

  const signup = useCallback(
    async (payload) =>
      acceptToken(
        await apiFetch('/auth/signup', { method: 'POST', auth: false, body: payload }),
      ),
    [acceptToken],
  )

  const logout = useCallback(() => {
    setToken(null)
    setUser(null)
  }, [])

  const value = useMemo(
    () => ({ user, loading, login, signup, logout }),
    [user, loading, login, signup, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (context === null) {
    throw new Error('useAuth must be used inside an AuthProvider')
  }
  return context
}
