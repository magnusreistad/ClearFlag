// GET /transactions serves one rationale string in either of two shapes,
// with nothing in the response saying which (SCRUM-53):
// - an Investigation Agent rationale: one composed paragraph, at most 500
//   characters, normally with no "Flagged: " marker, whatever the rule count;
// - the interim formatter's text (the SCRUM-56 fallback when no passing
//   agent rationale exists): per-rule sentences, each prefixed "Flagged: "
//   and concatenated by the rules engine's concatenate_rationales.
// Splitting on that prefix recovers the interim text's individual reasons;
// agent text comes back as a single item. Telling the two shapes apart is
// FlagBadge's job.
export const FLAG_MARKER = 'Flagged: '

export function splitRationale(rationale: string): string[] {
  return rationale
    .split(FLAG_MARKER)
    .map((segment) => segment.trim())
    .filter(Boolean)
}
