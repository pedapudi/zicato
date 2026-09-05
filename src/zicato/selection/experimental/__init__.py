"""Tournament structures an operator must opt into.

``single_elim``, ``double_elim`` and ``swiss`` live here. Each pairs
challengers against each other, so a candidate's fate depends on its draw;
the second life a losers' bracket buys is what ``replicates`` already
buys, and Swiss pairing is racing without the escalating board slice. None
has a measured case at zicato's field size of two to four candidates under
an expensive, noisy evaluator. The registry
(:mod:`zicato.selection.registry`) resolves them only when the contract
sets ``experimental.tournament_structures`` to ``true``; the default
structure choice is ``gauntlet`` or ``racing``.
"""
