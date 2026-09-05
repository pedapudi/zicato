# The concept atlas

`index.html` is a self-contained explainer for readers new to zicato: what it
does, the vocabulary, one round end to end, what the console draws, and the
command surface. Open the file in a browser; it has no build step and loads
nothing but its typefaces.

It centres on an interactive map of 88 concepts and 144 relationships, drawn as
the five parts of a round. A guided walk steps through the 28 that carry the
loop, in the order the loop moves through them.

## What it is written against

Every concept was read out of the module that implements it rather than out of
a glossary, so the definitions here and `docs/design/VOCABULARY.md` should
agree. Where they disagree, the source decides and both should be corrected.

The page borrows the console's design language: the six colour roles, the
sixteen themes, the twelve typefaces, and the rule that improvement and
regression colours mean direction rather than identity. The per-part colours on
the map are a separate categorical encoding, checked for lightness, chroma,
colour-vision separation and contrast against both grounds.

## Editing it

The file is hand-authored and is the source; nothing generates it. Two things
to know before changing the map:

- Chip widths are measured in the live typeface at layout time, because the
  typeface picker swaps between a monospace, a serif and a condensed grotesque.
  A character-width estimate is wrong for at least two of them.
- The map lays out for the width it is given, so it renders unscaled at any
  pane size. Changing the fixed viewBox back would reintroduce the scaling that
  made its labels illegible.
