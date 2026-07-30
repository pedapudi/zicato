# Mutation surface

The mutation surface is the set of source locations in the inner
harness that zicato is allowed to rewrite. It is **annotated** — every
mutable location is marked in the source with a comment-form marker —
not free-form. The patch proposer addresses mutations by stable id;
the applier resolves each id to one location and rewrites only what
the id covers.

This document specifies:

- The two marker forms (span-level, file-level) and their syntax.
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
  Validating "this still works" against a multi-agent system is
  extremely hard.
- **Pure span-level annotated mutations** maximise safety (only marked
  strings move) at the cost of reach (related strings in the same
  file may need to move together but can't be addressed as a group).

zicato takes the middle path. Span markers are the default. A file
marker covers the cases where a whole module — e.g. a `prompts.py`
with several closely-related templates — should move as one unit.
Both forms produce `MutationPoint`s with stable ids; both are
addressed by the proposer through those ids; neither lets the proposer
rewrite an unmarked file.

## 2. Marker syntax

Both forms are Python comments. The walker is a Python AST visitor;
the comments are recognised by their exact prefix and the AST node
they immediately precede or annotate.

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

### 2.3 What markers do NOT do

- Markers do not declare any *constraints* on the new text. They mark
  a target; validation happens at apply time, not at mark time.
- Markers do not carry the current value. The walker reads the value
  from the AST and stamps it onto the `MutationPoint` at enumeration
  time.
- Markers do not survive into the wire format. The JSONL event stream
  knows nothing about mutation points; this is a source-side concern
  only.
- Markers are not executable. They are comments. The Python interpreter
  ignores them.

## 3. AST resolution rules

The mutation enumerator is a Python AST visitor. For each registered
source root it walks every `.py` file, parses to AST, and walks the
nodes.

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
    id: str                       # globally unique within registration
    kind: Literal["span", "file"] # marker form
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

`--adk` is a dotted module path whose top-level module must be the basename of
one registered root (`agent_package` above): the snapshot copies each root
under its basename and is prepended to `sys.path`, which resolves top-level
packages only. Any other form imports the installed copy — every mutation
becomes a scored no-op — so `register` refuses it (issue #110).

The first registered root is conventionally the package containing the
agent factory; additional roots are added with repeated
`--mutable-tree` flags. All registered roots contribute mutation
points to the same enumeration.

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
| P3 | The `op` is compatible with the target point's `kind`: `replace` works on `span` or `file`; `set_numeric` / `set_enum` require a `span` point (they locate a constant after the marker). | A file-level rewrite has no single constant to retarget. |

A standalone helper, `check_forbidden_ids`, rejects any patch whose
`mutation_id` is in an operator-supplied forbidden set — the
mechanical enforcement behind the proposer brief's `## Forbidden` list.

**Post-apply** (`validate_post_apply`, after the candidate snapshot is
written — the tournament refuses to promote a snapshot with any
non-empty error list):

| # | Constraint | Why |
|---|---|---|
| A1 | Every touched `.py` file still parses (`ast.parse`). Non-Python touched files (e.g. markdown prompt bodies under a manifest-bridged surface) are checked for existence and readability only. | A non-parsing file can't be imported; the whole snapshot is unusable. |
| A2 | Every patch's `mutation_id` still resolves in a fresh enumeration of the snapshot. | The next round must be able to re-find this id. |
| A3 | For any point whose pre-apply `metadata` declared `required_placeholders`, each named placeholder (exact substring, braces included) survives in the patched content. | Prevents the proposer from silently dropping a `{user_message}` formatter the surrounding code injects. |
| A4 | Top-level imports in every patched `.py` file are preserved — the post-apply import set must be a superset of the pre-apply set. The proposer may add imports but not silently remove them. | A dropped import breaks the snapshot at runtime, not at parse time. |

A3 is opt-in per mutation point via the `required_placeholders`
metadata key on the marker; the validator never guesses placeholders
for an unannotated span.

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
| `--kind span\|file` | Restrict the listing to one mutation kind. |
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
- **JSON / YAML inner harnesses.** Markers in non-Python sources.
  Today the marker walker is Python-only; an inner harness whose
  prompts live in YAML must hoist them through a Python module to be
  mutable. Most ADK setups already do this; LangChain setups
  sometimes do not.
- **Type-narrowed mutation points.** A marker that asserts "the new
  value must satisfy this Pydantic shape." Today the
  `required_placeholders` check (A3) plus the `min` / `max` / `enum`
  metadata bounds are the only structural validators on patched text.

Each of these is straightforward to add later because the
`MutationPoint.metadata` mapping is an open `str`-keyed bag — a new
marker attribute is a new metadata key, not a schema change.
v0 starts narrow.
