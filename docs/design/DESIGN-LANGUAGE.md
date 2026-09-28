# zicato — the design language

This is the **canonical, reproducible design-language reference** for zicato.
Everything here is grounded in the live implementation — concrete token names,
hex values, font stacks, class names and SVG snippets, each traceable to a
source file. The goal is that someone could build **any** new zicato surface — a
new dashboard view or an execution-timeline figure — purely from this document
and reproduce the same look.

The system ships in the Console dashboard, the sole front end. The token sheet
is
[`console.css`](../../src/zicato/dashboard/static/css/console.css);
the figure language is
[`svg.js`](../../src/zicato/dashboard/static/js/svg.js); the chrome
and component vocabulary are in `shell.js`, `ui.js` and `views/**`. Where this
document and any source disagree, **the code is authoritative** — re-grep and
verify before treating a value here as current.

This document is the *language*. [CONSOLE-DESIGN-LANGUAGE.md](CONSOLE-DESIGN-LANGUAGE.md)
is the dashboard's *specific application* of it (the Console's information
architecture, the figure-to-purpose mapping, the live-vs-completed
conventions); it links back here for the shared tokens, typography and figure
grammar.

> **Design-inspiration note.** Citations to public design authorities (Tufte,
> the Gogh terminal palettes, the chess/tournament metaphor) appear in the
> documentation and never in source code.

---

## 1. Ethos

zicato's surface is a **dense observatory for a power user** — an instrument
rather than a consumer report. Five principles:

1. **Tufte data-ink / line-art.** Every data figure carries maximum data per
   stroke and drops decoration — no gridlines for their own sake, no 3-D, no
   chart frames, no chartjunk. Just the band, the dot, the rule, the label.
   (Edward Tufte, *The Visual Display of Quantitative Information*.)
2. **Sans for prose and controls, mono for data.** Prose, controls and the
   chrome (the top bar, the tree, buttons, headings) are set in a sans. The
   monospace face is reserved for data, code, ids, hashes, key names and the
   numbers in tables, so values read on a fixed advance grid and the chrome
   reads as text. Chrome set entirely in mono is a defect (§3).
3. **A single green accent on a calm ground.** The brand carries **one**
   non-foreground colour, the green plucked-note (`--zicato-accent`); everything
   else is ink on a quiet paper. The good/bad signal colours are earned by data
   direction, never spent as decoration.
4. **Theme-adaptive by construction.** Sixteen colour themes and three typeface
   modes swap by a single attribute on the root; every mark reads its colour and
   face from tokens, so a theme switch is a pure re-skin with **no re-render**.
5. **Fit-to-width, never flashing.** Figures scale to their pane (no pan/zoom, no
   horizontal scroll), and the DOM is never rebuilt on a no-op heartbeat
   (digest-gating, §7) — a live run animates *values*, never repaint-loops.

---

## 2. Color system

### 2.1 The role contract

Every theme is a CSS custom-property set scoped under
`#console-root[data-t-theme="<id>"]`, swapped by the
`[data-t-theme]` attribute. There is **no hardcoded hex in the marks** — every
figure reads its colour from the active theme's tokens. The contract is a small
set of semantic roles whose meaning is **fixed across all sixteen themes**:

| token | role | the rule it enforces |
| --- | --- | --- |
| `--v2-paper` | ground / page background | the deepest surface; everything sits on it |
| `--v2-panel` | surface of a panel / card / hovercard | one step lifted off the ground |
| `--v2-ink` | primary text + neutral mark strokes | the highest-contrast foreground |
| `--v2-ink-soft` | secondary text (sub-labels, captions) | |
| `--v2-ink-faint` | tertiary text (faint tags, empty-state italics) | |
| `--v2-rule` | borders / separators / hovercard outline | |
| `--v2-rule-soft` | fainter rule / inline-code background | |
| `--v2-good` | **improvement / promotion / survival** | a dot *below* the reference rule, a survivor's up mark, a crowned gate, a promoted verdict — *always* the better outcome |
| `--v2-good-soft` | tinted fill behind a good state | |
| `--v2-bad` | **regression / rejection / a cut** | a dot *above* the rule, a cut competitor's fail mark, a rejected verdict — *always* the worse outcome |
| `--v2-bad-soft` | tinted fill behind a bad state | |
| `--v2-caution` | caution / timeout (the budget-exceeded timeout mark) | |
| `--v2-accent` | **the one structural / interactive highlight** | the champion spine, the emphasised current line, an interactive focus — used sparingly so it stays meaningful |
| `--v2-flat` | unchanged / neutral-flat | a slope that neither improved nor regressed |
| `--v2-cell-empty` | an empty heatmap cell | |

**The cardinal rule:** `good` and `bad` are earned by **direction, never by
identity**. A challenger is not red because it is a challenger; it is red only
when it regressed or was cut. An unscored / in-flight candidate is *neutral*
(pending → `--v2-accent`), never `bad` — an undecided outcome must never
collapse into a rejection, which is what the code guards against
(`.dn-state.dn-pending`, `.ezn-edge-neutral` in `console.css`).

The single brand accent is a **separate** token from the structural `--v2-accent`:

| token | value | source |
| --- | --- | --- |
| `--zicato-accent` | `#2FA46A` (light grounds) / `#3FB87A` (dark grounds) | `console.css`, "the brand accent token" block |

The mark strokes with `currentColor` (so it flips dark/light with the theme) and
fills the plucked-note dot with `var(--zicato-accent)`. See §8 and
[docs/brand/README.md](../brand/README.md).

### 2.2 The sixteen themes

`monokai` is the default. The colour picker is a **swatch dropdown**
(`.dt-cd-trigger` / `.dt-cd-list`); each option shows a 6-swatch preview strip
(*ground · surface · ink · improve · regress · accent*) plus the theme name. The
JS preview tuples live in `ui.js` `COLOR_THEMES` as
`[paper, panel, ink, good, bad, accent]`; the authoritative per-theme palettes
are the `--v2-*` sets in `console.css`.

Every value below is lifted verbatim from `console.css`.

#### Monokai (default) — warm dark
| token | hex | | token | hex |
| --- | --- | --- | --- | --- |
| `--v2-paper` | `#1e1f1c` | | `--v2-good` | `#a6e22e` |
| `--v2-panel` | `#272822` | | `--v2-good-soft` | `#2c361a` |
| `--v2-ink` | `#f8f8f2` | | `--v2-bad` | `#f92672` |
| `--v2-ink-soft` | `#c9cabf` | | `--v2-bad-soft` | `#3a1622` |
| `--v2-ink-faint` | `#8f908a` | | `--v2-caution` | `#e6db74` |
| `--v2-rule` | `#3a3b34` | | `--v2-accent` | `#66d9ef` |
| `--v2-rule-soft` | `#2f302a` | | `--v2-flat` | `#75715e` |
| `--v2-cell-empty` | `#23241f` | | | |

#### The full set — ground, improve, regress, accent
The six-role contract holds in every theme; this table indexes them all (use the
`COLOR_THEMES` tuples in `ui.js` / the per-theme block in `console.css`
for the complete secondary palette). `lineage` = where the palette came from.

