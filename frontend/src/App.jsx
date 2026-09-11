import { Route, Routes } from 'react-router-dom'

import Nav from './components/Nav.jsx'
import RequireAuth from './components/RequireAuth.jsx'
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

          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
    </div>
  )
}
