import { useCallback, useEffect, useRef, useState } from 'react'

import Alert from './Alert.jsx'
import { apiFetch } from '../lib/api.js'
import { useTimeZone } from '../lib/config.jsx'
import { formatDateTime } from '../lib/datetime.js'

/**
 * The conversation between an owner and the cleaner booked on their job.
 *
 * **It self-hides**, like `DisputePanel` beside it and for the same reason: the
 * server decides whether there is anybody to talk to, and a screen that decided
 * for itself would eventually disagree with the endpoint. A 404 here is the
 * ordinary answer for "no live booking", not an error worth showing — so the
 * component renders nothing rather than an empty box promising a feature that
 * is not available yet.
 *
 * **Every label comes from the server.** Who sent a message is
 * `messages.visible_sender`, so the cleaner's copy says "the owner" and the
 * owner's says the cleaner's name. Working it out here from a user id would put
 * a second author on the privacy boundary — and the version that leaks renders
 * perfectly.
 *
 * Polling rather than a socket: one container, one process, and a thread that
 * refreshes while somebody is looking at it is the whole requirement. A socket
 * would be a second transport to keep alive for a feature measured in messages
 * per job.
 */
const POLL_MS = 20000

export default function MessageThread({ turnoverId }) {
  const timeZone = useTimeZone()
  const [thread, setThread] = useState(null)
  const [draft, setDraft] = useState('')
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const foot = useRef(null)

  const load = useCallback(async () => {
    try {
      setThread(await apiFetch(`/turnovers/${turnoverId}/messages`))
    } catch {
      // No live booking, or not this person's — either way there is nothing to
      // show and nothing has gone wrong. The server is the only judge of that.
      setThread(null)
    }
  }, [turnoverId])

  useEffect(() => {
    load()
    const timer = setInterval(load, POLL_MS)
    return () => clearInterval(timer)
  }, [load])

  useEffect(() => {
    // Newest at the bottom, which is where a conversation is read from.
    foot.current?.scrollIntoView({ block: 'nearest' })
  }, [thread?.messages?.length])

  if (!thread) return null

  async function send(event) {
    event.preventDefault()
    setSending(true)
    setError('')
    try {
      // The reply carries the whole thread, so the screen keeps everything it
      // had rather than being left with one message.
      setThread(
        await apiFetch(`/turnovers/${turnoverId}/messages`, {
          method: 'POST',
          body: { body: draft.trim() },
        }),
      )
      setDraft('')
    } catch (err) {
      setError(err.message)
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="card mt-6" data-testid="message-thread">
      <h2 className="font-semibold">Messages</h2>
      <p className="mt-1 text-sm text-slate-600">
        Just about this job. Both of you are emailed anything sent here.
      </p>

      {thread.messages.length === 0 ? (
        <p className="mt-4 text-sm text-slate-500" data-testid="no-messages">
          Nothing yet.
        </p>
      ) : (
        <ul className="mt-4 space-y-3">
          {thread.messages.map((message) => (
            <li
              key={message.id}
              className={message.mine ? 'flex justify-end' : 'flex justify-start'}
              data-testid="message"
            >
              <div
                className={`max-w-[85%] rounded-2xl px-4 py-2 ${
                  message.mine
                    ? 'rounded-br-sm bg-brand-600 text-white'
                    : 'rounded-bl-sm bg-slate-100 text-slate-900'
                }`}
              >
                <p
                  className={`text-xs font-medium ${
                    message.mine ? 'text-brand-100' : 'text-slate-500'
                  }`}
                  data-testid="message-sender"
                >
                  {message.sender_label}
                </p>
                <p className="mt-0.5 whitespace-pre-wrap text-sm">{message.body}</p>
                <p
                  className={`mt-1 text-[11px] ${
                    message.mine ? 'text-brand-100' : 'text-slate-400'
                  }`}
                >
                  {formatDateTime(message.sent_at, timeZone)}
                </p>
              </div>
            </li>
          ))}
        </ul>
      )}
      <div ref={foot} />

      {thread.can_send ? (
        <form onSubmit={send} className="mt-4 space-y-2">
          <label htmlFor={`message-${turnoverId}`} className="sr-only">
            Your message
          </label>
          <textarea
            id={`message-${turnoverId}`}
            rows={2}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Which gate should I use?"
            className="field-input"
            data-testid="message-input"
          />
          <button
            type="submit"
            className="btn-primary"
            disabled={sending || draft.trim().length === 0}
            data-testid="send-message"
          >
            {sending ? 'Sending…' : 'Send'}
          </button>
        </form>
      ) : (
        <p className="mt-4 text-sm text-slate-500" data-testid="thread-closed">
          {thread.closed_reason}
        </p>
      )}

      {error && (
        <div className="mt-3">
          <Alert>{error}</Alert>
        </div>
      )}
    </div>
  )
}