| id | ground | `--v2-paper` | `--v2-ink` | `--v2-good` | `--v2-bad` | `--v2-accent` | lineage |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `monokai` | dark | `#1e1f1c` | `#f8f8f2` | `#a6e22e` | `#f92672` | `#66d9ef` | original |
| `solarized-dark` | dark | `#04222B` | `#93A1A1` | `#8BB80E` | `#E0483C` | `#2AA198` | original |
| `solarized-light` | light | `#FDF6E3` | `#586E75` | `#6B9B0B` | `#DC322F` | `#268BD2` | original |
| `google-light` | light | `#FFFFFF` | `#474A4E` | `#34A853` | `#EA4335` | `#1B9CB8` | Gogh |
| `google-dark` | dark | `#202124` | `#FFFFFF` | `#34A853` | `#EA4335` | `#24C1E0` | Gogh |
| `lunaria-light` | light | `#EBE4E1` | `#363434` | `#497D46` | `#783C1F` | `#3778A9` | Gogh |
| `lunaria-eclipse` | dark | `#323F46` | `#DFE2ED` | `#BEDBC1` | `#BA9088` | `#C8429F`* | Gogh |
| `belafonte-day` | light | `#D5CCBA` | `#34292D` | `#6E6A4E` | `#BE100E` | `#426A79` | Gogh |
| `belafonte-night` | dark | `#20111B` | `#D5CCBA` | `#A6A07A` | `#D6403E` | `#6F8E97` | Gogh |
| `paper` | light | `#F2EEDE` | `#1A1A1A` | `#216609` | `#CC3E28` | `#1E6FCC` | Gogh |
| `zenburn` | dark | `#3A3A3A` | `#DCDCCC` | `#8FB28F` | `#CC9393` | `#8CD0D3` | Gogh |
| `selenized-black` | dark | `#181818` | `#DEDEDE` | `#83C746` | `#FF5E56` | `#56D8C9` | Gogh |
| `relaxed` | dark | `#353A44` | `#F7F7F7` | `#A0AC77` | `#BC5653` | `#7EAAC7` | Gogh |
| `espresso` | dark | `#323232` | `#FFFFFF` | `#A5C261` | `#D25252` | `#6C99BB` | Gogh |
| `dracula` | dark | `#282A36` | `#F8F8F2` | `#50FA7B` | `#FF5555` | `#BD93F9` | Gogh |
| `ubuntu` | dark | `#300A24` | `#EEEEEC` | `#8AE234` | `#CC0000` | `#34E2E2` | Gogh |

\* `lunaria-eclipse`'s **preview** swatch substitutes a distinct magenta
(`#C8429F`) because its true `--v2-accent` (`#BEDBC1`, a pale blue-green) is
near-indistinguishable from its pale ink in a 6-swatch strip. The **live**
`--v2-accent` token is unchanged.

The thirteen Gogh palettes are adapted from the terminal colour schemes at
gogh-co.github.io/Gogh. One principled rule maps each onto the role contract:
`paper ← background`, `panel ← background nudged toward the foreground`,
`ink ← bright-white/host`, `ink-soft ← foreground`, `good ← green`, `bad ← red`,
`caution ← yellow`, and `accent ← cyan`. Where a palette's cyan is a
low-contrast neutral, `accent` takes its blue instead, as in Belafonte and
Paper. A few accents and cautions sit off
the source palette for contrast; see the per-theme comments in `console.css`.

### 2.3 Derived colours

The heatmap ramp is built at draw time from the theme tokens — a cool→hot mix
`color-mix(in srgb, var(--v2-hm-hot) <pct>%, var(--v2-hm-cool))`, where
`--v2-hm-cool` defaults to `--v2-accent` and `--v2-hm-hot` to `--v2-bad`
(`console.css`, `svg.heatmap`). The projected-standing tokens
(`--v2-projected`, `--v2-projected-soft`, `--v2-projected-line`,
`--v2-projected-fill`, `--v2-projected-op`, `--v2-projected-dash`) derive the
in-flight "not yet committed" treatment from `--v2-caution` the same way. Tinted backgrounds and shadows likewise
use `color-mix(in srgb, var(--v2-…) <pct>%, transparent)` so they stay
theme-correct in light and dark. **Never** introduce a raw hex into a mark or
component — derive it from a token.

### 2.4 Contrast guidance

- Body ink on ground targets WCAG AA (4.5:1); secondary/faint inks step down for
  hierarchy but stay legible. The Gogh adjustments (Paper keys `ink` off
  near-black rather than its low-contrast palette white) exist to hold this.
- The good/bad/accent signal must read on *every* ground — verify a new figure in
  both a light theme (`paper`) and a dark one (`monokai`) before shipping.
- Focus rings are a solid `2px` `--v2-accent` outline with a small offset (§9).

---

## 3. Typography

Typography is a **separate axis** from colour. Two tokens carry the rule
"sans for prose and controls, mono for data":

- `--v2-sans` sets prose, controls and chrome. `#console-root` is set in it,
  so every element inherits it unless it is data.
- `--v2-mono` sets data, code, ids, hashes, key names and the numbers in
  tables and figures. An element opts in by its role: `.dn-mono`, a table
  (`.dn-mtx`), a tile or stat value (`.dn-tile-value`, `.dn-stat .v`), a tree
  row named by an id (`.dt-leaf[data-kind^="gen"] .dt-text`), the figure text
  classes that hold numbers or ids (tick values, losses, candidate names).

Inside a figure the same split holds. Axis captions, column heads, legends,
gate labels and sentences (`LOSS FLOOR ↓ IMPROVING · ROUNDS →`, `← worse`,
`vs champion v0 · every Δ is vs v0`) take the sans; tick values, losses,
deltas, candidate and entry ids take the mono. A displayed value led by a word
(`open`, `provisional · 1 game`, `single-turn`) is prose and takes the sans
through `ui.valueFace`, which adds `.dn-wordval`.

A typeface picker stamps one of twelve option ids on the root as
`[data-t-type]`; the default is `google-sans-mono`.

### 3.1 The three modes

The picker groups its twelve pairings under three **modes**, four each
(`TYPE_OPTIONS` in `ui.js`). Each option sets a heading face, a prose face and
a data face, and every option keeps the rule true: the prose face is always a
sans, the data face is always a monospace, and the heading face is never a
monospace. `test/interface_rules.test.mjs` checks all twelve.

| mode | heading face | prose face | data face |
| --- | --- | --- | --- |
| `technical` **(default)** | Open Sans · Source Sans 3 · Open Sans · Ubuntu | the same as the heading | **Google Sans Mono** (default) · Source Code Pro · Inconsolata · Ubuntu Mono |
| `editorial` | Fraunces · Bitter · Literata · Domine (serif) | Open Sans | JetBrains Mono |
| `display` | Archivo Narrow · Hanken Grotesk · Barlow Condensed · Bricolage Grotesque | Space Grotesk · Hanken Grotesk · Space Grotesk · Bricolage Grotesque | JetBrains Mono |

An option's id names the face it is chosen for: the heading face, or for the
two monospace faces (Google Sans Mono, Inconsolata) the data face. A
monospace face therefore sets data only and is paired with Open Sans for prose
and headings. An editorial option sets headings and the publication title in
its serif; a display option sets headings in its display face.

### 3.2 The token map

Each `[data-t-type]` rule in `console.css` sets all four tokens to literal
font stacks. The unconditional default on `#console-root` is the
`google-sans-mono` pairing, so a root with no `data-t-type` still lands on the
default:

