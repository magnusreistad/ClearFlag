---
name: frontend-design
description: Design guidance for ClearFlag frontend work — the existing visual language to extend and the generic AI-default patterns to avoid. Use before writing or restyling any component in frontend/src.
---

This is a fintech-style dashboard, not a marketing site. Restraint and information density are the goal — the transaction feed, its fraud flags and rationales, the disposition panel and the summary dashboard need to read like a bank statement, not a landing page.

## Existing design tokens

Extracted from `frontend/src/index.css` and the component CSS modules under `frontend/src/components/` (`TransactionFeed`, `FlagBadge`, `DispositionPanel`, `SummaryDashboard`) — reuse these values verbatim in new components rather than inventing new ones. If a new component needs a color/spacing value not listed here, choose something that sits naturally in this palette and add it below.

**Color**
- Page background: `#f7f7fc`
- Card background: `#ffffff`
- Card border: `#eff0f6`
- Row divider: `#f2f2f7`
- Primary ink (headings, amounts, merchant names): `#14142b`
- Body ink: `#1a1a2e`
- Muted text (metadata, timestamps, placeholders): `#a0a3bd`
- Secondary muted text (subtitles): `#6e7191`
- Accent (interactive/brand — buttons, avatar background, links): `#5551ff`, hover state `#4340d9`
- Accent tint (avatar background): `#eef0ff`

**Semantic colors** — alert-but-not-alarming, never a saturated stock red. Reuse these rather than adding new ones:
- **Flag badge amber** (`FlagBadge.module.css`), stepping up in saturation with the number of rules that fired instead of switching hue (text / background / border, hover background):
  - 1 rule: `#b5730c` on `#fdf2e3`, border `#f2dfb8`, hover `#fbe8cc`
  - 2 rules: `#9c5f0a` on `#fae4bf`, border `#e8c48a`, hover `#f7d9a3`, weight 700
  - 3+ rules: `#7a4808` on `#f7d9a3`, 1.5px border `#d9a94a`, hover `#f3cb85`
- **Disposition chips** (SCRUM-30, `DispositionPanel.module.css`): confirmed fraud is coral `#a8402f` on `#fbeceb`; false positive is green `#2f7a52` on `#e9f6ef`; ignored is inherited body ink on `#f2f2f7`; none is `#6e7191` with an `#eff0f6` border. The same coral and green are the panel's error and saved text.
- **Summary dashboard series** (`SummaryDashboard.tsx` `SERIES`, mirrored by the `.swatch` rules in its CSS module — keep the two in step): confirmed fraud `#a8402f`, false positive `#3f9a6b`, ignored `#6f7292` (low-chroma slate, matching its gray chip), unreviewed `#e2a84a`. Validated as a set for colorblind separation; the amber is below 3:1 on white, so the legend and visually hidden table carry the meaning. Change one color only by re-validating the whole set.

**Shape & spacing**
- Card/container radius: 16px; button/pill radius: 10–12px; avatar: fully round (`50%`)
- Card shadow: `0 1px 2px rgba(20, 20, 43, 0.04)` — barely-there, not a drop shadow
- Row padding: `14px 16px`; card list has no internal row shadows/borders except a 1px bottom divider

**Type**
- System font stack (`-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif`) — no imported webfont
- Numbers (amounts, times) use `font-variant-numeric: tabular-nums` so columns of digits align
- Section labels (day headings) are small, uppercase, letter-spaced, muted — not bold black text

## Conventions to keep

- **CSS Modules only** (`Component.module.css` + `styles.foo`) — no inline `style={}`, no new CSS-in-JS or utility-class framework introduced for one component.
- **Loading/empty/error states are first-class**, not an afterthought: every data-driven component should have explicit states like `TransactionFeed` does (`role="status" aria-live="polite"` while loading, `role="alert"` on error, a distinct empty-state message), styled with the same card/`stateCard` treatment rather than a bare "Loading..." string.
- Money is a string over the wire (see `CLAUDE.md`) — format at render time, never coerce to `number` for display logic beyond formatting.

## Avoid (generic AI-default patterns)

- Centered hero sections, large gradient blobs, or glassmorphism — nothing in this app is a landing page.
- Default framework look-alikes (stock shadcn blue, default Tailwind indigo/gray without adapting to the tokens above, Bootstrap-style card shadows).
- Decorative emoji in UI copy or headings.
- Saturated, high-chroma accent colors that clash with the existing muted indigo/ink palette — new colors should look like they belong next to `#5551ff` and `#a0a3bd`, not compete with them.
- Overly generous whitespace/padding "for breathing room" — this is a dense list-based dashboard; match the existing 14–16px row rhythm rather than inflating it.

Before finishing any new/restyled component, compare it side by side with `TransactionFeed` — if it doesn't look like it belongs in the same app, tighten it up rather than shipping a visually distinct one-off.
