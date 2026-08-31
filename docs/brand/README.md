# zicato — brand assets

> One continuous stroke: a golden-spiral scroll unwinds, is plucked (the note),
> and rings out as a decaying sparkline into a bridge tick.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="zicato-lockup-dark.svg">
  <img alt="zicato" src="zicato-lockup-light.svg" width="420">
</picture>

## The mark

The canonical mark is a **single continuous line** that tells the whole story of
the project in one gesture:

1. **The scroll** — a golden (logarithmic) spiral, the same `r = a·e^{bθ}` curve
   you find in a violin scroll, with `b = ln(φ)/(π/2)` so each quarter-turn grows
   by the golden ratio φ. The spiral unwinds and exits tangent-horizontal at the
   baseline.
2. **The string** — the stroke runs taut to the right…
3. **The pluck** — …then dips into a sharp downward notch. The single green
   accent dot sits at the pluck vertex: *the plucked note* (pizzicato → zicato).
4. **The ring-out** — the note rings out as a **damped-sine sparkline** (constant
   wavelength, decaying amplitude) — the loss curve settling generation over
   generation — and lands on a **bridge tick**.

The geometry is generated rather than drawn by hand. The spiral is sampled at 72 points
along `θ ∈ [0, 8.6]`, rotated so its outer end leaves horizontal, and translated
so that end meets the string at `(104, 80)`. The sparkline and bridge tick are
fixed quadratic/line segments. The accent dot marks the pluck vertex `(170, 102)`.

References for the framing: the violin scroll and the golden logarithmic spiral;
pizzicato (a *plucked* string); the damped sinusoid as a settling-loss sparkline;
and Tufte's data-ink ethic — every pixel of the mark is the line itself.

### Stroke weight & accent-dot size

The line-art mark is drawn with **`stroke-width: 5.0`** (round caps and joins),
and the green plucked-note accent dot has **radius `5.5`** in the mark's
coordinate space — the same `viewBox="71 35 229 75"` the lockup uses. At that
weight the stroke reads as kin to the `zicato` wordmark beside it, and the dot
stays above one pixel wherever the full spiral is used. A hairline stroke near
`2.4` with a `3.2` dot reads visibly thinner than the wordmark and collapses to
sub-pixel at small sizes. `zicato-mark-mono.svg` carries the same `5.0` and `5.5`
geometry with the dot filled in ink; it is the single-color print asset.

## Color tokens — and the theme-adaptive rule

The mark is **theme-adaptive by construction**. It carries no hard-coded ink
color; instead it inherits two values from its host:

| Token | Source | Default | Purpose |
|---|---|---|---|
| **Ink / foreground** | `currentColor` | host text color | the continuous stroke + bridge tick |
| **Accent** | `var(--zicato-accent)` | `#2FA46A` (light), `#3FB87A` (dark) | the single plucked-note dot — the *only* non-foreground color |

- Every adaptive asset strokes the path with `stroke="currentColor"`, so it
  renders **black-on-light, white-on-dark automatically** — it simply follows the
  surrounding text color.
- The accent dot fills with `var(--zicato-accent, #2FA46A)`. A host can override
  `--zicato-accent` (e.g. bump to `#3FB87A` on a dark ground for contrast); with
  no override it still renders green by default.

That is the whole rule: **ink = `currentColor`, accent = one CSS custom
property.** No second accent, no recoloring the stroke.

### Proof

The lockup on a light ground and on a dark ground — the stroke is dark on light
and light on dark, the accent stays green:

![light ground → dark stroke, green dot](proof-light.png)

![dark ground → light stroke, green dot](proof-dark.png)

(Rasterized with `cairosvg` from the **fixed-color** lockup variants —
`zicato-lockup-light.svg` (dark ink `#15181C`, accent `#2FA46A`) and
`zicato-lockup-dark.svg` (light ink `#EDEFEA`, accent `#3FB87A`). The adaptive
`zicato-lockup.svg` itself can't be rasterized directly here: `cairosvg` does not
resolve `currentColor` / `var(--zicato-accent)`, so the proofs are baked from the
fixed-color twins, which carry the identical `5.0` stroke + `5.5` dot geometry.)

## Assets

| File | What | Color model |
|---|---|---|
| `zicato-mark.svg` | icon only, transparent | `currentColor` + `var(--zicato-accent)` — adaptive |
| `zicato-mark-mono.svg` | icon, fully monochrome (accent = ink) | `currentColor` only — print / single-color |
| `zicato-lockup.svg` | mark + `zıcato` wordmark, horizontal | `currentColor` + `var(--zicato-accent)` — adaptive |
| `zicato-lockup-light.svg` | fixed-color lockup, dark ink `#15181C` | for hosts that can't supply `currentColor` |
| `zicato-lockup-dark.svg` | fixed-color lockup, light ink `#EDEFEA` | the dark half of a `<picture>` |
| `zicato-tile.svg` | rounded-square app tile, dark `#0E1116` ground | the **full mark** at app-icon scale (180px / large) |
| `zicato-favicon.svg` | **tab favicon** — a bold `z` + green plucked-note on the dark tile | legible at 16px (the full mark muddies that small) |
| `wordmark.svg` | `zıcato` wordmark alone | `currentColor` + `var(--zicato-accent)` — adaptive |
| `favicon-16.png` / `favicon-32.png` | rasterized tab favicons | from `zicato-favicon.svg` |
| `apple-touch-icon-180.png` | iOS home-screen icon | from `zicato-tile.svg` (full mark) |
| `favicon.ico` | multi-res icon (16/32/48) | from `zicato-favicon.svg` |

