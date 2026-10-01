# zicato — presentation deck

A Console–styled deck introducing zicato — *a self-improving harness for any
system you can measure*: the problem (systems **drift** and you can't tell if a
change helped), why it's hard, how it composes known-good selection theory, the
**novel advantage** (goldfive's custom judges turn agent behaviour into a shaped
drift **loss**), and a tour of the feature set. Multi-agent systems are the
founding and primary use case, so they carry the worked examples throughout.

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

Three slides do not match the shipped system and need revising:

- **Slide 8** labels the gauntlet as a default and shows `swiss`,
  `single_elim` and `double_elim` beside it. Racing is the one default
  structure, and the other three run only under the
  `experimental.tournament_structures` opt-in
  ([`SELECTION.md`](../design/SELECTION.md)).
- **Slide 9** shows a skill-composed default proposer and a custom
  `agent.py` agent. The shipped proposer is a Foe proposal runtime
  declared by the workspace's `proposer` block, and a proposer directory
  that carries an `agent.py` is refused
  ([`PROPOSER.md`](../design/PROPOSER.md)).
- **Slide 11** advertises 14 themes and a tournament builder with a chat
  copilot. The console ships 16 themes and no builder.

## Sources

`slides/slide-NN.svg` are the **only** source (1280×720, self-contained: JetBrains
Mono [SIL OFL] and FreeMono [GPL] embedded as base64 `@font-face`, so rendering
never depends on the host's installed fonts). `index.html`, `zicato-deck.pdf` and
`contact-sheet.png` are all **derived**. Rebuild all three with
`python3 docs/presentation/build.py` (needs headless `google-chrome`/`chromium`
on `PATH` and `pypdf`), **in the same commit as the slide edit**. Nothing checks
the exports against the slides, so an export rebuilt in a later commit leaves
the derived files out of step with the slides until that commit.
