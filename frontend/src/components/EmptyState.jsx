import { Link } from 'react-router-dom'

export default function EmptyState({ title, body, actionLabel, actionTo }) {
  return (
    <div className="rounded-xl border border-dashed border-slate-300 bg-white p-10 text-center">
      <h2 className="font-semibold text-slate-800">{title}</h2>
      {body && <p className="mx-auto mt-2 max-w-sm text-sm text-slate-600">{body}</p>}
      {actionLabel && actionTo && (
        <Link to={actionTo} className="btn-primary mt-6">
          {actionLabel}
        </Link>
      )}
    </div>
  )
}
