import { useState } from 'react'
import { Link, Navigate, useLocation } from 'react-router-dom'

import { useAuth } from '../lib/auth.jsx'

export default function Login() {
  const { user, login } = useAuth()
  const location = useLocation()

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  // Where a successful login lands, decided once.
  //
  // The route guard sends you here with the page you were heading for in
  // router state, so that is the destination; the dashboard is the fallback
  // for someone who came to the login page on purpose.
  //
  // This redirect is the only thing that navigates. Pairing it with an
  // imperative navigate() in the submit handler put two authors on one
  // decision and made the outcome a race — one that react-router 6 and 7
  // happened to resolve differently, so "log in, land back where you were
  // heading" silently became "always land on the dashboard".
  const destination = location.state?.from ?? '/dashboard'

  if (user) return <Navigate to={destination} replace />

  async function handleSubmit(event) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await login(email, password)
      // No navigate() here: setting the user re-renders, and the redirect
      // above takes it from there.
    } catch (err) {
      setError(err.message)
      setSubmitting(false)
    }
  }

  return (
    <div className="mx-auto max-w-md px-4 py-16">
      <h1 className="text-2xl font-bold tracking-tight">Log in</h1>

      <form onSubmit={handleSubmit} className="card mt-6 space-y-4">
        {error && (
          <p role="alert" className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">
            {error}
          </p>
        )}

        <div>
          <label htmlFor="email" className="field-label">
            Email
          </label>
          <input
            id="email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="field-input"
          />
        </div>

        <div>
          <label htmlFor="password" className="field-label">
            Password
          </label>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="field-input"
          />
        </div>

        <button type="submit" disabled={submitting} className="btn-primary w-full">
          {submitting ? 'Logging in…' : 'Log in'}
        </button>
      </form>

      <p className="mt-4 text-sm text-slate-600">
        No account yet?{' '}
        <Link to="/signup" className="font-medium text-brand-600 hover:underline">
          Sign up
        </Link>
      </p>
    </div>
  )
}
