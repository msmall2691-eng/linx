import { Link, useNavigate } from 'react-router-dom'

import { useAuth } from '../lib/auth.jsx'

const ROLE_LABEL = {
  owner: 'Property owner',
  cleaner: 'Cleaner',
  admin: 'Admin',
}

export default function Nav() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()

  function handleLogout() {
    logout()
    navigate('/')
  }

  return (
    <header className="border-b border-slate-200 bg-white">
      <nav className="mx-auto flex max-w-5xl items-center justify-between gap-4 px-4 py-3">
        <Link to="/" className="text-lg font-bold tracking-tight text-brand-700">
          linx
        </Link>

        {user ? (
          <div className="flex items-center gap-3">
            <span className="hidden text-sm text-slate-600 sm:inline">
              {user.full_name}
              <span className="ml-2 rounded-full bg-brand-50 px-2 py-0.5 text-xs font-medium text-brand-700">
                {ROLE_LABEL[user.role] ?? user.role}
              </span>
            </span>
            <button type="button" onClick={handleLogout} className="btn-secondary">
              Log out
            </button>
          </div>
        ) : (
          <div className="flex items-center gap-2">
            <Link to="/login" className="btn-secondary">
              Log in
            </Link>
            <Link to="/signup" className="btn-primary">
              Sign up
            </Link>
          </div>
        )}
      </nav>
    </header>
  )
}