> **Favicon vs. tile.** The full golden-spiral mark is glorious at the lockup /
> 180px, but collapses into noise at 16px. So the *tab favicon* is a simplified
> `z` + the green plucked-note (`zicato-favicon.svg`); the *full mark* tile stays
> for the 180px apple-touch icon and any large app-icon use. Different mark by
> size — standard favicon practice.

### Size rules — which mark at which size

The full spiral is a fine-detail asset. Below ~24px the spiral whorl and the
sparkline ripples collapse into mush, so it is **not** the right mark at small
sizes. The rule:

| Size band | Mark | Accent dot |
|---|---|---|
| **≥ 24px** | the **full spiral** (`zicato-mark.svg` / the lockup mark), stroke `5.0` | dot **kept**, r `5.5` (stays above one pixel) |
| **< ~24px** | the **z + plucked-note favicon** (`zicato-favicon.svg`) — a bold `z` that stays legible small | dot **kept** (it reads fine at 16px) |
| full spiral *forced* ≤ 24px (rare) | **drop the accent dot + thicken the stroke** — emit a no-dot heavier-stroke `zicato-mark-small.svg` only if an actual usage needs it | dot **dropped** |

In practice the small-size case is handled by swapping to the favicon `z` form
rather than by shrinking the spiral. The dashboard top bar sits above the
threshold and so keeps the full spiral: its inline mark renders at 26px
(`.dt-brand-mark { height: 26px }`), theme-adaptive, with a `currentColor`
stroke and a `var(--zicato-accent)` dot and no tile background. No
`zicato-mark-small.svg` exists, because nothing forces the *full spiral* below
24px; the favicon swap covers every small render. The favicon keeps its accent
dot at every size, since it stays legible at 16px; only a full spiral forced
below the threshold would drop it.

### The two green dots and where each sits

The lockup carries **two** green dots, and each is positioned by a different
rule:

- **The mark dot** (cx `170`, cy `102`, r `5.5`) sits at the pluck vertex of the
  spiral. Its position is fixed geometry.
- **The wordmark dot** (cx `358.936`, cy `52`, r `4.2` in the lockup; cx
  `50.977` in `wordmark.svg`) is the dot of the dotless **ı** (U+0131). Its `cx`
  is calibrated to the centre of the ı stem, measured from a high-zoom raster of
  the outlined `DejaVu Sans Mono` glyphs. Those two values are the stem-correct
  ones. Any other `cx` slides the dot off the stem, so neither value moves.

### The wordmark

The wordmark is `zıcato` set in a monospace stack
(`ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`) with a **dotless ı**
(U+0131) — the green accent circle *is* the i's dot, tying the wordmark back to
the plucked note.

> **TODO: a font-independent wordmark.** `wordmark.svg` is text-based, so it
> depends on a monospace font being present on the host. There is no
> `wordmark-outlined.svg` with the text converted to paths, because producing one
> requires a text-to-path tool (`inkscape` or `picosvg`) that this repository does
> not depend on. Remove this note once such a file exists; generate it with
> `inkscape --export-type=svg --export-text-to-path wordmark.svg`.

## Usage

### In a web page (adaptive)

Inline the SVG into the DOM (do **not** use `<img>` — an external image can't
inherit `currentColor`), then let it follow the host's text color and define the
accent:

```css
:root        { --zicato-accent: #2FA46A; }
.dark-theme  { --zicato-accent: #3FB87A; }   /* brighter on dark grounds */
.brand-mark  { color: inherit; }             /* stroke follows currentColor */
```

### In a GitHub README (light/dark)

GitHub strips CSS, so use the fixed-color variants with `<picture>`:

```html
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/brand/zicato-lockup-dark.svg">
  <img alt="zicato" src="docs/brand/zicato-lockup-light.svg" width="420">
</picture>
```

## Clear space & minimum sizes

- **Clear space:** keep a margin of at least the spiral's diameter (~24 mark
  units) on all sides; never crowd the bridge tick or the wordmark.
- **Minimum size:** the **full spiral** reads down to **~24 px**; below that swap
  to the **favicon `z`** (`zicato-favicon.svg`), which reads down to **16 px**.
  The lockup holds to **~120 px** wide before the wordmark gets fragile. See
  *Size rules* above.
- **Accent dot:** the mark dot (r `5.5`) and the favicon dot are kept at every
  size they ship at — they are the note. The *only* case that drops the dot is a
  full spiral *forced* below ~24 px (which the favicon swap normally avoids).

## Do / don't

**Do**
- Let the stroke inherit `currentColor` (black-on-light, white-on-dark).
- Keep the accent as the single green dot; override only `--zicato-accent`.
- Keep the mark **one continuous stroke** from scroll to bridge.

**Don't**
- Don't recolor the stroke arbitrarily, or apply a gradient to it.
- Don't add a second accent color, or recolor the dot away from green.
- Don't break, re-segment, or redraw the line — it is generated geometry.
- Don't stretch non-uniformly or rotate the mark.
