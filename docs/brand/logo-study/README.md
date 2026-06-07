# zicato logo / branding study

A standalone, self-contained study page (`index.html` — open it in a browser)
that explored and signed off the brand mark + wordmark revision. Preserved here
for posterity; it is the bake-off behind the branding changes shipped in the
`docs/brand/` assets.

The page carries the full 16-theme Console swatch picker (top-right) so every
mark/figure recolours live, and parameterizes the real `docs/brand/*.svg`
geometry to compare options across a size ladder.

## What it decided (the ✓ SELECTIONS, now applied to `docs/brand/`)

1. **Wordmark + lockup dot — KEEP current** (no change): `wordmark.svg` cx
   `50.977` / `zicato-lockup.svg` cx `358.936`. Bracketing in the study proved
   the current placement is the ı-stem-correct one (46.4 / 36 / 344 read too far
   left; the geometric stem-centre 52.4 / 360.4 too far right).
2. **Mark stroke weight 2.4 → 5.0** (the thin spiral went sub-pixel small).
3. **Accent dot r 3.2 → 5.5**, size-aware: kept (and gently grown) above ~24px,
   **dropped below ~24px** with the stroke thickened (a dot is noise that small).
4. **16 / 24px icons:** the full **spiral** drops its dot + thickens; the
   **z+note favicon keeps its accent dot** at every size (the bold-z reads small).
5. **Lockup — proposed:** stroke 5.0 · dot r 5.5 · wordmark dot kept at 358.936.

These were applied across `docs/brand/*.svg` + the dashboard mark; the dashboard
top-bar renders the full spiral at 26px (above the legibility floor).

> Sibling explorations from the same effort (not committed): a typeface study and
> a tournament-/evolution-visualization study, both served standalone during
> development.
