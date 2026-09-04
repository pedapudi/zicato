# Mutation surface

The mutation surface is the set of source locations in the system
under test that zicato is allowed to rewrite. It is **annotated** rather than
free-form: every mutable location is marked in the source with a
comment-form marker. The patch proposer addresses mutations by stable id;
the applier resolves each id to one location and rewrites only what
the id covers.

The contract covers:

- The three marker forms (span-level, file-level, region-level) and
  their syntax, in Python and in any other text file (§2).
- The AST resolution rules that map a marker to a source location.
- The shape of `MutationPoint` and the
  `HarnessAdapter.mutation_points()` protocol method.
- The `zicato inspect mutations` audit CLI.
- The applier's validator constraints.
- The interaction with the proposer brief's `## Forbidden` list.

The argument for requiring annotation rather than allowing free-form edits
lives in [RATIONALE.md](RATIONALE.md); this document is the contract.

## 1. Why annotated

A meta-harness has a safety-vs-reach trade-off:

- **Free-form source edits** maximise reach (the proposer can change
  anything) at the cost of safety (the proposer can break anything).
  Validating "this still works" against a live system — a multi-agent
  tree, a library, anything with real behaviour — is extremely hard.
- **Pure span-level annotated mutations** maximise safety (only marked
  strings move) at the cost of reach (related strings in the same
  file may need to move together but cannot be addressed as a group).

zicato admits three marker forms between those extremes. Span markers are
the default. A region
marker covers a bounded run of lines — a block of control flow, a prompt
body, a config stanza. A file marker covers the cases where a whole
module — e.g. a `prompts.py` with several closely-related templates —
should move as one unit. All three forms produce `MutationPoint`s with
stable ids; all three are addressed by the proposer through those ids;
none of them lets the proposer rewrite an unmarked file.

Widening the surface beyond `.py` (§2.4) does not relax this. It changes
*where a marker may live*, never whether one is required. An unmarked
`config.yaml` is as immutable as an unmarked `agent.py`.

## 2. Marker syntax

Markers are comments in whatever language hosts them. In a `.py` file
the walker pairs them with the AST, so a span marker can bind to the
string-literal node beneath it; in any other text file they resolve by
line position (§2.4). §2.1–§2.3 describe the three forms in their Python
spelling; §2.4 gives the general grammar.

### 2.1 Span marker

A span marker is a comment on the line immediately above a
**string-literal assignment** or a **string-valued keyword argument
inside a call**. The comment has the form:

```
# zicato:mutable id="<stable-id>" [key="value" ...]
```

`id` is required and globally unique within a single registration.
Any trailing `key="value"` pairs are parsed verbatim into the
`MutationPoint.metadata` mapping; they have no effect on AST
resolution. The conventional keys the validator and proposer act on
are `required_placeholders` (comma-separated f-string-style
placeholders the rewritten content must preserve), `min` / `max`
(numeric bounds for `set_numeric` patches), `enum` (a comma-separated
closed domain for `set_enum` patches), and free-form documentation
keys such as `language` / `role`.

Two examples:

```python
# Specialist's system prompt — coordinator routes user research turns here.
# zicato:mutable id="researcher_instruction"
INSTRUCTION = """You research the user's question by ..."""

researcher = LlmAgent(
    name="researcher",
    # zicato:mutable id="researcher_description" role="tool_description"
    description="Performs literature lookup and source aggregation.",
    instruction=INSTRUCTION,
    ...
)
```

In the first form the marker covers the right-hand side of the
assignment. In the second the marker covers the keyword argument's
value expression. Both cases resolve to a single string-literal node
the applier can rewrite.

### 2.2 File marker

A file marker is a comment in the header region of a file (module
docstring above, marker below, no statements before it). The `:file`
suffix attaches to the marker prefix rather than standing as a separate
`file` word:

```
# zicato:mutable:file id="<stable-id>" [key="value" ...]
```

Example:

```python
"""Specialist prompts for the presentation agent."""

# zicato:mutable:file id="presentation_agent_prompts"

INTRO = "..."
OUTLINE = "..."
REVISION = "..."
```

