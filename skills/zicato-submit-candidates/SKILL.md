---
name: zicato-submit-candidates
description: Write candidates by hand and queue them as the next evolve round with `zicato proposer checkout`, `zicato proposer submit` and `zicato proposer queue` — copy the champion, edit inside the declared mutation points, write the hypothesis, submit one or more copies, read the JSON, and find out what the round did with them. Use when an operator or an agent wants to test its own change, race several of its own variants, or improve the seed step by step under the normal gate. The hypothesis text reaches the proposer as written, so it must never name holdout entries or their results.
---

# zicato submit-candidates — queue a round of your own candidates

`zicato evolve` normally asks the proposer for every candidate. Three
commands let you, or an agent that works for you, write candidates by hand,
put them into the next round, and read what the round did:

1. `zicato proposer checkout` copies the epoch's current champion to a
   directory.
2. You edit the copy inside the declared mutation points and fill in the
   hypothesis.
3. `zicato proposer submit` runs the proposer's own checks on the copies,
   and queues them as one round.
4. The next `evolve` round runs them in its first slots. The board,
   replicates, gate, holdout confirmation and settlement are the same as for
   proposer candidates.
5. `zicato proposer queue` reports what the round did with them.

None of the three commands calls a model, takes the workspace lock, or
changes the contract. All can run while `zicato evolve` runs on the same
workspace. Each prints one JSON object. A success exits 0; a refusal exits 1,
with the findings in the JSON.

Always invoke via `.venv/bin/zicato`. The operating rules live in the
repo-root `AGENTS.md`.

## Guardrails (read first)

- **The round spends budget, the commands do not.** `checkout` and `submit`
  are free. The `evolve` round that runs your candidates calls models. Get
  the user's explicit go-ahead before you start it (AGENTS.md rule 1, and
  `skills/zicato-evolve`).
- **Keep holdout data out of the hypothesis.** The workspace holds holdout
  results and raw per-entry outcomes that the proposer never sees. Your
  `core_idea` and `why` reach the proposer as written: as a sibling in the
  same round and in its experiment memory later. Never name an entry id,
  quote task text, or describe a holdout result in them. As an agent, write
  from `evidence/` and the source only.
- **Say what you could see.** Pass `--holdout-access no` only when the author
  did not read holdout results. Pass `yes` when they did. The value is
  recorded with every candidate.
- **Many tries are a hill-climb by hand.** Every candidate that gets a
  generation stays in the journal, rejected ones included. A candidate that
  the round rejects before the tournament stays in the control log, and
  `zicato proposer queue` lists it under `rejected_candidates`. Queue a
  candidate only when you have a reason for it. A variant that wins only by
  chance after many tries does not win again on a new measurement.

## Walkthrough

```sh
Z=.venv/bin/zicato

# 1. One copy for each candidate. The copy is the current champion.
$Z proposer checkout --workspace .zicato --into /tmp/a
$Z proposer checkout --workspace .zicato --into /tmp/b

# 2. Edit /tmp/a/source/ and /tmp/b/source/ inside the declared mutation
#    points only. The checkout output lists the point ids.

# 3. Fill in /tmp/a/hypothesis.json and /tmp/b/hypothesis.json.

# 4. Queue both as one round. Add --exclusive to keep proposer candidates out.
$Z proposer submit --from /tmp/a --from /tmp/b --author alice --holdout-access no

# 5. Run the round (LLM — gated: get the user's go-ahead first).
$Z evolve --workspace .zicato --rounds 1

# 6. Read what the round did, with the queue_id that submit printed.
$Z proposer queue --workspace .zicato --queue-id <queue_id>
```

If an `evolve` loop is already running, skip step 5: the next round that
starts takes the queued round.

## What `checkout` writes

```
/tmp/a/
  source/            the champion's source tree: edit here, inside mutation points only
  hypothesis.json    the template to fill in
  evidence/          what the proposer may see: brief.md, mutations.json, proposer_inputs.jsonl (when present)
  checkout.json      where the copy came from: do not edit
```

`checkout.json` records the workspace, the epoch and `based_on`, the
generation that you copied. `submit` reads it to compare your edit with the
right tree and to refuse a copy from another workspace or an old epoch.

The `checkout` output (real, from a test workspace):

```json
{
  "based_on": "v0",
  "epoch": "2026-10-13_swiss_epoch",
  "evidence": ["brief.md"],
  "hypothesis": "/tmp/a/hypothesis.json",
  "mutation_points": ["greeting"],
  "ok": true,
  "source": "/tmp/a/source",
  "workspace": "/repo/.zicato"
}
```

Rules for the edit:

- Change only the content of declared mutation points (`# zicato:mutable`
  spans, `:file` and `:code` regions). The marker comments stay.
- Do not add, delete or rename files, and do not change file modes. The patch
  schema cannot express those changes.
- Remove editor backup files and other new files from `source/` before you
  submit.
- `zicato inspect mutations --show preview` lists the points and their
  current text (`skills/zicato-mutation-audit`).

## The hypothesis

`hypothesis.json` is the `hypothesis` object of the proposer's response
schema, unchanged, and `submit` parses it with the proposer's own parser. A
filled example:

```json
{
  "core_idea": "Greet with more warmth",
  "modulating": ["greeting"],
  "why": "Users read the short greeting as cold",
  "expected_pass_rate_delta": "+0 to +5 points",
  "expected_metric_movements": [
    {"metric_name": "drift:goal_drift", "direction": "decrease", "magnitude": "small"}
  ],
  "risks": "A longer greeting adds tokens"
}
```

| Field | Rule |
|---|---|
| `core_idea` | One sentence: the change. Required. It is also the rationale of every patch. |
| `modulating` | The mutation point ids that the change addresses. At least one; each must exist. |
| `why` | The observation behind the change. Required. |
| `expected_pass_rate_delta` | Free text, for example `"+0 to +5 points"`. Required. |
| `expected_metric_movements` | At least one prediction. `metric_name` is `<namespace>:<name>` (`drift:<kind>`, `judge:<name>`, `cost:tokens_spent`); `direction` is `decrease`, `increase`, `neutral`, `decrease_or_neutral` or `increase_or_neutral`; `magnitude` is `small`, `medium` or `large`. |
| `risks` | Optional free text. |

No other keys are allowed. The full reference is
`docs/design/EPOCHS-AND-JOURNALING.md` §3.1, and
`skills/zicato-design-experiment` explains how to write a falsifiable
prediction.

## `submit`

| Option | Use |
|---|---|
| `--from DIR` | A `checkout` directory. Repeat it to put several candidates in one round. Required. |
| `--author NAME` | Who wrote the candidates: a person or an agent. Required, and it must not be empty. |
| `--exclusive` | Run only these candidates in their round: no proposal episodes. Without it, the proposer fills the other slots. |
| `--holdout-access yes\|no` | Whether the author could read holdout results. Recorded with each candidate. |
| `--workspace PATH` | Optional. Must be the workspace the copies came from; any other is an error. |

For each copy, `submit` runs the proposer's checks in this order:

1. It reads the edit back as patches. An edit outside a mutation point fails
   with the message a proposer gets.
2. It parses the hypothesis.
3. It validates the patches: structure, application, the contract's static
   checks, and the load probe (`adapter.load` on the edited tree).

If any copy fails, nothing is queued. A round holds at most one candidate per
field slot (`slots` in the output): a gauntlet takes one copy, a racing field
of 4 takes up to 4. The field size is a contract value, so a round cannot be
wider than the field.

A successful submit (real output; exit 0):

```json
{
  "candidates": [
    {"based_on": "v0", "findings": [], "from": "/tmp/a", "notes": [], "patches": 1},
    {"based_on": "v0", "findings": [], "from": "/tmp/b", "notes": [], "patches": 1}
  ],
  "epoch": "2026-10-13_swiss_epoch",
  "exclusive": true,
  "ok": true,
  "queue_file": "/repo/.zicato/runtime/control/candidate/20261013161708247358957-9a2866",
  "queue_id": "20261013161708247358957-9a2866",
  "slots": 2,
  "workspace": "/repo/.zicato"
}
```

A refusal for one copy (real output; exit 1). Each copy has its own
`findings`:

```json
{
  "candidates": [
    {"based_on": "v0", "findings": [], "from": "/tmp/a", "notes": [], "patches": 1},
    {
      "based_on": "v0",
      "findings": [
        "agent.py: working source differs from the reconstructed patch source; restore edits outside the declared mutation units, file set, and executable state"
      ],
      "from": "/tmp/b",
      "notes": [],
      "patches": 0
    }
  ],
  "ok": false
}
```

A refusal of the whole command has a top-level `findings` list instead, for
example `{"findings": ["The same copy is named more than once."], "ok": false}`.
`notes` name checks that could not run (a static-check tool that is not
installed, for example). They do not reject a copy.

## What the round does

- **Slots.** The oldest queued round fills the first slots, one candidate per
  slot, in `--from` order. The proposer fills the rest unless the round is
  exclusive. One queued round runs per evolve round; later ones wait.
- **The same measurement.** Your candidates race the champion and any
  proposer candidates on the same board with the same gate. A crowned
  candidate goes through the holdout confirmation and spends one holdout
  query, like any other.
- **No duplicates check between your own candidates.** The field-diversity
  soft-reject never compares two of your candidates. A proposer candidate is
  still compared with yours.
- **A promotion before the round.** If the champion changed after your
  checkout, each candidate runs as written against the new champion. Where
  your copy and the new champion differ outside your edit, your candidate
  takes back the promoted change. The record lists those points as
  `reverted_points`. To build on the new champion instead, check out again.
- **No fallback.** A candidate that fails in the round is rejected. The
  proposer does not take its slot.
- **Things that can fail only in the round.** The epoch rolled, your version
  is now identical to the champion, or the post-apply check fails against a
  new champion. The round records the reason (below).

## Reading the result

`submit` does not wait for the round. Ask `zicato proposer queue` instead.
It reads the queue, the control log and the experiments; it takes no lock
and calls no model, so poll it as often as you need.

