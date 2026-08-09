# Mutation surface

The mutation surface is the set of source locations in the inner
harness that zicato is allowed to rewrite. It is **annotated** — every
mutable location is marked in the source with a comment-form marker —
not free-form. The patch proposer addresses mutations by stable id;
the applier resolves each id to one location and rewrites only what
the id covers.

This document specifies:

- The three marker forms (span-level, file-level, region-level) and
  their syntax, in Python and in any other text file (§2).
- The AST resolution rules that map a marker to a source location.
- The shape of `MutationPoint` and the
  `HarnessAdapter.mutation_points()` protocol method.
- The `zicato mutations` audit CLI.
- The applier's validator constraints.
- The interaction with the proposer brief's `## Forbidden` list.

The "why annotated, not free-form" argument lives in
[RATIONALE.md](RATIONALE.md); this document is the contract.

## 1. Why annotated

A meta-harness has a safety-vs-reach trade-off:

- **Free-form source edits** maximise reach (the proposer can change
  anything) at the cost of safety (the proposer can break anything).
  Validating "this still works" against a live system — a multi-agent
  tree, a library, anything with real behaviour — is extremely hard.
- **Pure span-level annotated mutations** maximise safety (only marked
  strings move) at the cost of reach (related strings in the same
  file may need to move together but can't be addressed as a group).

zicato takes the middle path. Span markers are the default. A region
marker covers a bounded run of lines — a block of control flow, a prompt
body, a config stanza. A file marker covers the cases where a whole
module — e.g. a `prompts.py` with several closely-related templates —
should move as one unit. All three forms produce `MutationPoint`s with
stable ids; all three are addressed by the proposer through those ids;
none of them lets the proposer rewrite an unmarked file.

Widening the surface beyond `.py` (§2.4) does not relax this. It changes
*where a marker may live*, never whether one is required. An unmarked
`config.yaml` is exactly as immutable as an unmarked `agent.py`.

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
docstring above, marker below, no statements before it). It has the
form — note the `:file` suffix on the marker prefix, not a separate
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

**Accepted lead-ins.** `#` · `//` · `/*` · `<!--` · `;` · `--` · `%`.
A trailing block-comment closer (`-->`, `*/`) is tolerated on any marker
line. So all of these are the same marker:

```yaml
# zicato:mutable:code id="retry_policy"
```
```markdown
<!-- zicato:mutable:code id="researcher_brief" role="prompt" -->
```
```javascript
// zicato:mutable:code id="tool_descriptions"
```

`*` is deliberately **not** a lead-in, even though C block comments
conventionally continue with it: every markdown bullet would become a
candidate marker line. Write the marker on the `/*` line instead.

`.py` files are parsed under the historical `#`-only grammar and nothing
else. That is not an oversight — it is what makes "widening the surface
moves zero Python points" a property of the grammar rather than a claim a
test has to keep re-establishing.

**Which forms carry over.** Two of three:

| Form | In a `.py` file | In any other text file |
|---|---|---|
| `:file` | whole file | whole file — identical |
| `:code` | region between the markers | region between the markers — identical |
| bare span | binds to the nearest string literal beneath | **not supported**; warns and contributes nothing |

A bare span marker has no meaning without a parser: "the nearest string
literal beneath" is an AST fact. The tempting approximation — bind to the
next non-blank line — is worse than nothing, because a `replace` against
`temperature: 0.7` would swallow the `temperature:` key the operator
meant to keep. So the enumerator refuses it, and says so by name:

```
enumerator: config/runtime.yaml:12 declares span marker id='temperature',
but span markers bind to a Python string literal and runtime.yaml is not a
Python file; use the region form (zicato:mutable:code ...
zicato:mutable:end) or zicato:mutable:file instead. This marker
contributes no mutation point.
```

**The region form is the workhorse**, and carries the safety argument.
Both anchors are explicit, both are written by the operator, and both sit
*outside* the mutable range:

```yaml
runtime:
  # zicato:mutable:code id="retry_policy"
  retries: 3
  backoff_seconds: 1.5
  # zicato:mutable:end
  owner: operator          # <- not mutable, and cannot become mutable
```

The point's `content` is exactly the lines strictly between the markers.
Three properties follow, and each is pinned by a test:

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

