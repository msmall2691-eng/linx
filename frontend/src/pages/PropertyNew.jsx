import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import PropertyForm from '../components/PropertyForm.jsx'
import { apiFetch } from '../lib/api.js'

export default function PropertyNew() {
  const navigate = useNavigate()
  const [error, setError] = useState(null)

  async function handleSubmit(payload) {
    setError(null)
    try {
      const created = await apiFetch('/properties', { method: 'POST', body: payload })
      navigate(`/properties/${created.id}`, { replace: true })
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <div className="mx-auto max-w-2xl px-4 py-10">
      <Link to="/properties" className="text-sm text-brand-600 hover:underline">
        ← Your properties
      </Link>
      <h1 className="mt-2 text-2xl font-bold tracking-tight">Add a property</h1>
      <PropertyForm onSubmit={handleSubmit} submitLabel="Add property" error={error} />
    </div>
  )
}
