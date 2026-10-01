# zicato typeface study

> **Status: a dated design study (2026-06-07), kept as a record.** Its
> picker and its twelve finalist faces ship in Settings → Appearance; the
> default is Google Sans Mono. For the typography as it stands, read
> [DESIGN-LANGUAGE.md §3](../DESIGN-LANGUAGE.md#3-typography).

A standalone, self-contained, theme-adaptive study page (`index.html` — open it
in a browser) that explored and signed off the Console dashboard's typeface
system: three typographic **modes**, a finalist set of faces per mode, and the
design of the **typeface-picker control** itself. Preserved here for posterity;
this is the archived design record, **not** a TODO. The grouped-popover picker
and the finalized faces are now **live in the dashboard**.

The page carries the full 16-theme Console swatch picker (top-right) so every
card recolours live. Each card applies one font option to the **same
representative zicato content** — a section heading, body prose, a data table
with tabular figures, a config/labels block, and one big metric — so prose,
data, code, and headings are all exercised side by side.

## The three modes

Each mode is a different voice for the dashboard; each was narrowed to **3–4
finalized options** (the curated final set is **4 per mode → 12 faces total**).

| mode | character | what to look for |
| --- | --- | --- |
| **Technical** | all-mono · prose↔code axis | does a prose-mono read warm and text-like beside a code mono — do the two monos feel like one family? |
| **Editorial** | serif voice everywhere | does a serif stay legible for DATA (tabular figures, the big metric)? do mono numerals earn a break from the serif? |
| **Display** | heading character vs body calm | does a condensed display headline add enough to justify a second face vs unifying on one family? |

### Finalized options (the curated final set, per mode)

The composer/study seeds these as the operator's pre-liked finalists
(`INITIAL_LIKES` in `index.html`):

| mode | finalist faces (dashboard id — study grid label) |
| --- | --- |
| Technical | **Google Sans Mono** (`google-sans-mono` — T7; all-mono, Noto Sans Mono fallback); Source Sans 3 + Source Code Pro (`source-sans-3` — T9); **Inconsolata** (`inconsolata` — T12); Ubuntu + Ubuntu Mono (`ubuntu` — T14) |
| Editorial | Fraunces (`fraunces` — E5; old-style display serif); Bitter (`bitter` — E7; slab); **Literata** (`literata` — E8; reading-optimized); Domine (`domine` — E15; screen body serif) |
| Display | Archivo Narrow + Space Grotesk (`archivo-narrow` — the pairing in use before the study); Bricolage Grotesque (`bricolage-grotesque`); Hanken Grotesk (`hanken-grotesk`); Barlow Condensed + Space Grotesk (`barlow-condensed`) |

> The study renders the full candidate set (15 options per mode: the pairing
> in use before the study plus fourteen numbered alternatives) so the finalists can be judged against
> everything that was considered; the table above is the curated short list.
> **T4 = Roboto + Roboto Mono** is called out in the study as a notable
> technical contender (see the iterations below).

The positional labels above index this study's candidate grid and mean nothing
outside it. The dashboard identifies each option by the face it sets headings
in — `google-sans-mono`, `fraunces`, `archivo-narrow` and so on — matching the
form its colour-theme ids use; the table above is the bridge from a label in
this record to the face the dashboard names.

## The picker study (the control itself)

Below the card matrix, the page prototypes the **typeface-picker control** — what
the operator actually uses to switch modes/faces. Three variants, each rendering
3 finalists per mode with live micro-samples:

| variant | design | trade-off |
| --- | --- | --- |
| **A** | Segmented mode control + option chips | most scannable — the whole choice for the active mode is visible at once |
| **B** | **Grouped popover (Console `dt-cd` idiom) — WON** | mirrors the real colour-picker dropdown: one compact trigger, a popover grouped by mode with a face-name + micro-preview per row; smallest footprint, fits a toolbar |
| C | 3×3 matrix (rows = modes, cols = finalists) | the entire picker visible with no toggling; best for comparing all nine faces at once |

**Variant B (the grouped popover) was the final pick** — it reuses the existing
Console swatch-dropdown idiom (`dt-cd`), so the typeface picker reads as a
sibling of the theme picker. (`INITIAL_PICKER_LIKES` seeds it as the hearted
pick: *"Picker style: B · Grouped popover (Console dt-cd idiom)"*.)

## Iteration history (what shaped the final form)

- **A "like" mechanism was added** on both the typeface options (the card matrix)
  and the picker-style variants — a localStorage-backed selection (with a "♡
  liked only" toggle to filter the card matrix) so the operator could mark and
  export a finalized set.
- **T7 was corrected** from **Roboto Mono → Google Sans Mono** (Noto Sans Mono
  fallback) after feedback.
- **The picker was trimmed** from dozens of options down to a small curated
  finalist set per mode (the data-driven `PICKER_FINALISTS` short list), so the
  control offers a manageable choice rather than the full candidate grid.
- **The top-bar typeface control moved to Settings.** The picker lives **only in
  Settings → Appearance**, because typography is a settings-level preference
  rather than a primary toolbar control.

## Status

The **grouped-popover picker (variant B)** and the **12 finalized faces** ship
in the dashboard (Settings → Appearance). This directory is the archived design
record behind that work.