When a file marker is present, the proposer may propose a `replace`
patch whose target is the file id and whose `new_content` is the
entire post-edit contents of the file. The applier writes the file in
full, then runs every validator constraint on it.

A file with a file marker MAY also carry span markers for finer-grained
targets. Both are emitted by `mutation_points()`; the proposer chooses
the granularity that fits the change.

### 2.3 Region marker

A region marker brackets a run of lines. The opening marker carries the
`:code` suffix and an id; a bare `:end` sentinel closes it:

```
# zicato:mutable:code id="<stable-id>" [key="value" ...]
...the mutable lines...
# zicato:mutable:end
```

The point's content is the lines **strictly between** the two markers.
The marker lines themselves are outside the mutable range and the applier
strips any the proposer echoes back, so a patch can neither move nor
delete its own anchors — which is what keeps the id resolving from one
generation to the next.

```python
def make_slug(title):
    # zicato:mutable:code id="slug_logic"
    slug = title.lower()
    slug = slug.replace(" ", "-")
    # zicato:mutable:end
    return slug
```

In a `.py` file the region body is real Python control flow — this is how
a block of logic becomes mutable surface without exposing the whole file
and without wrapping the body in a string literal. The applier owns the
indent: the replacement is dedented to a common baseline and re-anchored
to the region's column, so a proposer working from a truncated preview
cannot shift the block off its suite. An unterminated region (no `:end`
before EOF) is dropped.

Outside Python the same form does the same thing over that file's own
content — see §2.4.

### 2.4 Markers in non-Python files

A mutable tree is not a Python-only tree. A prompt can live in
`prompts/researcher.md`, a policy in `config/runtime.yaml`, a tool
manifest in `tools.toml`. The marker *token* is the same everywhere;
only the host language's comment lead-in changes.

**Accepted lead-ins.** Per file type, from the syntax table (§2.5). The
built-in text entries accept `#` and `<!--`, with a trailing `-->` closer
tolerated, so both of these are the same marker:

```yaml
# zicato:mutable:code id="retry_policy"
```
```markdown
<!-- zicato:mutable:code id="researcher_brief" role="prompt" -->
```

Nothing is carried speculatively, and nothing waits on a zicato release
either: a target whose surface is TypeScript declares `.ts` with leaders
`//` and `/*` in its own contract, and its markers parse from that epoch
on.

`.py` files are parsed under a `#`-only grammar and nothing else. That
restriction makes "widening the surface moves zero Python points" a
property of the grammar rather than a claim a test has to keep
re-establishing.

**Which forms carry over.** Two of three:

| Form | In a `.py` file | In any other text file |
|---|---|---|
| `:file` | whole file | whole file — identical |
| `:code` | region between the markers | region between the markers — identical |
| bare span | binds to the nearest string literal beneath | **not supported**; warns and contributes nothing |

A bare span marker has no meaning without a parser: "the nearest string
literal beneath" is an AST fact. Binding instead to the next non-blank line
would be unsafe, because a `replace` against `temperature: 0.7` would
swallow the `temperature:` key the operator meant to keep. The enumerator
therefore refuses a bare span marker outside Python, and names it in the
refusal:

```
enumerator: config/runtime.yaml:12 declares span marker id='temperature',
but span markers bind to a Python string literal and runtime.yaml is not a
Python file; use the region form (zicato:mutable:code ...
zicato:mutable:end) or zicato:mutable:file instead. This marker
contributes no mutation point.
```

**The region form covers most non-Python surface** and carries the safety
argument. Both anchors are explicit, both are written by the operator, and
both sit *outside* the mutable range:

```yaml
runtime:
  # zicato:mutable:code id="retry_policy"
  retries: 3
  backoff_seconds: 1.5
  # zicato:mutable:end
  owner: operator          # <- not mutable, and cannot become mutable
```

The point's `content` is the lines strictly between the markers. Three
properties follow, and each is pinned by a test:

1. **A patch cannot escape.** The applier rebuilds the file as
   `everything before the region` + `replacement` + `everything after`.
   The surrounding text is never in play.
