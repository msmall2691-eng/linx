import { useRef, useState } from 'react'

import Alert from './Alert.jsx'
import { apiFetch, getToken } from '../lib/api.js'

const TYPE_LABELS = {
  id: 'Photo ID',
  insurance: 'Proof of insurance',
  reference: 'Reference',
}

const STATUS_STYLES = {
  pending: 'bg-amber-100 text-amber-800',
  approved: 'bg-emerald-100 text-emerald-800',
  rejected: 'bg-red-100 text-red-700',
}

// What a cleaner needs to upload, and what each one is for. The insurance row
// says out loud that it is optional — a required-looking field that isn't is
// how supply gets scared off.
const SLOTS = [
  { type: 'id', hint: 'A driver’s licence or passport photo.', required: true },
  { type: 'reference', hint: 'A note from someone you have cleaned for.', required: true },
  { type: 'insurance', hint: 'Optional — shown to owners, never required to bid.', required: false },
]

export default function DocumentList({ documents, onChange }) {
  const [error, setError] = useState(null)
  const [busyType, setBusyType] = useState(null)
  const inputs = useRef({})

  async function handleUpload(type, file) {
    if (!file) return
    setError(null)
    setBusyType(type)

    const body = new FormData()
    body.append('upload', file)

    try {
      // Sent as multipart, so this one call bypasses apiFetch's JSON handling
      // and sets the auth header itself.
      const response = await fetch(
        `/api/cleaner/documents?document_type=${encodeURIComponent(type)}`,
        { method: 'POST', headers: { Authorization: `Bearer ${getToken()}` }, body },
      )
      const payload = await response.json().catch(() => null)
      if (!response.ok) {
        throw new Error(
          typeof payload?.detail === 'string'
            ? payload.detail
            : `Upload failed (${response.status})`,
        )
      }
      onChange(payload)
    } catch (err) {
      setError(err.message)
    } finally {
      setBusyType(null)
      if (inputs.current[type]) inputs.current[type].value = ''
    }
  }

  async function handleRemove(documentId) {
    setError(null)
    try {
      onChange(await apiFetch(`/cleaner/documents/${documentId}`, { method: 'DELETE' }))
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <div className="space-y-4">
      <Alert>{error}</Alert>

      {SLOTS.map((slot) => {
        const existing = documents.filter((doc) => doc.type === slot.type)
        return (
          <div key={slot.type} className="card">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h3 className="font-medium">
                  {TYPE_LABELS[slot.type]}
                  {!slot.required && (
                    <span className="ml-2 text-xs font-normal text-slate-500">optional</span>
                  )}
                </h3>
                <p className="mt-1 text-sm text-slate-600">{slot.hint}</p>
              </div>

              <label className="btn-secondary cursor-pointer">
                {busyType === slot.type ? 'Uploading…' : 'Upload'}
                <input
                  ref={(node) => {
                    inputs.current[slot.type] = node
                  }}
                  type="file"
                  accept="image/jpeg,image/png,image/heic,image/webp,application/pdf"
                  className="sr-only"
                  onChange={(event) => handleUpload(slot.type, event.target.files?.[0])}
                />
              </label>
            </div>

            {existing.length > 0 && (
              <ul className="mt-4 space-y-2">
                {existing.map((doc) => (
                  <li
                    key={doc.id}
                    className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-slate-50 px-3 py-2 text-sm"
                  >
                    <span className="min-w-0 truncate">
                      {doc.original_filename ?? 'Uploaded file'}
                    </span>
                    <span className="flex items-center gap-2">
                      <span
                        className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                          STATUS_STYLES[doc.status] ?? STATUS_STYLES.pending
                        }`}
                      >
                        {doc.status}
                      </span>
                      {doc.status === 'pending' && (
                        <button
                          type="button"
                          onClick={() => handleRemove(doc.id)}
                          className="text-xs font-medium text-red-700 hover:underline"
                        >
                          Remove
                        </button>
                      )}
                    </span>
                    {doc.review_notes && (
                      <p className="w-full text-xs text-slate-600">{doc.review_notes}</p>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )
      })}
    </div>
  )
}
