# Proposer brief: the note writer

The brief is what the proposer is told before it proposes. It is part of
the evaluation contract, so editing it rolls the epoch: candidates
proposed under a different brief were answering a different question and
are not comparable to these.

Write it as an instruction to whoever is making the next edit, because
that is what it is.

## What this system under test is for

It drafts short internal notes. A good note states the thing, attributes
any claim it makes to a source, and ends with a one-line summary. A note
that buries all three in a paragraph of throat-clearing is a bad note
even when every fact in it is right.

## What to change

`system_under_test/__init__.py` holds one mutation point, `style_rules`:
a semicolon-separated list of style rules the writer follows. That list
is the whole surface. Add a rule, remove one, or reword one.

## What not to change

Do not edit the predicates, the board, or this brief. They are how the
change is measured, and a proposer that edits its own grader has stopped
measuring anything.

## What is already known to be wrong

The seeded policy carries three rules that each suppress something the
board grades. Removing any one of them should turn one board entry from
fail to pass and leave every other entry where it is. Say which
entry you expect to move before the round runs — a prediction made
afterwards costs nothing and proves nothing.
