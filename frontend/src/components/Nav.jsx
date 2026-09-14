import { Link, NavLink, useNavigate } from 'react-router-dom'

import { useAuth } from '../lib/auth.jsx'

const ROLE_LABEL = {
  owner: 'Property owner',
  cleaner: 'Cleaner',
  admin: 'Admin',
}

// Only the links a role can actually use. This is for the person's benefit, not
// for security — the server's role gate is what actually refuses the request.
const LINKS = {
  owner: [
    { to: '/turnovers', label: 'Turnovers' },
    { to: '/properties', label: 'Properties' },
  ],
  cleaner: [
    { to: '/board', label: 'Open turnovers' },
    { to: '/jobs', label: 'Your jobs' },
    { to: '/cleaner/profile', label: 'Your profile' },
  ],
  admin: [{ to: '/admin/vetting', label: 'Vetting queue' }],
}

export default function Nav() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()

  function handleLogout() {
    logout()
    navigate('/')
  }

  const links = user ? (LINKS[user.role] ?? []) : []

  return (
    <header className="border-b border-slate-200 bg-white">
      <nav className="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-x-6 gap-y-3 px-4 py-3">
        <div className="flex items-center gap-6">
          <Link
            to={user ? '/dashboard' : '/'}
            className="text-lg font-bold tracking-tight text-brand-700"
          >
            linx
          </Link>

          {links.length > 0 && (
            <div className="flex items-center gap-4">
              {links.map((link) => (
                <NavLink
                  key={link.to}
                  to={link.to}
                  className={({ isActive }) =>
                    `text-sm font-medium transition ${
                      isActive ? 'text-brand-700' : 'text-slate-600 hover:text-slate-900'
                    }`
                  }
                >
                  {link.label}
                </NavLink>
              ))}
            </div>
          )}
        </div>

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
