import { useId, useState, type FormEvent } from 'react'
import { ApiError } from '../../api/client'
import { postDisposition } from '../../api/transactions'
import { formatDateTime } from '../../lib/format'
import type { CurrentDisposition, DispositionValue } from '../../types/transaction'
import styles from './DispositionPanel.module.css'

// Mirrors the backend's DISPOSITION_NOTE_MAX_LENGTH (app/models.py).
export const NOTE_MAX_LENGTH = 2000

const OPTIONS: { value: DispositionValue; label: string }[] = [
  { value: 'confirmed_fraud', label: 'Confirm fraud' },
  { value: 'false_positive', label: 'False positive' },
  { value: 'ignored', label: 'Ignore' },
]

const CHIP_LABELS: Record<DispositionValue, string> = {
  confirmed_fraud: 'Confirmed fraud',
  false_positive: 'False positive',
  ignored: 'Ignored',
}

const GENERIC_SAVE_ERROR = "Couldn't save, try again."

interface DispositionPanelProps {
  transactionId: number
  isFlagged: boolean
  disposition: CurrentDisposition | null
  onSaved: (disposition: CurrentDisposition) => void
}

// A save failure is shown in one of two places: a 422 (the note failed
// server-side validation) beside the note it's about, everything else
// below the Save button.
interface SaveError {
  message: string
  isNoteError: boolean
}

function toSaveError(err: unknown): SaveError {
  if (err instanceof ApiError) {
    if (err.status === 404) return { message: "This transaction couldn't be found.", isNoteError: false }
    if (err.status === 409) {
      return { message: 'This transaction is no longer flagged. Refresh the page to see its current status.', isNoteError: false }
    }
    if (err.status === 422) {
      return { message: err.detail ?? 'The server rejected this review.', isNoteError: true }
    }
  }
  // 500s and network failures (fetch's TypeError) alike - retrying is the
  // only thing the analyst can do about either.
  return { message: GENERIC_SAVE_ERROR, isNoteError: false }
}

// SCRUM-30. The review line under a feed row: the current disposition (or
// "Not reviewed") and, for a row that's still flagged, an expandable form
// that appends a new one. A row that's no longer flagged but was judged
// shows that judgment read-only - the POST would 409 for it anyway.
export function DispositionPanel({ transactionId, isFlagged, disposition, onSaved }: DispositionPanelProps) {
  const panelId = useId()
  const noteId = useId()
  const counterId = useId()
  const noteErrorId = useId()
  const [isOpen, setIsOpen] = useState(false)
  const [selected, setSelected] = useState<DispositionValue | null>(null)
  const [note, setNote] = useState('')
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [error, setError] = useState<SaveError | null>(null)
  const [justSaved, setJustSaved] = useState(false)

  if (!isFlagged && !disposition) return null

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!selected) return
    setJustSaved(false)
    // Backstop for maxLength, which a browser doesn't apply to text set
    // programmatically (and counts UTF-16 units the same way this does).
    if (note.length > NOTE_MAX_LENGTH) {
      setError({ message: `Notes can be at most ${NOTE_MAX_LENGTH} characters.`, isNoteError: true })
      return
    }

    setError(null)
    setIsSubmitting(true)
    try {
      const saved = await postDisposition(transactionId, { disposition: selected, note: note || null })
      onSaved({ value: saved.disposition, note: saved.note, created_at: saved.created_at })
      setNote('')
      setJustSaved(true)
    } catch (err) {
      setError(toSaveError(err))
    } finally {
      setIsSubmitting(false)
    }
  }

  const noteDescribedBy = error?.isNoteError ? `${counterId} ${noteErrorId}` : counterId

  return (
    <div className={styles.review}>
      <div className={styles.summary}>
        <span role="status" className={styles.summaryStatus}>
          <span className={styles.chip} data-disposition={disposition?.value ?? 'none'}>
            {disposition ? CHIP_LABELS[disposition.value] : 'Not reviewed'}
          </span>
          {disposition && (
            <time className={styles.reviewedAt} dateTime={disposition.created_at}>
              {formatDateTime(disposition.created_at)}
            </time>
          )}
        </span>
        {isFlagged && (
          <button
            type="button"
            className={styles.toggle}
            aria-expanded={isOpen}
            aria-controls={panelId}
            onClick={() => setIsOpen(!isOpen)}
          >
            Review
          </button>
        )}
      </div>

      {disposition?.note && <p className={styles.currentNote}>{disposition.note}</p>}

      {isFlagged && (
        <form id={panelId} className={styles.form} hidden={!isOpen} onSubmit={handleSubmit}>
          <fieldset className={styles.fieldset} disabled={isSubmitting}>
            <legend className={styles.legend}>{disposition ? 'Change your review' : 'Review this flag'}</legend>
            <div className={styles.options}>
              {OPTIONS.map((option) => (
                <label key={option.value} className={styles.option}>
                  <input
                    type="radio"
                    name={`disposition-${transactionId}`}
                    value={option.value}
                    checked={selected === option.value}
                    onChange={() => setSelected(option.value)}
                  />
                  {option.label}
                </label>
              ))}
            </div>

            <label htmlFor={noteId} className={styles.noteLabel}>
              Note (optional)
            </label>
            <textarea
              id={noteId}
              className={styles.note}
              rows={3}
              maxLength={NOTE_MAX_LENGTH}
              value={note}
              onChange={(event) => setNote(event.target.value)}
              aria-describedby={noteDescribedBy}
              aria-invalid={error?.isNoteError || undefined}
            />
            <div id={counterId} className={styles.counter}>
              {note.length} / {NOTE_MAX_LENGTH}
            </div>
            {error?.isNoteError && (
              <p id={noteErrorId} role="alert" className={styles.error}>
                {error.message}
              </p>
            )}

            <button type="submit" className={styles.saveButton} disabled={!selected}>
              {isSubmitting ? 'Saving…' : 'Save review'}
            </button>
          </fieldset>

          {error && !error.isNoteError && (
            <p role="alert" className={styles.error}>
              {error.message}
            </p>
          )}
          {justSaved && (
            <p role="status" className={styles.saved}>
              Review saved.
            </p>
          )}
        </form>
      )}
    </div>
  )
}