```sh
$Z proposer queue --workspace .zicato --queue-id <queue_id>   # one round
$Z proposer queue --workspace .zicato                         # every round, in submit order
```

| `state` | Meaning |
|---|---|
| `queued` | Waiting for an evolve round. |
| `running` | Minted into a round that has not settled yet. |
| `settled` | The round ran. Each entry of `generations` carries its `decision` and `rejection_reason`. |
| `rejected` | No candidate got a generation, and the item left the queue. `reason` says why. |

In any state, `rejected_candidates` lists the candidates that the round could
not enter, with their index in your `--from` order, the slot they would have
taken, and the findings. A `settled` round can have some: the others ran.
`record_errors` at the top level lists experiment records that could not be
read; while it is not empty, a `queued` or `rejected` round may have run.

Before and after an exclusive round (real output from a test workspace):

```json
{"ok": true, "record_errors": [],
 "round": {"author": "alice", "candidates": 1, "consumed_at": null,
  "epoch": "2026-10-13_alpha", "exclusive": true, "generations": [],
  "queue_id": "20261013193510560973556-26ecb5", "reason": null,
  "rejected_candidates": [], "state": "queued", "submitted_at": "2026-10-13T19:35:10Z"},
 "workspace": "/repo/.zicato"}
```

```json
{"ok": true, "record_errors": [],
 "round": {"author": "alice", "candidates": 1,
  "consumed_at": "2026-10-13T19:35:12Z", "epoch": "2026-10-13_alpha", "exclusive": true,
  "generations": [{"based_on": "v0", "decision": "promoted", "generation_id": "v1",
                   "rejection_reason": "", "reverted_points": []}],
  "queue_id": "20261013193510560973556-26ecb5",
  "reason": "minted as v1; round 0 settled", "rejected_candidates": [],
  "state": "settled", "submitted_at": "2026-10-13T19:35:10Z"},
 "workspace": "/repo/.zicato"}
```

An unknown `--queue-id` prints `{"ok": false, "findings": [...]}` and exits 1.

The `reason` values:

| Reason | Meaning |
|---|---|
| `minted as v5, v6; round 3 settled` | The candidates ran. Read each generation's `decision`. |
| `rejected before the tournament: v5: …` | No candidate could enter the round; each slot's finding follows. |
| `operator round for epoch '…' cannot run in epoch '…'; …` | The epoch rolled after the submit. Check out again. |
| `operator round has 3 candidates, but the round has 2 slot(s)` | The field shrank after the submit. Submit fewer copies. |
| `unreadable operator round: …` | The queue file was damaged or edited by hand. |
| `operator round already minted as …` | A restarted round found the candidates already settled. |

The records that `queue` reads, for deeper digging:

| Where | What it holds |
|---|---|
| `.zicato/runtime/control/candidate/<queue_id>` | A round still in the queue. |
| `.zicato/runtime/control_log/*_candidate_<queue_id>.json` | The audit record of a consumed round: the full body and the `reason`. |
| `.zicato/epochs/<epoch>/generations/vN/experiment.json` → `operator` | The provenance of each candidate: `author`, `based_on`, `queue_id`, `submitted_at`, `holdout_access`, `reverted_points`. The `outcome` holds the decision. |
| The epoch journal (in the epoch analysis, `skills/zicato-analyze-epoch`) | Each operator candidate shows `**source**: operator <author>, based on <gen>`. |
| The dashboard's proposing tracker | Each slot's status, with the rejection reason. See `skills/zicato-watch-dashboard`. |

## Common refusals

| Finding | Fix |
|---|---|
| `working source differs from the reconstructed patch source` | You edited outside a mutation point, or added, deleted or renamed a file. Restore it from a fresh checkout. |
| `the copy is unchanged` | Edit a declared mutation point first. |
| `hypothesis.modulating: id '…' does not match any known mutation point` | Use an id from the checkout output. |
| `schema violation at …` | A required hypothesis field is missing or has the wrong type. |
| `unknown drift kind '…'` / `unknown board judge '…'` | Use a drift kind or a judge that the board declares. |
| `load probe: adapter.load raised …` | The edited tree does not import. Fix the code, then submit again. |
| `… cannot hold 3 candidates` | The field has fewer slots. Submit fewer copies, or queue another round later. |
| `The workspace is now in epoch …` | The copies are stale. Check out again. |
| `No epoch under … yet` (checkout) | Run `zicato epoch new` first. |

## Reference

- [docs/design/CLI.md](../../docs/design/CLI.md) — the CLI reference.
- [docs/design/EPOCHS-AND-JOURNALING.md](../../docs/design/EPOCHS-AND-JOURNALING.md) §3.1 — the hypothesis schema and the `operator` record.
- `docs/dev-guide/05-proposer.md` §5.14a — how the queue, the slots and the record work; §5.8.5 — why operator text is outside the visibility envelope.
- `skills/zicato-design-experiment` — writing the hypothesis and its predictions.
- `skills/zicato-mutation-audit` — finding the mutation points you may edit.
- `skills/zicato-evolve` — running the round, and the live-run gate.
- `skills/zicato-step-loop` — the other single-stage commands.
