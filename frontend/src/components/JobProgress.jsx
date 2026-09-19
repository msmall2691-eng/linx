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
 * The arrival note is the one exception, and it is the shape of that decision:
 * **one reading, at the tap, stored as a distance from a point the owner
 * already knows** — a ring rather than a place. It renders nothing at all when
 * the check is `unchecked`, because a refused permission is the ordinary case
 * and a line saying so on every job would read as a warning about somebody.
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

/**
 * What the arrival check said, in words rather than a number.
 *
 * **Three states, phrased so the third is not an accusation.** "We could not
 * check" has to read as the ordinary thing it is — a refused permission, a
 * basement, a desktop — because the alternative is an owner reading a missing
 * browser prompt as somebody lying to them.
 *
 * The distance is shown only when it contradicts the tap, where a number is
 * the difference between a judgement and a bare accusation. It is deliberately
 * *not* shown on a confirmed arrival: "38m away" invites somebody to wonder
 * about 38 metres, which is inside the noise of a phone fix.
 */
function ArrivalNote({ check, distanceM }) {
  if (check === 'confirmed') {
    return (
      <span
        className="mt-1 inline-flex items-center gap-1.5 text-xs font-medium text-accent-600"
        data-testid="arrival-confirmed"
      >
        <span className="h-1.5 w-1.5 rounded-full bg-accent-500" aria-hidden="true" />
        Confirmed at the property
      </span>
    )
  }
  if (check === 'away') {
    const km = distanceM >= 1000 ? `${(distanceM / 1000).toFixed(1)} km` : `${distanceM} m`
    return (
      <span
        className="mt-1 block text-xs text-amber-700"
        data-testid="arrival-away"
      >
        Their phone reported {km} from the property
      </span>
    )
  }
  return null
}

function Step({ label, at, waiting, timeZone, children }) {
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
        {done && children}
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
        >
          <ArrivalNote
            check={award.arrival_check}
            distanceM={award.arrival_distance_m}
          />
        </Step>
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
