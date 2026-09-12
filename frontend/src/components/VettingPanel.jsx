import Alert from './Alert.jsx'

/**
 * Why a cleaner can or cannot bid.
 *
 * Renders the server's `vetting` object verbatim. It deliberately does not
 * recompute anything from the status fields: the API derives this in one place
 * (app/services/vetting.py) precisely so a "verified" badge can't end up beside
 * a bid button that refuses.
 */
export default function VettingPanel({ vetting }) {
  if (!vetting) return null

  return (
    <div className="card">
      <div className="flex items-start gap-3">
        <span
          aria-hidden="true"
          className={`mt-1 h-2.5 w-2.5 shrink-0 rounded-full ${
            vetting.can_take_jobs ? 'bg-emerald-500' : 'bg-amber-500'
          }`}
        />
        <div className="min-w-0">
          <h2 className="font-semibold">
            {vetting.can_take_jobs ? 'Cleared to bid' : 'Not cleared to bid yet'}
          </h2>
          <p className="mt-1 text-sm text-slate-600">{vetting.summary}</p>

          {vetting.blockers.length > 0 && (
            <ul className="mt-3 space-y-1">
              {vetting.blockers.map((blocker) => (
                <li key={blocker} className="flex gap-2 text-sm text-slate-700">
                  <span aria-hidden="true" className="text-amber-600">
                    •
                  </span>
                  {blocker}
                </li>
              ))}
            </ul>
          )}

          {vetting.warnings.length > 0 && (
            <div className="mt-3 space-y-2">
              {vetting.warnings.map((warning) => (
                <Alert key={warning} tone="warning">
                  {warning}
                </Alert>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
