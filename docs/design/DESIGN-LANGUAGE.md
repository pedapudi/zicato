# zicato — the design language

This is the **canonical, reproducible design-language reference** for zicato.
Everything here is grounded in the live implementation — concrete token names,
hex values, font stacks, class names and SVG snippets, each traceable to a
source file. The goal is that someone could build **any** new zicato surface — a
new dashboard view, an execution-timeline figure, a CLI TUI skin — purely from
this document and reproduce the look exactly.

The system as shipped lives in **Variant T** (the converged default dashboard,
"Console IV: dense observatory"). The token sheet is
[`console4.css`](../../src/zicato/dashboard/static/css/variants/T/console4.css);
the figure language is
[`svg.js`](../../src/zicato/dashboard/static/js/variants/T/svg.js); the chrome
and component vocabulary are in `shell.js`, `ui.js` and `views/**`. Where this
document and any source disagree, **the code is authoritative** — re-grep and
verify before treating a value here as current.

This document is the *language*. [CONSOLE-DESIGN-LANGUAGE.md](CONSOLE-DESIGN-LANGUAGE.md)
is the dashboard's *specific application* of it (the Console view IA, the
figure-to-purpose mapping, the live-vs-completed conventions); it links back
here for the shared tokens, typography and figure grammar.

> **Design-inspiration note.** Citations to public design authorities (Tufte,
> the Gogh terminal palettes, the chess/tournament metaphor) belong **in the
> docs** and appear here. They are deliberately **not** cited in source code.

---

## 1. Ethos

zicato's surface is a **dense observatory for a power user** — an instrument,
not a consumer report. Five sentences:

1. **Tufte data-ink / line-art.** Every data figure carries maximum data per
   stroke and drops decoration — no gridlines for their own sake, no 3-D, no
   chart frames, no chartjunk. Just the band, the dot, the rule, the label.
   (Edward Tufte, *The Visual Display of Quantitative Information*.)
2. **Monospace-forward.** The default voice is all-monospace — a terminal/console
   aesthetic where data, labels and code read on a fixed advance grid.
3. **A single green accent on a calm ground.** The brand carries exactly **one**
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
`#variant-root[data-variant="T"][data-t-theme="<id>"]`, swapped by the
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
| `--v2-good` | **improvement / promotion / survival** | a dot *below* the reference rule, a survivor `↑`, a crowned gate, a promoted verdict — *always* the better outcome |
| `--v2-good-soft` | tinted fill behind a good state | |
| `--v2-bad` | **regression / rejection / a cut** | a dot *above* the rule, a cut competitor `✕`, a rejected verdict — *always* the worse outcome |
| `--v2-bad-soft` | tinted fill behind a bad state | |
| `--v2-caution` | caution / timeout (the budget-exceeded `⏱`) | |
| `--v2-accent` | **the one structural / interactive highlight** | the champion spine, the emphasised current line, an interactive focus — used sparingly so it stays meaningful |
| `--v2-flat` | unchanged / neutral-flat | a slope that neither improved nor regressed |
| `--v2-cell-empty` | an empty heatmap cell | |

**The cardinal rule:** `good` and `bad` are earned by **direction, never by
identity**. A challenger is not red because it is a challenger; it is red only
when it regressed or was cut. An unscored / in-flight candidate is *neutral*
(pending → `--v2-accent`), never `bad` — this is the "Class B" mistake the code
guards against (`.dn-pill.dn-pending`, `.ezn-edge-neutral` in `console4.css`).

The single brand accent is a **separate** token from the structural `--v2-accent`:

| token | value | source |
| --- | --- | --- |
| `--zicato-accent` | `#2FA46A` (light grounds) / `#3FB87A` (dark grounds) | `console4.css` L1259–L1270 |

The mark strokes with `currentColor` (so it flips dark/light with the theme) and
fills the plucked-note dot with `var(--zicato-accent)`. See §8 and
[docs/brand/README.md](../brand/README.md).

### 2.2 The sixteen themes

`monokai` is the default. The colour picker is a **swatch dropdown**
(`.dt-cd-trigger` / `.dt-cd-list`); each option shows a 6-swatch preview strip
(*ground · surface · ink · improve · regress · accent*) plus the theme name. The
JS preview tuples live in `ui.js` `COLOR_THEMES` as
`[paper, panel, ink, good, bad, accent]`; the authoritative per-theme palettes
are the `--v2-*` sets in `console4.css`.

