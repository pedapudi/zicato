# 03 — The Contract and Epochs

> **Covers:** the entire epoch/contract subsystem as it exists on this branch —
> the five contract components and the six canonical forms they reduce to, the
> per-component canonicalizer (`_canon_board` incl. judge/predicate source
> folding, `_canon_brief`, `_canon_scoring` + the recursive `scoring_to_canon`,
> `_canon_entrypoint`, `_canon_mutable_trees` and its bug-#10
> normalized-never-resolved story, `_canon_proposer` incl. skill hashing but
> NOT the runtime tool registry), the ONE source-hashing mechanism
> (`_canon_dotted_spec`/`spec_with_source_hash`), the omit-at-default discipline
> (`_SCORING_OMIT_AT_DEFAULT_FIELDS`), the field-enumerating serializer and its
> completeness guard, `EpochConfig`'s never-hashed additive fields
> (`noise_floor`, `preflight`), the epoch lifecycle (`new_epoch`/`close_epoch`),
> the auto-roll (`ensure_epoch_for_contract`), lineage semantics (the `promoted`
> tri-state), and record-format versioning + refuse-on-newer.
>
> **Prerequisites:** 01-orientation.md §"Workspace layout" (what an epoch /
> generation / round is on disk), 02-architecture.md §"The evolve round" (where
> the auto-roll sits), 04-evaluation-statistics.md §"The A/A noise floor" (what
> `noise_floor` measures and why it is not a contract input). The proposer half
> of the contract is 05-proposer.md §"`_canon_proposer` and skills"; the runtime
> half of the storage seam is 07-runtime-and-durability.md.
>
> **Invariants you must not break (each is expanded below):**
> 1. **The contract hash identifies the CONTRACT, not the checkout.**
>    `_canon_mutable_trees` NORMALIZES paths, it never `resolve()`s them — no
>    cwd, absolute path, hostname, or clock may fold into any contract-identity
>    input (§3.2.6; bug #10).
> 2. **Byte-identical-at-default.** A contract that does not opt into a knob
>    canonicalizes byte-for-byte identically to one that predates the knob, and
>    hashes identically. Every purely-additive default-off field is registered
>    in `_SCORING_OMIT_AT_DEFAULT_FIELDS` (§3.4).
> 3. **Serializer completeness.** Every `ScoringWeights` field (and every field
>    of every nested config dataclass) round-trips through `to_json`/`from_json`
>    and appears in the frozen snapshot. A dropped field silently rolls every
>    epoch on the next `evolve` (§3.5; issue #13).
> 4. **Edit-the-body-rolls.** A grading plugin's SOURCE folds into the hash
>    through the ONE source-hashing mechanism (`_canon_dotted_spec`) — editing
>    the resolved plugin body rolls the epoch, not only swapping the dotted
>    string (§3.3).
> 5. **Runtime measurements are never hashed.** `EpochConfig.noise_floor` and
>    `.preflight` (and everything on `RuntimeConfig`) are recorded
>    post-creation and never fold into `contract_hash`; writing them never rolls
>    the epoch (§3.6, §3.12).
> 6. **Legacy-never-rolls is `is None`, not `== ""`.** A `None` stored
>    `contract_hash` reads as always-matching; a corrupt/empty *real* hash must
>    roll rather than read as legacy (§3.8.4).
> 7. **Refuse-on-newer.** A canonical JSON record stamped with a
>    `format_version` this build cannot read is refused loudly, never silently
>    misread; an absent stamp reads as version 1 (§3.10).
> 8. **The lineage `promoted` tri-state.** An applied-but-unresolved in-flight
>    generation persists `promoted=null`, never `False` (`False` reads as a
>    rejected dead branch); the settle-time upsert resolves it (§3.9).

---

## 3.0 Map of the subsystem

| File | What lives there | Approx. size |
|---|---|---|
| `src/zicato/epoch/contract.py` | `ContractInputs`, every `_canon_*`, `compute_contract_hash` / `compute_component_hashes`, `resolve_contract_inputs`, `_SCORING_OMIT_AT_DEFAULT_FIELDS`, `scoring_to_canon`, `round_floats` | 776 lines |
| `src/zicato/core/scoring_config.py` | `ScoringWeights` + the nested config dataclasses (`OverfittingConfig`, `LadderConfig`, `ProposerQualityConfig`, `ExperimentMemoryConfig`), `to_json`/`from_json`, `recommended_scaffold_weights` | 896 lines |
| `src/zicato/core/epoch.py` | `EpochConfig` (the frozen contract record) and `Generation` (one lineage node) | 177 lines |
| `src/zicato/epoch/lifecycle.py` | `new_epoch`, `close_epoch` / `close_epoch_async`, `load_epoch` / `list_epochs`, `switch_epoch`, `set_epoch_goal` / `set_epoch_noise_floor` / `set_epoch_preflight`, `scoring_to_dict` | 769 lines |
| `src/zicato/evolve/epoching.py` | `ensure_epoch_for_contract` (the auto-roll), `_create_epoch_from_contract`, the per-component sub-hash bookkeeping | 349 lines |
| `src/zicato/epoch/lineage.py` | the cross-epoch DAG (`register_epoch`, `append_to_lineage`, `mark_closed`, `render_lineage_summary`) | 269 lines |
| `src/zicato/epoch/journal.py` | `journal.md` + `experiment.json`/patches persistence, `write_seed_experiment`, `RECORD_FORMAT_VERSION` stamping at write | 541 lines |
| `src/zicato/epoch/_storage.py` | `RECORD_FORMAT_VERSION`, `RecordFormatError`, `check_record_format` (refuse-on-newer), storage-key helpers | — |
| `src/zicato/epoch/contract_serde.py` | `dataclass_to_jsonable` / `jsonable_to_dataclass` — the field-enumerating serde both the frozen snapshot and the loader route through | — |
| `src/zicato/scoring/plugins.py` | `spec_with_source_hash` — the source-hash half of `_canon_dotted_spec` | — |
| `src/zicato/core/runtime.py` | `RuntimeConfig` — the runtime knobs that DELIBERATELY never roll the epoch (§3.12) | — |
| `src/zicato/runtime_factory.py` | `make_runtime_config` — parses the workspace-config `runtime` block into a `RuntimeConfig` | — |

The topology, per `evolve` invocation (the roll decision runs once, before any round):

```
evolve entry (evolve/loop.py)
 └─ ensure_epoch_for_contract (evolve/epoching.py)          # §3.8.3
     ├─ resolve_contract_inputs(workspace_root)  → ContractInputs   (contract.py)
     ├─ compute_contract_hash(inputs)            → live hash
     ├─ compute_component_hashes(inputs)         → per-component sub-hashes
     ├─ cur = current_epoch_id(workspace_root)
     └─ compare live hash vs load_epoch(cur).contract_hash:
          None (legacy)   → no roll  (§3.8.4)
          equal           → no roll
          drift + auto    → close_epoch_async(cur) → new_epoch(e{N})  (§3.8.1/2)
          drift + no-auto → raise, naming the changed component (§3.8.3)
```

`compute_contract_hash` reduces the contract to ONE sha256 by canonicalizing
six components independently, joining them with a NUL-delimited separator, and
hashing the join:

```
_canon_board(board_path)          ─┐
_canon_brief(brief_path)           │
_canon_scoring(scoring_path)       ├─ _SEP.join(...) ─→ sha256 hex
_canon_entrypoint(entrypoint)      │
_canon_mutable_trees(mutable_trees)│
_canon_proposer(proposer_path)    ─┘
```

> ⚠️ TRAP — the six canonical forms are joined with a specific separator that
> "cannot appear in any canonical component" — a NUL byte plus a marker word
> (`_SEP = "\x00--zicato-contract-component--\x00"`,
> `src/zicato/epoch/contract.py`). If you add a seventh component, append it to
> BOTH `compute_contract_hash`'s `components` list and
> `compute_component_hashes`'s dict, in the same order semantics, or the two
> disagree about what changed and the auto-roll message names the wrong
> component. Never reuse a component's bytes as a delimiter.

---

## 3.1 The five components — and why they are the contract

An epoch is the unit of **evaluation contract**. The module docstring names the
five things that make it up (`src/zicato/epoch/contract.py`):

1. **The board** — test inputs + expectations + judges (`board.jsonl`).
2. **The proposer brief** — operator steering text (`brief.md`).
3. **The scoring** — weights + gate thresholds (`scoring.json`).
4. **The registered inner-harness IDENTITY** — the `--adk` entrypoint string
   plus the sorted `--mutable-tree` paths.
5. **The proposer** — agent identity, tools, and the skill modules under a
   configured `proposers/<name>/` dir (or the built-in default when none is
   configured).

Five conceptual components, **six canonical forms**: harness identity (#4)
splits into `entrypoint` and `mutable_trees` because they canonicalize by
different rules (a verbatim string vs a sorted normalized path set) and because
`compute_component_hashes` reports them separately so the auto-roll message can
say precisely which moved.

The through-line: **a change to any component means generations on either side
of the change are no longer directly comparable, so the epoch must roll.** A
generation crowned under board A is not evidence about board B; a challenger
scored under margin 0.01 is not comparable to one scored under margin 0.05. The
contract hash is the mechanical detector for "the ground shifted."

> ⛔ NEVER add the inner-harness's *source content* to the contract. The
> docstring is explicit: "The inner harness's *source content* is deliberately
> NOT part of the contract — that is exactly what zicato mutates within an
> epoch." The contract fixes the *rules of comparison*; the source is the thing
> being optimized under those rules. Folding source into the hash would roll the
> epoch on every promotion — the loop would never accumulate a lineage.

`ContractInputs` (`src/zicato/epoch/contract.py`) is the frozen bundle the
hasher consumes — three live file paths plus the two harness-identity strings
plus the optional proposer dir:

```python
# src/zicato/epoch/contract.py — ContractInputs
    board_path: Path
    brief_path: Path
    scoring_path: Path
    entrypoint: str
    mutable_trees: tuple[str, ...]
    #: Location of the proposer dir (``proposers/<name>/``) frozen for
    #: the epoch, or ``None`` for the built-in default proposer. ``None``
    #: by default so existing construction sites keep working.
    proposer_path: Path | None = None
```

`resolve_contract_inputs(workspace_root)` builds it by reading the workspace's
`config.json`: `contract.board_path` / `brief_path` (legacy alias
`rubric_path`) / `scoring_path`, `adk_entrypoint`, `mutable_trees` (legacy alias
`source_roots`), and the optional `contract.proposer_path`. A relative
`proposer_path` is absolutized against the workspace's *parent* (the operator's
project root). A missing `config.json` raises `FileNotFoundError` telling the
operator to run `zicato register`.

> ⚠️ TRAP — the live contract files sit NEXT TO the `.zicato/` directory (the
> operator's project root), not inside it. `_default_contract_path` resolves
> `<workspace_root>.parent / filename` "so the operator's live copies are not
> confused with the per-epoch frozen copies under `epochs/{id}/`." The frozen
> copies are what a *created* epoch hashes; the live copies are what the *next*
> `evolve` re-hashes to detect drift. If you point a canonicalizer at the wrong
> one, every round will look like a contract change.

### 3.1.1 The on-disk epoch layout

`lifecycle.py`'s module docstring is the authority for what an epoch is on disk
(`src/zicato/epoch/lifecycle.py`):

```
{workspace_root}/
  current_epoch                # marker file, single line = epoch id
  lineage.json                 # cross-cutting DAG (see lineage.py)
  epochs/
    {epoch_id}/
      board.jsonl              # frozen board
      brief.md                 # frozen proposer brief
      scoring.json             # serialized ScoringWeights
      config.json              # EpochConfig serialized
      journal.md               # appended per experiment (see journal.py)
      analysis.md              # written at close (see analysis.py)
```

The three FROZEN contract files (`board.jsonl` / `brief.md` / `scoring.json`)
are the per-epoch copies the stored `contract_hash` was computed over. They are
deliberately distinct from the operator's LIVE editable copies next to
`.zicato/` (§3.1's trap): editing a frozen copy after the fact would desync the
stored hash from the file it claims to describe. The auto-roll also writes
`contract_components.json` next to `config.json` (the per-component sub-hashes,
§3.8.3) and, on a roll, a roll-seed marker recording where the new epoch's `v0`
seeds from. Epoch ids are `{YYYY-MM-DD}_{slug}`; a same-name-same-day collision
gets a numeric suffix (`_make_epoch_id`).

Generations live under each epoch (`generations/{id}/`, resolved by the storage
layout, not spelled in this docstring): a `snapshot/` source tree, an
`experiment.json` + `patches/{id}.json` per-generation record (§3.9.2), and the
`events.jsonl` / `loss.json` the tournament writes. The `current_generation`
marker names the promoted head — what a cross-epoch roll seeds the next epoch's
`v0` from (`_promoted_head_snapshot`).

> ⚠️ TRAP — the epoch directory is created with `mkdir(..., exist_ok=False)`
> (`new_epoch` step 3): re-creating an existing epoch id is a hard error, not an
> overwrite. `list_epochs` silently skips a directory under `epochs/` with no
> readable `config.json` (a presumed torn `epoch new` from a crash), but it
> LOUDLY refuses one whose `config.json` carries a future `format_version`
> (§3.10) — the record is intact, so the operator must know why it won't load.

---

## 3.2 The canonicalizer, component by component

Every `_canon_*` function answers the same question: what edits are *spurious*
(whitespace, reordering, float-format noise, path spelling) and must leave the
hash fixed, versus what is *semantic* and must move it? Get the boundary wrong
in either direction and you get one of two failures: a **false roll** (a
cosmetic edit orphans the lineage and discards the warm start) or a **missed
roll** (an incomparable board is silently compared against the old one). Both
are severe; §3.5's guard tests exist to pin the boundary.

### 3.2.1 board — `_canon_board`

Semantic content only, id-sorted (`src/zicato/epoch/contract.py::_canon_board`):

- the board is loaded through `zicato.board.jsonl.load_board`, so the canonical
  form is the *validated parsed shape*, not raw bytes;
- entries are **sorted by id** and each serialized to a sorted-key JSON dict, so
  reordering rows or reformatting the JSONL leaves the hash unchanged; editing an
  entry's input / expectation / weight changes it;
- the **board-level metadata** — the configured `judges`, the board-level
  `disable_drift` kind list, and the `judge_only` flag — is folded in via
  `_canon_board_meta`, prepended as a NUL-marked line (`\x00board-meta\x00…`) so
  it participates in the hash without colliding with an entry row;
- a **missing** board file logs a warning and hashes as the empty string, so a
  board-less workspace still hashes deterministically.

`_canon_board_meta` reads the board-level object *defensively* — it prefers a
`zicato.board.jsonl.load_board_meta` callable if the board API exposes one,
otherwise scans the raw JSONL for a line carrying `judges` / `disable_drift` /
`judge_only` but no entry `id`. (The board API exposes `load_board_with_meta`,
not `load_board_meta`, so in practice the raw-scan branch is the live path; the
loader branch is kept for the day that API lands.) `judge_only` is folded in
**only when `True`**, so a board that never set it (every board written before
the flag existed) hashes byte-for-byte identically to before — the
omit-at-default discipline (§3.4) applied at the board level.

`_canon_disable_drift` canonicalizes `disable_drift` as the **sorted,
de-duplicated set of drift-kind wire strings** — not as a bare "any / none"
flag. Each named kind drops the built-in judge that emits it
(`zicato.judge_runtime.disable`), so *which* kinds are named decides which
judges are armed and therefore the loss surface; swapping `tool_error` for
`goal_drift` has to roll the epoch just as adding a judge does. Tokens are
reduced through `judge_runtime.disable.kind_to_wire_string` so `DriftKind`
members (from the loader branch) and bare strings (from the raw scan) agree on
one form. Declaration order and repeated kinds are no-ops.

The empty set canonicalizes to `false`, **not** `[]` — omit-at-default paid at
the *value* level. `false` is the byte this form has always carried for "nothing
disabled", so every board that disables nothing, including every board written
before `disable_drift` existed, keeps the exact hash it already has and no
workspace rolls its epoch for the kind-set change. A board that *does* name
kinds re-hashes once, by design.

`_canon_judges` reduces the judges list to an order-independent form: each judge
is normalized to a sorted-key dict, then the list is sorted by its serialized
form, so declaration order does not move the hash but adding / removing /
editing a judge does. A **python-mode judge** (`mode == "python"`) has its
`body` dotted-spec expanded through `_canon_dotted_spec` (§3.3), so editing the
judge's *source* rolls the epoch — not only swapping the dotted string.

`_fold_entry_grading_source` does the same at the entry level: a `predicate`
expectation's `spec` and each python-mode per-entry judge's `body` get a
`spec_source` / `body_source` key carrying their source hash. Non-predicate
expectations (text / regex / json_schema / rubric — not dotted plugins) and
inline judges are left untouched, so a board that names no plugin canonicalizes
byte-for-byte as before this mechanism existed.

> ✅ ALWAYS route a NEW board-level or per-entry grading channel through the
> defensive `_meta_get` / `_scan_raw_board_meta` pattern AND fold it at its
> default only when non-default. The `judge_only`-only-when-`True` line is the
> model: `if judge_only: canon["judge_only"] = True`
> (`src/zicato/epoch/contract.py::_canon_board_meta`). Emitting a new key
> unconditionally rolls every board that predates it — invariant #2 broken at
> the board component.

### 3.2.2 brief — `_canon_brief`

The proposer brief (`brief.md`) is normalized exactly like a skill body: CRLF →
LF, per-line trailing whitespace stripped, leading/trailing blank lines dropped
(`src/zicato/epoch/contract.py::_canon_brief`). "Whitespace-only edits
(re-indenting, CRLF churn, trailing-newline changes) do not move the hash;
editing the actual prose does." A missing brief hashes as empty with a warning.

The brief is an **epoch-level** concept — one brief governs every proposer call
within an epoch — and its normalized body is a contract input, so a semantic
edit rolls the epoch. This is why the proposer's per-round guidance lives in
the brief and not in code (see 05-proposer.md §"Where `brief_text` comes from").
`tests/test_epoch_contract.py::test_hash_stable_across_whitespace_only_brief_edits`
plants a CRLF + trailing-space + blank-line re-spelling and asserts the hash
holds.

### 3.2.3 scoring — `_canon_scoring` and `scoring_to_canon`

The subtlest component. `_canon_scoring` does NOT hash the raw `scoring.json`;
it parses the file into a **fully-defaulted `ScoringWeights`** and serializes
*that* (`src/zicato/epoch/contract.py::_canon_scoring`):

```python
# src/zicato/epoch/contract.py — _canon_scoring (tail)
    from zicato.workspace_loader import scoring_weights_from_dict  # noqa: PLC0415

    raw = json.loads(scoring_path.read_text(encoding="utf-8"))
    weights = scoring_weights_from_dict(raw)
    return json.dumps(round_floats(scoring_to_canon(weights)), sort_keys=True)
```

Why route through `ScoringWeights` rather than hash the JSON? Because the
operator's live `scoring.json` is commonly a *partial* document (only the fields
they care about) while the per-epoch frozen copy is the *full* serialized form.
Both must canonicalize identically or the stored hash would never match the
re-derived one and the epoch would roll on every `evolve`. Passing both through
the same fully-defaulted `ScoringWeights` collapses the two spellings.

`round_floats` rounds every float to 6 decimal places, so `0.1` and
`0.10000000001` collapse and float-format noise below the threshold does not
move the hash
(`tests/test_epoch_contract.py::test_hash_stable_across_scoring_float_noise`).

`scoring_to_canon(weights)` walks `dataclasses.fields()` — so it covers every
field automatically and can never desync from the dataclass — and does four
things per field:

| Field kind | Handling |
|---|---|
| in `_SCORING_OMIT_AT_DEFAULT_FIELDS` and at its default | **skipped** — the key is absent from the canonical form (§3.4) |
| in `_SCORING_PLUGIN_SPEC_FIELDS` (`scalar_fn`, `drift_reducer`, `outcome_summarizer_spec`) | expanded via `_canon_dotted_spec` to `{"spec": …, "source_sha256": …}` (§3.3) |
| a nested frozen dataclass (`overfitting`, `tournament_structure`, `proposer_quality`, `experiment_memory`, `ladder`) | **recursed** via `scoring_to_canon` — the nested block canonicalizes structurally, and the SAME omit-at-default check applies to its fields |
| a mapping / tuple / scalar | dict-ified / list-ified / passed through via `_canon_value` |

The recursion is what folds the tournament structure, the overfitting block, and
the proposer-quality block into the scoring hash "with zero new plumbing"
(`ScoringWeights` field comments) — and, critically, it is what lets a
default-off knob *nested* on `OverfittingConfig` or `ProposerQualityConfig`
(`random_baseline_every_n`, `screen_entries`, `screen_veto_only`,
`process_exemplars`) be omitted at its default: the same `_SCORING_OMIT_AT_DEFAULT_FIELDS`
name check runs at every recursion depth (§3.4).

> ⚠️ TRAP — a nested config dataclass folds into the scoring hash the moment it
> becomes a `ScoringWeights` field, whether you intended it or not. That is the
> feature (`OverfittingConfig`, `TournamentStructure`, and `ProposerQualityConfig`
> all rely on it), but it means adding a field to any of those nested dataclasses
> is a **contract change** with no extra code. If the field is additive/default-
> off, you MUST register it in `_SCORING_OMIT_AT_DEFAULT_FIELDS` (§3.4) or it
> rolls every existing epoch. If it is intended to roll, add a byte-identity test
> anyway (§3.11 step 8).

### 3.2.4 entrypoint — `_canon_entrypoint`

The simplest: the registered `--adk` entrypoint string, verbatim
(`_canon_entrypoint(entrypoint) -> entrypoint`). Changing the entrypoint (e.g.
`pkg.mod:agent` → `pkg.mod:OTHER_agent`) points the loop at a different inner
harness, which is a different contract, so it rolls
(`tests/test_epoch_contract.py::test_hash_changes_on_entrypoint_edit`). There is
no normalization — an entrypoint is an identifier, and any byte difference is
semantic.

### 3.2.5 mutable_trees — `_canon_mutable_trees` (the bug-#10 story)

The registered `--mutable-tree` paths — *which subtrees of the target are
mutable*. This is the single most instructive canonicalizer in the module,
because it is the one that was *wrong* and had to be fixed (bug #10; see
12-bug-casebook.md §"Case 10"). The current, correct body:

```python
# src/zicato/epoch/contract.py — _canon_mutable_trees (body)
    normalized = sorted(PurePosixPath(os.path.normpath(p)).as_posix() for p in mutable_trees)
    return "\n".join(normalized)
```

The identity being hashed is *which subtrees are mutable* — a property of the
**registration**, not of where the checkout happens to live. Paths are
**normalized** (`.`/`..`/separator spelling collapsed, POSIX-rendered, sorted)
but **NEVER resolved against the filesystem**. The bug was the earlier body:

```python
# BEFORE (bug #10) — do NOT reintroduce
    resolved = sorted(str(Path(p).resolve()) for p in mutable_trees)
    return "\n".join(resolved)
```

`Path(p).resolve()` folds the **process cwd** (for a relative registration) and
the **absolute checkout location** into the hashed string. Consequence: the same
workspace hashed *differently* when `evolve` ran from a different directory — or
after the workspace was moved — and **spuriously rolled its epoch** (lineage
reset, warm-start lost, an "epoch roll" event with no contract change). The fix
was taken as a **declared BREAKING change** (CHANGELOG'd, one-time hash move,
standard contract-roll behavior) rather than a compatibility shim: the hash was
wrong; keeping it stable would have frozen the wrongness.

The regression test makes cwd-invariance an explicit axis
(`tests/test_epoch_contract.py::test_contract_hash_is_cwd_and_checkout_invariant`):

```python
# tests/test_epoch_contract.py — test_contract_hash_is_cwd_and_checkout_invariant
    def compute_from(cwd):
        monkeypatch.chdir(cwd)
        return compute_contract_hash(
            ContractInputs(
                board_path=board,
                brief_path=brief,
                scoring_path=scoring,
                entrypoint="pkg.mod:agent",
                mutable_trees=("agent", "./skills/../skills"),
            )
        )

    other = tmp_path / "elsewhere"
    other.mkdir()
    assert compute_from(tmp_path) == compute_from(other)
```

Registration order still never moves the hash (sorted); adding or removing a
tree still does
(`test_hash_stable_across_mutable_tree_reordering`,
`test_hash_changes_on_adding_a_mutable_tree`).

> ⛔ NEVER call `Path.resolve()` — or `expanduser()`, `os.getcwd()`,
> `socket.gethostname()`, a tempdir name, a pid, or the wall clock — in ANY
> contract-identity, cache-key, seed-tuple, or dedup-fingerprint computation.
> This is invariant #1, and it generalizes the whole "identity vs location"
> lesson (12-bug-casebook.md §"Case 10"). `Path.resolve()` in a hash context is a
> review red flag. If you add a path-shaped field to `ContractInputs`,
> canonicalize it with `os.normpath` + `PurePosixPath.as_posix`, never a
> filesystem-touching call.

> ⚠️ TRAP — the `compute_contract_hash` *summary docstring* still describes
> `mutable_trees` as "sorted tuple of absolute path strings." That line is STALE
> (pre-bug-#10) and contradicts the code. The authority is `_canon_mutable_trees`
> (normalized, never resolved), not the summary; the two-line body above is what
> actually runs. Trust the function. (Fixing the stale docstring is a fine
> drive-by, but do not "fix" the *code* to match the stale prose.)

### 3.2.6 proposer — `_canon_proposer` (skills yes, runtime tools no)

The sixth component. `_canon_proposer(proposer_path)` resolves the proposer dir
(or `None` ⇒ the built-in default) to a `ProposerSpec` via
`zicato.proposer.skills.resolve_proposer_spec`, then reduces it to a sorted-key
JSON string (`src/zicato/epoch/contract.py::_canon_proposer`):

```python
# src/zicato/epoch/contract.py — _canon_proposer (tail)
    canon: dict[str, object] = {
        "agent_id": spec.agent_id,
        "tools": sorted(spec.tools),
        "skills": skills,
        "agent_source_sha256": spec.agent_source_sha256,
    }
    return json.dumps(canon, sort_keys=True, ensure_ascii=False)
```

What folds in, and what rolls the epoch:

| Element | Canonical form | Rolls when |
|---|---|---|
| `agent_id` | `"builtin:default"` or `"dir:<name>"` | you configure a proposer dir (builtin → dir), or rename the dir |
| `tools` | the tool names, **sorted** | — (see the trap below: always empty on this branch) |
| `skills` | `[{"name", "sha256"}]` sorted by name; each `sha256` is over the **normalized** body | a semantic skill edit; adding / removing / renaming a skill |
| `agent_source_sha256` | SHA-256 of a custom `agent.py`'s bytes, or `null` | any byte of the custom agent |

Skill bodies are normalized exactly like the brief (`normalize_skill_body`
mirrors `_canon_brief`), so a whitespace-only skill edit does not move the hash
while a semantic one does
(`tests/test_epoch_contract.py::test_proposer_whitespace_only_skill_edit_is_stable`
vs `::test_proposer_skill_body_edit_changes_hash`). Skills are discovered
**sorted by filename**, so filesystem enumeration order / mtimes never move the
hash (`::test_proposer_hash_stable_across_filesystem_reorder`).

> ⚠️ TRAP — the `tools` key is present in the canonical form but
> `ProposerSpec.tools` is **always empty** on this branch: `resolve_proposer_spec`
> sets `tools=()` for both the builtin and any dir proposer ("tool declaration is
> a later phase"). The read-only tool REGISTRY (`DEFAULT_PROPOSER_TOOLS`) is
> zicato *source code* — it ships with the package, is versioned with the code,
> and is identical for every workspace on a given zicato version — so hashing it
> would roll every epoch on every zicato upgrade without changing the operator's
> authored contract. The tools a custom `agent.py` constructs itself DO roll,
> because that choice lives in `agent_source_sha256`. See 05-proposer.md §"Why
> tools do NOT fold into the contract hash" for the full argument.

The built-in default (`proposer_path=None`) produces a stable canonical string,
and an *empty proposer dir* is deliberately NOT the builtin — its `agent_id` is
`"dir:<name>"` while the builtin's is `"builtin:default"`, so the two
canonicalize differently
(`tests/test_epoch_contract.py::test_proposer_builtin_differs_from_empty_dir`).

### 3.2.7 The sensitivity / stability matrix

The whole point of the canonicalizer is captured in one table: which concrete
operator edit moves which component, and which edits are deliberately invisible.
Each row is pinned by a named test in `tests/test_epoch_contract.py` — this is the
matrix to consult when you are unsure whether an edit "should" roll.

| Operator edit | Component | Rolls? | Pinning test |
|---|---|---|---|
| edit a board entry's input/expectation/weight | board | **rolls** | `test_hash_changes_on_board_entry_input_edit` |
| reorder board rows / reformat the JSONL | board | stable | `test_hash_stable_across_board_entry_reordering` |
| add/remove/edit a board judge | board | **rolls** | `test_canon_judges_sensitive_to_judge_change` |
| edit a python judge's / predicate's SOURCE (dotted string unchanged) | board | **rolls** | source-hash fold (§3.3) |
| re-indent / CRLF-churn the brief | brief | stable | `test_hash_stable_across_whitespace_only_brief_edits` |
| edit the brief prose | brief | **rolls** | (brief sensitivity) |
| retune a scoring weight | scoring | **rolls** | `test_hash_changes_on_scoring_weight_edit` |
| float-format noise below 6dp | scoring | stable | `test_hash_stable_across_scoring_float_noise` |
| flip an `overfitting` / `ladder` knob | scoring | **rolls** | `test_hash_changes_on_overfitting_knob_edit`, `test_hash_changes_on_ladder_knob_edit` |
| set `screen_entries`/`process_exemplars` to non-default | scoring | **rolls** | `test_hash_changes_when_screening_opted_in` |
| spell out a default-off knob's default explicitly | scoring | stable | `test_hash_stable_when_screening_fields_at_default` |
| change the `--adk` entrypoint | entrypoint | **rolls** | `test_hash_changes_on_entrypoint_edit` |
| add/remove a mutable tree | mutable_trees | **rolls** | `test_hash_changes_on_adding_a_mutable_tree` |
| reorder / re-spell mutable-tree paths; run from a different cwd | mutable_trees | stable | `test_hash_stable_across_mutable_tree_reordering`, `test_contract_hash_is_cwd_and_checkout_invariant` |
| edit a skill body semantically / add / remove / rename a skill | proposer | **rolls** | `test_proposer_skill_body_edit_changes_hash`, `_adding_`, `_removing_`, `_renaming_` |
| whitespace-only skill edit / mtime reorder | proposer | stable | `test_proposer_whitespace_only_skill_edit_is_stable`, `_filesystem_reorder` |
| edit a custom `agent.py` | proposer | **rolls** | `test_proposer_agent_source_edit_changes_hash` |

> ✅ ALWAYS add a matrix ROW (a stability test AND a sensitivity test) when you
> add a contract component or a component sub-field. The paired shape — "this
> spurious edit stays stable, this semantic edit rolls" — is the only thing that
> proves the boundary is in the right place. A component with only a sensitivity
> test can be over-eager (rolls on cosmetic edits); one with only a stability test
> can be blind (misses a real change). §3.11 step 8 is this discipline as a recipe.

---

## 3.3 The ONE source-hashing mechanism

A recurring need across the contract: an operator points a *grading* channel at
a dotted Python spec (`pkg.mod:fn`) — a scalar function, a drift reducer, an
outcome summarizer, a board predicate, a python judge. If the hash folded in
only the dotted STRING, editing the plugin's *body* without changing its name
would leave the contract hash fixed while silently scoring under different code
— a missed roll. The fix is the ONE source-hashing mechanism every grading
plugin shares (issue #19 cross-cutting #1), `_canon_dotted_spec`
(`src/zicato/epoch/contract.py`):

```python
# src/zicato/epoch/contract.py — _canon_dotted_spec (tail)
    if not isinstance(spec, str) or not spec:
        return {"spec": "", "source_sha256": None}
    return dict(spec_with_source_hash(spec))
```

`zicato.scoring.plugins.spec_with_source_hash(dotted)` resolves the module,
hashes its source bytes, and returns `{"spec": <dotted>, "source_sha256":
<hash-or-null>}`. An empty / non-string spec expands to `{"spec": "",
"source_sha256": null}` — **byte-identical to "no plugin"** — so a board or
contract that names no plugin canonicalizes exactly as it did before this
mechanism existed.

The one mechanism is applied uniformly in four places, so there is exactly one
answer to "does editing a plugin body roll the epoch?" (yes, everywhere):

| Channel | Where the fold happens |
|---|---|
| scoring `scalar_fn` / `drift_reducer` / `outcome_summarizer_spec` | `scoring_to_canon` via `_SCORING_PLUGIN_SPEC_FIELDS` |
| board `predicate` expectation `spec` | `_fold_entry_grading_source` → `spec_source` |
| per-entry python judge `body` | `_fold_entry_grading_source` → `body_source` |
| board-level python judge `body` | `_canon_judges` → `body_source` |

> ✅ ALWAYS route a NEW dotted-spec grading channel through `_canon_dotted_spec`,
> never fold the bare string. If you add a `ScoringWeights` field that holds a
> grading dotted spec, add its name to `_SCORING_PLUGIN_SPEC_FIELDS`. If it is a
> board-level channel, extend `_fold_entry_grading_source` / `_canon_judges`. The
> failure mode of forgetting is silent and severe: an operator edits their
> scorer, the loop keeps scoring under the new code, but the contract hash says
> the epoch is unchanged and generations from before and after the edit are
> compared as if comparable.

> ⚠️ TRAP — a plugin whose module is not importable at hash time expands to a
> *degraded null source hash* (`source_sha256: null`), and that is intentional:
> "a not-yet-written plugin must still construct so the contract can be hashed"
> (`ScoringWeights.__post_init__`). The dotted string still participates, so the
> contract is not identity-free; but do not rely on the source hash to detect an
> edit to a plugin that could not be imported. The guard tests
> (`tests/test_contract_serializer_completeness.py`) use bare `pkg.mod:...`
> strings precisely because the degraded-null path is a legitimate state.

---

## 3.4 The omit-at-default discipline

This is the single discipline a contract-knob author most often gets wrong, and
the one with the most expensive failure mode. It exists to reconcile two
requirements that pull in opposite directions:

- **completeness** — the canonical scoring form must be *complete* and
  independent of which fields the operator spelled out, so that a partial live
  `scoring.json` and a full frozen one hash identically (§3.2.3);
- **stability across upgrades** — adding a new field to `ScoringWeights` must NOT
  retroactively change the hash of every epoch already on disk. If it did, every
  workspace would auto-roll the moment it upgraded to a zicato version that has
  the field — a mass false roll.

The reconciliation: a purely-additive, default-off field is **omitted from the
canonical form while it holds its default**, and reintroduced (rolling the
epoch) only when set to a non-default value. `scoring_to_canon` implements it
(`src/zicato/epoch/contract.py`):

```python
# src/zicato/epoch/contract.py — scoring_to_canon (omit-at-default clause)
        if f.name in _SCORING_OMIT_AT_DEFAULT_FIELDS:
            # Resolve the field's default (plain default, or default_factory)
            # and skip the key entirely while the value matches it, so the
            # canonical form is byte-identical to a pre-field contract.
            if f.default is not MISSING:
                default_value: object = f.default
            elif f.default_factory is not MISSING:
                default_value = f.default_factory()
            else:
                default_value = object()  # no default ⇒ never matches; always emit
            if value == default_value:
                continue
```

The registered fields (`_SCORING_OMIT_AT_DEFAULT_FIELDS`,
`src/zicato/epoch/contract.py`):

| Field | Lives on | Default | What it opts into |
|---|---|---|---|
| `diff_complexity_weight` | `ScoringWeights` | `0.0` | the MDL / parsimony scalar term |
| `experiment_memory` | `ScoringWeights` | `ExperimentMemoryConfig()` | cross-epoch experiment memory |
| `random_baseline_every_n` | `OverfittingConfig` (nested) | `0` | the placebo / random-baseline arm |
| `block_on_containment_violation` | `ScoringWeights` | `False` | integrity BLOCKING (vs alarm-only) |
| `block_on_gate_contradiction` | `ScoringWeights` | `False` | gate-contradiction BLOCKING |
| `screen_entries` | `ProposerQualityConfig` (nested) | `0` | pre-tournament candidate screening |
| `screen_veto_only` | `ProposerQualityConfig` (nested) | `False` | screen veto-only (no tiebreak feed) |
| `process_exemplars` | `ProposerQualityConfig` (nested) | `0` | the redacted process-exemplar channel |

Note the **nested** entries: `random_baseline_every_n`, `screen_entries`,
`screen_veto_only`, and `process_exemplars` live on nested config dataclasses,
not on `ScoringWeights` directly. The omit check works for them because
`scoring_to_canon` recurses into nested dataclasses and applies the SAME
`_SCORING_OMIT_AT_DEFAULT_FIELDS` name check at every depth — the field NAME is
what is matched, wherever it sits. A comparison for the nested block is by
value against its `default_factory()` instance (an all-default
`ExperimentMemoryConfig` compares equal and is omitted; any opt-in differs and
rolls).

**What it buys:** byte-stable hashes across zicato upgrades — an epoch created
before a field existed hashes byte-for-byte identically to one that explicitly
spells the field's default. Pinned by
`tests/test_epoch_contract.py::test_hash_stable_when_screening_fields_at_default`
(omit == explicit-default) and
`::test_hash_changes_when_screening_opted_in` (any opt-in rolls).

**The failure mode of getting it wrong:** the guard docstring is blunt — a
field "added AFTER the parity goldens were captured; emitting it unconditionally
would inject a new key into the scoring hash and roll EVERY existing epoch (and
turn the CONTRACT-HASH parity gate red) the moment the field exists." A missed
registration is not a subtle bug; it mass-rolls the fleet on upgrade.

> ⛔ NEVER register a field in `_SCORING_OMIT_AT_DEFAULT_FIELDS` unless it is
> **purely additive and default-off** — the docstring says "Only purely-additive,
> default-off fields belong here." A field whose default is *behaviorally active*
> (e.g. `promote_margin = 0.01`, `best_of_n = 3`) must appear in the canonical
> form always: omitting it would make a contract that pins the default hash
> identically to one that predates the field, which is fine, but the field's
> *meaning* is not "off at default" — it is "0.01 / 3 at default." Omitting an
> active-default field hides real contract content. Only "0 / False / empty ⇒
> the feature does nothing" fields qualify.

> ⚠️ TRAP — omit-at-default and behavior-at-default are two different questions,
> and the answer to both must agree. `best_of_n` defaults to `3` (an active
> default, NOT omitted, always in the hash); `screen_entries` defaults to `0`
> (an inert default, omitted). Both live on `ProposerQualityConfig`. If you
> confuse them — omit `best_of_n`, or emit `screen_entries` unconditionally — you
> either hide the sampling rule from the hash or mass-roll the fleet. Decide "is
> the default inert?" first; the omit registration follows from the answer.

### 3.4.1 The recursion, traced (why a nested omit "just works")

The single fact that trips people up: `screen_entries` lives on
`ProposerQualityConfig`, not on `ScoringWeights`, yet its name is in
`_SCORING_OMIT_AT_DEFAULT_FIELDS` (a set that reads like it is about
`ScoringWeights` fields). Here is exactly why that works, step by step, for a
default `ScoringWeights()`:

```
scoring_to_canon(ScoringWeights())
  for f in fields(ScoringWeights):
    …
    f.name == "proposer_quality"  → value is a ProposerQualityConfig (dataclass)
      → is_dataclass(value) branch → out["proposer_quality"] = scoring_to_canon(value)
          scoring_to_canon(ProposerQualityConfig())        # SAME function, recursed
            for f in fields(ProposerQualityConfig):
              f.name == "best_of_n"        → 3, not in omit set → emitted
              f.name == "screen_entries"   → 0, in omit set AND == default → SKIPPED
              f.name == "screen_veto_only" → False, in omit set AND == default → SKIPPED
              f.name == "process_exemplars"→ 0, in omit set AND == default → SKIPPED
          → {"best_of_n": 3, "critique_enabled": true}     # the two active fields only
```

The omit check matches on the field NAME, and `scoring_to_canon` is the SAME
function at every recursion depth, so a name in the set is omitted wherever it
appears in the nested tree — `random_baseline_every_n` (on `OverfittingConfig`),
`screen_entries` / `screen_veto_only` / `process_exemplars` (on
`ProposerQualityConfig`), `experiment_memory` (a whole nested block on
`ScoringWeights`, compared by value against its `default_factory()`). This is why
`test_hash_stable_when_screening_fields_at_default` passes: the default nested
block canonicalizes to `{"best_of_n": 3, "critique_enabled": true}` whether the
operator omitted `proposer_quality` entirely or spelled out its OFF defaults.

> ⚠️ TRAP — the omit set is a FLAT set of names, but the fields it governs live at
> different depths. If two DIFFERENT nested dataclasses ever declared a field with
> the SAME name, one omit-registration would silently govern both. No two do today,
> but if you add a nested field whose name collides with an existing omit entry,
> the recursion will omit it too — audit the whole `_SCORING_OMIT_AT_DEFAULT_FIELDS`
> set against `fields()` of every nested config before adding a name.

---

## 3.5 Serializer completeness — the field-enumerating serde

The scoring contract is serialized by two code paths that MUST agree on which
fields exist:

- the **contract-hash canonicalizer** (`scoring_to_canon`), which enumerates
  `dataclasses.fields()` and therefore covers every field; and
- the **frozen-epoch snapshot** writer/parser/loader (`scoring_to_dict` /
  `_scoring_from_dict` in `src/zicato/epoch/lifecycle.py`,
  `scoring_weights_from_dict` in the loader).

Historically the snapshot path was a hand-maintained, field-by-field dict. When
a new field was threaded through the canonicalizer but NOT the hand-written
writer, the frozen `scoring.json` silently dropped it; on the next `evolve` the
live contract hashed differently from the frozen one and the orchestrator
performed a **spurious epoch auto-roll** (issue #13 — the same class as
`per_judge_weights` / `pass_rate_monotonicity_scope` / `drift_kind_aggregation`
desyncs). The fix routes BOTH `to_json` and `from_json` through the
field-enumerating serde (`zicato.epoch.contract_serde`):

```python
# src/zicato/core/scoring_config.py — ScoringWeights.to_json
        from zicato.epoch.contract_serde import dataclass_to_jsonable  # noqa: PLC0415

        return dataclass_to_jsonable(self)
```

`from_json` is the exact inverse: `ScoringWeights.from_json(w.to_json()) == w`
for every field. It is tolerant of a partial / absent payload (a missing key
falls back to the field's dataclass default, so a legacy `scoring.json` loads
cleanly) and re-runs `__post_init__` validation (a corrupt transform spec fails
fast). One defensive coercion lives at this seam: a
`pass_rate_monotonicity_scope` token outside `{"per_entry", "aggregate"}` (a
corrupt / future args file) is coerced back to the default rather than desyncing
the worker's gate view.

The completeness invariant is pinned STRUCTURALLY, so a future field is covered
automatically. `tests/test_contract_serializer_completeness.py` iterates
`dataclasses.fields()` and, for each contract dataclass, constructs an instance
with EVERY field set to a curated non-default value, then asserts three
properties:

```
from_dict(to_dict(x)) == x                       # round-trip identity
canon(x) == canon(from_dict(to_dict(x)))         # no spurious roll
every dataclass field appears in to_dict(x)      # no dropped field
```

The curated values live in a `_NONDEFAULT_VALUES` table keyed by class then
field; the test *fails* if a future field is missing from the table, which is
the mechanism that forces a new-field author to extend the guard. The
`ProposerQualityConfig` entry is the shape you copy when adding a nested-config
field:

```python
# tests/test_contract_serializer_completeness.py — _NONDEFAULT_VALUES (excerpt)
    "ProposerQualityConfig": {
        "best_of_n": 4,
        "critique_enabled": False,
        "screen_entries": 3,
        "screen_veto_only": True,
        "process_exemplars": 2,
    },
```

> ✅ ALWAYS add a serde field through the dataclass (`ScoringWeights` or a nested
> config), never through a hand-written dict. `dataclass_to_jsonable` /
> `jsonable_to_dataclass` cover it automatically; a bespoke serialization path
> re-introduces the issue-#13 drop-a-field class. The one place a field is
> spelled by hand is the guard table — and that is deliberate: the hand-curated
> value is a *test input*, and the test raises with an actionable message
> (`"add one to _NONDEFAULT_VALUES"`) if it is missing.

> ⚠️ TRAP — the tournament structure is persisted under the legacy `"tournament"`
> key, NOT `"tournament_structure"` (the field name). `scoring_to_dict` remaps it
> for byte-compatibility with every on-disk `scoring.json` and the dashboard
> builder (`tests/test_contract_serializer_completeness.py::test_tournament_block_uses_legacy_key`).
> If you rename a contract field, decide explicitly whether the on-disk key moves
> — a moved key is itself a format change that strands existing snapshots.

---

## 3.6 `EpochConfig` — the frozen record and the never-hashed fields

`EpochConfig` (`src/zicato/core/epoch.py`) is the frozen evaluation contract for
one epoch, plus the bookkeeping that surrounds it. It is a frozen, slotted
dataclass; its fields split into three classes:

| Class | Fields | Folds into `contract_hash`? |
|---|---|---|
| **Contract inputs** | `board_path`, `brief_path`, `scoring`, `proposer_path` (+ the harness identity carried at creation) | YES — these ARE the contract |
| **Identity / bookkeeping** | `id`, `name`, `created_at`, `closed`, `closed_at`, `contract_hash`, `goal` | no — metadata about the epoch |
| **Runtime measurements** | `noise_floor`, `preflight` | **NEVER** (§3.6.1) |

`contract_hash` is the sha256 computed at epoch-creation time (§3.7). Its default
is `None`, which means "epoch created before contract-hash auto-epoching landed"
— such legacy epochs are treated as *always matching* (§3.8.4). A legacy on-disk
`""` is normalized to `None` on read (`_config_from_dict`).

### 3.6.1 `noise_floor` and `preflight` — recorded, never hashed

`noise_floor` (the measured A/A noise floor) and `preflight` (the contract
pre-flight verdict) are RUNTIME measurements recorded *post-creation*, exactly
like `goal`. They are written to `config.json` alongside the contract, but they
are **not contract inputs** — writing them does not touch `contract_hash` and
never rolls the epoch. `_config_to_dict` writes them as additive keys:

```python
# src/zicato/epoch/lifecycle.py — _config_to_dict (tail)
        # Measured A/A noise floor (runtime measurement, never hashed).
        # ``None`` ⇒ never measured; written as null so it round-trips.
        "noise_floor": cfg.noise_floor,
        # Contract pre-flight verdict (runtime measurement, never hashed).
        # ``None`` ⇒ never run; written as null so it round-trips.
        "preflight": cfg.preflight,
```

They are set through dedicated mutators that load the config, `replace` the one
field, and write back — `set_epoch_noise_floor` and `set_epoch_preflight`
(`src/zicato/epoch/lifecycle.py`), mirroring `set_epoch_goal`. Re-measuring
overwrites the prior record.

**Why never hashed?** Because a measurement of the contract's noise is not part
of the contract — it is an *observation about* the contract. If the noise floor
folded into the hash, re-measuring it (running `zicato board audit` again, or the
opt-in evolve-start calibration) would roll the epoch and orphan the lineage you
were trying to calibrate. The whole point of `noise_floor` is to *inform* the
margin decision within an epoch; rolling on measurement would make it useless.
The same logic covers `preflight`: a pre-flight verdict is a diagnostic read of
whether the contract can distinguish signal from noise, consumed by the
loop-health detector — not a rule the tournament scores under.

> ⛔ NEVER add a *measurement* to the contract hash. Measurements (noise floors,
> pre-flight verdicts, fertility maps, calibration accuracy) describe how the
> contract *behaves*; the contract is the *rules*. This is invariant #5, and it
> is the flip side of §3.1's "no source in the hash": both keep the hash a pure
> function of the operator's authored rules. If you find yourself wanting to roll
> the epoch when a measurement changes, you have confused the map for the
> territory — the measurement belongs on `EpochConfig` as an additive
> never-hashed field with a `set_epoch_*` mutator, or on `RuntimeConfig` (§3.12).

> ⚠️ TRAP — because `noise_floor` and `preflight` are additive `config.json`
> keys, an epoch `config.json` written before they existed lacks them, and
> `_config_from_dict` must default them to `None`. It does (`raw_floor if
> isinstance(raw_floor, dict) else None`). If you add another additive
> `EpochConfig` field, give it the same absent-reads-as-default treatment on the
> read side, or a legacy `config.json` fails to load — the `format_version` guard
> (§3.10) protects the *record shape*, not individual missing keys.

---

## 3.7 Computing the hash — `compute_contract_hash` / `compute_component_hashes`

`compute_contract_hash(inputs)` canonicalizes the six components, joins them with
`_SEP`, and returns the sha256 hex digest (`src/zicato/epoch/contract.py`). A
missing file for any component hashes as the empty string for that component
(with a logged warning), so a partially-registered workspace still hashes
deterministically. The digest is 64 hex chars
(`tests/test_epoch_contract.py::test_missing_files_hash_deterministically`).

`compute_component_hashes(inputs)` returns the per-component sub-hashes under the
keys `board` / `brief` / `scoring` / `entrypoint` / `mutable_trees` /
`proposer`. This is what the auto-roll uses to tell the operator *which*
component drifted (§3.8.3). The two functions share every `_canon_*` helper, so
they can never disagree about a component's canonical bytes.

At epoch creation, `new_epoch` computes the hash over the just-written *frozen*
copies (§3.8.1), so the stored hash is exactly what a later
`resolve_contract_inputs` over equivalent *live* files produces — the invariant
that makes the drift check meaningful.

> ⚠️ TRAP — the stored hash is computed over the FROZEN copies at creation; the
> drift check re-computes over the LIVE copies. These must canonicalize
> identically for an unchanged contract. The whole `ScoringWeights`-round-tripping
> discipline (§3.2.3) and serializer-completeness guard (§3.5) exist to keep that
> true. If you introduce any asymmetry between how the frozen copy is written and
> how the live copy is read — a field the writer drops, a normalization the
> reader skips — you manufacture a spurious roll on the very next `evolve`.

---

## 3.8 The epoch lifecycle

### 3.8.1 `new_epoch` — create + freeze + hash + switch

`new_epoch` (`src/zicato/epoch/lifecycle.py`) is the only supported way to mint
an epoch. Its steps, in order:

1. if `auto_close_previous` and the current epoch is open, close it first
   (warning to stderr; the analysis pass needs `aux_call_llm`);
2. construct the id `{YYYY-MM-DD}_{slug}` (a same-day name collision gets a
   numeric suffix);
3. create `epochs/{id}/` and **materialize the frozen board + brief** into it
   (`_materialize_board` / `_materialize_brief` accept an in-memory `Board` /
   `ProposerBrief` / a `str` of brief text / a `Path` to copy verbatim);
4. serialize `weights` to the frozen `scoring.json` through the storage seam;
5. **compute the contract hash over the just-written frozen copies** plus the
   `entrypoint` + `mutable_trees` + `proposer_path`;
6. write `config.json`, register the epoch in `lineage.json`;
7. point the `current_epoch` marker at the new id.

The key design property: when given in-memory objects, `new_epoch` owns
canonicalization and persistence end to end — "the on-disk files the contract
hash is computed from are this function's responsibility, not the caller's." A
caller never needs a prior `.save()`.

`entrypoint`, `mutable_trees`, and `proposer_path` default to empty / `None` so
existing callers keep working — an epoch created without them hashes those
components as empty (stable and back-compatible), and `proposer_path=None`
canonicalizes to the built-in default's stable form.

> ⚠️ TRAP — `new_epoch`'s `auto_close_previous` default is `True`, but the
> auto-roll path (§3.8.3) passes `auto_close_previous=False` because
> `ensure_epoch_for_contract` closes the previous epoch *explicitly* (it needs to
> re-stamp the closed report and drain stale overrides between the close and the
> open). If you add a new epoch-creation call site, decide who owns the close: a
> double close is guarded (`close_epoch` is idempotent on an already-closed
> epoch) but a *missed* close leaves two epochs looking open.

### 3.8.2 `close_epoch` — mark closed + analysis

`close_epoch` (sync) and `close_epoch_async` (async) share `_close_epoch_prelude`,
which marks the epoch `closed=True` with a `closed_at` timestamp, stamps
lineage's `closed_at`, and runs the opt-in snapshot GC. The only difference is
how the analysis pass is driven: `close_epoch` uses `asyncio.run` (so it must NOT
be called from inside a running event loop), while `close_epoch_async` awaits it.
The orchestrator's auto-roll uses the async form because it already runs inside a
loop — a nested `asyncio.run` would raise.

When no `aux_call_llm` is supplied, both write a *stub* `analysis.md` (plus its
HTML companion) rather than skipping the file, so callers always see a non-empty
artifact; the analysis pass is rerunnable by hand later.

### 3.8.3 The auto-roll — `ensure_epoch_for_contract`

The roll-at-evolve-time hook (`src/zicato/evolve/epoching.py`), called before the
orchestrator resolves an epoch. It computes the live contract hash and compares:

```python
# src/zicato/evolve/epoching.py — ensure_epoch_for_contract (the decision)
    cfg = load_epoch(workspace_root, cur)
    if cfg.contract_hash is None or cfg.contract_hash == current_hash:
        # Legacy epoch (``None`` stored hash → treated as always-matching)
        # or the contract is unchanged. Either way: no roll. The check is
        # ``is None``, NOT ``== ""``: a corrupted/empty real hash must roll
        # rather than silently read as legacy.
        return cur
```

The four outcomes:

| State | `auto_epoch=True` | `auto_epoch=False` |
|---|---|---|
| no `current_epoch` marker | create `e0` from the contract, return it | raise `FileNotFoundError` (run `zicato epoch new`) |
| stored hash `None` (legacy) or `== live` | return `cur` (no roll) | return `cur` (no roll) |
| stored hash drifts | close `cur`, open `e{N}` seeded from `cur`'s promoted head, return the new id | raise `RuntimeError` naming the changed component |

On an auto-roll it: closes the drifted epoch (async, generating `analysis.md`),
re-stamps the persisted closed report, creates the new epoch (auto-named
`e{len(list_epochs)}`), writes the new epoch's per-component sub-hashes to
`contract_components.json`, records where the new epoch's `v0` seeds from (the
closed epoch's promoted head, via a roll-seed marker), **drains stale gate
overrides** (a pending override targets a bare `v3` id, which restarts at `v0`
in the new epoch, so a survivor would mis-fire), and logs/prints a
`contract changed (<components>) — rolled cur -> new` line. The changed-component
label comes from `_component_diff_label` comparing the stored vs live
sub-hashes.

The `--no-auto-epoch` path raises with the *same* changed-component label so the
operator knows what to revert:

```
evaluation contract has drifted from the current epoch '2026-…' (changed:
scoring); either revert the contract files or run `zicato epoch new` …
```

> ✅ ALWAYS report the changed component, not just "the contract changed." The
> auto-roll path names it (`_component_diff_label`) from the stored
> `contract_components.json`. When you add a new contract component, extend
> `compute_component_hashes` so the diff label can name it — otherwise a drift on
> your component falls back to the generic `"contract"` and the operator cannot
> tell what moved.

> ⚠️ TRAP — an auto-rolled epoch has an EMPTY `goal` (the auto-roll path "has no
> operator interaction surface"). `ensure_epoch_for_contract` logs + prints a
> nudge to run `zicato epoch set-goal --epoch <id> --goal "..."`. Do not treat a
> blank goal as a bug; it is the expected state after a roll, filled in
> post-hoc. Consumers that render the goal must handle "" as "no goal recorded."

### 3.8.4 Legacy-never-rolls: `is None`, not `== ""`

The legacy rule is subtle and load-bearing: a stored `contract_hash` of `None`
means "created before auto-epoching" and reads as *always matching* — so a
workspace that predates the feature never spuriously rolls. But the check is
`cfg.contract_hash is None`, **NOT `== ""`**. A corrupted or empty *real* hash
must roll (it means something went wrong at write time), not silently read as
legacy. `EpochConfig.contract_hash`'s docstring and `_config_from_dict` both
enforce this — a legacy on-disk `""` is normalized to `None` on read, and only
`is None` reads as legacy downstream.

> ⛔ NEVER widen the legacy check to `not cfg.contract_hash` or `cfg.contract_hash
> in (None, "")`. That would make a corrupt empty hash read as legacy and
> suppress a roll that should happen — the loop would keep comparing generations
> under a contract it can no longer verify. This is invariant #6; the `is None`
> spelling is deliberate and tested implicitly by the round-trip through
> `_config_from_dict` (which produces `None`, never `""`).

---

## 3.9 Lineage semantics

`lineage.json` (`src/zicato/epoch/lineage.py`) is the cross-epoch DAG, persisted
as one JSON document with atomic rewrites. It is shallow by construction: epochs
are linear (one is "current"), generations within an epoch form a linear chain
(`v0`, `v1`, …), and the cross-cutting edge is `epoch.v0_parent` pointing at the
predecessor's lineage head.

`register_epoch` appends a thin epoch entry (idempotent — re-registering is a
no-op). `mark_closed` stamps `closed_at`. `append_to_lineage` records a
generation under its epoch, **update-in-place if it already exists** (so a
generation appended pending and again at settle is one node, upserted).

The load-bearing subtlety is the `promoted` **tri-state**. A generation is one
of three states, and the difference between "rejected" and "still racing" is a
`False` vs `null`:

```python
# src/zicato/epoch/lineage.py — append_to_lineage (the tri-state)
    # ``None`` (pending) for an applied-but-unresolved in-flight challenger;
    # the resolved ``True`` / ``False`` for the settle-time upsert.
    promoted: bool | None = None if pending else generation.promoted
```

| `promoted` | Meaning | Renders as |
|---|---|---|
| `True` | crowned by a tournament | a promoted head |
| `False` | a rejected dead branch (kept for analysis) | rejected |
| `null` (pending) | applied-but-unresolved in-flight challenger — has a snapshot + parent + birth round, not yet crowned or cut | neither (still racing) |

Why the tri-state matters: an in-flight racer (a multi-challenger field slot
that has landed its snapshot but not yet been resolved) is written with
`pending=True` so its `promoted` persists as `null`. If it were written `False`
(the `Generation.promoted` default), it would render as a **rejected dead
branch** while it is still racing — the lineage summary would under-count the
field, and a lineage walker would treat a live racer as dead. The settle-time
append (`pending=False`, the default) upserts the same node to its resolved
`True` / `False`. The two writes compose because the upsert is idempotent
update-in-place. `render_lineage_summary` respects the tri-state: it counts
`promoted is True` and `promoted is False and id != "v0"` separately, and a
pending node counts toward neither column.

> ⛔ NEVER append an in-flight generation with `pending=False`. Invariant #8: an
> applied-but-unresolved generation MUST persist `promoted=null`. The field loop
> (`_mint_challenger_field`) appends each accepted sibling with `pending=True`
> precisely so a racer is not mistaken for a rejection; the tournament's settle
> path upserts the resolution. Writing `False` early is a data-corruption bug —
> the lineage would record a live experiment as a dead branch, and cross-epoch
> seeding (which reads the promoted head) could pick the wrong baseline.

> ⚠️ TRAP — `round_index` on a lineage node is the BIRTH round and NEVER changes
> once set. `append_to_lineage`'s upsert deliberately re-stamps `round_index`
> from the generation on every write, but a champion carried into later rounds
> keeps its ORIGINAL birth round (it is re-recorded with the same value, not the
> current round). If you add a per-round lineage field, decide whether it is
> birth-stamped (set once) or defense-updated (re-stamped) — mixing the two makes
> the "Epoch → Round → challengers minted that round" grouping lie.

`lineage.json` reads are deliberately more forgiving than the storage backend's
default: a missing / unreadable / malformed document collapses to the empty DAG
(the file is rebuilt forward by the mutators). The ONE exception is a
future-`format_version` document — that is an INTACT record this build cannot
interpret, so it refuses loudly rather than silently dropping history (§3.10).

### 3.9.1 The seed generation `v0`

The `v0` of every epoch is not a proposer experiment — it is the initial
workspace snapshot the epoch starts from (or, after a roll, the closed
predecessor's promoted head). But every downstream consumer (the analyzer report
loader, the index dual-write, the dashboard lineage walker) expects every
generation directory to carry an `experiment.json`. `write_seed_experiment`
(`src/zicato/epoch/journal.py`) writes a seed-shaped marker to keep the on-disk
shape uniform without inventing tournament numbers: `id = "exp_{epoch}_v0"`,
`parent_generation_id = None` (the seed has no in-epoch parent — cross-epoch
lineage lives in `lineage.json`), `hypothesis.core_idea = "baseline seed"`, and
`outcome = None` (the seed never ran a tournament round, so loaders render its
row with empty deltas and decision `"baseline"`). The write is idempotent —
`experiment.json` already present ⇒ return `False` without rewriting.

> ⚠️ TRAP — a seed's `parent_generation_id` is `None` (JSON `null`), and a legacy
> on-disk `""` is normalized to `None` on read (`read_experiment`,
> `_config_from_dict` do the same for their `None`-defaulting fields). Do not
> write `""` for "no parent" — a downstream lineage walker distinguishes "root"
> (`None`) from "parent is the generation literally named empty-string." The
> tri-state discipline (§3.9) and this null-vs-empty discipline are the same
> lesson at two scopes: an absent value must be representable distinctly from a
> present-but-empty one.

### 3.9.2 Per-generation records — `experiment.json` + patches

`write_experiment` (`src/zicato/epoch/journal.py`) persists an `Experiment` in
the per-generation split-file layout: each patch to `patches/{patch_id}.json`,
then the body to `experiment.json` carrying `patch_ids: [...]`. Write order is
**patches FIRST, `experiment.json` LAST**, so a crash between the two phases
leaves *orphan patch files* (harmless — no reader picks them up because the
`patch_ids` list is authoritative) rather than a dangling reference to a missing
patch file. Each individual write is atomic (the storage backend's tmp → fsync →
rename discipline; 07-runtime-and-durability.md §"The atomic-write contract").
`experiment.json` is stamped with `RECORD_FORMAT_VERSION` at write and checked
with `check_record_format` at read (§3.10); the reader accepts BOTH the new
per-patch shape (`patch_ids`) and the legacy inline shape (`patches: [...]`) for
back-compat.

`update_experiment_outcome` re-reads the experiment, `replace`s only its
`outcome`, and rewrites `experiment.json` (the patch files are NOT rewritten —
the same `patch_ids` list rides along). This is the tournament's settle-time
write: the proposer landed the experiment with `outcome=None`; the tournament
fills the outcome and re-journals in one swoop.

`append_journal_entry` renders one markdown section per experiment (`journal.md`)
— the running narrative operators read via `zicato journal show`. The section is
appended before the run (hypothesis landed) and again after (verdict): "appending
twice is fine — operators see the proposal then the verdict." The append is a
read-modify-write of the whole text through the atomic `write_text`, so a crash
leaves the prior journal intact rather than a truncated file.

> ⛔ NEVER write `experiment.json` before its patch files. The patches-first
> order is the invariant that makes a torn write recoverable: a body that
> references a patch id whose file was never written is unreadable, but an orphan
> patch file no `patch_ids` list points at is inert. If you add a per-generation
> record with internal references, write the referenced-to files first and the
> referencing index last — the same discipline `write_experiment` uses.

### 3.9.3 The `Generation` node and cross-epoch seeding

A `Generation` (`src/zicato/core/epoch.py`) is one node in an epoch's lineage —
the typed companion to a `lineage.json` entry. Its two subtle fields:

- `promoted` — `True` iff crowned; the epoch's current head is the most-recent
  promoted generation, and `promoted=False` generations are dead branches kept for
  analysis. On disk the lineage node carries the tri-state (`True`/`False`/`null`,
  §3.9); the `Generation` dataclass default is `False`, which is exactly why an
  in-flight append must pass `pending=True` to persist `null` instead.
- `round_index` — the evolve round that MINTED this generation, its BIRTH round.
  Zero-based; the epoch's genesis seed `v0` is round `0`. A champion carried into
  later rounds keeps its birth round — "it is NOT re-stamped each round it
  defends." Consumers group an epoch's generations as `Epoch → Round →
  {challengers minted that round}`; a mis-stamped `round_index` breaks that
  grouping (§3.9's trap).

The cross-epoch edge is what a roll (§3.8.3) rides. When `ensure_epoch_for_contract`
rolls, it resolves the closed epoch's promoted head snapshot
(`_promoted_head_snapshot` reads the `current_generation` marker and returns that
generation's `snapshot/` dir) and writes a roll-seed marker so the new epoch's
first `evolve` seeds its `v0` from that snapshot — the new lineage's baseline is
the old lineage's best, not a from-scratch reset. If the closed epoch has no
promoted generation beyond an unrun seed (or the snapshot dir is empty), the
resolver returns `None` and the caller falls back to seeding from the registered
mutable trees.

> ⚠️ TRAP — a roll opens a **fresh, incomparable contract**, and generation ids
> restart at `v0`/`v1` in the new epoch (§3.13's trap). Cross-epoch continuity is
> carried by the SNAPSHOT (the promoted head's source tree becomes the new `v0`),
> NOT by the generation id or any per-epoch scalar — the numbers do not transfer
> across a roll (the same reason cross-epoch experiment memory carries directions
> only, never deltas; 05-proposer.md §"Cross-epoch memory"). Seeding the tree
> warm-starts the search; it does not make the two epochs' scalars comparable.

---

## 3.10 Record-format versioning — refuse-on-newer

Three canonical JSON records carry a `format_version`: each epoch's
`config.json`, every `experiment.json`, and `lineage.json`. The version is
stamped at write time and checked at read time (`src/zicato/epoch/_storage.py`):

```python
# src/zicato/epoch/_storage.py — check_record_format
    raw = body.get("format_version")
    if raw is None:
        return  # pre-stamp record — version 1 by definition this release
    if isinstance(raw, int) and not isinstance(raw, bool) and raw == RECORD_FORMAT_VERSION:
        return
    raise RecordFormatError(
        f"{record_name}: format_version {raw!r} is not readable by this "
        f"zicato (expects {RECORD_FORMAT_VERSION}); the record was written "
        "by an incompatible (likely newer) version — upgrade zicato rather "
        "than letting an old reader misinterpret it"
    )
```

The rules (`RECORD_FORMAT_VERSION = 1` on this branch):

- **absent ⇒ version 1** — every pre-stamp workspace / fixture keeps loading;
- **equal ⇒ fine**;
- **higher ⇒ refuse** — a record written by a newer zicato whose shape this
  build cannot promise to interpret. The reader raises `RecordFormatError` with
  upgrade guidance rather than silently misreading a future shape.

There are **NO migration shims**; bumping the constant is a deliberate format
break. `list_epochs` treats a torn in-progress write (unparseable JSON) as a
silent skip, but a future-`format_version` record is a LOUD refusal — "unlike a
torn in-progress write, the record is intact and the operator must know why it
won't load."

> ⛔ NEVER bump `RECORD_FORMAT_VERSION` for an *additive* change. Adding an
> optional field that reads back as a default (the `noise_floor` / `preflight` /
> `goal` pattern, §3.6) is backward-compatible and needs no bump. Bump ONLY for
> a change that would make an *old reader misinterpret* a record — a renamed key
> an old reader would read as absent-and-default, a changed value semantics.
> Because there are no migration shims, a bump is a hard break: every older
> zicato refuses every record the new one writes. This is invariant #7; see
> 07-runtime-and-durability.md §"Record format versioning" for the storage-wide
> view (D12).

> ⚠️ TRAP — the SQLite index has its OWN versioning (`user_version`) and its own
> rule: a newer `user_version` is never re-stamped down (07-runtime-and-durability.md
> D12). Do not conflate the two. `format_version` guards canonical *file* records;
> `user_version` guards the derived *index*. A file record is authoritative; the
> index is a rebuildable projection. When you add a versioned record, use
> `check_record_format` at every read seam that reconstructs it, exactly as
> `load_epoch`, `read_experiment`, and `_load_raw` (lineage) do.

---

## 3.11 FLAGSHIP RECIPE: Add a contract knob end-to-end

Goal: add a new operator-tunable knob that is part of the frozen evaluation
contract — so a non-default value rolls the epoch, and a default value never
rolls one already on disk. This is the highest-blast-radius change class in the
subsystem: get the omit-at-default or serializer step wrong and you mass-roll
the fleet (§3.4) or silently compare incomparable generations (§3.5).

The worked example is a hypothetical nested `ProposerQualityConfig` knob, so we
can lean on the **screening-scaffolded-vs-exemplars-NOT-scaffolded asymmetry**
(step 5) — the real, shipped decision about whether the scaffold sets a knob.

1. **Add the dataclass field + validation.** Put the field on the right
   dataclass — a scoring weight on `ScoringWeights`, a proposer-quality lever on
   `ProposerQualityConfig`, an overfitting control on `OverfittingConfig`
   (`src/zicato/core/scoring_config.py`). Give it a default that makes the
   feature *inert* (`0` / `False` / `""` / an all-default nested instance).
   Validate in `__post_init__` (`raise ValueError` on out-of-range) — the
   dataclass is constructed at contract load, so a bad value is rejected there,
   not deep in a run. Write a docstring that states the default's behavior AND
   the omit-at-default + epoch-roll semantics (the existing `screen_entries` /
   `process_exemplars` docstrings are the model).
   **Verify:**
   ```bash
   uv run python -c "from zicato.core.scoring_config import ProposerQualityConfig as C; C(<field>=<bad>)"  # expect ValueError
   uv run pytest tests/test_scoring_config.py -q
   ```

2. **Register omit-at-default (if the default is inert).** Add the field NAME to
   `_SCORING_OMIT_AT_DEFAULT_FIELDS` (`src/zicato/epoch/contract.py`). For a
   nested-config field this is all that is needed — `scoring_to_canon` recurses
   and matches the name at any depth (§3.4). Add a comment naming what it opts
   into, mirroring the existing entries. If the default is *behaviorally active*
   (like `best_of_n = 3`), do NOT register it — it must always appear in the
   canonical form.
   **Verify:**
   ```bash
   uv run python -c "from zicato.core.types import ScoringWeights as W; from zicato.epoch.contract import scoring_to_canon; assert '<field>' not in scoring_to_canon(W())"
   ```

3. **Add the serializer-completeness guard-table entry.** Add a curated
   non-default value for the field to `_NONDEFAULT_VALUES` in
   `tests/test_contract_serializer_completeness.py` (under the field's class).
   The value must be constraint-VALID (the dataclass validates on construct) and
   genuinely different from the default (the test asserts non-vacuousness). This
   is what makes the structural round-trip + no-roll guards cover your field.
   **Verify:**
   ```bash
   uv run pytest tests/test_contract_serializer_completeness.py -q
   ```

4. **Confirm the from_json round-trip.** Nothing extra to write — `to_json` /
   `from_json` are field-enumerating (§3.5) — but prove
   `ScoringWeights.from_json(w.to_json()) == w` for your non-default value, and
   that the lifecycle parser and the loader agree (a divergence would hash the
   frozen and live contracts differently).
   **Verify:**
   ```bash
   uv run pytest tests/test_contract_serializer_completeness.py \
       -k "round_trip or lifecycle or loader" -q
   ```

5. **Make the scaffold decision — the asymmetry.** Decide whether
   `recommended_scaffold_weights` (`src/zicato/core/scoring_config.py`) sets your
   knob. The rule is a real distinction, not taste:
   - an **evaluation-side** knob that only changes how candidates are *measured*
     may be scaffolded — the shipped precedent is the candidate screen, which the
     scaffold enables explicitly:
     ```python
     # src/zicato/core/scoring_config.py — recommended_scaffold_weights (tail)
             proposer_quality=ProposerQualityConfig(screen_entries=2),
         )
     ```
   - a knob that **widens what the proposer can see** of the board (the
     overfitting boundary — 05-proposer.md §"The restricted-visibility envelope")
     must NOT be scaffolded. The shipped precedent is `process_exemplars`: it is
     "evaluation-side, while exemplars widen the proposer-visibility channel, so
     the operator opts in deliberately under the doc's §5 harm-detection runbook"
     (`ProposerQualityConfig.process_exemplars` docstring). It defaults `0` and
     the scaffold leaves it `0`.
   In both cases the in-code default stays OFF; only the scaffold (what a NEW
   workspace's `scoring.json` spells out) differs. If your knob touches proposer
   visibility, treat it like `process_exemplars`: design-note first (§3.11 is not
   enough — 14-goals-and-roadmap.md §"Design-first zones"), and leave the scaffold
   alone.
   **Verify:**
   ```bash
   uv run pytest tests/test_scoring_config.py -k "scaffold" -q
   uv run python -c "from zicato.core.scoring_config import recommended_scaffold_weights as s; print(s().proposer_quality)"
   ```

6. **Wire the builder op + GUI + copilot.** Operators set contract knobs through
   the tournament builder, not by hand-editing `scoring.json`. There are three
   surfaces that COMPOSE on the same nested block (so they never clobber each
   other):
   - the op in `src/zicato/builder/operations.py` (`set_proposer_quality` /
     `set_screening` are the models — each `dataclasses.replace`s only its keys
     on the nested `proposer_quality` block and returns a `DraftPatch`);
   - the JSON dispatch in `src/zicato/builder/api.py` (the `op ==
     "set_screening"` / `"set_proposer_quality"` branches) — this is the GUI /
     settings-panel entry point;
   - the copilot tool wrapper in `src/zicato/builder/copilot_tools.py` (so the
     chat copilot can drive the knob), registered in that module's `__all__`.
   **Verify:**
   ```bash
   uv run pytest tests/test_builder_operations.py tests/test_builder_api.py \
       tests/test_builder_copilot_tools.py -q
   ```

7. **Answer the cost-meter question.** Does your knob change how many board runs
   a round costs? The builder's `estimate_cost` (`src/zicato/builder/operations.py`)
   prices the round; a read-side-only knob (like `process_exemplars`, which only
   adds prompt content) leaves it untouched, while an evaluation-side knob (like
   `screen_entries`, which adds `proposes × best_of_n × panel` panel runs) MUST
   add a `CostLine` so the operator sees and prices the extra spend before opting
   in. Decide explicitly which yours is; if it adds runs, add the line and label
   auxiliary-LLM-call costs separately from the board-runs headline (the existing
   `candidate-screen runs` vs `best-of-N propose calls` split is the model).
   **Verify:**
   ```bash
   uv run pytest tests/test_builder_operations.py -k "cost or estimate" -q
   ```

8. **Add the contract-hash byte-identity tests.** In
   `tests/test_epoch_contract.py`, pin BOTH halves against the on-disk
   `scoring.json` shape (not just the dataclass): a contract at the default
   hashes identically to one that omits the field
   (`test_hash_stable_when_screening_fields_at_default` is the model), and any
   non-default value rolls it (`test_hash_changes_when_screening_opted_in` is the
   model). Test through `compute_contract_hash` over a written `scoring.json`, so
   you exercise the full parse → `ScoringWeights` → canon path.
   **Verify:**
   ```bash
   uv run pytest tests/test_epoch_contract.py -q
   ```

9. **CHANGELOG the behavior-affecting default (if any).** If your change alters a
   *default* that shipping workspaces do not pin, it is a BREAKING default: those
   workspaces auto-roll on the next `evolve`. Add a `CHANGELOG.md` entry under
   the existing `⚠️ BREAKING DEFAULTS` idiom, stating exactly which knob to pin to
   keep an epoch byte-stable (the noise-aware-defaults and bug-#10 entries are the
   template — loud, declared, with the pin spelled out). A purely-additive
   default-off knob (the common case) is NOT breaking and needs only a normal
   changelog line.
   **Verify:**
   ```bash
   grep -n "<your-knob>" CHANGELOG.md   # the entry exists
   uv run pytest tests/test_epoch_contract.py tests/test_contract_serializer_completeness.py -q
   ```

**The whole-recipe verification** (run before you open the PR):

```bash
uv sync --all-extras
uv run pytest tests/test_epoch_contract.py \
    tests/test_contract_serializer_completeness.py \
    tests/test_scoring_config.py tests/test_builder_operations.py -q
uv run ruff check src/zicato/core/scoring_config.py src/zicato/epoch/contract.py
uv run mypy src/zicato/core/scoring_config.py src/zicato/epoch/contract.py
# + the per-branch vendor scan: no model-vendor names, ids, or trailers
# anywhere in the diff or commit message (14-goals-and-roadmap.md §"Anti-goals")
```

If you skip step 2, every existing epoch rolls on upgrade (invariant #2 broken).
If you skip step 3 or 8, a future field that drops in serialization rolls the
fleet silently (issue #13, invariant #3). If you skip step 5's asymmetry and
scaffold a visibility-widening knob, you default-enable an overfitting channel
the operator never consented to (05-proposer.md §"The channel-author's
checklist").

---

## 3.12 MIRROR RECIPE: Add a runtime-only knob

Goal: add a knob that tunes *how the loop runs* without changing *what it
measures* — so it must NEVER roll the epoch. This is the mirror of §3.11: same
"add a knob" shape, opposite contract answer. The home for it is `RuntimeConfig`
(`src/zicato/core/runtime.py`), which is assembled per-`evolve` from the
workspace-config `runtime` block and CLI flags — it is not part of the frozen
contract and is not serialized into any epoch snapshot.

The shipped precedents are `diversity_tolerance`, `infra_abort_round_threshold`,
and `max_tokens_per_round` — each docstring says the same thing verbatim: "A
RUNTIME tuning knob, NOT part of the frozen evaluation contract — flipping it
does not roll the epoch."

1. **Add the field to `RuntimeConfig`** (`src/zicato/core/runtime.py`) with an
   inert default and a docstring that states, in these words, that it does not
   roll the epoch. Validate only cheap scalar bounds in `__post_init__` (the
   dataclass keeps the two-callable identity check out of `__post_init__` by
   design — do not add expensive validation there).
   **Verify:**
   ```bash
   uv run pytest tests/test_runtime_config.py -q
   ```

2. **Parse it in `make_runtime_config`** (`src/zicato/runtime_factory.py`) from
   the `runtime` block of the workspace config — never from `scoring.json`. The
   existing knobs are the model:
   ```python
   # src/zicato/runtime_factory.py — make_runtime_config (runtime block)
       tolerance_raw = runtime_dict.get("diversity_tolerance")
       diversity_tolerance = float(tolerance_raw) if tolerance_raw is not None else None
   ```
   **Verify:**
   ```bash
   uv run pytest tests/test_runtime_factory.py -q
   ```

3. **Do NOT touch any contract path.** No `scoring_config.py` field, no
   `_SCORING_OMIT_AT_DEFAULT_FIELDS` entry, no `scoring_to_canon` change, no
   guard-table row. If you find yourself editing `contract.py`, stop — the knob
   is in the wrong place.
   **Verify (the negative test — the contract hash must be blind to it):**
   ```bash
   uv run pytest tests/test_epoch_contract.py -q   # unchanged; your knob is invisible here
   ```

### The choose-which decision table

Before you add ANY knob, answer one question: *does its value change what a
promotion means?* If two generations crowned under different values of the knob
are still directly comparable, it is runtime; if not, it is contract.

| Question | Contract knob (§3.11) | Runtime knob (§3.12) |
|---|---|---|
| Does it change what/how candidates are *measured* or *selected*? | yes | no |
| Are generations across two values still directly comparable? | **no** — must roll | **yes** — must not roll |
| Home | `ScoringWeights` / nested config | `RuntimeConfig` |
| Source of value | frozen `scoring.json` (per-epoch) | workspace-config `runtime` block + CLI flags (per-run) |
| Folds into `contract_hash`? | yes (unless omitted-at-default) | never |
| Serializer guard | `_NONDEFAULT_VALUES` completeness table | — |
| Examples | `promote_margin`, `screen_entries`, `best_of_n`, `holdout_fraction`, `tournament_structure`, a grading `scalar_fn` | `parallelism`, `diversity_tolerance`, `infra_abort_round_threshold`, `max_tokens_per_round`, `scrub_worker_env` |

The tell: `parallelism` (how many boards run at once), `max_tokens_per_round` (a
scheduling budget), and `diversity_tolerance` (a field-dedup ceiling) all change
*how fast / how cheaply* a round runs, but a challenger crowned under
`parallelism=4` is exactly as comparable to one crowned under `parallelism=8` as
two crowned under the same value — the *rule of comparison* is untouched. That is
the invariant-#5 line: runtime knobs describe execution, contract knobs describe
comparison.

> ⛔ NEVER put a knob on `RuntimeConfig` if changing it makes two generations
> incomparable. `diversity_tolerance` is a borderline-looking case that resolves
> correctly: it *soft-rejects* overlapping siblings from a field, changing which
> experiments run — but every experiment that DOES run is still scored under the
> same contract, so the promoted generation is comparable across tolerance
> values. If your knob changes the *scoring or gating rule* (what counts as a
> win), it is a contract knob no matter how "operational" it feels — put it on
> `ScoringWeights` and roll the epoch.

> ⚠️ TRAP — a `RuntimeConfig` knob does NOT get a `format_version` bump, a
> CHANGELOG BREAKING entry, or an epoch-roll, but it DOES need a byte-identical
> default: a workspace that omits the `runtime` key must run byte-for-byte as
> before (`make_runtime_config` defaults the whole block to `{}`). The
> `scrub_worker_env=False` / `max_tokens_per_round=0` / `infra_abort_round_threshold=0`
> defaults are all "OFF ⇒ today's behavior exactly" for this reason. An inert
> default is the runtime-side analogue of §3.4's byte-identical-at-default.

---

## 3.13 One epoch, create to roll (worked trace)

The full sequence under stock defaults — `zicato register` has recorded the
contract in `config.json`, and the operator runs `zicato evolve` — annotated
with the file that owns each step. Read this once before your first
contract-adjacent change; every trap in this chapter appears in situ here.

```
first `evolve` on a fresh workspace (no current_epoch marker):
  ensure_epoch_for_contract(auto_epoch=True)                 evolve/epoching.py
    inputs = resolve_contract_inputs(root)                   # from config.json
      board=<parent>/board.jsonl  brief=<parent>/brief.md    # LIVE copies (§3.1)
      scoring=<parent>/scoring.json  entrypoint  mutable_trees  proposer_path
    cur = current_epoch_id(root)  → None
    _create_epoch_from_contract(name="e0"):
      new_epoch(root, "e0", board, brief, weights, …):        epoch/lifecycle.py
        edir = epochs/2026-…_e0/  (mkdir exist_ok=False)
        materialize FROZEN board.jsonl / brief.md / scoring.json into edir
        contract_hash = compute_contract_hash(               epoch/contract.py
            ContractInputs(FROZEN paths, entrypoint, mutable_trees, proposer))
          _canon_board  _canon_brief  _canon_scoring          # §3.2
          _canon_entrypoint  _canon_mutable_trees  _canon_proposer
        write config.json (format_version=1, contract_hash, noise_floor=null)
        register_epoch(lineage.json, v0_parent=None)          epoch/lineage.py
        switch_epoch → current_epoch = "2026-…_e0"
    _write_component_hashes(e0, compute_component_hashes(inputs))
  write_seed_experiment(root, e0, "v0")                       # §3.9.1
  → round 0 proposes v1, tournament settles, append_to_lineage(v1, promoted=…)

second `evolve`, operator edited scoring.json (bumped promote_margin):
  ensure_epoch_for_contract(auto_epoch=True)
    inputs = resolve_contract_inputs(root)                   # LIVE scoring.json
    current_hash = compute_contract_hash(inputs)             # DIFFERENT now
    cur = "2026-…_e0"
    cfg = load_epoch(e0);  cfg.contract_hash != current_hash # not None, drifted
    → auto-roll:
      close_epoch_async(e0)  → analysis.md + mark closed      epoch/lifecycle.py
      restamp_persisted_report(e0)                            # deterministic
      new_epoch("e1", …)  → fresh contract_hash, seeded from e0's promoted head
      _write_component_hashes(e1, …)
      roll_seed_marker(e1) = e0's _promoted_head_snapshot     evolve/epoching.py
      drain_stale_gate_overrides()                            # v3 → v0 mis-fire
      print "contract changed (scoring) — rolled 2026-…_e0 -> 2026-…_e1"
      log "epoch e1 opened by auto-roll with no goal recorded; set-goal …"
  → round 0 of e1 proposes v1 (generation ids restart at v0/v1)
```

Points where the trace changes under non-default inputs:

- `--no-auto-epoch` on the drift path → `ensure_epoch_for_contract` RAISES a
  `RuntimeError` naming the changed component (`scoring`) instead of rolling;
  the operator reverts `scoring.json` or runs `zicato epoch new` (§3.8.3).
- a **whitespace-only** edit to `brief.md` (CRLF churn, trailing spaces) →
  `_canon_brief` normalizes it away, `current_hash` is UNCHANGED, no roll
  (§3.2.2).
- a **legacy** workspace (its `e0` predates auto-epoching, stored hash `None`) →
  the drift check short-circuits at `is None`, never rolls (§3.8.4), whatever
  the live files say.
- an edit to a **grading plugin body** the scoring `scalar_fn` points at, with
  the dotted string unchanged → `_canon_scoring` → `_canon_dotted_spec` folds
  the new source hash, `current_hash` MOVES, the epoch rolls (§3.3).
- `zicato board audit` between the two evolves → writes `noise_floor` onto e0's
  `config.json` via `set_epoch_noise_floor`; `current_hash` is UNCHANGED (a
  measurement never hashes, §3.6.1), so it does NOT trigger a roll.

> ⚠️ TRAP — generation ids restart at `v0`/`v1` in the new epoch. A pending gate
> override (or any bookkeeping keyed on a bare `v3`) written under `e0` would
> mis-fire on `e1`'s same-named generation, which is why the roll DRAINS stale
> overrides (`drain_stale_gate_overrides`). If you add state keyed on a bare
> generation id, decide what a roll does to it — the roll opens a fresh,
> incomparable contract, so per-epoch state must not survive it silently.

---

## 3.14 Cross-references

- 01-orientation.md §"Workspace layout" — where `epochs/{id}/`, `current_epoch`,
  `lineage.json`, and the operator's live contract files sit.
- 02-architecture.md §"The evolve round" — where `ensure_epoch_for_contract`
  runs in the round pipeline.
- 04-evaluation-statistics.md §"The A/A noise floor" — what `noise_floor`
  measures; §"Contract pre-flight" — what `preflight` records. Both are the
  never-hashed measurements of §3.6.
- 05-proposer.md §"`_canon_proposer` and skills" — the proposer component in
  full, and §"Why tools do NOT fold into the contract hash" (the registry
  argument behind §3.2.6); §"The restricted-visibility envelope" — the
  overfitting boundary the scaffold asymmetry (§3.11 step 5) protects.
- 06-tournament-and-selection.md §"The tournament structure" — how
  `tournament_structure` (a nested `ScoringWeights` field) folds into the
  contract via the same recursion as `overfitting` (§3.2.3).
- 07-runtime-and-durability.md §"Record format versioning" (D12) — the
  storage-wide view of §3.10; §"The generation store" — what a generation
  snapshot the lineage nodes point at actually is.
- 10-builder-cli-library.md §"Builder operations" — the `set_screening` /
  `set_proposer_quality` surface the flagship recipe's step 6 wires into.
- 12-bug-casebook.md §"Case 10" — the contract-hash-embeds-checkout bug behind
  invariant #1 and §3.2.5; §"The meta-lessons" (M1) — the shared-mutable-state
  class the lineage tri-state (§3.9) avoids.
- 14-goals-and-roadmap.md §"Design-first zones" — why a contract-hash or
  overfitting-boundary knob needs a design note before code.

---

## 3.15 Test map for the subsystem

Where to add (and what will catch) a regression, by concern:

| Concern | Tests |
|---|---|
| hash stability (whitespace / reorder / float noise) + sensitivity (board / scoring / entrypoint / mutable trees) | `tests/test_epoch_contract.py` |
| bug-#10 cwd/checkout invariance + path-spelling normalization | `tests/test_epoch_contract.py::test_contract_hash_is_cwd_and_checkout_invariant` |
| board-level meta (judges / disable_drift) + per-entry grading source folding | `tests/test_epoch_contract.py` (`test_canon_board_meta_*`, `test_canon_judges_*`) |
| proposer component: skill body / add / remove / rename / agent source / whitespace / fs-reorder / builtin-vs-dir | `tests/test_epoch_contract.py` (`test_proposer_*`) |
| omit-at-default (screening / experiment_memory) omit == explicit-default, opt-in rolls | `tests/test_epoch_contract.py`, `tests/test_contract_serializer_completeness.py::test_experiment_memory_omitted_from_canon_at_default` |
| serializer completeness (no dropped field, round-trip identity, no spurious roll) — structural, covers future fields | `tests/test_contract_serializer_completeness.py` |
| lifecycle round-trip (parser + loader agree) | `tests/test_contract_serializer_completeness.py` (`test_scoring_lifecycle_round_trip_every_field`, `test_lifecycle_parser_and_loader_agree`) |
| `resolve_contract_inputs` (config keys, legacy `rubric_path`, defaults, proposer_path) | `tests/test_epoch_contract.py` (`test_resolve_contract_inputs_*`) |
| epoch lifecycle (new / close / list / switch, id construction, stub analysis) | `tests/test_epoch_lifecycle.py` |
| auto-roll decision (legacy no-roll, drift rolls, `--no-auto-epoch` raises, component label, override drain) | `tests/test_evolve_epoching.py`, `tests/test_orchestrator*.py` |
| lineage DAG (register / append / upsert / pending tri-state / summary) | `tests/test_epoch_lineage.py` |
| record-format guard (absent ⇒ v1, higher ⇒ refuse) | `tests/test_epoch_storage.py`, and the guard's callers in `tests/test_epoch_journal.py` / `tests/test_epoch_lifecycle.py` |
| journal + experiment persistence (per-patch layout, seed marker, outcome update) | `tests/test_epoch_journal.py` |
| source-hash folding (edit-the-body rolls) | `tests/test_scoring_plugins.py`, `tests/test_epoch_contract.py` (predicate/judge source rows) |
| runtime-knob-does-not-roll (the §3.12 negative) | `tests/test_runtime_config.py`, `tests/test_runtime_factory.py` |
