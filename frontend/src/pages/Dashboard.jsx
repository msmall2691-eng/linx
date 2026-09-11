import { useAuth } from '../lib/auth.jsx'

// What each role will find here once its phase lands. Written out rather than
// left blank so the next phase has an explicit target, and so nobody ships a
// screen that quietly drops one of these.
const NEXT_UP = {
  owner: [
    'Add your properties — address, beds, baths, access notes.',
    'Post a turnover with the checkout and the next checkin.',
    'Review bids and award the job.',
  ],
  cleaner: [
    'Finish your profile and set your service area.',
    'Upload your ID and a reference, and clear a background check.',
    'Bid on open turnovers near you once you are cleared.',
  ],
  admin: [
    'Review ID and background checks waiting in the queue.',
    'Watch for turnovers still unclaimed close to checkout.',
    'Handle disputes and reconcile the payment ledger.',
  ],
}

export default function Dashboard() {
  const { user } = useAuth()
  const steps = NEXT_UP[user.role] ?? []

  return (
    <div className="mx-auto max-w-3xl px-4 py-12">
      <h1 className="text-2xl font-bold tracking-tight">
        Welcome, {user.full_name.split(' ')[0]}
      </h1>
      <p className="mt-1 text-slate-600">
        Your account is set up. Here is what comes next.
      </p>

      <ul className="card mt-6 space-y-3">
        {steps.map((step) => (
          <li key={step} className="flex gap-3 text-sm text-slate-700">
            <span
              aria-hidden="true"
              className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-brand-500"
            />
            {step}
          </li>
        ))}
      </ul>

      <p className="mt-6 text-xs text-slate-500">
        These screens are still being built. Accounts and sign-in work today.
      </p>
    </div>
  )
}