Every value below is lifted verbatim from `console4.css`.

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
`COLOR_THEMES` tuples in `ui.js` L47–L64 / the per-theme block in `console4.css`
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
gogh-co.github.io/Gogh, mapped onto the role contract by one principled rule:
`paper ← background`, `panel ← background nudged toward the foreground`,
`ink ← bright-white/host`, `ink-soft ← foreground`, `good ← green`, `bad ← red`,
`caution ← yellow`, `accent ← cyan` (or the palette's blue where its cyan is a
low-contrast neutral — Belafonte, Paper). A few accents/cautions were nudged for
contrast; see the comments in `console4.css` §"Gogh palettes".

### 2.3 Derived colours

The heatmap ramp is built at draw time from the theme tokens — a cool→hot mix
`color-mix(in srgb, var(--v2-hm-hot) <pct>%, var(--v2-hm-cool))`, where
`--v2-hm-cool` defaults to `--v2-accent` and `--v2-hm-hot` to `--v2-bad`
(`console4.css` L745, `svg.heatmap`). Tinted backgrounds and shadows likewise
use `color-mix(in srgb, var(--v2-…) <pct>%, transparent)` so they stay
theme-correct in light and dark. **Never** introduce a raw hex into a mark or
component — derive it from a token.

### 2.4 Contrast guidance

- Body ink on ground targets WCAG AA (4.5:1); secondary/faint inks step down for
  hierarchy but stay legible. The Gogh nudges (e.g. Paper keys `ink` off
  near-black, not its low-contrast palette white) exist precisely to hold this.
- The good/bad/accent signal must read on *every* ground — verify a new figure in
  both a light theme (`paper`) and a dark one (`monokai`) before shipping.
- Focus rings are a solid `2px` `--v2-accent` outline with a small offset (§9).

---

## 3. Typography

Typography is a **separate axis** from colour. A typeface-mode picker swaps the
family tokens via `[data-t-type]` on the root; the default is **Technical**.

### 3.1 The three modes

| id | voice | body | data / mono | headings | display |
| --- | --- | --- | --- | --- | --- |
| `editorial` | typeset, literary reading serif | Source Serif 4 | Source Serif 4 | Source Serif 4 | Source Serif 4 |
| `technical` **(default)** | console technical — all-mono mixture | iA Writer Mono (prose) | JetBrains Mono (code) | iA Writer Mono | iA Writer Mono |
| `display` | punchy headline | Space Grotesk (geometric) | JetBrains Mono | Archivo Narrow (condensed) | Space Grotesk |

**Technical is a mono *mixture* along a prose↔code axis:** a warm humanist prose
mono (**iA Writer Mono**) for body / headings / publication, and a crisp code
mono (**JetBrains Mono**) for data / labels / axis text / code. It reads as
prose-mono everywhere except data and code.

### 3.2 The token map

The CSS resolves the modes through intermediate `--n-font-*` families and exposes
the two tokens the marks read — `--v2-sans` (body) and `--v2-mono` (all data,
labels, axis text, code) — plus `--n-font-head` (headings) and `--n-font-paper`
(publication body). From `console4.css` L432–L476:

```css
#variant-root[data-variant="T"] {
  --n-font-base:       "Open Sans", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  --n-font-mono-real:  "JetBrains Mono", ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace;
  --n-font-prose-mono: "iA Writer Mono", "iA Writer Mono S", ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --n-font-serif:      "Source Serif 4", Georgia, "Times New Roman", serif;
  --n-font-display:    "Archivo Narrow", "Space Grotesk", "Open Sans", system-ui, sans-serif;
  --n-font-geo:        "Space Grotesk", "Segoe UI", system-ui, sans-serif;

  /* the brand wordmark pins to a FIXED mono, independent of the user's choice */
  --v2-brand-mono:     "JetBrains Mono", ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, monospace;

  /* Technical (DEFAULT): prose mono body, code mono data */
  --v2-sans:      var(--n-font-prose-mono);
  --v2-mono:      var(--n-font-mono-real);
  --n-font-head:  var(--n-font-prose-mono);
  --n-font-paper: var(--n-font-prose-mono);
}
```

The picker re-points the four exposed tokens per mode:

