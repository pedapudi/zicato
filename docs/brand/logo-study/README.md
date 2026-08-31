# zicato logo / branding study

A standalone study page (`index.html` — open it in a browser) that compared
options for the brand mark and wordmark. This is a historical record of how the
values in `docs/brand/` were chosen.

The page carries the console's full sixteen-theme swatch picker in its top right
corner, so every mark and figure recolours live, and it parameterizes the real
`docs/brand/*.svg` geometry to compare options across a ladder of sizes.

## The choices it settled, now applied to `docs/brand/`

1. **The wordmark and lockup dot positions stand**: `wordmark.svg` cx `50.977`
   and `zicato-lockup.svg` cx `358.936`. Bracketing showed these are the
   ı-stem-correct values; 46.4, 36 and 344 read too far left, and the geometric
   stem centres 52.4 and 360.4 too far right.
2. **Mark stroke weight 2.4 → 5.0** (the thin spiral went sub-pixel small).
3. **Accent dot r 3.2 → 5.5**, size-aware: kept (and gently grown) above ~24px,
   **dropped below ~24px** with the stroke thickened (a dot is noise that small).
4. **16 / 24px icons:** the full **spiral** drops its dot + thickens; the
   **z+note favicon keeps its accent dot** at every size (the bold-z reads small).
5. **Lockup — proposed:** stroke 5.0 · dot r 5.5 · wordmark dot kept at 358.936.

These values are in `docs/brand/*.svg` and in the dashboard mark; the dashboard
top bar renders the full spiral at 26px, above the legibility floor.

> Two further studies from the same effort — one on typefaces and one on
> tournament and evolution visualizations — were served standalone during
> development and are not in this repository.
