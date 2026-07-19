"""WS-SURFACE — the eval-suggestion surface (persistence + render + seams).

The operator-facing front of eval synthesis (EVAL-SYNTHESIS.md §6). WS-MINE
extracts episodes; **WS-SYNTH** turns them into suggestions (§3), **WS-ADMIT**
stamps admission statistics onto them (§5), and this module is what the operator
touches: the persisted-suggestion shape, its tolerant reader/writer (beside
``findings.json`` — the reflection persistence idiom), the honest render of the
admission stats (§5 — measured numbers with n, ``unmeasured`` states, the
recommended bands as quiet advice, never auto-verdicts), and the two thin SEAM
protocols the CLI calls into.

Contamination note (§4/§7): everything here is operator-facing only and NEVER
enters the proposer envelope. Nothing auto-edits a contract — applying a
suggestion stages a builder DRAFT the operator seals (:mod:`zicato.reflection.apply`).

Seam contract (for the integration merge)
------------------------------------------
The three workstreams build in parallel against the DOC's shapes, not each
other's branches. This module therefore defines the persisted-suggestion JSON
shape (:class:`Suggestion`) and two callable seams mirroring the doc:

* :class:`SynthesizeSeam` — ``(episodes, *, allow_llm) -> list[Suggestion]``
  (WS-SYNTH; ``reflection.synthesis.synthesize``).
* :class:`AdmitSeam` — ``(suggestions, *, probe, workspace_root, epoch_id) ->
  list[Suggestion]`` (WS-ADMIT; ``reflection.admission.admit``).

:func:`resolve_synthesize` / :func:`resolve_admit` late-bind those sibling
modules (absent ⇒ ``None``, an honest degrade), and both are monkeypatch points
for the CLI round-trip tests. At integration, the sibling ``synthesize`` /
``admit`` return dicts or :class:`Suggestion` objects matching this shape; the
readers accept either.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# --- suggestion types (EVAL-SYNTHESIS.md §3; mirror mining.HINT_*) ----------
SUGGESTION_REGRESSION_ENTRY: str = "regression_entry"
SUGGESTION_COVERAGE_ENTRY: str = "coverage_entry"
SUGGESTION_JUDGE: str = "judge_suggestion"
SUGGESTION_RUBRIC_REVISION: str = "rubric_revision"
SUGGESTION_HARDER_VARIANT: str = "harder_variant"

# --- artifact kinds (which typed draft the suggestion carries) -------------
ARTIFACT_BOARD_ENTRY: str = "board_entry"
ARTIFACT_JUDGE: str = "judge"
ARTIFACT_RUBRIC_REVISION: str = "rubric_revision"

# --- target slices (EVAL-SYNTHESIS.md §4) ----------------------------------
SLICE_INCOMING_ROTATION: str = "incoming_rotation"
SLICE_TRAIN: str = "train"
SLICE_EXISTING_JUDGE: str = "existing_judge"

#: Advisory bands (EVAL-SYNTHESIS.md §5) — rendered as quiet advice text next to
#: the measured numbers, NEVER as a silent drop or an auto-verdict.
RECOMMENDED_FLIP_CEILING: float = 0.25
RECOMMENDED_MIN_DISCRIMINATION: int = 1

#: The reserved replicate base WS-ADMIT measures A/A noise at (EVAL-SYNTHESIS.md
#: §5; dev-guide 04 §8.1 — 6000 is the next free base after board reflection's
#: 5000). Declared HERE only for the plan-cost narrative; WS-ADMIT owns the
#: canonical constant + the r0-isolation proof.
SYNTHESIS_REPLICATE_BASE: int = 6000


@dataclass(frozen=True, slots=True)
class Suggestion:
    """One synthesised, optionally admission-measured eval suggestion (§3–§5).

    The persisted JSON shape is the cross-workstream contract; the fields track
    EVAL-SYNTHESIS.md §3 (draft artifact), §4 (provenance), and §5 (admission).

    Fields
    ------
    suggestion_id:
        Content-stable ``sug-{8hex}`` over ``(suggestion_type, subject, sorted
        source_episodes)`` — independent of ranking so a re-run resolves the
        same id.
    suggestion_type:
        One of the five ``SUGGESTION_*`` kinds.
    artifact_kind:
        Which typed draft ``draft_artifact`` holds (``board_entry`` / ``judge``
        / ``rubric_revision``) — the apply seam dispatches on it.
    subject:
        What the suggestion concerns (entry id / judge name / mutation id /
        metric); rides the id + the table.
    summary / rationale:
        One-line summary + the longer why (the motivating episodes).
    target_slice:
        The §4 rotation target — ``incoming_rotation`` (default), ``train`` (a
        regression entry, allowed), or ``existing_judge`` (a rubric revision).
    draft_artifact:
        The BOARD-FORMAT entry JSON or the ``{name, mode, body, severity}``
        judge JSON WS-SYNTH drafted (validated against the real loader before
        it ships, §3).
    proposed_op:
        The ``{op, args}`` the apply seam stages onto a builder draft
        (``add_board_entry`` / ``add_judge``), or ``None`` when no mechanical op
        applies yet (a rubric revision — the recorded gap).
    provenance:
        The §4 block (miner_version, source_episodes, source_refs,
        source_lineage_ids, suggestion_type, target_slice).
    admission:
        The §5 record (execution / noise / discrimination / leakage), or
        ``None`` when synthesis ran without the probe tier (``unmeasured``).
    severity_rank / recency_key / coverage_key:
        The ranking keys inherited from the motivating episode (§2 total order).
    """

    suggestion_id: str
    suggestion_type: str
    artifact_kind: str
    subject: str
    summary: str
    rationale: str
    target_slice: str
    draft_artifact: dict[str, Any] = field(default_factory=dict)
    proposed_op: dict[str, Any] | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    admission: dict[str, Any] | None = None
    severity_rank: int = 0
    recency_key: int = 0
    coverage_key: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "suggestion_id": self.suggestion_id,
            "suggestion_type": self.suggestion_type,
            "artifact_kind": self.artifact_kind,
            "subject": self.subject,
            "summary": self.summary,
            "rationale": self.rationale,
            "target_slice": self.target_slice,
            "draft_artifact": dict(self.draft_artifact),
            "proposed_op": dict(self.proposed_op) if self.proposed_op is not None else None,
            "provenance": dict(self.provenance),
            "admission": dict(self.admission) if self.admission is not None else None,
            "severity_rank": self.severity_rank,
            "recency_key": self.recency_key,
            "coverage_key": self.coverage_key,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Suggestion:
        """Reconstruct a suggestion from its JSON shape (tolerant of extras)."""
        op = raw.get("proposed_op")
        adm = raw.get("admission")
        return cls(
            suggestion_id=str(raw.get("suggestion_id", "")),
            suggestion_type=str(raw.get("suggestion_type", "")),
            artifact_kind=str(raw.get("artifact_kind", "")),
            subject=str(raw.get("subject", "")),
            summary=str(raw.get("summary", "")),
            rationale=str(raw.get("rationale", "")),
            target_slice=str(raw.get("target_slice", "")),
            draft_artifact=dict(raw.get("draft_artifact") or {}),
            proposed_op=dict(op) if isinstance(op, dict) else None,
            provenance=dict(raw.get("provenance") or {}),
            admission=dict(adm) if isinstance(adm, dict) else None,
            severity_rank=int(raw.get("severity_rank", 0) or 0),
            recency_key=int(raw.get("recency_key", 0) or 0),
            coverage_key=int(raw.get("coverage_key", 0) or 0),
        )


def suggestion_id(suggestion_type: str, subject: str, source_episodes: tuple[str, ...]) -> str:
    """Content-stable id — a sha256 over the kind, subject, and sorted episodes."""
    payload = "|".join([suggestion_type, subject, *sorted(source_episodes)])
    return "sug-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def _as_suggestion(item: Any) -> Suggestion:
    """Coerce a seam result item (``Suggestion`` OR its JSON dict) to a suggestion."""
    if isinstance(item, Suggestion):
        return item
    if isinstance(item, dict):
        return Suggestion.from_json(item)
    raise TypeError(f"not a suggestion or suggestion dict: {type(item).__name__}")


def rank_suggestions(suggestions: list[Suggestion]) -> list[Suggestion]:
    """Sort by ``(−severity, −recency, −coverage, suggestion_id)`` — a TOTAL order.

    The same deterministic order the miner ranks episodes by (§2), so the inbox
    and the CLI table are byte-stable across a re-run.
    """
    return sorted(
        suggestions,
        key=lambda s: (-s.severity_rank, -s.recency_key, -s.coverage_key, s.suggestion_id),
    )


# --- the two seams (mirrors of the doc; late-bound to the sibling modules) --


@runtime_checkable
class SynthesizeSeam(Protocol):
    """WS-SYNTH: ranked episodes → suggestions (EVAL-SYNTHESIS.md §3).

    The synthesiser loads the epoch board (to pin regressions / perturb dead
    entries / host judges) from ``workspace_root`` + ``epoch_id``, and resolves
    the auxiliary callable for the LLM tier only when ``allow_llm``.

    ``imported_traces`` (TRAJECTORY-BOOTSTRAP.md §7) carries the foreign-trace
    reconstructions the bootstrap tier drafts entries from; it defaults to empty
    so every existing caller stays valid and the seam is a no-op for them.
    """

    def __call__(
        self,
        episodes: Any,
        *,
        allow_llm: bool = False,
        workspace_root: Path | None = None,
        epoch_id: str | None = None,
        imported_traces: Any = (),
    ) -> list[Any]: ...


@runtime_checkable
class AdmitSeam(Protocol):
    """WS-ADMIT: suggestions → admission-stamped suggestions (EVAL-SYNTHESIS.md §5)."""

    def __call__(
        self,
        suggestions: Any,
        *,
        probe: bool = False,
        workspace_root: Path | None = None,
        epoch_id: str | None = None,
    ) -> list[Any]: ...


def _resolve_seam(module_name: str, attr: str) -> Any | None:
    """Import ``zicato.reflection.<module_name>`` and return its ``attr`` callable.

    ``importlib`` (not a static import) so this file type-checks and imports
    cleanly BEFORE the sibling workstreams land — the parallel-build discipline.
    Absent module / attribute ⇒ ``None`` (the honest degrade).
    """
    import importlib  # noqa: PLC0415

    try:
        module = importlib.import_module(f"zicato.reflection.{module_name}")
    except ImportError:
        return None
    fn = getattr(module, attr, None)
    return fn if callable(fn) else None


def resolve_synthesize() -> SynthesizeSeam | None:
    """Late-bind ``reflection.synthesis.synthesize`` (WS-SYNTH), or ``None``.

    A monkeypatch point for the CLI round-trip tests (which inject a fake synth
    seam) and the honest degrade when the sibling has not landed yet.
    """
    return _resolve_seam("synthesis", "synthesize")


def resolve_admit() -> AdmitSeam | None:
    """Late-bind ``reflection.admission.admit`` (WS-ADMIT), or ``None``."""
    return _resolve_seam("admission", "admit")


# --- persistence (beside findings.json — the reflection idiom) --------------


def write_suggestions(
    workspace_root: Path,
    epoch_id: str,
    reflection_id: str,
    suggestions: list[Suggestion],
) -> Path:
    """Atomically write ``suggestions.json`` (tmp + rename); return the path."""
    from zicato.core.workspace import reflection_suggestions_path  # noqa: PLC0415

    path = reflection_suggestions_path(workspace_root, epoch_id, reflection_id)
    payload = {
        "reflection_id": reflection_id,
        "suggestions": [s.to_json() for s in rank_suggestions(suggestions)],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_suggestions_json(
    workspace_root: Path, epoch_id: str, reflection_id: str
) -> list[dict[str, Any]]:
    """Read the persisted suggestion dicts (tolerant: absence/defect ⇒ ``[]``)."""
    from zicato.core.workspace import reflection_suggestions_path  # noqa: PLC0415

    path = reflection_suggestions_path(workspace_root, epoch_id, reflection_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    if isinstance(raw, dict):
        raw = raw.get("suggestions")
    if not isinstance(raw, list):
        return []
    return [s for s in raw if isinstance(s, dict)]


def read_suggestions(workspace_root: Path, epoch_id: str, reflection_id: str) -> list[Suggestion]:
    """Read persisted suggestions as :class:`Suggestion` objects (tolerant)."""
    out: list[Suggestion] = []
    for raw in read_suggestions_json(workspace_root, epoch_id, reflection_id):
        try:
            out.append(Suggestion.from_json(raw))
        except (TypeError, ValueError):
            continue
    return out


# --- honest admission rendering (EVAL-SYNTHESIS.md §5) ----------------------


def format_admission(admission: dict[str, Any] | None) -> str:
    """A one-line, HONEST admission summary — measured numbers with n, or ``unmeasured``.

    Renders the §5 record exactly: a probe that did not run reads ``unmeasured``
    (never a fabricated 0.0); a measured probe reads its number WITH its n. The
    recommended bands are appended as quiet advice (``advisory: …``), never a
    verdict that drops the suggestion.
    """
    if not isinstance(admission, dict):
        return "unmeasured (plan mode — no probe spent)"

    parts: list[str] = []
    noise = admission.get("noise")
    if isinstance(noise, dict) and noise.get("measured"):
        flip = noise.get("flip_rate")
        runs = noise.get("runs")
        base = noise.get("base")
        base_s = f" @base {base}" if base is not None else ""
        parts.append(f"flip {flip} (n={runs}{base_s})")
    else:
        parts.append("flip unmeasured")

    disc = admission.get("discrimination")
    if isinstance(disc, dict) and disc.get("measured"):
        parts.append(f"sep {disc.get('separated')}/{disc.get('pairs')}")
    else:
        parts.append("sep unmeasured")

    leak = admission.get("leakage")
    if isinstance(leak, dict):
        if leak.get("target_slice_ok") is False:
            parts.append("LEAK: motivating proposer saw the target slice")
        if leak.get("self_preference_flag"):
            parts.append("self-preference: judge shares the answer's model family")

    advisory = _admission_advisory(admission)
    if advisory:
        parts.append(f"advisory: {advisory}")
    return "; ".join(parts)


def _admission_advisory(admission: dict[str, Any]) -> str:
    """Quiet advice text from the recommended bands — never an auto-verdict."""
    notes: list[str] = []
    noise = admission.get("noise")
    if isinstance(noise, dict) and noise.get("measured"):
        flip = noise.get("flip_rate")
        if isinstance(flip, int | float) and flip > RECOMMENDED_FLIP_CEILING:
            notes.append(f"flip above the {RECOMMENDED_FLIP_CEILING} advisory ceiling (noisy eval)")
    disc = admission.get("discrimination")
    if isinstance(disc, dict) and disc.get("measured"):
        sep = disc.get("separated")
        if isinstance(sep, int) and sep < RECOMMENDED_MIN_DISCRIMINATION:
            notes.append("separated nothing (a dead channel before it ships)")
    return "; ".join(notes)


# --- plan-vs-probe cost (the endpoint-gated discipline) --------------------


def plan_cost(suggestions: list[Suggestion], *, replicates: int = 5) -> dict[str, Any]:
    """What the LIVE admission probes WOULD spend — shown, not spent (plan mode).

    Plan mode (``--no-probe``, the default) mines + synthesises + validates
    artifacts only; the execution / noise / discrimination probes (§5) spend
    real champion budget and are endpoint-gated (G3-class). This reports the
    spend they would incur so the operator decides before passing ``--probe``.
    """
    n = len(suggestions)
    return {
        "suggestions": n,
        "would_probe": n,
        "noise_runs": n * replicates,
        "replicate_base": SYNTHESIS_REPLICATE_BASE,
        "note": (
            f"plan mode spent 0 champion runs; --probe would run execution + "
            f"{replicates}-replicate A/A noise (base {SYNTHESIS_REPLICATE_BASE}) + "
            f"discrimination for {n} suggestion(s) — endpoint-gated, real budget"
        ),
    }


# --- table + report rendering (quiet register) -----------------------------


def render_suggestions_table(suggestions: list[Suggestion]) -> str:
    """The ranked suggestions table for ``reflect suggest`` stdout (quiet)."""
    ranked = rank_suggestions(suggestions)
    if not ranked:
        return "no suggestions (a cold or well-covered instrument yields none)"
    lines = [f"{len(ranked)} suggestion(s), ranked:"]
    for i, s in enumerate(ranked, start=1):
        lines.append(
            f"{i:>2}. [{s.suggestion_type}] {s.subject} -> {s.target_slice}\n"
            f"    {s.summary}\n"
            f"    admission: {format_admission(s.admission)}\n"
            f"    apply: zicato reflect apply <reflection_id> {s.suggestion_id}"
        )
    return "\n".join(lines)


def render_suggestions_md(suggestions: list[Suggestion]) -> list[str]:
    """The 'Eval suggestions' section for the ``reflect report`` Markdown."""
    ranked = rank_suggestions(suggestions)
    lines: list[str] = [f"## Eval suggestions ({len(ranked)})"]
    if not ranked:
        lines.append("(none — run `zicato reflect suggest` to synthesise)")
        lines.append("")
        return lines
    for s in ranked:
        lines.append(f"### [{s.suggestion_type}] {s.subject} → {s.target_slice}")
        lines.append(s.summary)
        if s.rationale:
            lines.append(f"- rationale: {s.rationale}")
        lines.append(f"- admission: {format_admission(s.admission)}")
        prov = s.provenance or {}
        lineage = prov.get("source_lineage_ids") or []
        if lineage:
            lines.append(f"- source lineage: {', '.join(str(g) for g in lineage)}")
        foreign = prov.get("foreign_source")
        if isinstance(foreign, dict):
            src = str(foreign.get("source_file", "?"))
            dialect = str(foreign.get("dialect", "?"))
            lines.append(f"- foreign source: {src} ({dialect}) — trajectory bootstrap")
        if s.proposed_op:
            lines.append(
                f"- apply with: `zicato reflect apply {{reflection_id}} {s.suggestion_id}`"
            )
        else:
            lines.append("- apply: recommendation only (no mechanical op — an authoring decision)")
        lines.append("")
    return lines


__all__ = [
    "ARTIFACT_BOARD_ENTRY",
    "ARTIFACT_JUDGE",
    "ARTIFACT_RUBRIC_REVISION",
    "RECOMMENDED_FLIP_CEILING",
    "RECOMMENDED_MIN_DISCRIMINATION",
    "SLICE_EXISTING_JUDGE",
    "SLICE_INCOMING_ROTATION",
    "SLICE_TRAIN",
    "SUGGESTION_COVERAGE_ENTRY",
    "SUGGESTION_HARDER_VARIANT",
    "SUGGESTION_JUDGE",
    "SUGGESTION_REGRESSION_ENTRY",
    "SUGGESTION_RUBRIC_REVISION",
    "SYNTHESIS_REPLICATE_BASE",
    "AdmitSeam",
    "Suggestion",
    "SynthesizeSeam",
    "format_admission",
    "plan_cost",
    "rank_suggestions",
    "read_suggestions",
    "read_suggestions_json",
    "render_suggestions_md",
    "render_suggestions_table",
    "resolve_admit",
    "resolve_synthesize",
    "suggestion_id",
    "write_suggestions",
]