| token | `editorial` | `technical` | `display` | which surface |
| --- | --- | --- | --- | --- |
| `--v2-sans` | serif | prose-mono | geo (Space Grotesk) | body text |
| `--v2-mono` | serif | code-mono (JetBrains) | code-mono | data / labels / axis / code |
| `--n-font-head` | serif | prose-mono | display (Archivo Narrow) | `.dn-h1`, big numerals |
| `--n-font-paper` | serif | prose-mono | geo | the ACM-style publication body |

### 3.3 Self-hosted vs loaded

- **Technical's two monos are self-hosted woff2** under
  `src/zicato/dashboard/static/fonts/` — `iAWriterMonoS-Regular/Bold.woff2` and
  `JetBrainsMono-Regular/Bold.woff2` — declared with `@font-face` +
  `font-display: swap` (`console4.css` L39–L58). **The default mode never touches
  a CDN**: a blocked network never affects the page.
- **Editorial's serif + Display's families load from Google Fonts** — the only
  external dependency — injected in `app_T.js` `ensureFonts()` with
  `display=swap`: `Open Sans`, `Source Serif 4` (optical-size axis `8..60`),
  `Space Grotesk`, `Archivo Narrow`. Every stack lists a system fallback so a
  slow/blocked font never breaks layout.

### 3.4 Type scale and weights

Base size `calc(13px * var(--dt-font-scale, 1))` (`--dt-font-scale: 1.04`), all
sizing tuned by the page-scale pill (§4). Representative sizes from `console4.css`:

| element | class | size / weight |
| --- | --- | --- |
| page title | `.dn-h1` | 19px / 600 |
| section heading | `.dn-h2` | 13px / 600 |
| subhead (eyebrow) | `.dn-subhead` | 11px / uppercase / `0.07em` tracking |
| tile value (big number) | `.dn-tile-value` | 20px mono, `tabular-nums` |
| tile key | `.dn-tile-key` | 10px / uppercase / `0.06em` |
| lede / prose | `.dn-lede` | 12.5px, line-height 1.45, `max-width: 78ch` |
| publication title | `.dn-paper-title` | 28px / 700, `--n-font-paper` |
| SVG axis / labels | (mark classes) | 9–11px `var(--v2-mono)` |

Numerics use `font-variant-numeric: tabular-nums` everywhere they appear in a
column or animate, so digits don't jitter.

### 3.5 The dotless-ı wordmark rule