2. **A patch cannot eat its own markers.** Marker lines are stripped from
   the replacement body before it is written, so a proposer that echoes
   `<!-- zicato:mutable:end -->` back — or tries to open a *new* region —
   has those lines dropped rather than honoured. The anchors survive, so
   the id still resolves next round.
3. **Relative indentation survives.** The replacement is dedented to a
   common baseline and re-anchored to the region's own indent, so a
   proposer that emits at column 0 still lands correctly inside a nested
   YAML block, and nested structure is not flattened.

A `:file` marker gives up property 2 by definition, because a whole-file
replace *can* delete the marker. The post-apply id-resolution check (`A2`)
catches that after the fact and rejects the snapshot.

**JSON.** Strict JSON has no comment syntax, so there is no way to write a
marker in a `.json` file without invalidating it. JSON is therefore not
walked at all.

### 2.5 Which files are walked

Discovery is a **declared suffix table** rather than a content sniff. The
built-ins (`zicato.mutation.markers.BUILTIN_SYNTAXES`) are `.md` ·
`.markdown` · `.txt` · `.yaml` · `.yml` · `.toml` — prompts and config,
the two shapes the surface exists for — plus the reserved `.py`. Every
other file type the operator DECLARES, in the contract's
`mutation_surface` table:

```json
{
  "mutation_surface": {
    ".ts":  {"leaders": ["//", "/*"], "trailers": ["*/"]},
    ".sql": {"leaders": ["--"]}
  }
}
```

Each entry names the comment leaders a marker may be written under and,
optionally, the block closers tolerated at end of line. The table folds
over the built-ins: an entry for a built-in suffix overrides it, and an
absent table means the built-ins alone.

**The leaders are the containment mechanism.** `zicato:mutable id="..."`
is nearly collision-proof for *discovery*, so an implementation could
enumerate any text file carrying the token and drop the table entirely.
But when the applier rebuilds a `:code` region it strips echoed marker
lines out of the replacement body (§6), and it can only do that under a
comment syntax it knows. In a file type with no declared leaders, the
proposer could insert a live `:end` marker into a region body. Declared
syntax is *enforceable* containment, which is why a leaderless entry is
rejected outright.

Two consequences follow from the table being contract:

- **Editing it rolls the epoch.** The table decides what is enumerable,
  hence what the proposer may rewrite, hence comparability across
  generations. It is folded into the contract hash, omitted at its empty
  default so that a workspace declaring nothing keeps the hash it has.
  Widening the surface is a material contract change and is recorded as
  one.
- **`.py` is reserved.** The table governs the text pass only; an entry
  for `.py` is an error. That reservation keeps "widening the surface moves
  zero Python points" a property of the grammar rather than a claim a
  test has to keep re-establishing.

Widening the envelope never gives the *proposer* new reach on its own:
patches carry no paths, only enumerated mutation ids. It lets the
*operator* declare more.

Two properties rule out the alternative of opening every file and guessing
from its bytes whether it is text. Such a walk reads *every* file in the
tree including binaries, and the enumerator re-runs after **every applied
patch**, so the walk sits on the hot path. A surface that decides by
guessing also has contents that can change because a file's first 8KB
changed. A suffix decides without opening the file and cannot reach a
binary at all.

Strict `.json` stays out: no comment can host a marker without
invalidating the document. A `.jsonc`-style type is one table entry.

Three further guards apply:

- Vendored / generated directories (`.git`, `node_modules`, `.venv`,
  `__pycache__`, `dist`, `build`, …) are pruned from the **text pass
  only**. Pruning the Python pass would change which `.py` files
  enumerate, and that set must not move.
- Text files over 2 MB are skipped. A multi-megabyte file in a mutable
  tree is data rather than surface.
- A file that is not valid UTF-8, or that contains a NUL byte, yields
  nothing — a second check behind the declared suffix.

A **single-file root** resolves by suffix: `.py` through the Python pass,
a declared suffix through the text pass, and anything else warns rather
than enumerating silently to zero points.