```css
#console-root {
  /* the brand wordmark pins to a FIXED mono, independent of the user's choice */
  --v2-brand-mono: "JetBrains Mono", ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace;

  /* DEFAULT (Open Sans + Google Sans Mono) */
  --v2-sans:      'Open Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  --v2-mono:      'Google Sans Mono', 'Noto Sans Mono', ui-monospace, monospace;
  --n-font-head:  'Open Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  --n-font-paper: 'Open Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
}
/* an editorial option: a serif heading and paper-title voice */
#console-root[data-t-type="fraunces"] {
  --v2-sans:      'Open Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
  --v2-mono:      'JetBrains Mono', ui-monospace, 'SF Mono', 'Cascadia Mono', Menlo, Consolas, monospace;
  --n-font-head:  'Fraunces', Georgia, serif;
  --n-font-paper: 'Fraunces', Georgia, serif;
}
```

| token | set from the option's | which surface |
| --- | --- | --- |
| `--v2-sans` | prose face | prose, controls, chrome |
| `--v2-mono` | data face | data, ids, code, key names, table and figure numbers |
| `--n-font-head` | heading face | `.dn-h1`, big numerals |
| `--n-font-paper` | heading face (editorial) or prose face | the publication title |

The faces come from the [typeface study](typeface-study/README.md); the
pairings put each face in the role the interface rule allows.

### 3.3 Self-hosted vs loaded

- **One mono is self-hosted woff2**: JetBrains Mono, as
  `JetBrainsMono-Regular/Bold.woff2` under
  `src/zicato/dashboard/static/fonts/`, declared with `@font-face` +
  `font-display: swap` at the top of `console.css`. It backs the fixed brand
  mono (`--v2-brand-mono`) and is the data face of the editorial and display
  options.
- **The other picker faces load from Google Fonts** — the only external
  dependency — injected by `console.js` `ensureFonts()` with `display=swap`
  and a preconnect to the font origins. Every stack lists a system fallback,
  so a slow or blocked font never breaks layout; with the network blocked, the
  default prose face falls back to the system sans (`-apple-system`,
  `Segoe UI`, `system-ui`) and the default data face to Noto Sans Mono or the
  system monospace. The fallbacks are the same in light and dark themes.

### 3.4 Type scale and weights

Base size `calc(13.5px * var(--dt-font-scale, 1))`. `--dt-font-scale` is the
text-only multiplier the **S/M/L** size control stamps on the root (`small`
1.15, the default · `medium` 1.3 · `large` 1.45, `FONTSIZE_OPTIONS` in
`ui.js`); the page scale (§4.4) zooms text and figures together on top of it.
SVG figure text is sized by `svg.js`, so figures do not grow with the size
control. Representative sizes from `console.css`, before the multiplier:

| element | class | size / weight |
| --- | --- | --- |
| page title | `.dn-h1` | 19px / 600 |
| section heading | `.dn-h2` | 13px / 600 |
| subhead (eyebrow) | `.dn-subhead` | 11px sans / uppercase / `0.07em` tracking |
| tile value (big number) | `.dn-tile-value` | 20px mono, `tabular-nums` |
| tile key | `.dn-tile-key` | 10px / uppercase / `0.06em` |
| lede / prose | `.dn-lede` | 12.5px, line-height 1.45, `max-width: 78ch` |
| publication title | `.dn-paper-title` | 28px / 700, `--n-font-paper` |
| SVG captions, axis titles, legends | (mark classes) | 9–11px `var(--v2-sans)` |
| SVG tick values, losses, ids | (mark classes) | 9–11px `var(--v2-mono)`, `tabular-nums` |

Numerics use `font-variant-numeric: tabular-nums` everywhere they appear in a
column or animate, so digits do not jitter.

### 3.5 The dotless-ı wordmark rule

