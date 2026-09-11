import { Navigate, useLocation } from 'react-router-dom'

import { useAuth } from '../lib/auth.jsx'

/**
 * Route guard. `roles` narrows a route to specific roles.
 *
 * This is a convenience for the person using the app, not a security boundary —
 * the real gate is `require_role` on the server. Hiding a link never protects
 * anything; the endpoint refusing the request does.
 */
export default function RequireAuth({ children, roles }) {
  const { user, loading } = useAuth()
  const location = useLocation()

  if (loading) {
    return (
      <div className="flex min-h-[60vh] items-center justify-center text-sm text-slate-500">
        Loading…
      </div>
    )
  }

  if (!user) {
    return <Navigate to="/login" state={{ from: location.pathname }} replace />
  }

  if (roles && !roles.includes(user.role)) {
    return <Navigate to="/dashboard" replace />
  }

  return children
}