A `:file` marker gives up property 2 by definition — a whole-file replace
*can* delete the marker. That is caught after the fact by post-apply check
A2 (every patched id must still resolve), which rejects the snapshot.

**JSON.** Strict JSON has no comment syntax, so there is no way to write a
marker in a `.json` file without invalidating it. `.json` / `.jsonl` are
therefore not walked at all; the comment-bearing dialects `.jsonc` and
`.json5` are.

### 2.5 Which files are walked

Discovery is an **extension allowlist**, not a content sniff — see
`TEXT_FILE_SUFFIXES` / `TEXT_FILE_NAMES` in
`zicato.mutation.enumerator` for the current set (prose, config,
templates, shell, markup, and the common source languages).

The alternative — open every file and guess from its bytes whether it is
text — was rejected on two grounds. It reads *every* file in the tree
including binaries, and the enumerator re-runs after **every applied
patch**, so the walk sits on the hot path. And a surface that decides by
guessing is a surface whose contents can change because a file's first
8KB changed. An allowlist decides without opening the file and cannot
wander into a binary at all.

The cost is real and worth naming: an operator with an unusual extension
must add it to the allowlist. That is a visible, reviewable edit — the
right failure mode for the thing that decides what an LLM may rewrite.

Three guards ride along:

- Vendored / generated directories (`.git`, `node_modules`, `.venv`,
  `__pycache__`, `dist`, `build`, …) are pruned from the **text pass
  only**. Pruning the Python pass would change which `.py` files
  enumerate, and that is exactly what must not move.
- Text files over 2 MB are skipped. A multi-megabyte `.jsonl` in a
  mutable tree is data, not surface.
- A file that is not valid UTF-8, or that contains a NUL byte, yields
  nothing — belt and braces behind the allowlist.

A **single-file root** now resolves by suffix: `.py` through the Python
pass, an allowlisted suffix through the text pass, and anything else
warns. Previously a non-`.py` single-file root fell through to the
directory branch, whose `rglob` on a file matches nothing, and enumerated
to zero points in complete silence.

### 2.6 What markers do NOT do

- Markers do not declare any *constraints* on the new text. They mark
  a target; validation happens at apply time, not at mark time.
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

1. Iterate over every comment in the file (the enumerator uses
   `tokenize` for comment lines paired with the `ast.parse` tree).
2. For each `# zicato:mutable id="..."` comment that is NOT a file
   marker (i.e. lacks the `:file` suffix), find the AST node on the
   **next non-blank, non-comment source line**.
3. The target node MUST be one of:
   - `ast.Assign` whose `value` is `ast.Constant` of type `str`
     (covers `X = "..."` and `X = """..."""`).
   - `ast.AnnAssign` whose `value` is `ast.Constant` of type `str`.
   - `ast.keyword` (inside a `ast.Call`) whose `value` is
     `ast.Constant` of type `str`.
4. If the marker's id has been seen earlier in the walk, this is an
   error: ids must be unique within a registration.
5. The enumerator records the target location (file path, start line,
   end line, start column, end column) and the current string value.

**File marker resolution:**

1. In the file's header region, search for a line matching
   `# zicato:mutable:file id="..."`.
2. If found, the entire file is the target. The recorded `content` is
   the file's full text.
3. A file may carry at most one file marker.

**Unrecognized marker form:** the enumerator emits a warning to stderr
and skips it. The mutation surface does not silently absorb typos.

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
`line_start` / `line_end` range, not by character columns. There is no
separate `label` field — the marker's optional attributes all live in
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
roots**, not a single tree. v0 typically uses one root — the inner
harness's package. v0+1 uses two — the inner harness *and* the
adapter-instrumented goldfive checkout it wraps (target 2 — see
[DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md)).

The CLI exposes this with the `--mutable-tree` flag on `register`:

```
zicato register --adk agent_package.agent:root_agent \
    --mutable-tree path/to/agent_package \
    --mutable-tree path/to/another/package
```

