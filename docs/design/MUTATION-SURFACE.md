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
# zicato:mutable id="<stable-id>" [kind="prompt"|"description"|"template"|...]
```

`id` is required and globally unique within a single registration.
`kind` is an optional symbolic label for documentation and proposer
heuristics; it has no effect on AST resolution.

Two examples:

```python
# Specialist's system prompt — coordinator routes user research turns here.
# zicato:mutable id="researcher.instruction" kind="prompt"
INSTRUCTION = """You research the user's question by ..."""

researcher = LlmAgent(
    name="researcher",
    # zicato:mutable id="researcher.description" kind="description"
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

A file marker is a comment in the first 16 lines of a file (header
region — module docstring above, marker below, no statements before
it). It has the form:

```
# zicato:mutable file id="<stable-id>" [kind="prompts"|"templates"|...]
```

Example:

```python
"""Specialist prompts for the presentation agent."""

# zicato:mutable file id="presentation_agent.prompts" kind="prompts"

INTRO = "..."
OUTLINE = "..."
REVISION = "..."
```

When a file marker is present, the proposer may propose a patch whose
target is the file id and whose `new_text` is the entire post-edit
contents of the file. The applier writes the file in full, then runs
every validator constraint on it.

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
   marker, find the AST node on the **next non-blank, non-comment
   source line**.
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

1. Within the first 16 lines of the file, search for a line matching
   `# zicato:mutable file id="..."`.
2. If found, the entire file (excluding the marker line) is the
   target. The current value is the file's full text minus that line.
3. A file may carry at most one file marker.

**Unrecognized marker form:** the enumerator emits a warning to stderr
and skips it. The mutation surface does not silently absorb typos.

## 4. `MutationPoint`

The enumerator returns a list of `MutationPoint` objects. The dataclass
shape:

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

@dataclass(frozen=True)
class MutationPoint:
    id: str                       # globally unique within registration
    kind: Literal["span", "file"] # marker form
    label: str | None             # `kind=` attribute on the marker; None if absent
    file: Path                    # absolute path to the source file
    start_line: int               # 1-indexed inclusive
    end_line: int                 # 1-indexed inclusive
    start_col: int                # 0-indexed (None for file markers)
    end_col: int                  # 0-indexed (None for file markers)
    current_text: str             # value at enumeration time
    source_root: Path             # which registered root this came from
```

`current_text` is a snapshot — the next round will re-enumerate after
patches land, and `current_text` will reflect the new value. The id is
stable across snapshots; the location may drift if patches above it
add or remove lines.

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
zicato register --adk path/to/agent.py:root_agent \
    --mutable-tree path/to/agent_package \
    --mutable-tree path/to/another/package
```

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

When the applier writes a candidate snapshot, every patch must pass
every validator constraint. Failures reject the patch (and the
proposer is informed via the round's wall-clock budget).

| # | Constraint | Why |
|---|---|---|
| V1 | The patched file parses as valid Python (`ast.parse`). | A non-parsing file can't be imported. The whole snapshot is unusable. |
| V2 | Every import name in the patched file resolves on import. | Catches `from foo import bar` where the patch deleted `bar`. |
| V3 | The mutation-point id targeted by the patch resolves to exactly one location after the rewrite. | The next round must be able to re-find this id. |
| V4 | For span markers labeled `kind="prompt"` or `kind="template"`, all `{...}` named placeholders in the pre-patch text are present in the post-patch text. | Prevents the proposer from silently dropping a `{user_message}` formatter that the surrounding code injects. |
| V5 | The patch does NOT touch any mutation-point id that appears in the proposer brief's `## Forbidden` list. | Operator's mechanical guard against the proposer rewriting things they marked off-limits. |
| V6 | For file markers, the post-patch file contains the same file marker line (preserved verbatim). | The id must survive into the next round. |
| V7 | The patch's `new_text` does not contain another `# zicato:mutable` marker that would introduce a new id. | New mutation points must be added by the operator, not the proposer. |

V4 is intentionally specific to prompt-shaped spans. Placeholder
preservation for arbitrary strings would be a false-positive factory;
labelling a marker with `kind="prompt"` is the opt-in.

V7 is the load-bearing rule that keeps the mutation surface
**operator-owned**. The proposer rewrites within the surface; the
operator decides what the surface is.

## 7. The `zicato mutations` CLI

The audit command. Walks the registered adapter, calls
`mutation_points()`, and renders the result.

```
$ zicato mutations
4 mutation points (2 span, 1 file, 1 file with spans)

[span]   researcher.instruction      kind=prompt
         agent.py:18 (col 0-3)
         "You research the user's question by ..."

[span]   researcher.description       kind=description
         agent.py:38 (col 4-15)
         "Performs literature lookup and source aggregation."

[file]   presentation_agent.prompts   kind=prompts
         prompts.py (entire file, 142 lines)

[span]   prompts.outline              kind=prompt
         prompts.py:24 (col 0-7)
         "Outline the presentation in three sections: ..."
```

Flags:

| Flag | Meaning |
|---|---|
| `--id <glob>` | Filter by id glob, e.g. `--id 'researcher.*'`. |
| `--kind span\|file` | Filter by marker form. |
| `--show full` | Print the full `current_text` instead of a preview. |
| `--format json` | Emit JSON (the full `MutationPoint` shape) instead of human-readable text. |
| `--root <path>` | Restrict to one registered source root. |

The intended workflow is:

1. Operator marks new mutation points in the inner harness's source.
2. Operator runs `zicato mutations` to confirm every marker resolves
   cleanly (no warnings, no duplicate ids).
3. Operator runs `zicato evolve` and the proposer addresses patches
   against the surface they just confirmed.

`zicato mutations` is also the right place to invoke when an operator
adds an id to the proposer brief's `## Forbidden` list — the CLI
surfaces forbidden ids as `[forbidden]` next to the kind so the
operator can sanity-check that the right ids are excluded.

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
  value must satisfy this Pydantic shape." Today the placeholder
  check (V4) is the only structural validator on patched text.

Each of these is straightforward to add later because the
`MutationPoint` shape is open-ended on the `kind` and `label` fields.
v0 starts narrow.
