import { formatDateTime } from '../lib/datetime.js'

/**
 * Where a booked job has got to: on the way, on site, done.
 *
 * **One component, rendered to both sides**, because the owner asking "is
 * anybody coming" and the cleaner asking "what have I told them" are the same
 * three facts. Two renderings of it would eventually disagree about what a
 * missing timestamp means, and the owner's copy is the one somebody acts on.
 *
 * The times are **timestamps somebody wrote by pressing a button**, not
 * positions. Nothing here is a live location, and that is a product decision
 * rather than a gap: continuous location on an independent contractor carries
 * its own consent, retention and disclosure questions, and a map drawn from a
 * column added quietly is how that arrives without any of them being asked.
 *
 * A step that has not happened is shown greyed rather than hidden, so the
 * shape of the job is the same before and after — a list that grows as it goes
 * reads as "nothing has happened" when the real answer is "not yet".
 */
function elapsed(fromIso, toIso) {
  const minutes = Math.round(
    (new Date(toIso).getTime() - new Date(fromIso).getTime()) / 60000,
  )
  if (!Number.isFinite(minutes) || minutes < 1) return null
  if (minutes < 60) return `${minutes} min`
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return rest === 0 ? `${hours} hr` : `${hours} hr ${rest} min`
}

function Step({ label, at, waiting, timeZone }) {
  const done = Boolean(at)
  return (
    <li className="flex items-start gap-3">
      <span
        className={`mt-1 h-2.5 w-2.5 shrink-0 rounded-full ${
          done ? 'bg-brand-600' : 'bg-slate-300'
        }`}
        aria-hidden="true"
      />
      <span className="min-w-0">
        <span
          className={`block text-sm font-medium ${
            done ? 'text-slate-900' : 'text-slate-400'
          }`}
        >
          {label}
        </span>
        <span className="block text-xs text-slate-500">
          {done ? formatDateTime(at, timeZone) : waiting}
        </span>
      </span>
    </li>
  )
}

export default function JobProgress({ award, timeZone, cancelled = false }) {
  if (!award) return null
  // A booking that came undone is not a job in progress, and a timeline
  // frozen mid-way reads as one still running.
  if (cancelled) return null

  const onSite = elapsed(award.started_at, award.completed_at)

  return (
    <div data-testid="job-progress">
      <ol className="space-y-3">
        <Step
          label="On the way"
          at={award.en_route_at}
          waiting="Not set off yet"
          timeZone={timeZone}
        />
        <Step
          label="On site"
          at={award.started_at}
          waiting="Not arrived yet"
          timeZone={timeZone}
        />
        <Step
          label="Finished"
          at={award.completed_at}
          waiting="Not finished yet"
          timeZone={timeZone}
        />
      </ol>
      {onSite && (
        <p className="mt-3 text-xs text-slate-500" data-testid="job-time-on-site">
          {onSite} on site
        </p>
      )}
    </div>
  )
}
