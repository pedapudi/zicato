# Converge by removing defect tokens

The target's only mutation point is the `style_rules` token list in
`agent/policy.py`. Every remaining defect token costs one drift frame
per run and (for the known tokens) fails exactly one board predicate.

- Propose exactly ONE `replace` patch per round, targeting
  `style_rules`, with the FULL new token list as `new_content`.
- Remove one defect token per round; never introduce a new one.
- Never add `fabricate-metrics` — an unverified metric claim fails the
  no-fabrication predicate and regresses the board.
