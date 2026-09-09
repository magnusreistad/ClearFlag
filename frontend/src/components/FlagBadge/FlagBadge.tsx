import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { splitRationale } from '../../lib/rationale'
import styles from './FlagBadge.module.css'

interface FlagBadgeProps {
  rationale: string
  isOpen: boolean
  onOpenChange: (open: boolean) => void
}

const PANEL_WIDTH = 280
const VIEWPORT_MARGIN = 16

// Rule count is derived by splitting `rationale` (see splitRationale) since
// the API doesn't expose rule_name per hit today. Exposed here via
// data-rule-count so SCRUM-26's severity treatment has a hook to key off of
// without re-deriving it.
export function FlagBadge({ rationale, isOpen, onOpenChange }: FlagBadgeProps) {
  const buttonRef = useRef<HTMLButtonElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  // The transaction list clips overflow for its rounded-card look, so the
  // panel is portaled to document.body and positioned from the button's
  // viewport rect rather than rendered as a CSS-absolute child - otherwise
  // the list's `overflow: hidden` cuts it off.
  const [panelPosition, setPanelPosition] = useState<{ top: number; left: number } | null>(null)
  const reasons = useMemo(() => splitRationale(rationale), [rationale])
  const ruleCount = reasons.length

  useLayoutEffect(() => {
    if (!isOpen || !buttonRef.current) {
      setPanelPosition(null)
      return
    }
    const rect = buttonRef.current.getBoundingClientRect()
    const left = Math.min(rect.left, window.innerWidth - PANEL_WIDTH - VIEWPORT_MARGIN)
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
        panelPosition &&
        createPortal(
          <div
            ref={panelRef}
            className={styles.panel}
            role="region"
            aria-label="Why this transaction was flagged"
            style={{ top: panelPosition.top, left: panelPosition.left }}
          >
            {ruleCount === 1 ? (
              <p className={styles.singleReason}>{reasons[0]}</p>
            ) : (
              <ul className={styles.reasonList}>
                {reasons.map((reason, index) => (
                  <li key={index} className={styles.reasonItem}>
                    {reason}
                  </li>
                ))}
              </ul>
            )}
          </div>,
          document.body,
        )}
    </>
  )
}
