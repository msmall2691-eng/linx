const STYLES = {
  draft: 'bg-slate-100 text-slate-600',
  open: 'bg-brand-50 text-brand-700',
  awarded: 'bg-emerald-100 text-emerald-800',
  in_progress: 'bg-emerald-100 text-emerald-800',
  completed: 'bg-slate-100 text-slate-600',
  cancelled: 'bg-slate-200 text-slate-500 line-through',
}

const LABELS = {
  draft: 'Draft',
  open: 'Taking bids',
  awarded: 'Awarded',
  in_progress: 'In progress',
  completed: 'Completed',
  cancelled: 'Cancelled',
}

export default function StatusBadge({ status, className = '' }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${
        STYLES[status] ?? STYLES.draft
      } ${className}`}
    >
      {LABELS[status] ?? status}
    </span>
  )
}
