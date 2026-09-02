# Conversation execution outline

The board comparison and live conversation panes place an execution outline
beneath the conversation turns that own recorded activity. The outline answers
which agent or tool was active while reading a champion and challenger side by
side. The layout stays decision-oriented: the conversation is the spine, and
execution expands locally beneath it.

The outline is a compact reconstruction from the run's canonical
`events.jsonl`. Harmonograf holds the full temporal trace for one run. It
provides timing, lifelines, and the complete event stream when the operator
needs more detail than the comparison pane can carry.

## Server-owned structure

`query/transcript_reconstruction.py` reconstructs two related structures:

- each conversation turn carries `activity_ids`, the execution nodes displayed
  beneath that turn;
- the transcript carries an `execution` object with `nodes`, `root_ids`,
  `unresolved_ids`, and an overall `fidelity`.

An agent node receives a parent only when an invocation event states both
`invocation_id` and `parent_invocation_id`. A missing parent or parent cycle is
kept in `unresolved_ids`. The reader never infers a parent from timestamps,
event order, task names, or matching text.

A node's status is the last statement in stream order among the invocation
lifecycle events: `agent_invocation_completed` marks `completed`;
`invocation_boundary_exited` restates the terminal reason (`completed`,
`cancelled`, or anything else as `failed` with the reason surfaced as the
summary); `invocation_cancelled` marks `cancelled`. A node's name comes only
from the invocation start and completion events — the boundary events
attribute nested invocations to the host agent, so their `agent_name` is not
consumed.

A delegation observation becomes a tool node associated with its conversation
turn, with the stable identity `tool:<run-id>:<source-index>` (the event
carries no per-call identifier). The event does state the delegating
invocation's id: when that id names a known agent node, the tool node takes it
as an explicit parent and nests beneath the invocation that made the call,
with `fidelity: "exact"`. Without a resolvable id the node stays parentless
with `fidelity: "turn"` — the observation is recorded without claiming a
relationship the stream did not state. Deep mixtures follow from the
agent-to-agent parent edge and the tool-to-invocation parent edge alone: a
sub-agent's own delegations carry that sub-invocation's id, so agent → tool →
agent chains render at their full stated depth.

The transcript's overall `fidelity` values have these meanings:

| Value | Meaning |
|---|---|
| `exact` | Every displayed edge comes from explicit invocation identifiers. |
| `partial` | The outline includes parentless turn-scoped tools or unresolved agent records. |
| `unavailable` | The run contains no supported execution records. |

Nodes that have no conversation-turn anchor appear under **Run activity**.
This keeps an in-flight invocation visible before it produces a message.
Missing-parent and cyclic records appear under **Unresolved activity** when
they are the only unattached records.

## Browser behavior

`dashboard/static/js/turns.js` renders the supplied node identifiers as a
collapsible tree. It does not derive edges. The same renderer serves the board
comparison and live conversation panes, so settled and streaming runs use one
visual grammar.

Execution state participates in the existing per-turn content digest. A status
change patches the turn that owns the node. Unrelated turns retain their DOM
nodes, scroll position, selection, and focus. A heartbeat with no content
change performs no DOM write.

The trajectory strip is a whole-run overview. It shows conversation
rhythm, signals, and budget consumption. The execution outline supplies local
structure beneath the corresponding conversation. The two views do not share
a selection cursor because imported trajectories do not provide stable causal
positions for execution nodes.

## Supported and deferred data

The shipped reader reconstructs explicit agent invocation trees and
turn-scoped delegation observations. The generic renderer also understands
tool and artifact node kinds so richer adapters can use the same visual
contract when their canonical events provide stable identifiers.

The transcript reader adds the run's durable `artifacts.json` inventory as
parentless artifact nodes with `fidelity: "run"`. These nodes appear under
**Run activity**. They are not children of a turn or invocation because the
manifest does not record a producer identifier. Filename proximity or creation
time is insufficient evidence of causality.

Retries are visible only when the canonical stream represents them as distinct
activity records. The reader does not create retry edges from repeated names.

## Verification

Python tests cover explicit parents, missing parents, cycles, turn-scoped tool
observations, and stable serialization. Browser tests cover nested rendering,
absence fallback, node-local live updates, cycles, unattached running roots,
and unresolved records.

Real-browser inspection covered recursive, parallel, failed, pending, narrow,
collapsed, dark-theme, and light-theme states. The captured evidence is stored
in `artifacts/visual-inspection/execution-view/`.
