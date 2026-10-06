// The backend concatenates per-rule rationale sentences with each one
// independently prefixed "Flagged: " (see the rules engine's
// concatenate_rationales) rather than exposing rule_name per hit, so
// splitting on that repeated prefix is how the frontend recovers the
// individual reasons.
export const FLAG_MARKER = 'Flagged: '

export function splitRationale(rationale: string): string[] {
  return rationale
    .split(FLAG_MARKER)
    .map((segment) => segment.trim())
    .filter(Boolean)
}