`--adk` is a dotted module path. Each registered root's basename must be the
importable package name (`agent_package` above): the snapshot copies each root
under its basename and is prepended to `sys.path`, which resolves top-level
names only, so a root Python cannot name as a module could never be shown to
have run from the snapshot — `register` refuses that (issue #110). The
entrypoint itself may sit inside a root or outside all of them (the dependency
shape: mutate a package the harness imports); every root is verified per run
either way — see [DOGFOOD-TARGETS.md](DOGFOOD-TARGETS.md) §2.4.

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

Two in-tree targets bracket the range. Target 0
(`examples/zicato_examples/target_0_convergence`) is a deterministic policy
with no LLM at all, whose entire surface is one marked module-level string
constant. Target 2 is goldfive — a library, registered with
`zicato register --mutable-tree <checkout>/goldfive` while the entrypoint
stays outside every tree.

Nor is the surface Python-only. The native marker pass walks `*.py` **and**
every allowlisted text file (§2.4, §2.5), so a markdown prompt or a YAML
config sitting in a mutable tree is real, addressable surface — the marker
requirement is unchanged, only the set of places a marker may live has
widened. What stays out of reach is a file with no marker in it, and a
file type the allowlist does not name.

A second, additive pass covers one shape that declares its surface
elsewhere: the **manifest bridge**
(`zicato.synthetic.manifest_bridge`). When a root carries a goldfive-shaped
`optimization/manifest.toml`, each manifest entry becomes a
`MutationPoint` — prompt entries pointing at markdown bodies, numeric
entries at `.py` attributes. It is how target 2 exposes goldfive's prompt
surface without sprinkling zicato markers through an upstream tree, and it
no-ops silently for every root that has no such manifest.

The two passes are independent, which means a manifest id and a marker id
*can* collide if an operator marks a file the manifest already declares.
Nothing prevents it at enumeration time; `validate_patches` rejects the
ambiguous id (see §6) rather than letting the applier edit whichever point
was enumerated last.

The list shape is part of the v0 contract even though v0 typically
uses one root. Forcing the shape now means target 2 plugs in without
schema breakage later.

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

| # | Constraint | Why |
|---|---|---|
| P1 | Each patch's `mutation_id` resolves to an enumerated `MutationPoint`. | A patch that targets nothing cannot be applied. |
| P2 | The `op` matches its payload: `replace` carries `new_content`, `set_numeric` carries `new_numeric`, `set_enum` carries `new_enum`, and no foreign payload field is set. | Catches a malformed proposer response before it touches disk. |
| P3 | The `op` is compatible with the target point's `kind`: `replace` works on `span`, `file`, or `code`; `set_numeric` / `set_enum` require a `span` point (they locate a constant after the marker). | A file-level or region rewrite has no single constant to retarget. Since the text pass emits only `file` / `code` points, this is also what stops a `set_numeric` landing in a YAML file. |
| P4 | Every `mutation_id` a patch targets resolves to exactly ONE point across the whole surface. | The Python pass, the text pass, and the manifest bridge are independent; an id declared twice would otherwise resolve last-write-wins. |

A standalone helper, `check_forbidden_ids`, rejects any patch whose
`mutation_id` is in an operator-supplied forbidden set — the
mechanical enforcement behind the proposer brief's `## Forbidden` list.

**Post-apply** (`validate_post_apply`, after the candidate snapshot is
written — the tournament refuses to promote a snapshot with any
non-empty error list):

| # | Constraint | Why |
|---|---|---|
| A1 | Every touched `.py` file still parses (`ast.parse`). Non-Python touched files are checked for existence and readability only. | A non-parsing file can't be imported; the whole snapshot is unusable. |
| A2 | Every patch's `mutation_id` still resolves in a fresh enumeration of the snapshot. | The next round must be able to re-find this id. |
| A3 | For any point whose pre-apply `metadata` declared `required_placeholders`, each named placeholder (exact substring, braces included) survives in the patched content. | Prevents the proposer from silently dropping a `{user_message}` formatter the surrounding code injects. |
| A4 | Top-level imports in every patched `.py` file are preserved — the post-apply import set must be a superset of the pre-apply set. The proposer may add imports but not silently remove them. | A dropped import breaks the snapshot at runtime, not at parse time. |
| A5 | A **whole-file** patch against a `.toml` file that parsed *before* the batch must still parse after it. Enforced by the applier, not `validate_post_apply`. | The cheap non-Python counterpart to A1 — see below. |

A3 is opt-in per mutation point via the `required_placeholders`
metadata key on the marker; the validator never guesses placeholders
for an unannotated span. It is format-agnostic — a placeholder is an
exact substring, so it fires on a markdown region body exactly as it does
on a Python literal.

A5 is deliberately the narrowest useful check, and it is worth being
explicit about what it is *not*. There is no general "still valid" notion
for text, so the gate covers only what the standard library can check for
free, and only where a failure is unambiguously the patch's fault:

- **`.toml` only.** YAML is absent because no YAML parser is a zicato
  runtime dependency. JSON is absent for a sharper reason: strict JSON has
  no comment syntax, so a `.json` file cannot host a marker at all, and
  the comment-bearing dialects (`.jsonc` / `.json5`) that can are by
  construction not parseable by `json.loads`.
- **Whole-file patches only.** A region patch rewrites a *fragment* whose
  syntactic self-containment depends on where the operator put the
  markers, not on what the proposer wrote. Failing a snapshot there would
  reject legitimate edits and blame the wrong party.
- **Files that parsed beforehand only.** The gate catches a patch that
  *breaks* a working file. A fixture the operator committed malformed is
  not the proposer's doing and does not fail the round.

Everything outside that intersection is unchecked, on purpose. A5 is a
convenience, not the safety property; the safety property is that a patch
can only ever land inside an operator-marked region.

The mutation surface stays **operator-owned**: the proposer addresses
patches by id and rewrites within an enumerated point, but only the
operator's markers define what the surface is. A2 enforces that every
patched id still resolves after the rewrite.

## 7. The `zicato mutations` CLI

The audit command — `Advanced: audit the mutable surface the proposer
may change`. It resolves the registered adapter from the workspace,
enumerates `mutation_points()`, and renders the result. `zicato evolve`
enumerates this surface itself every round; this command exists to let
an operator audit what the proposer is allowed to change.

```
$ zicato mutations
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

There is no `--root` flag — the listing always covers every registered
source root. Filter by id glob (`--id`) when you want a subset.

The intended workflow is:

1. Operator marks new mutation points in the inner harness's source.
2. Operator runs `zicato mutations` to confirm every marker resolves
   cleanly (no warnings, no duplicate ids).
3. Operator runs `zicato evolve` and the proposer addresses patches
   against the surface they just confirmed.

`zicato mutations` is also the right place to confirm the exact id
spellings before adding one to the proposer brief's `## Forbidden`
list — the forbidden-id check (`check_forbidden_ids`) matches on the
literal id, so the operator can copy the id straight out of the
listing.

## 8. Adding new markers to existing code

The recommended workflow for marking up an inner harness:

1. Identify the smallest unit you want the proposer to be able to
   rewrite. Usually one string literal — a specialist instruction, a
   coordinator routing template, a tool description.
2. Hoist the literal to a named binding if it isn't already (`INSTR =
   "..."` near the top of the module, used by reference in the agent
   definition). Span markers don't decorate inline string literals
   buried in expression contexts cleanly; the named-binding form is
   the canonical shape.
3. Add a span marker on the line above with a meaningful id. The id
   should encode the role and the role's part: `coordinator.routing`,
   `researcher.instruction`, `writer.tools.summarize.description`.
4. Run `zicato mutations` and confirm the marker resolves.

For a whole module of related strings (a `prompts.py`), add one file
marker at the top of the file. Span markers within it become optional.

## 9. Future shapes deliberately out of scope for v0

The marker syntax above is the v0 contract. Several extensions are
plausible but deliberately deferred:

- **Multi-string span markers.** A marker that covers a sequence of
  consecutive string literals. The use case is "rewrite this tuple of
  related prompts as a group." Today the operator hoists them to a
  single module-level string or uses a file marker.
- ~~**JSON / YAML inner harnesses.** Markers in non-Python sources.~~
  **Shipped** — see §2.4 / §2.5. An inner harness whose prompts live in
  markdown or YAML no longer has to hoist them through a Python module.
  The two caveats that remain: there is no span form outside Python (use
  a region), and strict JSON cannot host a marker at all because it has
  no comment syntax.
- **Type-narrowed mutation points.** A marker that asserts "the new
  value must satisfy this Pydantic shape." Today the
  `required_placeholders` check (A3) plus the `min` / `max` / `enum`
  metadata bounds are the only structural validators on patched text.

Each of these is straightforward to add later because the
`MutationPoint.metadata` mapping is an open `str`-keyed bag — a new
marker attribute is a new metadata key, not a schema change.
v0 starts narrow.
