"""Main entry point for the decision-telemetry analyzer.

The analyzer's job for one epoch:

1. Walk the workspace's ``epochs/{epoch}/generations/{*}/runs/{*}/events.jsonl``
   tree and collect the events files of the training-slice entries the
   caller names.
2. Aggregate the five decision-telemetry event types into a
   :class:`zicato.analyzer.aggregator.DecisionEventSummary`.
3. Render the system + user prompts.
4. Call the evaluation LLM with a bounded per-call timeout
   (:func:`zicato.aux_timeout.aux_call_timeout_s`).
5. Persist the LLM's markdown response as
   ``.zicato/epochs/{epoch}/insights/round_{N}.md`` (or
   ``insights/latest.md`` when ``round_n is None``).

Every failure mode (no events at all, LLM timeout, LLM error) is
handled by writing a short markdown placeholder rather than raising —
the orchestrator calls this best-effort and a wedge here must not
abort the round. A model analysis, and nothing else, opens with
:data:`TRAINING_SLICE_ANALYSIS_MARKER`.

:func:`load_latest_insight` reads the highest-numbered
``insights/round_{N}.md`` back for the next round's proposal evidence, and
delivers it only when it opens with that marker.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Collection, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from zicato.analyzer.aggregator import (
    DecisionEventSummary,
    aggregate_decision_events,
)
from zicato.analyzer.prompts import (
    INSIGHT_SYSTEM_PROMPT,
    render_insight_user_prompt,
)
from zicato.aux_timeout import aux_call_timeout_s
from zicato.core.settings import AuxConfig
from zicato.core.workspace import epoch_dir
from zicato.storage import atomic_write_text
from zicato.workspace import WorkspaceLayout, is_events_file, read_board_entries

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from zicato.telemetry.meta_loop import MetaLoopEmitter


#: First line of every insight that holds a model analysis of training-slice
#: runs. An HTML comment, so it is invisible in rendered markdown.
#: :func:`load_latest_insight` delivers a file only when it opens with this
#: line. Placeholders, files written before the analyzer was limited to the
#: training slice, and hand-written files lack it and are withheld, so
#: delivery fails closed.
TRAINING_SLICE_ANALYSIS_MARKER = (
    "<!-- zicato: decision-telemetry analysis of the training slice -->"
)

#: The most characters of an insight the proposal evidence carries. The
#: model's response is unbounded, and the block is sent with every proposal
#: of the next round, so a longer insight is cut here with a visible note.
#: The bound is the one the proposer's mutation manifest applies to a span
#: (``zicato.proposer.prompts._MUTATION_CONTENT_LIMIT_CHARS``).
INSIGHT_LIMIT_CHARS = 8000


def _collect_events_jsonl_paths(
    workspace_root: Path, epoch_id: str, entry_ids: Collection[str] | None = None
) -> list[Path]:
    """Walk the epoch's generation tree and return every current events path.

    The walk is filesystem-driven (rather than reading the board) so
    every generation's runs surface — including rejected ones, whose
    telemetry is still useful for analysis. Returns an empty list when
    the epoch directory does not exist or carries no events files yet
    (e.g. a freshly-created epoch with no completed rounds).

    EVERY replicate of a unit is collected rather than just replicate 0, and the
    insight prompt therefore aggregates across replicate bands: a unit run
    at ``replicates=3`` contributes three transcripts of the same board
    entry. That is deliberate for a whole-epoch drift summary — but it
    means a per-entry count read off this list counts draws rather than units.
    Archived predecessors (``*.prev.jsonl``) are excluded.

    ``entry_ids``, when given, keeps only the runs of those board entries
    (the run directory under ``runs/`` is named by entry id); ``None``
    keeps every run.
    """

    root = epoch_dir(workspace_root, epoch_id) / "generations"
    if not root.exists():
        return []
    wanted = None if entry_ids is None else frozenset(entry_ids)
    out: list[Path] = []
    for path in sorted(root.glob("*/runs/*/seed-*/events.*.r*.jsonl")):
        if wanted is not None and path.parents[1].name not in wanted:
            continue
        if path.is_file() and is_events_file(path):
            out.append(path)
    return out


def proposer_visible_entry_ids(workspace_root: Path, epoch_id: str) -> tuple[str, ...]:
    """The training slice of the epoch's frozen board, in board order.

    Splits the board with the epoch's frozen ``overfitting`` block and the
    same rotation seed the round preparation uses, so the result is the
    slice the proposer's patterns and loss summary are computed on.
    Raises :class:`FileNotFoundError` when the epoch has no board, because
    an analysis that cannot tell training runs from holdout runs must not
    write into the proposer's insight directory.
    """
    from zicato.board.split import rotation_seed, split_board  # noqa: PLC0415
    from zicato.workspace_loader import scoring_weights_from_dict  # noqa: PLC0415

    layout = WorkspaceLayout.from_root(workspace_root)
    board = read_board_entries(layout, epoch_id)
    if board is None:
        raise FileNotFoundError(f"epoch {epoch_id!r} has no board at {layout.board(epoch_id)}")
    scoring_path = layout.epoch_dir(epoch_id) / "scoring.json"
    raw = json.loads(scoring_path.read_text(encoding="utf-8")) if scoring_path.exists() else {}
    overfitting = scoring_weights_from_dict(raw).overfitting
    train_ids, _holdout_ids = split_board(
        board.entries, overfitting, seed=rotation_seed(overfitting, epoch_id)
    )
    return train_ids


def _insights_dir(workspace_root: Path, epoch_id: str) -> Path:
    return epoch_dir(workspace_root, epoch_id) / "insights"


def _insight_target(workspace_root: Path, epoch_id: str, round_n: int | None) -> Path:
    out_dir = _insights_dir(workspace_root, epoch_id)
    if round_n is None:
        return out_dir / "latest.md"
    # Zero-pad to width 4 so lexicographic ordering matches numeric
    # ordering up to 10k rounds — well beyond any plausible operator
    # workflow.
    return out_dir / f"round_{round_n:04d}.md"


def _empty_insight_body(epoch_id: str, summary: DecisionEventSummary) -> str:
    """Fallback insight body when no decision telemetry was observed.

    Returned ahead of any LLM call so the caller doesn't burn an
    aux-LLM budget on a prompt the model has nothing to say about.
    """

    return (
        f"# Decision telemetry insights — epoch {epoch_id}\n\n"
        f"No decision-telemetry events were observed in this epoch's "
        f"runs (total_events_seen={summary.total_events_seen}). This is "
        "expected for goldfive builds that pre-date the decision-"
        "telemetry events (tags 39-43) or for epochs whose runs have "
        "not yet executed against the new build.\n\n"
        "_No actionable patterns to surface._\n"
    )


def _error_insight_body(epoch_id: str, err: str) -> str:
    """Fallback insight body when the LLM call fails / times out.

    The orchestrator treats this as best-effort, so we still write a
    file (the proposer can ignore an empty actionable section). The
    error string surfaces in the insight so an operator inspecting the
    workspace can see what went wrong.
    """

    return (
        f"# Decision telemetry insights — epoch {epoch_id}\n\n"
        f"_(evaluation LLM call failed: {err}; no insights generated for "
        "this round)_\n"
    )


async def analyze_epoch_telemetry(
    workspace_root: Path,
    epoch_id: str,
    aux_call_llm: Callable[[str, str, str], Awaitable[str]],
    model: str = "",
    round_n: int | None = None,
    mutation_ids: Sequence[str] | None = None,
    meta_loop_emitter: MetaLoopEmitter | None = None,
    aux_config: AuxConfig | None = None,
    *,
    training_entry_ids: Collection[str],
) -> Path:
    """Build the decision-event summary, call the LLM, persist the insight.

    Parameters
    ----------
    workspace_root:
        Absolute path to the ``.zicato/`` workspace root.
    epoch_id:
        The epoch whose accumulated telemetry should be analyzed.
    aux_call_llm:
        The evaluation LLM callable (see
        :class:`zicato.core.types.RuntimeConfig`). Wrapped in
        :func:`asyncio.wait_for` against
        :func:`zicato.aux_timeout.aux_call_timeout_s`.
    model:
        Optional model identifier forwarded verbatim to *aux_call_llm*.
        Free-form; the analyzer does not switch on its value.
    round_n:
        Round number for the output filename. When ``None`` the insight
        is written to ``insights/latest.md`` instead of
        ``insights/round_{N}.md``.
    mutation_ids:
        The agent's real enumerated mutation-surface ids (the
        :attr:`zicato.core.types.MutationPoint.id` values for the
        epoch's current generation). Threaded into the insight prompt
        so the LLM's "Suggested next mutations" section is grounded in
        ids that actually exist — without it, the LLM hallucinated
        mutation target ids absent from the agent's surface. When
        ``None`` the prompt still renders, with a "none provided"
        marker, and the system prompt forbids inventing an id.
    training_entry_ids:
        The board entries whose runs are analyzed: the epoch's training
        slice. Required, because the insight is read back into the next
        round's proposal evidence and carries the training-slice
        provenance line; no holdout run may reach it.

    Returns
    -------
    Path
        Absolute path of the markdown file that was written. Always
        written — even when no telemetry was observed or the LLM call
        failed — so the orchestrator's best-effort caller has something
        deterministic to inspect.

    Notes
    -----
    This function does not raise on LLM failures or empty telemetry.
    Internal failures (path math, disk write) DO raise, which is the
    right behaviour for the orchestrator's ``try / except`` wrapper.
    """

    events_paths = _collect_events_jsonl_paths(workspace_root, epoch_id, training_entry_ids)
    summary = aggregate_decision_events(events_paths)

    target = _insight_target(workspace_root, epoch_id, round_n)

    if summary.total_events_seen == 0:
        atomic_write_text(target, _empty_insight_body(epoch_id, summary), mode=None)
        return target

    user_prompt = render_insight_user_prompt(summary, epoch_id, mutation_ids)
    # The decision-telemetry analyzer is the meta-loop's "process judge"
    # — it surveys the round's telemetry and emits a verdict (the
    # insight markdown). Bracket the LLM call with a paired
    # ``judge_invoked`` / ``judgment_emitted`` envelope so the dashboard
    # / harmonograf timeline shows the analyzer as a judge alongside
    # the proposer. Every emit is best-effort and isolated from a
    # misconfigured emitter.
    invocation_id: str | None = None
    judge_name = "decision_telemetry_analyzer"
    started_at = time.monotonic()
    if meta_loop_emitter is not None:
        try:
            invocation_id = await meta_loop_emitter.judge_invoked(
                judge_name=judge_name,
                kind="process",
            )
        except Exception:  # noqa: BLE001 — additive telemetry only
            invocation_id = None
    try:
        response = await asyncio.wait_for(
            aux_call_llm(INSIGHT_SYSTEM_PROMPT, user_prompt, model),
            timeout=aux_call_timeout_s(aux_config),
        )
    except TimeoutError:
        atomic_write_text(
            target,
            _error_insight_body(
                epoch_id,
                f"timeout after {aux_call_timeout_s(aux_config):.1f}s",
            ),
            mode=None,
        )
        if meta_loop_emitter is not None and invocation_id is not None:
            try:
                await meta_loop_emitter.judgment_emitted(
                    invocation_id=invocation_id,
                    judge_name=judge_name,
                    verdict_kind="boolean",
                    score=None,
                    detail=f"timeout after {aux_call_timeout_s(aux_config):.1f}s",
                    latency_s=time.monotonic() - started_at,
                )
            except Exception:  # noqa: BLE001 — additive telemetry only
                pass
        return target
    except Exception as exc:  # noqa: BLE001 — opaque LLM errors are common
        atomic_write_text(
            target,
            _error_insight_body(
                epoch_id,
                f"{type(exc).__name__}: {exc}",
            ),
            mode=None,
        )
        if meta_loop_emitter is not None and invocation_id is not None:
            try:
                await meta_loop_emitter.judgment_emitted(
                    invocation_id=invocation_id,
                    judge_name=judge_name,
                    verdict_kind="boolean",
                    score=None,
                    detail=f"{type(exc).__name__}: {exc}",
                    latency_s=time.monotonic() - started_at,
                )
            except Exception:  # noqa: BLE001 — additive telemetry only
                pass
        return target

    if meta_loop_emitter is not None and invocation_id is not None:
        try:
            await meta_loop_emitter.judgment_emitted(
                invocation_id=invocation_id,
                judge_name=judge_name,
                verdict_kind="rubric",
                # No structured score available from a markdown insight —
                # the dashboard treats ``None`` as "narrative judgement
                # only" and renders the detail field instead.
                score=None,
                detail=f"insight written ({len(response or '')} chars)",
                latency_s=time.monotonic() - started_at,
            )
        except Exception:  # noqa: BLE001 — additive telemetry only
            pass

    # The LLM body is written verbatim under the provenance marker. The
    # system prompt already constrains it to a markdown shape; we don't
    # second-guess by post-processing.
    body = (
        f"{TRAINING_SLICE_ANALYSIS_MARKER}\n{response.strip()}\n"
        if response and response.strip()
        else _empty_insight_body(epoch_id, summary)
    )
    atomic_write_text(target, body, mode=None)
    return target


def load_latest_insight(workspace_root: Path, epoch_id: str) -> str:
    """The most recent round's insight, for the next round's proposal evidence.

    Reads only the highest-numbered ``insights/round_{N}.md`` (the zero
    padding makes lexicographic order numeric), so the evidence holds one
    analysis however many rounds the epoch has run. ``insights/latest.md``,
    which ``zicato inspect telemetry`` writes when no round is given, is an
    operator report and is not read.

    The file is delivered, without its first line, only when that line is
    :data:`TRAINING_SLICE_ANALYSIS_MARKER`. Otherwise, and when the epoch
    has no round insight or the file cannot be read, the result is the
    empty string, which omits the evidence block. An older marked file is
    never substituted for an unmarked latest one. A delivered insight longer
    than :data:`INSIGHT_LIMIT_CHARS` is cut to that length and followed by
    a note saying so.
    """

    files = sorted(_insights_dir(workspace_root, epoch_id).glob("round_*.md"))
    if not files:
        return ""
    try:
        text = files[-1].read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    marker, _, body = text.partition("\n")
    body = body.strip()
    if marker.strip() != TRAINING_SLICE_ANALYSIS_MARKER or not body:
        return ""
    if len(body) > INSIGHT_LIMIT_CHARS:
        body = (
            f"{body[:INSIGHT_LIMIT_CHARS].rstrip()}\n"
            f"[... truncated: the insight exceeds {INSIGHT_LIMIT_CHARS} chars ...]"
        )
    return body + "\n"


__all__ = [
    "INSIGHT_LIMIT_CHARS",
    "TRAINING_SLICE_ANALYSIS_MARKER",
    "analyze_epoch_telemetry",
    "load_latest_insight",
    "proposer_visible_entry_ids",
]
