# Unprompted UI system

Two shared files carry the design system for every page: `ui.css` (tokens and components) and `ui.js` (interactions,
exposed as `window.U`). `/ui/kit.html` shows every component working. The reasoning is in
`reports/Interactive UI overhaul for Unprompted.md`.

## Page setup

```html
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans+Arabic:wght@400;500;600&family=Host+Grotesk:ital,wght@0,400;0,500;0,600;0,700;0,800;1,400&family=Instrument+Serif:ital@0;1&family=Newsreader:ital,opsz,wght@1,6..72,400;1,6..72,500&display=swap">
<link rel="stylesheet" href="/ui/ui.css?v=1">
<script src="/ui/ui.js?v=1"></script>          <!-- in <head>, not deferred: applies theme/present/motion before first paint -->
<style> @layer page { /* this page's rules only */ } </style>
```

Page CSS goes in `@layer page`, so it wins over components without specificity fights. Use the semantic tokens
(`--surface`, `--text`, `--text-2`, `--border`, `--accent`, `--highlight` …), never raw colours.

## Rules

- **Lime (`--highlight`) means one thing: the brand was said here.** The `<mark>` behind the brand in a quote, a mention
  tick, the playhead, the logo's highlighter. Never a button, never decoration. Actions use `--accent` (forest green):
  `.u-btn-primary`.
- **Verbatim is ink, computed is grey.** Quotes and transcript in `--text`; scores, labels, summaries in `--text-2`
  (`.u-computed`), with `.u-computed-tag` where it helps.
- **Lead with the receipt.** Quote, timestamp and frame come first; counts follow. Every number links to its video.
  Say what's missing ("views unavailable") instead of showing a zero. Absence is evidence: say what was searched.
- **Motion is feedback**: under 300 ms, `transform`/`opacity` only, nothing on keyboard or repeated actions (j/k,
  palette, re-sorting). `U.transition()` for one-off morphs. Skeletons only after ~300 ms.
- **Every hover has a focus and click equivalent.** Targets at least 24×24 (40 px for icon buttons). Visible focus is the
  global 2 px outline; don't remove it.
- **Light by default**, present mode for projectors (`data-present`, toggled from the palette or `?present=1`), dark as
  an option. Arabic: `dir="auto"` on user content, `<bdi>` around handles and brand names, no letter-spacing.
- **No money or rights changes optimistically**: payments, licence accept/decline, refunds and sending pitches wait for
  the server and confirm in a `<dialog>`.

## Components (ui.css)

| Class | What |
|---|---|
| `.u-bar`, `.u-logo` (with `<mark>`), `.u-bar-link` | Sticky top bar with blur, the wordmark |
| `.u-btn` + `-primary`, `-ghost`, `-danger`, `-sm`, `-lg`, `-icon`; `aria-busy="true"` | Buttons |
| `.u-field`, `.u-label`, `.u-input` (`-lg`), `.u-select`, `.u-textarea`, `.u-hint`, `.u-error` | Fields |
| `.u-seg` (+ `data-u-seg` for the sliding thumb) with `label > input[type=radio]` or `button[aria-pressed]` | Segmented control |
| `.u-switch > input[type=checkbox]` | Switch |
| `.u-tabs` with `[role=tab]` + `U.tabs(el)` | Tabs with a sliding underline |
| `.u-card` (`-pad`), `.u-well` | Flat surfaces |
| `.u-kind` + `-spoken/-tagged/-sponsored/-quiet` | How a brand shows up in a video |
| `.u-pill` + `-ok/-warn/-danger/-live` | Status |
| `.u-receipt` (built by `U.receipt()`), `.u-quote`, `.u-mark`, `.u-time` | The receipt |
| `.u-ring` (`U.scoreRing()`), `.u-meter` (`U.meter()`), `.u-why` (`U.why()`), `.u-band` | Score that explains itself |
| `.u-spark` (`U.sparkline()`) | Sparkline, the mention video marked in lime |
| `.u-table` (`td.num` end-aligned tabular; `tr[aria-selected]`; `.row-actions` revealed on hover) | Data table |
| `.u-progress` (`.u-progress-bar` with `--pct`, `.u-progress-text`) | Progress that shows the work |
| `.u-skel`, `.u-empty` | Loading, absence |
| `.u-dialog` (`.u-dialog-body`, `.u-dialog-foot`), `.u-sheet` | Dialogs and side sheets |
| `.u-reveal` | Reveal on scroll (one-off sections only) |
| `.u-eyebrow`, `.u-display`, `.u-h1`, `.u-h2`, `.u-h3`, `.u-lead`, `.u-stamp`, `.u-kbd` | Type |
| `.u-container`, `.u-stack` / `.u-row` (`--gap`), `.u-muted`, `.u-grow`, `.no-present` | Layout helpers |

## Interactions (`window.U`)

| Call | Does |
|---|---|
| `U.h(tag, attrs, ...kids)` | Build DOM; strings become text nodes (never innerHTML with API data) |
| `U.receipt(r, {size, bare, spot, draw, onTime, actions, extraMeta})` | The receipt. `r = {quote: {text, hit: [a, b], start}, frame, url, platform, handle, views, publishedAt, kind}` |
| `U.scoreRing(value, {size, why})` | Ring with the number printed; `why` (element or function) opens beside it on click |
| `U.why({title, parts: [{label, value, max, detail}], note})`, `U.meter(parts)`, `U.band(v)` | Score explanation |
| `U.sparkline(values, {mark, label})` | Inline SVG |
| `U.card.provide(selector, anchor => element)` | Hover/focus card for matching elements (350 ms first open, hoverable, Esc) |
| `data-u-tip="text"` | Tooltip |
| `U.toast(msg, {kind: 'ok'|'error'|'info', action: {label, run}})` | Toast; errors and toasts with actions stay until dismissed |
| `U.optimistic({apply, send, revert, done, undo})` | Instant reversible change with retry on failure |
| `U.copy(text, 'Link copied')` | Clipboard with feedback |
| `U.palette.add({id, label, hint, group, shortcut, keywords, run})` or `{id, search: q => [commands]}` | Ctrl/⌘+K palette |
| `U.keys.add('j', fn, {label})` | Shortcut (single keys obey the "single-key shortcuts" setting) |
| `U.rovingList(container, {item, onOpen, onMove, onClose})` | ↑/↓, j/k, Home/End, Enter, Esc over rows |
| `U.tabs(el, onChange)`, `U.transition(fn)`, `U.type(el, words)` | Tabs, View Transition, typed hero text |
| `data-u-count="48200" data-u-format="compact"` | Count-up on first view |
| `U.fmt(n, 'compact'|'money'|'pct')`, `U.clock(s)`, `U.ago(iso)`, `U.date(iso)` | Formatting |
| `U.settings()`, `U.shortcutSheet()`, `U.prefs`, `U.setPref()` | Display settings (theme, density, motion, shortcuts, present) |
