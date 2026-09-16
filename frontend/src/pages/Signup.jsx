import { useState } from 'react'
import { Link, Navigate, useSearchParams } from 'react-router-dom'

import { CleanerPreview, OwnerPreview } from '../components/ProductPreview.jsx'
import { useAuth } from '../lib/auth.jsx'

const MIN_PASSWORD_LENGTH = 10

// Owner and cleaner only. Admins approve IDs and background checks, so that
// account is created deliberately — the API refuses to make one from this form.
const ROLES = [
  {
    value: 'owner',
    // One role covers a short-term rental and a home. The label says both out
    // loud because the landing page sends "I own a home" here, and a form that
    // then asks whether you own a rental has just told that person they took a
    // wrong turn.
    label: 'I have a place to clean',
    hint: 'A rental or a home — post jobs and pick a cleaner.',
    heading: 'What happens next',
    points: [
      'Add your property — a short-term rental or a home, with anything a cleaner needs to know.',
      'Post a job: a turnover between guests, or the date a home is due a clean.',
      'Cleaners near you bid. You see their price, their rating and whether their vetting is finished.',
      'You pay when the job is marked done. Not when you book it.',
    ],
  },
  {
    value: 'cleaner',
    label: 'I clean',
    hint: 'Bid on turnovers near you.',
    heading: 'What happens next',
    points: [
      'Tell us the town you work from and how far you travel — no coordinates, just a town.',
      'Upload a photo ID and a reference. A person reviews them, usually in a day or two.',
      'Once you are cleared, turnovers in range show up on your board and you name your price.',
      'You are paid through Stripe when you mark a job complete.',
    ],
  },
]

export default function Signup() {
  const { user, signup } = useAuth()
  const [searchParams] = useSearchParams()

  const roleFromUrl = searchParams.get('role')
  const [form, setForm] = useState({
    full_name: '',
    email: '',
    phone: '',
    password: '',
    role: ROLES.some((r) => r.value === roleFromUrl) ? roleFromUrl : 'owner',
  })
  const [error, setError] = useState(null)
  const [submitting, setSubmitting] = useState(false)

  if (user) return <Navigate to="/dashboard" replace />

  function update(field) {
    return (event) => setForm((prev) => ({ ...prev, [field]: event.target.value }))
  }

  async function handleSubmit(event) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await signup({
        full_name: form.full_name,
        email: form.email,
        phone: form.phone || null,
        password: form.password,
        role: form.role,
      })
      // As in Login: the redirect above owns where a new account lands.
    } catch (err) {
      setError(err.message)
      setSubmitting(false)
    }
  }

  const chosen = ROLES.find((r) => r.value === form.role) ?? ROLES[0]

  return (
    <div className="mx-auto grid max-w-4xl gap-10 px-4 py-16 lg:grid-cols-2">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Create your account</h1>

        {/* What you are signing up for, beside the form rather than a page
            back. A signup form on its own asks for five fields and gives
            nothing until it is submitted; this is the same page still showing
            the thing the fields are for. */}
        <div className="mt-6 lg:hidden">
          {form.role === 'owner' ? <OwnerPreview /> : <CleanerPreview />}
        </div>

        <form onSubmit={handleSubmit} className="card mt-6 space-y-4">
        {error && (
          <p role="alert" className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-700">
            {error}
          </p>
        )}

        <fieldset>
          <legend className="field-label">I am a…</legend>
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            {ROLES.map((role) => (
              <label
                key={role.value}
                className={`cursor-pointer rounded-lg border p-3 text-sm transition ${
                  form.role === role.value
                    ? 'border-brand-500 bg-brand-50 ring-1 ring-brand-500'
                    : 'border-slate-300 hover:bg-slate-50'
                }`}
              >
                <input
                  type="radio"
                  name="role"
                  value={role.value}
                  checked={form.role === role.value}
                  onChange={update('role')}
                  className="sr-only"
                />
                <span className="block font-medium">{role.label}</span>
                <span className="mt-0.5 block text-xs text-slate-500">{role.hint}</span>
              </label>
            ))}
          </div>
        </fieldset>

        <div>
          <label htmlFor="full_name" className="field-label">
            Full name
          </label>
          <input
            id="full_name"
            required
            autoComplete="name"
            value={form.full_name}
            onChange={update('full_name')}
            className="field-input"
          />
        </div>

        <div>
          <label htmlFor="email" className="field-label">
            Email
          </label>
          <input
            id="email"
            type="email"
            required
            autoComplete="email"
            value={form.email}
            onChange={update('email')}
            className="field-input"
          />
        </div>

        <div>
          <label htmlFor="phone" className="field-label">
            Phone <span className="font-normal text-slate-400">(optional)</span>
          </label>
          <input
            id="phone"
            type="tel"
            autoComplete="tel"
            value={form.phone}
            onChange={update('phone')}
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
            required
            minLength={MIN_PASSWORD_LENGTH}
            autoComplete="new-password"
            value={form.password}
            onChange={update('password')}
            className="field-input"
          />
          <p className="mt-1 text-xs text-slate-500">
            At least {MIN_PASSWORD_LENGTH} characters.
          </p>
        </div>

        <button type="submit" disabled={submitting} className="btn-primary w-full">
          {submitting ? 'Creating account…' : 'Create account'}
        </button>
        </form>

        <p className="mt-4 text-sm text-slate-600">
          Already have an account?{' '}
          <Link to="/login" className="font-medium text-brand-600 hover:underline">
            Log in
          </Link>
        </p>
      </div>

      <aside className="hidden lg:block">
        <div className="sticky top-8 space-y-6">
          {form.role === 'owner' ? <OwnerPreview /> : <CleanerPreview />}

          <div className="card">
            <h2 className="font-semibold">{chosen.heading}</h2>
            <ol className="mt-3 space-y-2 text-sm text-slate-600">
              {chosen.points.map((point, index) => (
                <li key={point} className="flex gap-3">
                  <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-brand-100 text-xs font-bold text-brand-700">
                    {index + 1}
                  </span>
                  <span>{point}</span>
                </li>
              ))}
            </ol>
          </div>
        </div>
      </aside>
    </div>
  )
}