### 2.6 What markers do NOT do

- Markers do not declare any *constraints* on the new text. They mark
  a target; validation happens at apply time rather than at mark time.
- Markers do not carry the current value. The walker reads the value
  from the AST and stamps it onto the `MutationPoint` at enumeration
  time.
- Markers do not survive into the wire format. The JSONL event stream
  knows nothing about mutation points; this is a source-side concern
  only.
- Markers are not executable. They are comments — in whatever language
  hosts them. The Python interpreter, the YAML loader, and the markdown
  renderer all ignore them.

## 3. AST resolution rules

These rules govern the **Python pass**. For each registered source root
the enumerator walks every `.py` file, parses to AST, and walks the
nodes. The text pass (§2.4) needs none of this: it has no span form, and
its `:file` / `:code` forms resolve by line position alone.

**Span marker resolution:**

1. Marker lines are found by **line regex** rather than by `tokenize`:
   one pass over the file's lines, matching the leader + token + `id="..."`
   shape. The AST is used for exactly two things a regex cannot supply:
   the set of lines covered by a string literal, and the literal spans a
   marker can bind to.
2. A marker written **inside a string literal** (a docstring showing the
   syntax) is treated as documentation and contributes nothing. That is
   the first use of the literal-line set.
3. A span marker binds to the **nearest string literal beneath it** —
   the literal whose start line is the smallest line strictly greater
   than the marker's. The binding is not restricted to a fixed list of
   node types: assignment value, keyword argument, positional argument,
   and any other expression context all resolve, because the rule is
   about position rather than node shape.
4. The enumerator records **whole lines** (file, `line_start`,
   `line_end`) and the sliced text. Column precision lives in the
   applier, which re-resolves the exact literal node at apply time so a
   replacement never eats the `NAME =` in front of it (§6).
5. A span marker with **no literal beneath it** is dropped silently; the
   `zicato inspect mutations` listing is where the operator sees the id missing.

**Duplicate ids** are not rejected at enumeration — the enumerator emits
one point per marker and does not dedupe. They are rejected at
*validation*, and only for ids a patch actually targets: a patch can only
mean one location, so an ambiguous target fails the batch rather than
editing an arbitrary one of the colliding spans. A duplicate elsewhere in
the tree does not block an otherwise-clean batch.

**File marker resolution:** a `# zicato:mutable:file id="..."` line
anywhere in the file (not only a header region) makes the whole file one
point, with the file's full text as `content`. Nothing limits a file to
one file marker; two markers make two points, which the duplicate-id rule
then catches if they share an id.

**Unrecognized lines** are not markers and are skipped in silence — a
comment that merely resembles the syntax is not a typo to report. The
enumerator warns for the one case where intent is unambiguous and the
form is unsupported: a bare span marker in a non-Python file (§2.4).

## 4. `MutationPoint`

The enumerator returns a list of `MutationPoint` objects. The dataclass
shape:

```python
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

@dataclass(frozen=True, slots=True)
class MutationPoint:
    id: str                                 # globally unique within registration
    kind: Literal["span", "file", "code"]   # marker form ("code" = region)
    file: Path                    # absolute path to the source file
    source_root: Path             # which registered root this came from
    line_start: int               # 1-indexed inclusive
    line_end: int                 # 1-indexed inclusive
    content: str                  # current text of the region at enumeration
    content_hash: str             # hex SHA-256 of `content` (stale-write guard)
    metadata: Mapping[str, str] = field(default_factory=dict)
```

`content` is a snapshot — the next round will re-enumerate after
patches land, and `content` will reflect the new value. `content_hash`
is the hex-encoded SHA-256 of `content`; the applier checks it before
writing so a stale proposer round cannot clobber an already-rewritten
region. `metadata` is the bag of `key="value"` pairs lifted from the
marker line (e.g. `required_placeholders`, `min` / `max`, `enum`,
`role`). The id is stable across snapshots; the location may drift if
patches above it add or remove lines.

The dataclass is column-free: a span is recorded by its inclusive
`line_start` / `line_end` range rather than by character columns. There is
no separate `label` field — the marker's optional attributes all live in
`metadata`.

