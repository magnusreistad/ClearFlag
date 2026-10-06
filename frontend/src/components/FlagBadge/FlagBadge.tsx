import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { FLAG_MARKER, splitRationale } from '../../lib/rationale'
import styles from './FlagBadge.module.css'

interface FlagBadgeProps {
  rationale: string
  ruleNames: string[]
  isOpen: boolean
  onOpenChange: (open: boolean) => void
}

const VIEWPORT_MARGIN = 16
// A flag with no explanation text still keeps its badge (the flag and its
// severity are real); the panel says so rather than opening empty.
const NO_RATIONALE_FALLBACK = 'No explanation is available for this flag.'

// Rule count comes from `ruleNames` (one entry per hit, API-provided -
// SCRUM-64) rather than from splitting `rationale`. The individual reason
// text still comes from splitRationale since the API exposes one combined
// rationale string, not per-rule text. Exposed here via data-rule-count so
// SCRUM-26's severity treatment has a hook to key off of without re-deriving
// it.
export function FlagBadge({ rationale, ruleNames, isOpen, onOpenChange }: FlagBadgeProps) {
  const buttonRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  // The transaction list clips overflow for its rounded-card look, so the
  // panel is portaled to document.body and positioned from the button's
  // viewport rect rather than rendered as a CSS-absolute child - otherwise
  // the list's `overflow: hidden` cuts it off.
  const [panelPosition, setPanelPosition] = useState<{ top: number; left: number } | null>(null)
  const trimmedRationale = rationale.trim()
  const reasons = useMemo(() => splitRationale(rationale), [rationale])
  const ruleCount = ruleNames.length
  const isInterimFormat = trimmedRationale.startsWith(FLAG_MARKER)
  const isReasonList = isInterimFormat && reasons.length > 1
  const paragraph = (isInterimFormat ? reasons[0] : trimmedRationale) || NO_RATIONALE_FALLBACK

  // The panel renders hidden for one layout pass so its real width can be
  // measured before it's placed - `.panel` is content-box, so its padding
  // and border sit outside the 280px CSS width, and clamping against 280
  // let it run ~14px past the right edge on narrow viewports. Both passes
  // happen before paint.
  useLayoutEffect(() => {
    if (!isOpen || !buttonRef.current || !panelRef.current) {
      setPanelPosition(null)
      return
    }
    const rect = buttonRef.current.getBoundingClientRect()
    const panelWidth = panelRef.current.getBoundingClientRect().width
    const left = Math.min(rect.left, window.innerWidth - panelWidth - VIEWPORT_MARGIN)
    setPanelPosition({ top: rect.bottom + 6, left: Math.max(VIEWPORT_MARGIN, left) })
  }, [isOpen])

  useEffect(() => {
    if (!isOpen) return

    function handlePointerDown(event: PointerEvent) {
      const target = event.target as Node
      if (buttonRef.current?.contains(target) || panelRef.current?.contains(target)) return
      onOpenChange(false)
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === 'Escape') onOpenChange(false)
    }

    // Simplest correct behavior for a viewport-positioned popover: close on
    // scroll rather than continuously repositioning it mid-scroll.
    function handleScroll() {
      onOpenChange(false)
    }

    document.addEventListener('pointerdown', handlePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    window.addEventListener('scroll', handleScroll, true)
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
      window.removeEventListener('scroll', handleScroll, true)
    }
  }, [isOpen, onOpenChange])

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className={styles.badge}
        onClick={() => onOpenChange(!isOpen)}
        aria-expanded={isOpen}
        data-rule-count={ruleCount}
      >
        <span className={styles.dot} aria-hidden="true" />
        Flagged
      </button>
      {isOpen &&
        createPortal(
          <div
            ref={panelRef}
            className={styles.panel}
            role="region"
            aria-label="Why this transaction was flagged"
            style={panelPosition ? { top: panelPosition.top, left: panelPosition.left } : { visibility: 'hidden' }}
          >
            {isReasonList ? (
              <ul className={styles.reasonList}>
                {reasons.map((reason, index) => (
                  <li key={index} className={styles.reasonItem}>
                    {reason}
                  </li>
                ))}
              </ul>
            ) : (
              <p className={styles.singleReason}>{paragraph}</p>
            )}
          </div>,
          document.body,
        )}
    </>
  )
}
