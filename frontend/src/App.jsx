import { Route, Routes } from 'react-router-dom'

import Nav from './components/Nav.jsx'
import RequireAuth from './components/RequireAuth.jsx'
import AdminVetting from './pages/AdminVetting.jsx'
import Board from './pages/Board.jsx'
import CleanerJobs from './pages/CleanerJobs.jsx'
import CleanerProfile from './pages/CleanerProfile.jsx'
import Dashboard from './pages/Dashboard.jsx'
import Landing from './pages/Landing.jsx'
import Login from './pages/Login.jsx'
import NotFound from './pages/NotFound.jsx'
import PropertyDetail from './pages/PropertyDetail.jsx'
import PropertyList from './pages/PropertyList.jsx'
import PropertyNew from './pages/PropertyNew.jsx'
import Signup from './pages/Signup.jsx'
import TurnoverDetail from './pages/TurnoverDetail.jsx'
import TurnoverList from './pages/TurnoverList.jsx'
import TurnoverNew from './pages/TurnoverNew.jsx'

/** Properties and turnovers are the owner's side of the marketplace. */
function OwnerRoute({ children }) {
  return <RequireAuth roles={['owner']}>{children}</RequireAuth>
}

/** The profile and the bench board are the cleaner's side. */
function CleanerRoute({ children }) {
  return <RequireAuth roles={['cleaner']}>{children}</RequireAuth>
}

/**
 * Admin-only screens.
 *
 * A convenience for the person, not a security boundary — `require_role` on
 * the server is what actually refuses the request.
 */
function AdminRoute({ children }) {
  return <RequireAuth roles={['admin']}>{children}</RequireAuth>
}

export default function App() {
  return (
    <div className="min-h-screen">
      <Nav />
      <main>
        <Routes>
          <Route path="/" element={<Landing />} />
          <Route path="/login" element={<Login />} />
          <Route path="/signup" element={<Signup />} />

          <Route
            path="/dashboard"
            element={
              <RequireAuth>
                <Dashboard />
              </RequireAuth>
            }
          />

          <Route
            path="/properties"
            element={
              <OwnerRoute>
                <PropertyList />
              </OwnerRoute>
            }
          />
          <Route
            path="/properties/new"
            element={
              <OwnerRoute>
                <PropertyNew />
              </OwnerRoute>
            }
          />
          <Route
            path="/properties/:propertyId"
            element={
              <OwnerRoute>
                <PropertyDetail />
              </OwnerRoute>
            }
          />

          <Route
            path="/turnovers"
            element={
              <OwnerRoute>
                <TurnoverList />
              </OwnerRoute>
            }
          />
          <Route
            path="/turnovers/new"
            element={
              <OwnerRoute>
                <TurnoverNew />
              </OwnerRoute>
            }
          />
          <Route
            path="/turnovers/:turnoverId"
            element={
              <OwnerRoute>
                <TurnoverDetail />
              </OwnerRoute>
            }
          />

          <Route
            path="/cleaner/profile"
            element={
              <CleanerRoute>
                <CleanerProfile />
              </CleanerRoute>
            }
          />
          <Route
            path="/board"
            element={
              <CleanerRoute>
                <Board />
              </CleanerRoute>
            }
          />
          <Route
            path="/jobs"
            element={
              <CleanerRoute>
                <CleanerJobs />
              </CleanerRoute>
            }
          />

          <Route
            path="/admin/vetting"
            element={
              <AdminRoute>
                <AdminVetting />
              </AdminRoute>
            }
          />

          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
    </div>
  )
}