### Stability across generations

The id is the contract. The proposer addresses patches by id. The
applier resolves the id to a new location after every patch round.
The id MUST resolve to exactly one mutation point after a patch lands
— otherwise the patch is rejected at validation time (see §6).

This is why the marker syntax requires `id="..."` on every marker: an
id-less marker would be unaddressable from one generation to the next
and the proposer would have no way to refer back to it.

## 5. The `HarnessAdapter.mutation_points()` protocol

The `HarnessAdapter` protocol exposes mutation-point enumeration:

```python
from typing import Protocol

class HarnessAdapter(Protocol):
    async def run_entry(self, entry: BoardEntry, *, sinks: list[EventSink]) -> RunResult:
        ...

    def mutation_points(self) -> list[MutationPoint]:
        """Walk every registered source root and return every annotated
        mutation point. Idempotent; safe to call multiple times per
        round.
        """
        ...
```

### Walking multiple source roots

`mutation_points()` returns a list over the **registered list of source
roots** rather than over a single tree. One root is the common case: the
system under test's package. Two roots are needed when the target is a library
the harness wraps, such as the system under test together with the
adapter-instrumented goldfive checkout (the goldfive steering target — see
[DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md)).

The CLI exposes this with the `--mutable-tree` flag on `register`:

```
zicato epoch register --adk agent_package.agent:root_agent \
    --mutable-tree path/to/agent_package \
    --mutable-tree path/to/another/package
```

