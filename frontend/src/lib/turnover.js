// Statuses where the job is still real work on somebody's schedule.
const LIVE_STATUSES = ['draft', 'open', 'awarded', 'in_progress']

/**
 * Whether the urgency ladder still means anything for this turnover.
 *
 * Urgency is a scheduling pressure signal. On a cancelled or completed job
 * there is no pressure left, and a red "Same day" badge on a dead turnover
 * reads as an alarm about something nobody needs to act on.
 */
export function showsUrgency(status) {
  return LIVE_STATUSES.includes(status)
}
