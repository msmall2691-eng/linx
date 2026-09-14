import { useEffect, useState } from 'react'

import Alert from '../components/Alert.jsx'
import DocumentList from '../components/DocumentList.jsx'
import PayoutPanel from '../components/PayoutPanel.jsx'
import VettingPanel from '../components/VettingPanel.jsx'
import { apiFetch } from '../lib/api.js'
import { useConfig } from '../lib/config.jsx'

const BLANK = {
  bio: '',
  service_lat: '',
  service_lng: '',
  service_radius_miles: 25,
}

export default function CleanerProfile() {
  const { region_name: regionName } = useConfig()

  const [profile, setProfile] = useState(null)
  const [form, setForm] = useState(BLANK)
  const [error, setError] = useState(null)
  const [formError, setFormError] = useState(null)
  const [saving, setSaving] = useState(false)
  const [checkBusy, setCheckBusy] = useState(false)

  function adopt(loaded) {
    setProfile(loaded)
    setForm({
      bio: loaded.bio ?? '',
      service_lat: loaded.service_lat ?? '',
      service_lng: loaded.service_lng ?? '',
      service_radius_miles: loaded.service_radius_miles ?? 25,
    })
  }

  useEffect(() => {
    apiFetch('/cleaner/profile')
      .then(adopt)
      .catch((err) => {
        // 404 is the ordinary first visit, not a failure — the form below is
        // how the profile gets created.
        if (err.status === 404) setProfile({ isNew: true, documents: [], vetting: null })
        else setError(err.message)
      })
  }, [])

  function update(field) {
    return (event) => setForm((prev) => ({ ...prev, [field]: event.target.value }))
  }

  async function handleSave(event) {
    event.preventDefault()
    setFormError(null)
    setSaving(true)
    try {
      adopt(
        await apiFetch('/cleaner/profile', {
          method: 'PUT',
          body: {
            bio: form.bio || null,
            service_lat: form.service_lat === '' ? null : String(form.service_lat),
            service_lng: form.service_lng === '' ? null : String(form.service_lng),
            service_radius_miles: Number(form.service_radius_miles),
          },
        }),
      )
    } catch (err) {
      setFormError(err.message)
    } finally {
      setSaving(false)
    }
  }

  async function handleBackgroundCheck() {
    setError(null)
    setCheckBusy(true)
    try {
      adopt(await apiFetch('/cleaner/background-check', { method: 'POST' }))
    } catch (err) {
      setError(err.message)
    } finally {
      setCheckBusy(false)
    }
  }

  if (error && !profile) {
    return (
      <div className="mx-auto max-w-2xl px-4 py-10">
        <Alert>{error}</Alert>
      </div>
    )
  }

  if (!profile) {
    return <p className="mx-auto max-w-2xl px-4 py-10 text-sm text-slate-500">Loading…</p>
  }

  const checkStatus = profile.background_check_status
  const checkOrdered = checkStatus && checkStatus !== 'not_started'

  return (
    <div className="mx-auto max-w-3xl px-4 py-10">
      <h1 className="text-2xl font-bold tracking-tight">Your cleaner profile</h1>
      <p className="mt-1 text-sm text-slate-600">
        Everyone who cleans through linx passes ID verification and a background check
        first. A person reviews each one, which usually takes a day or two.
      </p>

      <div className="mt-6 space-y-6">
        <Alert>{error}</Alert>

        {profile.vetting && <VettingPanel vetting={profile.vetting} />}
        {/* Nothing to connect until there is a profile to connect it to —
            asking Stripe about an account that cannot exist yet just 409s. */}
        {!profile.isNew && <PayoutPanel />}

        <form onSubmit={handleSave} className="card space-y-4">
          <h2 className="font-semibold">Where you work</h2>
          <Alert>{formError}</Alert>

          <div>
            <label htmlFor="bio" className="field-label">
              About you <span className="font-normal text-slate-400">(optional)</span>
            </label>
            <textarea
              id="bio"
              rows={3}
              value={form.bio}
              onChange={update('bio')}
              placeholder="Ten years of turnovers. I bring my own supplies."
              className="field-input"
            />
          </div>

          <div className="grid gap-4 sm:grid-cols-3">
            <div>
              <label htmlFor="service_lat" className="field-label">
                Latitude
              </label>
              <input
                id="service_lat"
                inputMode="decimal"
                value={form.service_lat}
                onChange={update('service_lat')}
                placeholder="43.6591"
                className="field-input"
              />
            </div>
            <div>
              <label htmlFor="service_lng" className="field-label">
                Longitude
              </label>
              <input
                id="service_lng"
                inputMode="decimal"
                value={form.service_lng}
                onChange={update('service_lng')}
                placeholder="-70.2568"
                className="field-input"
              />
            </div>
            <div>
              <label htmlFor="service_radius_miles" className="field-label">
                Radius (miles)
              </label>
              <input
                id="service_radius_miles"
                type="number"
                min={1}
                max={200}
                required
                value={form.service_radius_miles}
                onChange={update('service_radius_miles')}
                className="field-input"
              />
            </div>
          </div>
          <p className="text-xs text-slate-500">
            Turnovers show up on your board when they fall inside this circle. We serve{' '}
            {regionName}.
          </p>

          <button type="submit" disabled={saving} className="btn-primary w-full">
            {saving ? 'Saving…' : profile.isNew ? 'Create profile' : 'Save changes'}
          </button>
        </form>

        {!profile.isNew && (
          <>
            <div>
              <h2 className="font-semibold">Your documents</h2>
              <p className="mt-1 text-sm text-slate-600">
                A photo ID and one reference, reviewed by a person before you can bid.
              </p>
              <div className="mt-3">
                <DocumentList documents={profile.documents} onChange={adopt} />
              </div>
            </div>

            <div className="card">
              <h2 className="font-semibold">Background check</h2>
              <p className="mt-1 text-sm text-slate-600">
                A photo ID confirms who you are, not your history. Owners are handing
                over keys to their homes, so a criminal background check is required
                before bidding.
              </p>
              {checkOrdered ? (
                <p className="mt-3 text-sm font-medium text-slate-700">
                  Status: {checkStatus.replace(/_/g, ' ')}
                </p>
              ) : (
                <button
                  type="button"
                  onClick={handleBackgroundCheck}
                  disabled={checkBusy}
                  className="btn-primary mt-4"
                >
                  {checkBusy ? 'Starting…' : 'Start my background check'}
                </button>
              )}
              <p className="mt-2 text-xs text-slate-500">
                You give your details straight to the background-check provider. linx
                never sees or stores your Social Security number or date of birth.
              </p>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
