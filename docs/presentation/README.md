# zicato — presentation deck

A Console–styled deck introducing zicato: the problem (multi-agent systems
**drift** and you can't tell if a change helped), why it's hard, how it composes
known-good selection theory, the **novel advantage** (goldfive's custom judges
turn agent behaviour into a shaped drift **loss**), and a tour of the feature set.

## View
- Open **`index.html`** in a browser — self-contained (every slide inlined as
  vector SVG, JetBrains Mono embedded as `@font-face`; zero `fetch`, so it opens
  straight off `file://`). Keys: `←`/`→` navigate · `g` grid · `f` fullscreen ·
  `1`–`9` jump · `#n` deep-link.
- **`zicato-deck.pdf`** — 12-page vector export.
- **`contact-sheet.png`** — all twelve at a glance.

## Slides
1. Title · 2. The problem · 3. Why it's hard · 4. The champion/challenger loop
(**epoch ⊃ round ⊃ generation** — a round mints a field of generations) ·
5. Standing on known-good techniques · 6. The novel advantage — goldfive's judges
→ a shaped loss · 7. The gate (protected incumbent) · 8. Tournament structures ·
9. The modular proposer · 10. Overfitting defenses · 11. Operate it (Console) ·
12. Closing.

## Sources
`slides/slide-NN.svg` are self-contained vector sources (1280×720, JetBrains Mono
[SIL OFL] embedded). `index.html` inlines all twelve under one shared `@font-face`.