The wordmark is **`zıcato`** — set in `--v2-brand-mono` (a *fixed* mono,
independent of the user's typeface choice so the mark never reflows) with a
**dotless ı** (U+0131); the green accent circle **is** the i's dot, tying the
wordmark back to the plucked note. In the dashboard it is an inline SVG (so the
dot can be pinned geometrically over the stem and the letters can inherit
`currentColor` while the dot takes `--zicato-accent`) — see `shell.js`
`brandWordmark()` and [docs/brand/README.md](../brand/README.md).

---

## 4. Layout & spacing

### 4.1 The spacing baseline (cozy — the one permanent rhythm)

The density picker was removed; **cozy** is the single permanent spacing rhythm,
baked unconditionally onto the root (`console4.css` L489–L502). The page-scale
pill is the sizing control now.

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
| `--dt-font-scale` | `1.04` | global font-size multiplier |

Radii: panels `4px`, cards/buttons `5px`, pills `8–11px` (full-round), hovercard
`6px`. Hairlines are always `1px solid var(--v2-rule)` (or `--v2-rule-soft` for a
fainter inner rule). SVG strokes use `vector-effect: non-scaling-stroke` so a
hairline stays a hairline under the page-zoom.

### 4.2 Grid & containment

- **Fluid detail pane.** `.dn-viewhost { width:100%; max-width: min(100%, 2200px) }`
  — fills the available width; only a generous cap guards prose line-length on
  ultra-wide monitors. Bigger diagrams on bigger screens.
- **Containment guarantee.** No panel ever scrolls horizontally or lets a child
  escape. Figures are fit-to-width (`width:100%` + a `viewBox`); genuinely-wide
  tables carry their *own* contained overflow via `.dn-table-scroll`, never the
  panel. (`console4.css` L630–L638, L937–L941.)
- **Body split.** `.dt-body` is a 3-track grid: `var(--dt-rail) · 0 · minmax(0,1fr)`
  — a sticky tree sidebar, a zero-width draggable resize handle (`.dt-rail-handle`,
  hit-area widened by negative margins), and the reflowing detail pane.

### 4.3 Top-bar anatomy (`.dt-topbar`)

Sticky, blurred, hairline-bottomed (`console4.css` L1274; assembled in
`shell.js` L509). Left → right:

1. **`.dt-back`** — the `↑ up` control. Navigates *up the selection hierarchy*
   (candidate → generations → epoch → environment), not browser-back. Disabled
   state `.dt-back-off`.
2. **`.dt-brand`** — the inline-SVG mark (`.dt-brand-mark`) + the inline-SVG
   wordmark (`.dt-brand-name`, `zıcato`) + a `.dt-brand-variant` tag reading
   `console`.
3. **`.dt-crumbs`** — breadcrumb trail (mono, faint), `.dt-crumb` links +
   `.dt-crumb-sep`.
4. `.dt-topbar-spacer` (flex spacer).
5. **`.dt-nav-build`** — a `⚙ settings` entry (opens the Settings surface, which
   homes the tournament builder).
6. **Colour swatch dropdown** (`.dt-cd`, §6) and the **typeface switch**
   (`.dt-type-switch`, 3 inline buttons).
7. **`.dt-scale-pill`** — the page-scale slider (§4.4).
8. **`.dt-status`** — the status pill (§4.5).

> **Note — there is no cmd-K command palette in the shipped code.** Despite a
> historical "cmd-K palette" note, no palette is implemented in
> `js/variants/T/**` as of this writing. Navigation is via the tree sidebar
> (`tree.js`), the breadcrumbs, and the `↑ up` control. If you add a palette,
> dock it from the top bar and theme it with the dropdown tokens (`.dt-cd-list`
> bg `--v2-panel`, border `--v2-rule`, options on `--v2-rule-soft` hover).

### 4.4 The page-scale pill (`.dt-scale-pill`)

The sole sizing control: a native range input (`.dt-scale-range`,
≈70 %–150 % in 5 % steps, default 100 %) + a `%` readout + a `⟲` reset button.
It applies page-wide via `zoom` on the app root (`shell.applyScale`), which
**reflows** (not a transform) so the page re-wraps at the scaled size and never
clips. Persisted under `zicato.T.scale`. The slider thumb is `--v2-accent` with a
`--v2-paper` ring; focus ring `2px --v2-accent`.

### 4.5 The status pill (`.dt-status`)

A connection dot + a connection word, plus a **RUN badge** that lights up for any
active tournament structure (`shell.js` L489):

```html
<span class="dt-status dt-connected">       <!-- or .dt-running -->
  <span class="dt-status-dot"></span>        <!-- flat→good (connected)→caution (running) -->
  <span class="dt-status-text">connected</span>
  <span class="dt-run-badge" aria-live="polite">   <!-- shown only when .dt-running -->
    <span class="dt-run-pulse" aria-hidden="true"></span>
    <span class="dt-run-label">racing · rung 0</span>
    <span class="dt-run-count">3 in flight</span>
  </span>
</span>
```

The pulse dot (`.dt-run-pulse`) is the **only** keyframe animation in the chrome
— a 1.6s expanding box-shadow ring (`@keyframes dt-run-pulse`), disabled under
`prefers-reduced-motion`. The `LIVE` pill (`.dt-live-pill`) and the structure
pill (`.dt-structure-pill`, `structure: Racing · 3 rungs`) ride in the view
header, not the top bar.

---

## 5. Line-art figure language

This is the distinctive part — the conventions that let you draw a **new** figure
(an execution timeline, a Gantt, a flow) in-language. Every figure is built in
[`svg.js`](../../src/zicato/dashboard/static/js/variants/T/svg.js) with a tiny
dependency-free helper layer (`svgEl`, `scale`, `extent`, `fmt`).

### 5.1 Stroke & ink conventions

These hold for **every** mark (drawn from the `.dn-*` / `.ezn-*` rules in
`console4.css`):

| convention | concrete value | where |
| --- | --- | --- |
| data line stroke | `stroke-width: 1.2–1.4`, `fill:none`, `vector-effect: non-scaling-stroke` | `.dn-spark-line` 1.4, `.dn-pslope-line` 1.2 |
| the champion spine (the one emphasis) | `stroke: var(--v2-accent); stroke-width: 2.0–2.4` | `.dn-spine-line` 2.4, `.ezn-edge-spine` 2.0, `.dn-roundtl-spineline` 2.2 |
| reference / baseline rule | `stroke: var(--v2-ink-faint); stroke-width:1; stroke-dasharray: 3 3` | `.dn-ref-rule` |
| a pending / racing edge | `stroke: var(--v2-accent); stroke-dasharray: 4 3` (never red) | `.ezn-edge-neutral` |
| good / bad mark | `fill`/`stroke: var(--v2-good)` / `var(--v2-bad)` | `.dn-dot.dn-good`, `.dn-glyph-fail` |
| node dot radius | `r: 2.2–4.5` (champion bigger than challenger) | `bumps` 4.5/3.5, sparkline endDot 2.2 |
| band fill | soft token mix, ~18–20% | `color-mix(in srgb, var(--v2-accent) 18%, transparent)` (`.dn-funnel-band`) |
| ribbon fill-opacity | `0.32` idle → `0.55` hover | `.dn-sankey-ribbon` |
| line caps/joins (chrome glyphs) | `stroke-linecap:"round"`, `stroke-linejoin:"round"` | brand mark, `structureGlyphSvg` |
| status glyph aspect | a **fixed 1:1 `viewBox`** overlay so a stretched cell never shears it | `outcomeGlyph`, `sparkbar` verdict |

**Fit-to-width is mandatory:** every figure SVG carries `width:"100%"`, an
explicit `viewBox`, a `preserveAspectRatio`, and `role:"img"`. There is **no
fixed pixel width that exceeds the pane, and no pan/zoom**. A figure that must
stretch its bars uses `preserveAspectRatio:"none"` but then puts any glyph that
must stay round into a separate 1:1 overlay (see `sparkbar`).

**Shared semantic glyphs** (one source of truth, `svg.js` L21):

```js
export const CROWN = { current: '♛', former: '♔' };
```

`↑` survives · `✕` cut · `○` pending · `♛` current champion · `♔` former
champion · `⏱` timeout · `✓` pass. The reference rule means **good = below /
lower loss, bad = above / higher loss**.

### 5.2 Worked snippet — the sparkline

The word-sized trend mark. Note: `width:"100%"`, the `viewBox`, the
pen-up/pen-down path for gaps, and the end-dot coloured good/bad by direction
(`svg.js` L98):

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
top, the current item in `--v2-accent` + `♛`, former items dim ink + `♔`
(`svg.js` L2019):

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
    // label: "id ♛" current / "id ♔" former
    g.appendChild(hov(svgEl('rect', {
      x: x0, y: cy - rowH * 0.32, width: Math.max(4, x1 - x0), height: rowH * 0.64, rx: 3,
      class: 'dn-reigngantt-bar' + (current ? ' dn-reigngantt-bar-current' : ' dn-reigngantt-bar-former'),
    }), `${r.id} ${current ? CROWN.current + ' current' : CROWN.former + ' former'} · held r${r.fromRound}…`));
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

### 5.4 Worked snippet — a structure glyph (`structureGlyphSvg`)

The 24×24 line-art icons for tournament structures — pure `currentColor`,
`stroke-width: 1.6`, round caps/joins, optional faded cut-arms via `fill-opacity`
(`builder/model.js` L203):

```js
const GLYPH = {
  gauntlet:    { dots: [{cx:7,cy:12,r:2.4},{cx:17,cy:12,r:2.4}], paths: ['M9.8,12 H14.2'] },
  swiss:       { paths: ['M5,7 H17', 'M5,12 H14', 'M5,17 H19'] },
  single_elim: { paths: ['M5,7 H11 V12', 'M5,17 H11 V12', 'M11,12 H19'] },
  double_elim: { paths: ['M5,6 H11 V11', 'M5,11 H11', 'M11,11 H19', 'M5,16 H11 V11', 'M5,20 H17'] },
  racing:      { dots: [ {cx:7,cy:7,r:1.8}, {cx:12,cy:7,r:1.8}, {cx:17,cy:7,r:1.8,o:0.32},
                         {cx:9.5,cy:12,r:1.8}, {cx:14.5,cy:12,r:1.8,o:0.32}, {cx:12,cy:17,r:1.8} ] },
};
export function structureGlyphSvg(structure) {
  const g = GLYPH[structure] || GLYPH.gauntlet;
  // stroked paths: <g fill="none" stroke="currentColor" stroke-width="1.6"
  //                   stroke-linecap="round" stroke-linejoin="round">
  // filled dots:   <g fill="currentColor" stroke="none">  (a faded cut → fill-opacity)
  return svgEl('svg', { class: 'dn-bld-cardglyph', width: 24, height: 24,
    viewBox: '0 0 24 24', role: 'img', 'aria-hidden': 'true', focusable: 'false' }, kids);
}
```

A compact-glyph wordbank for the structures also exists for inline labels
(`builder/model.js` L46): `gauntlet ⚔ · single_elim ◣ · double_elim ◳ ·
swiss ⇄ · racing ⥥`.

### 5.5 The figure catalogue

The full inventory of figures (purpose-mapped) is documented in
[CONSOLE-DESIGN-LANGUAGE.md §4.1](CONSOLE-DESIGN-LANGUAGE.md). The language-level
point: each is a small, single-purpose, fit-to-width SVG honouring §5.1. Build
new figures from the same vocabulary — bands narrow at a cut, lanes converge at a
match, dots sit relative to a reference rule, the spine is the one accent line.

### 5.6 The hovercard (hover-for-detail)

Hover-for-detail is first-class. `hovercard.js` mounts a **singleton** card
*inside* `#variant-root`, so it inherits the live per-theme tokens
(`--v2-panel` bg, `--v2-ink` text, `--v2-rule` border, mono face). Every mark
calls `hov(node, tip)`. Crucially it is a **transient overlay outside the
digest-gated render** (§7) — show/hide only toggles `.dn-hovercard-on`, so it can
never trigger a repaint loop. It is `pointer-events:none` (never steals hover),
viewport-flipped/clamped, keyboard-accessible (`role="tooltip"` via
`aria-describedby`), and collapses its fade under `prefers-reduced-motion`
(`console4.css` L1102–L1133).

---

## 6. Components

All scoped under `#variant-root[data-variant="T"]`; all token-only.

### 6.1 Buttons & links

| element | class | look |
| --- | --- | --- |
| primary action / themed link-button | `a.dn-linkbtn` | mono, `1px solid var(--v2-accent)`, transparent → on hover fills `--v2-accent` with `--v2-paper` text |
| up / back | `.dt-back` | mono, `1px solid var(--v2-rule)`, hover → accent fill |
| icon button (reset) | `.dt-scale-reset` | 17px square, `⟲`, hover → accent fill |

```html
<a class="dn-linkbtn" href="#/e/epoch-3">open transcript →</a>
```

Do: keep buttons mono and outline-first, filling the accent only on hover/active.
Don't: leave a link unstyled (the historical "unstyled open-transcript" bug).

### 6.2 Pills & badges

```html
<span class="dn-pill dn-promoted">♛ promoted</span>
<span class="dn-pill dn-rejected">rejected</span>
<span class="dn-pill dn-pending">racing</span>   <!-- accent, NOT red -->
```

`.dn-pill` is mono, `2px 8px`, `border-radius:10px`, `1px solid`. Variants:
`.dn-promoted` (good), `.dn-rejected` (bad), `.dn-deferred` (caution),
`.dn-baseline` (rule), `.dn-pending` (**accent** — an in-flight candidate is
neutral, never red), `.dn-live` (good + soft fill). Smaller chips: `.dn-chip`
(8px-radius, lowercase) with `.dn-chip-live` / `-open` / `-closed`.

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

The mark-level hovercard is §5.6. For richer board-status popovers, the same card
hosts a titled body: `.dn-hc-body` > `.dn-hc-title` + `.dn-hc-row` +
`.dn-hc-link`. The lifecycle DAG's `?` info badge (`.ezn-dag-info`) and the gate
node (`.ezn-gate-node { cursor: help }`) open the full how-to in the hovercard
rather than crowding the figure.

### 6.6 Tabs / section rails

The Settings surface (`.dn-settings`) is a section **rail + host**:
`a.dn-set-railitem` (active → `.dn-set-railitem-active`, accent glyph
`.dn-set-railglyph`). Disclosure sections use `.dn-brief` (a `<details>` with a
rotating `.chev`). The epoch publication "tabs" are panels, not a tab strip.

### 6.7 The resizable chat-copilot pane (`.dn-bld-chat`)

A full-height docked column — a 3-row flex column (header · scrolling log ·
composer) inside the builder grid (`console4.css` L2308):

```html
<aside class="dn-bld-chat">
  <div class="dn-bld-chat-handle"></div>          <!-- col-resize drag handle -->
  <div class="dn-bld-chat-head">
    <button class="dn-bld-chat-collapse">‹</button>
    <span class="dn-bld-chat-title">copilot</span>
    <span class="dn-bld-chat-model">…model…</span>
  </div>
  <div class="dn-bld-chat-log">
    <div class="dn-bld-bubble dn-bld-bubble-user">…</div>   <!-- accent fill, right -->
    <div class="dn-bld-bubble dn-bld-bubble-asst">…</div>   <!-- rule-soft, left -->
    <div class="dn-bld-chat-typing"><span class="dn-bld-chat-dot"></span>…</div>
  </div>
  <div class="dn-bld-chat-composer">
    <textarea class="dn-bld-chat-input"></textarea>
    <button class="dn-bld-chat-send">send</button>
  </div>
</aside>
```

Resizable via `.dn-bld-chat-handle` (a `7px` `col-resize` strip that highlights
accent on hover/focus); collapsible (`.dn-bld-chat-collapsed` swaps to a vertical
`.dn-bld-chat-strip`). User bubbles fill `--v2-accent` (`--v2-paper` text) and
align right; assistant bubbles sit on `--v2-rule-soft` and align left. The typing
dots animate but disable under `prefers-reduced-motion`. Below `1080px` the
docked frame collapses to a normal scrolling single column.

### 6.8 The swatch / typeface pickers

- **Colour** — `.dt-cd` swatch dropdown: a `.dt-cd-trigger` (current name + a
  6-chip `.dt-swatch-strip` preview + a `.dt-cd-caret`) opens a
  `.dt-cd-list` listbox (`role` listbox; options `.dt-cd-option` with
  `aria-selected`; selected name in `--v2-accent`).
- **Typeface** — `.dt-type-switch`: three inline `.dt-type-btn` (no dropdown,
  only three options); active button fills `--v2-ink-soft` with `--v2-paper`.

Both persist to `localStorage` (`zicato.T.theme`, `zicato.T.typeface`) and drive
the same `applyTheme` / `applyTypeface` the Settings → Appearance grid uses.

---

## 7. Motion & render discipline

### 7.1 Digest-gating — the no-flash rule

**Never rebuild the DOM on a no-op SSE heartbeat.** This is a hard rule. The bug
class it prevents — the **flashing / refresh bug** — is a steady heartbeat
re-dispatch wiping and rebuilding a panel every tick, flashing the screen, losing
scroll position, and destroying hovercard/focus state.

The mechanism is `gatedSwap(host, digest, build)` in
[`ui.js` L19](../../src/zicato/dashboard/static/js/variants/T/ui.js):

```js
export function gatedSwap(host, digest, build) {
  if (!host) return false;
  const next = String(digest);
  // a view computes `digest` over ONLY its structural/content data —
  // timestamps and heartbeat fields are EXCLUDED.
  if (host.getAttribute('data-t-digest') === next && host.firstChild) return false; // ← no-op
  clearChildren(host);
  const built = build();
  for (const n of (Array.isArray(built) ? built : [built])) { if (n) host.appendChild(n); }
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
- **The hovercard is outside the gated render** (§5.6) — toggling a class never
  repaints a figure.

### 7.2 Transitions & reduced motion

- Motion is CSS `transition` (theme swap, hovers, the page-scale reflow), **never
  `animation: …infinite`** for structure. Live state animates *values /
  positions* (GPU-friendly `transform` / `opacity` / `width`); digest-gating
  governs *structure*.
- The **only** keyframe animations are the status-pill pulse (`dt-run-pulse`),
  the in-flight-count pulse, and the copilot typing dots — all gated behind
  `@media (prefers-reduced-motion: reduce)` to instant.
- Theme/colour transitions are `background 0.18s ease, color 0.18s ease` on the
  root; hovercard fade is `120ms`, also reduced-motion-aware.

---

## 8. Iconography

- **Structure glyphs** — 24×24 line-art, `currentColor`, `stroke-width:1.6`,
  round caps (§5.4). One per tournament structure.
- **The brand mark** — a single continuous stroke (golden-spiral scroll → string
  → pluck → damped-sine sparkline → bridge tick), `stroke:currentColor`,
  `stroke-width:2.4`, round caps; the one accent dot at the pluck vertex fills
  `var(--zicato-accent)`. The canonical asset:

```svg
<svg xmlns="http://www.w3.org/2000/svg" viewBox="71 35 229 75" role="img" aria-label="zicato">
  <g fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">
    <path d="M94,52.5 … L104,80 L150,80 L170,102 L190,80 Q206,56 222,80 Q236,102 250,80 Q261,68 272,80 L292,80"/>
    <path d="M292,66 L292,94"/>                           <!-- the bridge tick -->
  </g>
  <circle cx="170" cy="102" r="3.2" fill="var(--zicato-accent, #2FA46A)"/>
</svg>
```

  (full path in [docs/brand/zicato-mark.svg](../brand/zicato-mark.svg) /
  `shell.js` `_MARK_PATH`).
- **Favicon vs mark.** The full golden-spiral mark is glorious at lockup/180px
  but muddies at 16px, so the **tab favicon** is a simplified `z` + green
  plucked-note (`docs/brand/zicato-favicon.svg`); the full mark stays for the
  180px apple-touch tile. Different mark by size — standard favicon practice.

See [docs/brand/README.md](../brand/README.md) for the asset table and usage.

---

## 9. Accessibility

- **Contrast.** Body ink targets WCAG AA (4.5:1) on every ground; the Gogh
  ink/accent nudges exist to hold it (§2.4). Verify a new surface in both a light
  and a dark theme.
- **Focus rings.** A consistent solid `2px solid var(--v2-accent)` outline with a
  small `outline-offset` on every interactive control (`:focus-visible`):
  `.dt-cd-trigger`, `.dt-scale-range`, `.dt-rail-handle`, `.dn-set-*`, and every
  focusable SVG mark (`.dn-*-lane:focus-visible`, `.dn-reigngantt-row:focus-visible`).
- **Skip link.** `index.html` ships `<a class="skip-link" href="#main-content">`
  (visually hidden until focused, then pinned top-left — `style.css` L50).
- **`prefers-reduced-motion: reduce`** — disables every pulse/typing animation
  and the hovercard fade (multiple `@media` blocks in `console4.css`).
- **`prefers-color-scheme`** — the brand assets adapt automatically: the mark
  strokes `currentColor` (dark-on-light / light-on-dark) and READMEs use
  `<picture>` with light/dark sources. The dashboard's theme is an explicit
  user choice (sixteen themes), but the brand never needs recolouring.
- **Roles & labels.** Figures are `role="img"` with an `aria-label`; the
  hovercard is `role="tooltip"` wired via `aria-describedby`; the status badge is
  `aria-live="polite"`; interactive marks are keyboard-activatable
  (Enter/Space → click, `svg.js` `clickable`).

---

## 10. Worked example — building a new surface in the language

**A harmonograf execution timeline** — a per-run, step-by-step Gantt showing a
run's lifecycle phases over wall-clock. (Conceptual illustration only — *do not*
implement it here; this section proves the doc is reproducible.)

**1 — Frame & colour roles.** The view is a `.dn-section` panel
(`background:var(--v2-panel)`, `1px solid var(--v2-rule)`, radius 4px) on the
`--v2-paper` ground. The timeline's "current phase" carries the **one** accent
(`--v2-accent`); completed phases that succeeded read `--v2-good`, failed phases
`--v2-bad`, a pending/in-flight phase reads `--v2-accent` dashed (never red),
skipped phases `--v2-flat`. Axis ticks and gridlines read `--v2-ink-faint` /
`--v2-rule-soft`.

**2 — Typeface roles.** The panel heading takes `--n-font-head`; phase labels and
the time axis take `--v2-mono` with `font-variant-numeric: tabular-nums` (so
durations align). Nothing in the figure needs the body sans.

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
  // current phase gets a ♛-style marker; the running edge is dashed accent
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

## 11. Do / Don't

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

**Don't**
- Don't rebuild the DOM on a no-op heartbeat (the flashing bug).
- Don't add chartjunk — no gridframes, 3-D, decorative rails, or a line through a
  label (the cut-name strikethrough bug).
- Don't introduce a second accent colour or recolour the brand dot away from
  green; don't recolour the mark stroke (it is `currentColor`).
- Don't force horizontal scroll on a panel; wrap a genuinely-wide table in
  `.dn-table-scroll` instead.
- Don't pin a figure to a fixed pixel width that overflows its pane; don't add
  pan/zoom.
- Don't run a structure (a card, a bracket) on `animation: …infinite` — animate
  values, not structure.