`--adk` is a dotted module path. Each registered root's basename must be the
importable package name (`agent_package` above). The snapshot copies each root
under its basename and prepends the snapshot to `sys.path`, which resolves
top-level names only. A root whose basename Python cannot use as a module name
could therefore never be shown to have run from the snapshot, so `register`
refuses it (issue #110). The entrypoint itself may sit inside a root or
outside all of them; the second shape is the dependency case, where zicato
mutates a package the harness imports. Every root is verified per run in
either shape — see [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md) §2.4.

The first registered root is conventionally the package containing the
agent factory; additional roots are added with repeated
`--mutable-tree` flags. All registered roots contribute mutation
points to the same enumeration.

Nothing about a root has to be agent-shaped. A root is any importable
Python package whose behaviour the board can score — its basename must be
a valid, non-keyword Python identifier, and that is the whole constraint.
The enumerator resolves markers through the AST without ever asking what a
file *means*: it does not look for agent classes, role graphs, or prompt
attributes, and the applier dispatches on the point's kind and the file's
suffix alone.

Two in-tree targets bracket the range. The deterministic convergence
recipe (`examples/zicato_examples/target_0_convergence`) is a policy with no
LLM at all, whose entire surface is one marked module-level string constant.
The goldfive steering target is a library, registered with
`zicato epoch register --mutable-tree <checkout>/goldfive` while the entrypoint
stays outside every tree.

Nor is the surface Python-only. The native marker pass walks `*.py` **and**
every declared text file (§2.4, §2.5), so a markdown prompt or a YAML
config sitting in a mutable tree is addressable surface. The marker
requirement applies to those files as it does to Python; a marker may
simply live in more kinds of file. What stays out of reach is a file with
no marker in it, and a file type the contract's syntax table does not
declare.

A second, additive pass covers one shape that declares its surface
elsewhere: the **manifest bridge**
(`zicato.synthetic.manifest_bridge`). When a root carries a goldfive-shaped
`optimization/manifest.toml`, each manifest entry becomes a
`MutationPoint` — prompt entries pointing at markdown bodies, numeric
entries at `.py` attributes. The bridge is how the goldfive steering target
exposes goldfive's prompt surface without adding zicato markers throughout
an upstream tree, and it no-ops silently for every root that has no such
manifest.

The two passes are independent, which means a manifest id and a marker id
*can* collide if an operator marks a file the manifest already declares.
Nothing prevents it at enumeration time; `validate_patches` rejects the
ambiguous id (see §6) rather than letting the applier edit whichever point
was enumerated last.

The list shape is part of the contract even though a single root is the
common case, so a multi-root target such as the goldfive steering target
needs no schema change.

### Idempotency

`mutation_points()` re-parses every source file on every call. There
is no caching by design. Parsing a few hundred Python files is fast
(<100ms in practice) and the freshness guarantee is more valuable than
the speedup.

## 6. Validator constraints

The validator (`zicato.mutation.validator`) is deterministic and
side-effect-free; it returns a list of human-readable problem strings
rather than raising, so the tournament can log one rejection record
per experiment. It runs in two phases.

**Pre-apply** (`validate_patches`, before the patch set is written —
so a malformed batch is refused as a whole rather than half-applied):

| Code | Check | Constraint | Why |
|---|---|---|---|
| `P1` | the target id resolves | Each patch's `mutation_id` resolves to an enumerated `MutationPoint`. | A patch that targets nothing cannot be applied. |
| `P2` | the operation matches its payload | The `op` matches its payload: `replace` carries `new_content`, `set_numeric` carries `new_numeric`, `set_enum` carries `new_enum`, and no foreign payload field is set. | Catches a malformed proposer response before it touches disk. |
| `P3` | the operation suits the point kind | The `op` is compatible with the target point's `kind`: `replace` works on `span`, `file`, or `code`; `set_numeric` / `set_enum` require a `span` point (they locate a constant after the marker). | A file-level or region rewrite has no single constant to retarget. Since the text pass emits only `file` / `code` points, this is also what stops a `set_numeric` landing in a YAML file. |
| `P4` | the target id is unique across the surface | Every `mutation_id` a patch targets resolves to exactly ONE point across the whole surface. | The Python pass, the text pass, and the manifest bridge are independent; an id declared twice would otherwise resolve last-write-wins. |

A standalone helper, `check_forbidden_ids`, rejects any patch whose
`mutation_id` is in an operator-supplied forbidden set — the
mechanical enforcement behind the proposer brief's `## Forbidden` list.

**Post-apply** (`validate_post_apply`, after the candidate snapshot is
written — the tournament refuses to promote a snapshot with any
non-empty error list):

| Code | Check | Constraint | Why |
|---|---|---|---|
| `A1` | patched Python still parses | Every touched `.py` file still parses (`ast.parse`). Non-Python touched files are checked for existence and readability only. | A non-parsing file cannot be imported; the whole snapshot is unusable. |
| `A2` | every patched id still resolves | Every patch's `mutation_id` still resolves in a fresh enumeration of the snapshot. | The next round must be able to re-find this id. |
| `A3` | required placeholders survive | For any point whose pre-apply `metadata` declared `required_placeholders`, each named placeholder (exact substring, braces included) survives in the patched content. | Prevents the proposer from silently dropping a `{user_message}` formatter the surrounding code injects. |
| `A4` | top-level imports are preserved | Top-level imports in every patched `.py` file are preserved — the post-apply import set must be a superset of the pre-apply set. The proposer may add imports but not silently remove them. | A dropped import breaks the snapshot at runtime rather than at parse time. |

Each post-apply error string is **prefixed with its check code** — `A1: `,
`A2: `, `A3: `, `A4: ` — so a consumer counting per-check failure rates reads
the code rather than parsing the prose. `classify_post_apply_error`
(`zicato/mutation/validator.py`) is the one reader of that prefix, and the
prose after it stays free to reword. `GateEvaluated` draws the same division
between its numeric fields and its presentational `rule_fired`: the code is
the contract, and the sentence is for a human reader. An error carrying no
recognised code classifies as `None`, an honest unknown, rather than being
attributed to a check that may not have run — see the proposer scorecard,
[PROPOSER.md §6.1](PROPOSER.md).

The required-placeholder check (`A3`) is opt-in per mutation point via the
`required_placeholders` metadata key on the marker; the validator never
guesses placeholders for an unannotated span. The check is format-agnostic,
because a placeholder is an exact substring, so it fires on a markdown
region body in the same way as on a Python literal.

There is **no** non-Python counterpart to the parse check (`A1`). "Still
parses" has no cheap, dependency-free meaning for markdown or YAML, and a
gate that covered only the one format the standard library can check would
buy inconsistent protection at the cost of a second validation path. The
safety property comes from elsewhere: a patch can only ever land inside an
operator-marked region.

The mutation surface stays **operator-owned**: the proposer addresses
patches by id and rewrites within an enumerated point, but only the
operator's markers define what the surface is. The post-apply id-resolution
check (`A2`) enforces that every patched id still resolves after the
rewrite.

## 7. The `zicato inspect mutations` CLI

The audit command carries the help text `Advanced: audit the mutable
surface the proposer may change`. It resolves the registered adapter from
the workspace, enumerates `mutation_points()`, and renders the result.
`zicato evolve`
enumerates this surface itself every round; this command exists to let
an operator audit what the proposer is allowed to change.

```
$ zicato inspect mutations
[span]   researcher_instruction
         agent.py:18-18
         "You research the user's question by ..."

[span]   researcher_description
         agent.py:38-38
         "Performs literature lookup and source aggregation."

[file]   presentation_agent_prompts
         prompts.py (entire file)

[span]   outline_prompt   required_placeholders={section_count}
         prompts.py:24-24
         "Outline the presentation in three sections: ..."
```

Flags:

| Flag | Meaning |
|---|---|
| `--workspace PATH` | Path to the zicato workspace directory (default `.zicato`). |
| `--id <glob>` | Filter mutation points by id glob, e.g. `--id 'researcher_*'`. |
| `--kind span\|file\|code` | Restrict the listing to one mutation kind. |
| `--show preview\|full` | Truncate content previews (`preview`, the default) or dump full content (`full`). |
| `--format table\|json` | Output format: human-readable `table` (default) or `json` (the full `MutationPoint` shape). |

There is no `--root` flag; the listing always covers every registered
source root. Use the id glob (`--id`) to narrow it to a subset.

The intended workflow is:

1. Operator marks new mutation points in the system under test's source.
2. Operator runs `zicato inspect mutations` to confirm every marker resolves
   cleanly (no warnings, no duplicate ids).
3. Operator runs `zicato evolve` and the proposer addresses patches
   against the surface they just confirmed.

`zicato inspect mutations` is also the right place to confirm the exact id
spellings before adding one to the proposer brief's `## Forbidden`
list — the forbidden-id check (`check_forbidden_ids`) matches on the
literal id, so the operator can copy the id straight out of the
listing.

## 8. Adding new markers to existing code

The recommended workflow for marking up a system under test:

1. Identify the smallest unit the proposer should be able to rewrite.
   Usually one string literal — a specialist instruction, a coordinator
   routing template, a tool description.
2. Hoist the literal to a named binding if it is not already one (`INSTR =
   "..."` near the top of the module, used by reference in the agent
   definition). Span markers do not decorate inline string literals
   buried in expression contexts cleanly; the named-binding form is
   the canonical shape.
3. Add a span marker on the line above with a meaningful id. The id
   should encode the role and the role's part: `coordinator.routing`,
   `researcher.instruction`, `writer.tools.summarize.description`.
4. Run `zicato inspect mutations` and confirm the marker resolves.

For a whole module of related strings (a `prompts.py`), add one file
marker at the top of the file. Span markers within it become optional.

## 9. Deferred extensions

The marker syntax above is the whole contract. Two extensions are plausible
and are deferred:

- **Multi-string span markers.** A marker that covers a sequence of
  consecutive string literals, so that a tuple of related prompts can be
  rewritten as a group. Without it, the operator hoists them to a single
  module-level string or uses a file marker.
- **Type-narrowed mutation points.** A marker that asserts "the new
  value must satisfy this Pydantic shape." The `required_placeholders`
  check (`A3`) and the `min` / `max` / `enum` metadata bounds are the only
  structural validators on patched text.

Either is straightforward to add later, because the
`MutationPoint.metadata` mapping is an open `str`-keyed bag: a new marker
attribute is a new metadata key rather than a schema change. The contract
starts narrow.