The wordmark is **`zıcato`** — set in `--v2-brand-mono` (a *fixed* mono,
independent of the user's typeface choice so the mark never reflows) with a
**dotless ı** (U+0131). The green accent circle **is** the i's dot, which ties
the wordmark back to the plucked note. In the dashboard the wordmark is an
inline SVG, so the dot can be pinned geometrically over the stem and the
letters can inherit `currentColor` while the dot takes `--zicato-accent`. See
`shell.js` `brandWordmark()` and [docs/brand/README.md](../brand/README.md).

---

## 4. Layout & spacing

### 4.1 The spacing baseline (cozy — the one permanent rhythm)

**Cozy** is the single permanent spacing rhythm, baked unconditionally onto the
root in `console.css`. The page scale (§4.4) is the sizing control.

| token | value | role |
| --- | --- | --- |
| `--dt-rail` | `288px` | tree-sidebar rail width (resizable) |
| `--dt-pad-x` | `56px` | detail horizontal padding |
| `--dt-pad-y` | `40px` | detail vertical padding |
| `--dt-section-gap` | `30px` | gap between sections |
| `--dt-panel-pad-x` / `-y` | `19px` / `17px` | panel inner padding |
| `--dt-row-gap` | `30px` | `.dn-row` flex gap |
| `--dt-card-min` | `270px` | card grid min column |
| `--dt-card-gap` | `18px` | card grid gap |
| `--dt-card-pad` | `16px` | card inner padding |
| `--dt-reel-scale` | `1.18` | vertical scale of the round-timeline spine |
| `--dt-font-scale` | `1` (the shell stamps the chosen size over it) | global text-size multiplier (§3.4) |

Radii: panels `4px`, cards/buttons `5px`, hovercard and popovers `6px`. A
rounded box holds content (a panel, a card, a popover) or takes input (a
button, a field); a label never sits in one (§6.2). Hairlines are always `1px solid var(--v2-rule)` (or `--v2-rule-soft` for a
fainter inner rule). SVG strokes use `vector-effect: non-scaling-stroke` so a
hairline stays a hairline under the page-zoom.

### 4.2 Grid & containment

- **Fluid detail pane.** `.dt-viewhost { width:100%; max-width: min(100%, 2400px) }`
  — fills the available width; only a generous cap guards prose line-length on
  ultra-wide monitors. Bigger diagrams on bigger screens.
- **Containment guarantee.** No panel ever scrolls horizontally or lets a child
  escape. Figures are fit-to-width (`width:100%` + a `viewBox`); a table wider
  than its pane carries its *own* contained overflow via `.dn-table-scroll`, never the
  panel.
- **Body split.** `.dt-body` is a 3-track grid: `var(--dt-rail) · 0 · minmax(0,1fr)`
  — a sticky tree sidebar, a zero-width draggable resize handle (`.dt-rail-handle`,
  hit-area widened by negative margins), and the reflowing detail pane.

### 4.3 Top-bar anatomy (`.dt-topbar`)

Sticky and hairline-bottomed, assembled in `shell.js` `mountShell`, and set
in the sans. At desktop widths it holds **one line**: nothing wraps, and an id
never breaks inside itself. The breadcrumb shrinks first: each crumb truncates
with an ellipsis, while its DOM text keeps the full value and a crumb of ten
or more characters shows it in a hovercard. The lower-priority items then
yield as the viewport narrows:

| viewport width | yields |
| --- | --- |
| ≤ 1480px | the research-preview note |
| ≤ 1220px | the words beside the log and settings marks, and the colour theme's name (each control keeps its `aria-label`) |
| ≤ 1080px | the "last seen" note, the in-flight count and the `console` sub-word |
| ≤ 760px (phone) | the bar wraps to two lines, the breadcrumb takes its own line, and the wordmark and phase label hide, so the page never scrolls sideways |

Left → right:

1. **`.dt-back`** — the `↑ up` control. Navigates *up the selection hierarchy*
   (candidate → rounds → epoch → environment) rather than browser-back. Disabled
   state `.dt-back-off`.
2. **`.dt-brand`** — the inline-SVG mark (`.dt-brand-mark`) + the inline-SVG
   wordmark (`.dt-brand-name`, `zıcato`) + a `.dt-brand-sub` tag reading
   `console` + a stacked "research preview" note (`.dt-respreview`).
3. **`.dt-crumbs`** — breadcrumb trail (sans, faint), `.dt-crumb` links +
   `.dt-crumb-sep`.
4. `.dt-topbar-spacer` (flex spacer).
5. **`.dt-nav-exec`** — the liveness-gated `execution` link (with the external-link
   icon) into the
   harmonograf meta-loop session; empty when no harmonograf server is
   reachable.
6. **`.dt-nav-logs`** — a `log` entry (list icon) into the operator-log pane (`#/logs`).
7. **`.dt-nav-build`** — a `settings` entry (gear icon; opens read-only contract and model
   configuration plus editable appearance preferences).
8. **Colour swatch dropdown** (`.dt-cd`, §6.8). The typeface picker, the text
   size, the page scale and the side-panel width live in Settings →
   Appearance.
9. **`.dt-loopctl`** — the pause/resume and skip-round controls, rendered only
   while the loop is live and the workspace is writable.
10. **`.dt-status`** — the status line (§4.5).

> **Note — there is no command palette.** Nothing under `static/js/**`
> implements one. Navigation is via the tree sidebar
> (`tree.js`), the breadcrumbs, and the `↑ up` control. If you add a palette,
> dock it from the top bar and theme it with the dropdown tokens (`.dt-cd-list`
> bg `--v2-panel`, border `--v2-rule`, options on `--v2-rule-soft` hover).

### 4.4 The page scale

The page-sizing control is a native range input in Settings → Appearance
(`.dn-set-range`, 70 %–150 % in 5 % steps, default 100 %) + a `%` readout
(`.dn-set-readout`) + a `⟲` reset button (`.dn-set-reset`). It applies
page-wide via `zoom` on the app root (`shell.applyScale`), which **reflows**
(not a transform) so the page re-wraps at the scaled size and never clips.
Persisted under `zicato.console.scale`. Focus ring `2px --v2-accent`.

### 4.5 The status line (`.dt-status`)

The status area reads as one status: ONE drawn mark, the connection word
(present only while the socket is broken), and ONE liveness label
(`.dt-run-state`) that reads `<STATE> · <structure · phase> · <N units>`, or
`<STATE> · last seen Ns ago` when the heartbeat has frozen (`shell.js`
`mountShell` and `renderStatus`, `livestatus.statusMark`,
`livestatus.runStateLabel`):

```html
<span class="dt-status dt-connected">          <!-- + .dt-running while a run is live -->
  <span class="dt-status-mark" role="img" data-state="live"
        aria-label="Run live: the loop is making progress">
    <svg class="zi zi-status-running" data-icon="status-running" aria-hidden="true">…</svg>
  </span>
  <span class="dt-status-text"></span>          <!-- empty (hidden) while the socket is healthy -->
  <span class="dt-run-state dt-rs-on dt-rs-live" aria-live="polite">
    <span class="dt-rs-text">LIVE</span>        <!-- LIVE / STALLED / SETTLED / DEAD / INTERRUPTED -->
    <span class="dt-run-label">racing · rung 0</span>
    <span class="dt-run-count">3 in flight</span>
    <span class="dt-status-stale"></span>       <!-- "last seen Ns ago" when frozen -->
  </span>
</span>
```

The mark folds two signals into one drawing, because they answer one
question, whether the loop is running now. The browser's event stream
connection decides whether the console holds a current verdict at all. The
run verdict decides which verdict that is. The mark takes one of four
drawings from `js/icons.js`:

| drawing | icon | states |
| --- | --- | --- |
| filled circle | `status-running` | LIVE, STALLED |
| open circle | `status-settled` | SETTLED |
| struck circle | `status-unsettled` | DEAD, INTERRUPTED |
| dashed circle | `status-unknown` | socket down (`offline`), no run recorded (`idle`) |

A broken socket outranks the run verdict: the mark turns dashed and flat
while the connection word says `connecting…` or `disconnected — retrying`,
and the last-known state word stays beside it. The mark's colour speaks the
colour roles by direction: LIVE = good, STALLED = caution, SETTLED = calm ink
(a clean end), DEAD and INTERRUPTED = bad (gone without settling), no run =
faint ink, socket down = flat. The mark's `aria-label` (also its `title`)
names the state in a sentence. The four run states key on the orchestrator
progress cursor rather than a heartbeat timestamp. Only the LIVE mark pulses
(`@keyframes dt-status-pulse`, a 1.6s opacity fade), and the pulse is
disabled under `prefers-reduced-motion`. The `LIVE` word (`.dt-live-state`) and the structure
label (`.dt-structure-label`, `structure Racing (successive halving)`) ride in
the view header rather than the top bar, as plain text.

---

## 5. Line-art figure language

This is the distinctive part — the conventions that let you draw a **new** figure
(an execution timeline, a Gantt, a flow) in-language. Every figure is built in
[`svg.js`](../../src/zicato/dashboard/static/js/svg.js) with a tiny
dependency-free helper layer (`svgEl`, `scale`, `extent`, `fmt`).

### 5.1 Stroke & ink conventions

These hold for **every** mark (drawn from the `.dn-*` / `.ezn-*` rules in
`console.css`):

| convention | concrete value | where |
| --- | --- | --- |
| data line stroke | `stroke-width: 1.2–1.4`, `fill:none`, `vector-effect: non-scaling-stroke` | `.dn-spark-line` 1.4, `.dn-pslope-line` 1.2 |
| the champion spine (the one emphasis) | `stroke: var(--v2-accent); stroke-width: 2.0–2.4` | `.dn-spine-line` 2.4, `.ezn-edge-spine` 2.0, `.dn-roundtl-spineline` 2.2 |
| reference / baseline rule | `stroke: var(--v2-ink-faint); stroke-width:1; stroke-dasharray: 3 3` | `.dn-ref-rule` |
| a pending / racing edge | `stroke: var(--v2-accent); stroke-dasharray: 4 3` (never red) | `.ezn-edge-neutral` |
| good / bad mark | `fill`/`stroke: var(--v2-good)` / `var(--v2-bad)` | `.dn-dot.dn-good`, `.dn-glyph-fail` |
| node dot radius | `r: 2.2–4.5` (champion bigger than challenger) | `bumps` 4.5 and 3.5, sparkline endDot 2.2 |
| soft fill | a token mixed toward transparent, ~12–18% | `color-mix(in srgb, var(--v2-accent) 18%, transparent)` (`.dt-node.dt-sel`) |
| line caps/joins (chrome glyphs) | `stroke-linecap:"round"`, `stroke-linejoin:"round"` | brand mark |
| status glyph aspect | a **fixed 1:1 `viewBox`** overlay so a stretched cell never shears it | `outcomeGlyph`, `sparkbar` verdict |

**Fit-to-width is mandatory:** every figure SVG carries `width:"100%"`, an
explicit `viewBox`, a `preserveAspectRatio`, and `role:"img"`. There is **no
fixed pixel width that exceeds the pane, and no pan/zoom**. A figure that must
stretch its bars uses `preserveAspectRatio:"none"` but then puts any glyph that
must stay round into a separate 1:1 overlay (see `sparkbar`).

**Shared semantic marks** are drawn icons from `js/icons.js` (§8), never typed
symbols. The crowns have one definition, re-exported by `svg.js`:

```js
export const CROWN = Object.freeze({ current: 'crown', former: 'crown-former' });
```

`up` survives · `fail` cut · `ring` pending · `crown` (solid) current champion ·
`crown-former` (open) former champion · `timeout` timeout · `pass` pass. Inside a
figure, `figIcon` places an icon on a text centre line and `iconBeside` sets one
beside a `<text>` label. The reference rule means **good = below / lower loss,
bad = above / higher loss**.

### 5.2 Worked snippet — the sparkline

The word-sized trend mark. Note: `width:"100%"`, the `viewBox`, the
pen-up/pen-down path for gaps, and the end-dot coloured good/bad by direction
(`svg.js`, abridged; the `responsive` and `intrinsic` options pick the
full-width hero or the intrinsic-width sizing described in
[`js/CONTRACTS.md`](../../src/zicato/dashboard/static/js/CONTRACTS.md) §4a):

```js
export function sparkline(opts) {
  const o = opts || {};
  const w = o.width || 120, h = o.height || 28, pad = 2;
  const raw = Array.isArray(o.values) ? o.values : [];
  const svg = svgEl('svg', {
    class: 'dn-spark', width: '100%', height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'none', role: 'img',
  });
  // ... scales ...
  let d = '', penDown = false;
  raw.forEach((v, i) => {
    if (!isNum(v)) { penDown = false; return; }       // gap → lift the pen
    d += `${penDown ? 'L' : 'M'}${x(i).toFixed(2)},${y(v).toFixed(2)} `;
    penDown = true;
  });
  svg.appendChild(svgEl('path', { d: d.trim(), class: 'dn-spark-line', fill: 'none' }));
  // end-dot: dn-good if the series improved vs its first point, else dn-bad
}
```

```css
.dn-spark-line { stroke: var(--v2-ink); stroke-width: 1.4; vector-effect: non-scaling-stroke; }
.dn-spark-baseline { stroke: var(--v2-rule); stroke-width: 1; stroke-dasharray: 2 2; }
.dn-spark-dot { fill: var(--v2-ink); }
.dn-spark-dot.dn-good { fill: var(--v2-good); }
.dn-spark-dot.dn-bad  { fill: var(--v2-bad); }
```

### 5.3 Worked snippet — the reign Gantt (`reignGantt`)

A horizontal-bar tenure chart — **directly the model for an execution timeline**:
one row per entity, a bar spanning the rounds it held, round-axis ticks along the
top, the current item in `--v2-accent` + the solid crown, former items dim ink + the open crown
(`svg.js`, abridged):

```js
export function reignGantt(opts) {
  const reigns = (Array.isArray(o.reigns) ? o.reigns : []).filter(r => r && r.id != null);
  const w = o.width || 640, rowH = o.rowHeight || 22, padL = o.labelWidth || 120;
  const svg = svgEl('svg', {
    class: 'dn-reigngantt', width: '100%', height: h,
    viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMinYMin meet', role: 'img',
    'aria-label': 'Champion reign across rounds',
  });
  const x = scale([0, Math.max(1, maxRound)], [padL + 4, w - padR]);
  for (let ri = 0; ri <= maxRound; ri++) {            // round-axis ticks + gridlines
    const tx = x(ri);
    /* <text class="dn-reigngantt-axis">r{ri}</text> */
    svg.appendChild(svgEl('line', { x1: tx, x2: tx, y1: top - 4, y2: h - 6, class: 'dn-reigngantt-grid' }));
  }
  reigns.forEach((r, i) => {
    const cy = top + i * rowH + rowH / 2;
    const x0 = x(r.fromRound ?? 0), x1 = x(r.toRound ?? maxRound);
    const current = !!r.current;
    // label: the id, then the solid crown (current) or the open crown (former)
    iconBeside(g, lbl, current ? CROWN.current : CROWN.former, 10.5, { tone: current ? 'accent' : 'faint' });
    g.appendChild(hov(svgEl('rect', {
      x: x0, y: cy - rowH * 0.32, width: Math.max(4, x1 - x0), height: rowH * 0.64, rx: 3,
      class: 'dn-reigngantt-bar' + (current ? ' dn-reigngantt-bar-current' : ' dn-reigngantt-bar-former'),
    }), `${r.id} ${current ? 'current' : 'former'} champion · held r${r.fromRound}…`));
  });
}
```

```css
.dn-reigngantt-axis { fill: var(--v2-ink-faint); font: 9.5px var(--v2-mono); }
.dn-reigngantt-grid { stroke: var(--v2-rule-soft); stroke-width: 0.6; vector-effect: non-scaling-stroke; }
.dn-reigngantt-bar  { stroke: none; }
.dn-reigngantt-bar.dn-reigngantt-bar-current { fill: var(--v2-accent); fill-opacity: 0.85; }
.dn-reigngantt-bar.dn-reigngantt-bar-former  { fill: var(--v2-ink-faint); fill-opacity: 0.45; }
.dn-reigngantt-row:hover .dn-reigngantt-bar { fill-opacity: 1; }
.dn-reigngantt-row:focus-visible { outline: 2px solid var(--v2-accent); }
```

Takeaways for any new timeline: **faint dashed/thin gridlines** (`0.6` width,
`--v2-rule-soft`), **`rx:3` bars** filled with a token at reduced `fill-opacity`
that lifts to `1` on hover, the **one emphasis** carried by `--v2-accent`, and a
`hov()` hovercard on each bar.

### 5.4 The figure catalogue

The full inventory of figures (purpose-mapped) is documented in
[CONSOLE-DESIGN-LANGUAGE.md §4.1](CONSOLE-DESIGN-LANGUAGE.md). The language-level
point: each is a small, single-purpose, fit-to-width SVG honouring §5.1. Build
new figures from the same vocabulary — a lane ends at a cut, lanes converge at a
match, dots sit relative to a reference rule, the spine is the one accent line.

### 5.5 The hovercard (hover-for-detail)

Hover-for-detail is first-class. `hovercard.js` mounts a **singleton** card
*inside* `#console-root`, so it inherits the live per-theme tokens
(`--v2-panel` bg, `--v2-ink` text, `--v2-rule` border, the sans face for its
prose; an id or value inside it takes `.dn-mono`). Every mark
calls `hov(node, tip)`. Crucially it is a **transient overlay outside the
digest-gated render** (§7) — showing and hiding it only toggles the singleton
`.dn-hovercard`'s visibility, so it can never trigger a repaint loop. It is
`pointer-events:none` (never steals hover), viewport-flipped/clamped,
keyboard-accessible (`role="tooltip"` via `aria-describedby`), and collapses
its fade under `prefers-reduced-motion`.

---

## 6. Components

All scoped under `#console-root`; all token-only.

### 6.1 Buttons & links

| element | class | look |
| --- | --- | --- |
| primary action / themed link-button | `.dn-linkbtn` (on an `<a>` or a `<button>`) | sans, `1px solid var(--v2-accent)`, transparent → on hover fills `--v2-accent` with `--v2-paper` text |
| up / back | `.dt-back` | sans, `1px solid var(--v2-rule)`, hover → accent fill |
| an id that links to its page (a generation, a judge) | `.dn-idlink` | the id in `--v2-mono` and the accent colour, no box, underlined on hover |
| icon button (reset) | `.dn-set-reset` | the `reset` icon, hover → accent fill |

```html
<a class="dn-linkbtn" href="#/e/epoch-3">open transcript <svg class="zi zi-forward" data-icon="forward" …/></a>
```

Do: keep buttons sans and outline-first, filling the accent only on hover.
Do not: leave a link or a native `<button>` unstyled.

### 6.2 States and verdicts are plain text, never chips

A state reads as plain text in its semantic colour, led by a drawn mark from
`js/icons.js` where the state has one. No state, verdict, role or kind sits
in a pill, a tag or a badge: nothing draws a rounded box, a fill or a border
round a label. `test/interface_rules.test.mjs` fails on any class named for a
pill, chip, tag or badge, and on any rounded, filled or bordered box that is
not a container or a control.

```html
<span class="dn-state dn-promoted"><svg class="zi zi-up" …/>promoted</span>
<span class="dn-state dn-rejected"><svg class="zi zi-fail" …/>rejected</span>
<span class="dn-state dn-pending"><svg class="zi zi-more" …/>racing…</span>   <!-- accent, NOT red -->
```

- `verdictLabel(decision)` in `ui.js` builds a decision: `.dn-promoted` (good,
  the `up` mark), `.dn-rejected` (bad, `fail`), `.dn-deferred` (caution,
  `timeout`), `.dn-pending` (**accent**, `more` — an in-flight candidate is
  neutral, never red), `.dn-baseline` (ink, no mark).
- `stateLabel(tone, word)` colours a caller's word by a decision tone without
  a mark (a role such as `champion`, a severity).
- `flagLabel(tone, word)` builds a `.dn-flag` word: lowercase, coloured by
  `.dn-flag-live` (caution) / `-open` (good) / `-closed` (faint) and the loop
  verdict tones.
- A tree row's caption is a word at the row's right edge: a leaf's role
  (`champion`, `former champion`, `defends · cached`) in `.dt-role`, and a
  branch's count, `current`, `workspace` or gate outcome
  (`v0 defends · ↑ v2 promoted`) in `.dt-sub`. When the rail is short of room
  the caption truncates with an ellipsis first. The row's name keeps its full
  text until the caption has no room left, so a round row never reads
  `Roun…`. The row label is a four-column grid (mark, name, live pulse,
  caption) whose name column is `minmax(0, max-content)` and whose caption
  column is `minmax(0, 1fr)`. Hovering the caption shows the row name and the
  full caption in a hovercard, and the row button's accessible name holds
  both.
- A held-out board entry is marked by the drawn `holdout` padlock and the
  accent colour, in the evals matrix and in the board-status entry grid.
- A pane letter in the side-by-side compare (`.dt-split-letter`, A or B) is
  bold text in the pane's colour.

A filter or level toggle (`.dn-evals-filter`, `.dt-logs-level`) is a text
button; the selected one reads in the accent colour.

### 6.3 Cards

```html
<a class="dn-fleet-card dn-is-current">
  <div class="dn-fleet-head"><span class="dn-fleet-id">epoch-7</span> …</div>
  <div class="dn-fleet-goal">…goal text, clamped to 3.4em…</div>
  <div class="dn-fleet-spark"><!-- sparkline --></div>
  <div class="dn-fleet-stats">…<div class="dn-mini">…</div></div>
</a>
```

`.dn-fleet-card`: `1px solid var(--v2-rule)`, `border-radius:5px`,
`background:var(--v2-panel)`; hover lifts (`translateY(-1px)`) and borders
accent; the current item borders `--v2-good`. The small-multiples trellis uses
`.dn-trellis-cell` on the same idiom; a live cell gets `.dn-trellis-live`
(accent border + inset ring).

### 6.4 Tables

`.dn-board-table` / `.dn-md-table` / `.dn-scores-table`: `border-collapse`,
`1px solid var(--v2-rule)` cells, header row on `--v2-rule-soft`. Numeric columns
get `.dn-num` (`text-align:right; tabular-nums`). The champion row tints
`--v2-good-soft` (`tr.dn-board-champ`). Wide tables wrap in `.dn-table-scroll`
(contained overflow) so the page never scrolls sideways.

### 6.5 Popovers / tooltips

The mark-level hovercard is §5.5. For richer board-status popovers, the same card
hosts a titled body: `.dn-hc-body` > `.dn-hc-title` + `.dn-hc-row` +
`.dn-hc-link`. The lifecycle DAG's `?` info mark (`.ezn-dag-info`) and the gate
node (`.ezn-gate-node { cursor: help }`) open the full how-to in the hovercard
rather than crowding the figure.

### 6.6 Tabs / section lists

The Settings surface (`.dn-settings`) is a section **list + host**:
`a.dn-set-railitem` (the open section, `.dn-set-railitem-active`, renders its
name in the accent colour with no fill; the section icon sits in
`.dn-set-railglyph` in accent). Disclosure sections use `.dn-brief` (a `<details>` with a
rotating `.chev` chevron icon). The epoch publication renders as panels rather than a tab
strip.

### 6.7 Selection and edges

The selected item renders its **name in the accent colour**, with no fill and
no edge bar: the selected tree row (`.dt-tree .dt-node.dt-sel .dt-text`), the
pinned mutation-surface row (`.dn-mtx-pinned .dn-mtx-file`), the selected
trace episode (`.dn-trace-ep-on .dn-trace-ep-sum`), the open Settings
section, the selected filter or text size, and the selected option in a
picker.

No container and no selection draws an **accent left rail**: no coloured
`border-left`, no inset edge shadow, no `::before` bar. A meaning a container
must show rides a drawn mark or the text colour:

- a transcript turn names its role with a mark in its head (`agent`,
  `message` for the user, `note` for the system), coloured by role;
- a held-out entry carries the `holdout` padlock and the accent colour;
- a finding row leads with its tone mark and its tone-coloured verdict word;
- a trace episode leads with its signal mark in the signal's tone;
- a deferred or inconclusive caption leads with the `caution` mark in the
  caution colour.

A neutral 1px hairline in `--v2-rule` or `--v2-rule-soft` is structure and
stays: the execution outline's tree connectors, the side-by-side diff's column
divider, the settings drawer's edge. `test/interface_rules.test.mjs` fails on
any other left edge, in the stylesheet or in an inline style.

### 6.8 The swatch / typeface pickers

- **Colour** — `.dt-cd` swatch dropdown: a `.dt-cd-trigger` (current name + a
  six-swatch `.dt-swatch-strip` preview + a `.dt-cd-caret`) opens a
  `.dt-cd-list` listbox (`role` listbox; options `.dt-cd-option` with
  `aria-selected`; selected name in `--v2-accent`).
- **Typeface** — a **grouped popover** (`typefacedropdown.js`): a trigger
  (current pairing + a micro-specimen) opens a listbox grouped under three mode
  headers (Technical · Editorial · Display), each over four pairings (twelve
  total, §3.1), every option a true type specimen of its heading, prose and
  data faces. The popover also carries an **S/M/L** font-size segmented control
  (`FONTSIZE_OPTIONS`), orthogonal to the page scale; the selected size reads
  in the accent colour. It lives in Settings → Appearance; the colour dropdown appears
  both there and in the top bar.

The theme + typeface persist to `localStorage` (`zicato.console.theme`,
`zicato.console.typeface`; the size as `zicato.console.fontsize`) and drive the same
`applyTheme` / `applyTypeface` / `applyFontSize` the Settings → Appearance
section uses — one source of truth, synced across every live picker.

---

## 7. Motion & render discipline

### 7.1 Digest-gating — the no-flash rule

**Never rebuild the DOM on a no-op server-sent-events (SSE) heartbeat.** This
is a hard rule. The bug
class it prevents — the **flashing / refresh bug** — is a steady heartbeat
re-dispatch wiping and rebuilding a panel every tick, flashing the screen, losing
scroll position, and destroying hovercard/focus state.

The mechanism is `gatedSwap(host, digest, build)` in
[`ui.js`](../../src/zicato/dashboard/static/js/ui.js):

```js
export function gatedSwap(host, digest, build) {
  if (!host) return false;
  const next = String(digest);
  // a view computes `digest` over ONLY its structural/content data —
  // timestamps and heartbeat fields are EXCLUDED.
  if (host.getAttribute('data-t-digest') === next && host.firstChild) return false; // ← no-op
  const built = build();          // build first, so a throwing builder leaves the old DOM
  clearChildren(host);
  const nodes = Array.isArray(built) ? built : [built];
  for (const n of nodes) { if (n) host.appendChild(n); }
  host.setAttribute('data-t-digest', next);
  return true;
}
```

The discipline in full:
- **Digest over structural data only.** A view computes a stable digest excluding
  timestamps/heartbeat fields. Named digests (`treeDigest`, `structureDigest`,
  `funnelDigest`, `proposingDigest`, `liveStatusDigest`, per-view/per-pane) each
  gate their own host. A steady heartbeat is a true no-op.
- **One persistent host per pane**, independently gated. Each compare side and
  each board sub-host (`board-upper` vs `board-xscript`) is gated separately, so
  advancing in-flight progress repaints the upper pane while the transcript host
  keeps its scroll position.
- **The host clears only on a real selection change** (a `~cmp` compare change
  counts as a selection change).
- **The hovercard is outside the gated render** (§5.5) — showing it never
  repaints a figure.

### 7.2 Transitions & reduced motion

- Motion is CSS `transition` (theme swap, hovers, the page-scale reflow), **never
  `animation: …infinite`** for structure. Live state animates *values /
  positions* (GPU-friendly `transform` / `opacity` / `width`); digest-gating
  governs *structure*.
- The keyframe animations are the liveness pulses (`dt-status-pulse` on the
  top bar's LIVE status mark; `dt-run-pulse` on the live hero and band dots
  and the in-flight count), the
  projected-row pulse (`dt-proj-pulse`), and two one-shot entry fades
  (`dt-live-fade`, `dt-ticker-in`). Every one is switched off under
  `@media (prefers-reduced-motion: reduce)`.
- Theme/colour transitions are `background 0.18s ease, color 0.18s ease` on the
  root; hovercard fade is `120ms`, also reduced-motion-aware.

---

## 8. Iconography

- **The brand mark** — a single continuous stroke (golden-spiral scroll → string
  → pluck → damped-sine sparkline → bridge tick), `stroke:currentColor`,
  `stroke-width:5.0`, round caps; the one accent dot at the pluck vertex fills
  `var(--zicato-accent)`. The canonical asset:

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="71 35 229 75" role="img" aria-label="zicato">
  <g fill="none" stroke="currentColor" stroke-width="5.0" stroke-linecap="round" stroke-linejoin="round">
    <path d="M94,52.5 … L104,80 L150,80 L170,102 L190,80 Q206,56 222,80 Q236,102 250,80 Q261,68 272,80 L292,80"/>
    <path d="M292,66 L292,94"/>                           <!-- the bridge tick -->
  </g>
  <circle cx="170" cy="102" r="5.5" fill="var(--zicato-accent, #2FA46A)"/>
</svg>
```

  (full path in [docs/brand/zicato-mark.svg](../brand/zicato-mark.svg) /
  `shell.js` `_MARK_SPIRAL_PATH` and `_MARK_BRIDGE_PATH`).
- **Favicon vs mark.** The full golden-spiral mark is glorious at lockup/180px
  but muddies at 16px, so the **tab favicon** is a simplified `z` + green
  plucked-note (`docs/brand/zicato-favicon.svg`); the full mark stays for the
  180px apple-touch tile. Different mark by size — standard favicon practice.
- **The icon set** — every other mark the console draws comes from one module,
  `js/icons.js`: loop controls, close, verdicts, crowns, tree marks, live-feed
  marks, chevrons, refresh, overflow, execution kinds and link arrows. The
  console never types these as Unicode symbols. The bundled faces carry few of
  them, so a browser would substitute a system font, whose weight, baseline and
  advance change with the operating system and the typeface choice, and would
  render some as colour emoji. Each icon follows the brand mark's line
  character on a smaller grid:
  - a `0 0 16 16` viewBox, a `1.5`-unit stroke with round caps and joins, and
    `stroke: currentColor`, so an icon takes the colour of the text around it;
    a filled part (the current champion's crown, the play triangle, the dots of
    the overflow mark) fills with `currentColor` too;
  - `aria-hidden="true"`: the control or row carrying the icon names itself
    through its words or its `aria-label`;
  - in running text an icon is one em square (`.zi` in `console.css`); inside a
    figure it takes explicit `x`, `y` and size (`svg.js` `figIcon`,
    `iconBeside`) and a `dt-icon-<tone>` class for its colour;
  - `icon(name)` builds one, `iconLabel(name, words)` returns an icon plus its
    words as children, and `patchIconLabel` updates a long-lived node only when
    the icon or the words change, so a no-op heartbeat writes nothing.

  Characters that are text inside words or values stay typed: the `·`
  separator, dashes, the `…` truncation marker, typographic quotes, arrows
  inside prose and axis captions (`cause → effect`, `rounds →`, `scalar ↓`),
  `×` and `±` in values, and the mathematical signs of formulas. The node test
  `test/icons.test.mjs` fails when any other non-letter symbol appears in the
  modules' string text.

See [docs/brand/README.md](../brand/README.md) for the asset table and usage.

---

## 9. Accessibility

- **Contrast.** Body ink targets WCAG AA (4.5:1) on every ground; the Gogh
  ink/accent nudges exist to hold it (§2.4). Verify a new surface in both a light
  and a dark theme.
- **Focus rings.** A consistent solid `2px solid var(--v2-accent)` outline with a
  small `outline-offset` on interactive controls (`:focus-visible`):
  `.dt-cd-trigger`, `.dn-set-range`, `.dt-rail-handle`, and focusable SVG marks
  (`.dn-duelflow-lane:focus-visible`, `.dn-reigngantt-row:focus-visible`).
- **Skip link.** `index.html` ships `<a class="skip-link" href="#main-content">`
  (visually hidden until focused, then pinned top-left — `.skip-link` in
  `style.css`). Its target is the shell's view host, the `<main>` element
  that holds the active view; it carries `id="main-content"` and
  `tabindex="-1"`, so activating the link moves keyboard focus past the top
  bar and the navigation panel.
- **`prefers-reduced-motion: reduce`** — disables every pulse animation, the
  entry fades and the hovercard fade (several `@media` blocks in `console.css`).
- **`prefers-color-scheme`** — the brand assets adapt automatically: the mark
  strokes `currentColor` (dark-on-light / light-on-dark) and READMEs use
  `<picture>` with light/dark sources. The dashboard's theme is an explicit
  user choice (sixteen themes), but the brand never needs recolouring.
- **Roles & labels.** Figures are `role="img"` with an `aria-label`; the
  hovercard is `role="tooltip"` wired via `aria-describedby`; the run-state
  pill is `aria-live="polite"`; interactive marks are keyboard-activatable
  (Enter/Space → click, `svg.js` `clickable`).

---

## 10. Worked example — building a new surface in the language

**A harmonograf execution timeline** — a per-run, step-by-step Gantt showing a
run's lifecycle phases over wall-clock. It is a conceptual illustration; do not
implement it here.

**1 — Frame & colour roles.** The view is a `.dn-section` panel
(`background:var(--v2-panel)`, `1px solid var(--v2-rule)`, radius 4px) on the
`--v2-paper` ground. The timeline's "current phase" carries the **one** accent
(`--v2-accent`); completed phases that succeeded read `--v2-good`, failed phases
`--v2-bad`, a pending/in-flight phase reads `--v2-accent` dashed (never red),
skipped phases `--v2-flat`. Axis ticks and gridlines read `--v2-ink-faint` /
`--v2-rule-soft`.

**2 — Typeface roles.** The panel heading takes `--n-font-head`; phase labels and
the axis title take `--v2-sans`; the time-axis tick values and durations take
`--v2-mono` with `font-variant-numeric: tabular-nums` (so durations align).

**3 — Draw it in-language (clone `reignGantt`, §5.3).** One row per phase, a bar
spanning `[startT, endT]` mapped through `scale([t0, t1], [padL, w-padR])`:

```js
const svg = svgEl('svg', {
  class: 'hg-timeline', width: '100%', height: h,
  viewBox: `0 0 ${w} ${h}`, preserveAspectRatio: 'xMinYMin meet',
  role: 'img', 'aria-label': 'Run execution timeline',
});
const x = scale([t0, t1], [padL + 4, w - padR]);
ticks.forEach(t => svg.appendChild(svgEl('line', {
  x1: x(t), x2: x(t), y1: top - 4, y2: h - 6,
  class: 'hg-grid',                       // stroke:var(--v2-rule-soft); stroke-width:0.6; non-scaling
})));
phases.forEach((p, i) => {
  const cy = top + i * rowH + rowH / 2;
  const cls = p.current ? 'hg-bar-current'
            : p.failed  ? 'hg-bar-bad'
            : p.done    ? 'hg-bar-good'
            : 'hg-bar-pending';
  const bar = svgEl('rect', {
    x: x(p.start), y: cy - rowH * 0.32, width: Math.max(4, x(p.end) - x(p.start)),
    height: rowH * 0.64, rx: 3, class: 'hg-bar ' + cls,
  });
  hov(bar, `${p.name} · ${fmt((p.end - p.start) / 1000, 1)}s · ${p.status}`);
  // the current phase gets a crown-style marker; the running edge is dashed accent
});
```

```css
.hg-bar.hg-bar-current { fill: var(--v2-accent);   fill-opacity: 0.85; }
.hg-bar.hg-bar-good    { fill: var(--v2-good);      fill-opacity: 0.8;  }
.hg-bar.hg-bar-bad     { fill: var(--v2-bad);       fill-opacity: 0.8;  }
.hg-bar.hg-bar-pending { fill: var(--v2-rule-soft);
                         stroke: var(--v2-accent); stroke-dasharray: 4 3; }
.hg-bar:hover          { fill-opacity: 1; }
.hg-timeline-lane:focus-visible { outline: 2px solid var(--v2-accent); }
```

No gridframe, no 3-D, hairline ticks, `rx:3` bars at reduced `fill-opacity` that
lift on hover, the one accent for "now" — Tufte data-ink throughout.

**4 — Hover detail.** Each bar calls `hov(bar, tip)` — the singleton hovercard
inherits the live theme tokens and reads correctly across all sixteen themes; it
is `pointer-events:none` and outside the gated render, so it never repaints the
figure.

**5 — Theme-adaptive.** Because every value above is a `--v2-*` token, switching
from `monokai` to `paper` (or any of the sixteen) re-skins the whole timeline
with **no re-render** — a pure CSS swap. No hardcoded hex anywhere.

**6 — Digest-gated & live.** The view computes a digest over the *structural*
phase data only (phase ids, statuses, start/end) — excluding the heartbeat
timestamp — and renders through `gatedSwap(host, digest, build)`. A steady SSE
heartbeat is a no-op (no flash, scroll preserved). The "now" marker and a live
phase's progress bar animate via `transform`/`width` (GPU-friendly), collapsing
under `prefers-reduced-motion`. The result reads as alive without faking
completed state.

That is the whole recipe — tokens for colour and type, the line-art conventions
for the figure, the hovercard for detail, digest-gating for liveness. Any new
zicato surface is built the same way.

---

## 11. Do and do not

**Do**
- Read colour from `--v2-*` tokens and type from `--v2-sans` / `--v2-mono` /
  `--n-font-head` — never hardcode hex or a font family in a mark.
- Earn `--v2-good` / `--v2-bad` by data **direction**; render an in-flight /
  unscored item as neutral `--v2-accent`, never red.
- Make every figure fit-to-width (`width:100%` + `viewBox` + `role="img"`); put a
  glyph that must stay round into a separate 1:1 overlay.
- Reserve `--v2-accent` for the **one** structural emphasis (the spine, the
  current item, an interactive focus) and the single `--zicato-accent` for the
  brand dot.
- Render through `gatedSwap` with a structural digest; keep the hovercard outside
  the gated render.
- Give every interactive control a `:focus-visible` `2px var(--v2-accent)` ring;
  gate motion behind `prefers-reduced-motion`.
- Set the brand wordmark in `--v2-brand-mono` with the dotless ı and the green
  accent as its dot.

**Do not**
- Do not rebuild the DOM on a no-op heartbeat (the flashing bug, §7.1).
- Do not add chartjunk — no gridframes, 3-D, decorative rails, or a line drawn
  through a label.
- Do not draw an accent left rail on a container or a selection; render the
  selected name in the accent colour (§6.7).
- Do not put a state, verdict, role or kind in a pill, tag or badge; set it as
  plain text in its colour, led by its drawn mark (§6.2).
- Do not set chrome (the top bar, the tree, buttons, headings, prose) in the
  mono token; mono is for data, code, ids and key names (§3).
- Do not introduce a second accent colour or recolour the brand dot away from
  green; do not recolour the mark stroke (it is `currentColor`).
- Do not force horizontal scroll on a panel; wrap a table wider than its pane in
  `.dn-table-scroll` instead.
- Do not pin a figure to a fixed pixel width that overflows its pane; do not add
  pan/zoom.
- Do not run a structure (a card, a bracket) on `animation: …infinite` — animate
  values rather than structure.
