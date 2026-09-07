"""Persisted evaluation suggestions, admission summaries, and deterministic ranking.

Synthesis produces draft artifacts; admission adds measured evidence. This owner
validates the complete stored collection before exposing suggestions to review
or draft application. Corruption cannot become an empty or partial inbox.
The callable protocols resolve synthesis and admission only when requested;
reading records never starts either operation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from zicato.core.measurement import SYNTHESIS_REPLICATE_BASE as SYNTHESIS_REPLICATE_BASE
from zicato.epoch._storage import RecordError
from zicato.storage import atomic_write_json

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


@dataclass(frozen=True, slots=True)
class Suggestion:
    """One synthesised, optionally admission-measured eval suggestion (§3–§5).

    The persisted fields retain the draft artifact, its provenance, and the
    optional admission measurements described in EVAL-SYNTHESIS.md §3–§5.

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
        judge JSON synthesis drafted (validated against the real loader before
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
    _json: str | None = field(default=None, repr=False, compare=False)

    def to_json(self) -> dict[str, Any]:
        values = {
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
        stored = json.loads(self._json) if self._json is not None else {}
        stored.update(values)
        return stored

    @classmethod
    def from_json(cls, raw: Any) -> Suggestion:
        """Accept recorded suggestion facts without coercion or probe execution."""
        if not isinstance(raw, dict):
            raise RecordError("suggestion: expected a JSON object")
        for key in ("suggestion_id", "subject", "summary", "rationale"):
            if not isinstance(raw.get(key), str):
                raise RecordError(f"suggestion: {key} must be a string")
        if not raw["suggestion_id"]:
            raise RecordError("suggestion: suggestion_id must not be empty")
        vocabularies = {
            "suggestion_type": (
                SUGGESTION_REGRESSION_ENTRY,
                SUGGESTION_COVERAGE_ENTRY,
                SUGGESTION_JUDGE,
                SUGGESTION_RUBRIC_REVISION,
                SUGGESTION_HARDER_VARIANT,
            ),
            "artifact_kind": (ARTIFACT_BOARD_ENTRY, ARTIFACT_JUDGE, ARTIFACT_RUBRIC_REVISION),
            "target_slice": (SLICE_INCOMING_ROTATION, SLICE_TRAIN, SLICE_EXISTING_JUDGE),
        }
        for key, vocabulary in vocabularies.items():
            if raw.get(key) not in vocabulary:
                raise RecordError(f"suggestion: invalid {key}")
        for key in ("draft_artifact", "provenance"):
            if not isinstance(raw.get(key), dict):
                raise RecordError(f"suggestion: {key} must be an object")
        for key in ("admission", "proposed_op"):
            if key not in raw or (raw[key] is not None and not isinstance(raw[key], dict)):
                raise RecordError(f"suggestion: {key} must be an object or null")
        operation = raw["proposed_op"]
        if operation is not None and (
            not isinstance(operation.get("op"), str)
            or not operation["op"]
            or not isinstance(operation.get("args"), dict)
        ):
            raise RecordError("suggestion: proposed_op requires a name and argument object")
        for key in ("severity_rank", "recency_key", "coverage_key"):
            if isinstance(raw.get(key), bool) or not isinstance(raw.get(key), int):
                raise RecordError(f"suggestion: {key} must be an integer")
        try:
            encoded = json.dumps(raw, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise RecordError(f"suggestion: invalid JSON value: {exc}") from exc
        fields = json.loads(encoded)
        return cls(
            **{
                key: fields[key]
                for key in (
                    "suggestion_id",
                    "suggestion_type",
                    "artifact_kind",
                    "subject",
                    "summary",
                    "rationale",
                    "target_slice",
                    "draft_artifact",
                    "proposed_op",
                    "provenance",
                    "admission",
                    "severity_rank",
                    "recency_key",
                    "coverage_key",
                )
            },
            _json=encoded,
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
    """Synthesis: ranked episodes → suggestions (EVAL-SYNTHESIS.md §3).

    The synthesiser loads the epoch board (to pin regressions / perturb dead
    entries / host judges) from ``workspace_root`` + ``epoch_id``, and resolves
    the evaluation callable for the LLM tier only when ``allow_llm``.

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
    """Admission: suggestions → admission-stamped suggestions (EVAL-SYNTHESIS.md §5)."""

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
    """Late-bind ``reflection.synthesis.synthesize``, or ``None``.

    A monkeypatch point for the CLI round-trip tests (which inject a fake synth
    seam) and the honest degrade when the sibling has not landed yet.
    """
    return _resolve_seam("synthesis", "synthesize")


def resolve_admit() -> AdmitSeam | None:
    """Late-bind ``reflection.admission.admit``, or ``None``."""
    return _resolve_seam("admission", "admit")


# --- persistence (beside findings.json — the reflection idiom) --------------


def write_suggestions(
    workspace_root: Path,
    epoch_id: str,
    reflection_id: str,
    suggestions: list[Suggestion],
) -> Path:
    """Validate and durably publish suggestions in their deterministic rank order."""
    from zicato.core.workspace import reflection_suggestions_path  # noqa: PLC0415

    if not isinstance(reflection_id, str) or not reflection_id:
        raise RecordError("suggestions: reflection_id must be a nonempty string")
    path = reflection_suggestions_path(workspace_root, epoch_id, reflection_id)
    payload = {
        "reflection_id": reflection_id,
        "suggestions": [
            s.to_json()
            for s in rank_suggestions([Suggestion.from_json(s.to_json()) for s in suggestions])
        ],
    }
    if len({s.suggestion_id for s in suggestions}) != len(suggestions):
        raise RecordError("suggestions: duplicate suggestion identity")
    atomic_write_json(path, payload)
    return path


def read_suggestions(workspace_root: Path, epoch_id: str, reflection_id: str) -> list[Suggestion]:
    """Read the accepted collection; absence is empty and present corruption is explicit."""
    from zicato.core.workspace import reflection_suggestions_path  # noqa: PLC0415

    path = reflection_suggestions_path(workspace_root, epoch_id, reflection_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise RecordError(f"suggestions {path}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("suggestions"), list):
        raise RecordError(f"suggestions {path}: expected an object with a suggestions list")
    if raw.get("reflection_id") != reflection_id:
        raise RecordError(f"suggestions {path}: reflection identity differs from its location")
    suggestions = [Suggestion.from_json(item) for item in raw["suggestions"]]
    if len({s.suggestion_id for s in suggestions}) != len(suggestions):
        raise RecordError(f"suggestions {path}: duplicate suggestion identity")
    return suggestions


# --- honest admission rendering (EVAL-SYNTHESIS.md §5) ----------------------


def format_admission(admission: dict[str, Any] | None) -> str:
    """A one-line, HONEST admission summary — measured numbers with n, or ``unmeasured``.

    Renders the §5 record: a probe that did not run reads ``unmeasured``
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


def format_admission_compact(admission: dict[str, Any] | None) -> str:
    """A COMPACT, evidence-tier-led admission summary — consistent with the cards.

    The Console inbox cards / Evals ghost rows lead a suggestion's admission with
    its evidence TIER (``probed`` = a probe was spent / firm; ``planned`` =
    unmeasured / faint), then the flip rate WITH its n (over the advisory ceiling
    is flagged) and the discrimination as ``sep/pairs``. This renders that same
    reading as one text line for ``reflect report``. Honest throughout: an
    unmeasured probe reads ``unmeasured``, never a fabricated ``0.0``.
    """
    if not isinstance(admission, dict):
        return "[planned] flip unmeasured · sep unmeasured"

    noise = admission.get("noise")
    disc = admission.get("discrimination")
    noise_measured = isinstance(noise, dict) and bool(noise.get("measured"))
    disc_measured = isinstance(disc, dict) and bool(disc.get("measured"))
    tier = "probed" if (noise_measured or disc_measured) else "planned"

    if noise_measured and isinstance(noise, dict):
        flip = noise.get("flip_rate")
        runs = noise.get("runs")
        over = (
            f" over the {RECOMMENDED_FLIP_CEILING} ceiling"
            if isinstance(flip, int | float) and flip > RECOMMENDED_FLIP_CEILING
            else ""
        )
        flip_s = f"flip {flip} (n={runs}){over}"
    else:
        flip_s = "flip unmeasured"

    if disc_measured and isinstance(disc, dict):
        sep_s = f"sep {disc.get('separated')}/{disc.get('pairs')}"
    else:
        sep_s = "sep unmeasured"
    return f"[{tier}] {flip_s} · {sep_s}"


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
    """What the LIVE admission probes WOULD spend: reported without spending it.

    Plan mode (``--no-probe``, the default) mines + synthesises + validates
    artifacts only; the execution / noise / discrimination probes (§5) spend
    real champion budget, so they need the operator's explicit go-ahead. This
    reports the
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
            f"    apply: zicato inspect reflection apply <reflection_id> {s.suggestion_id}"
        )
    return "\n".join(lines)


def render_suggestions_md(suggestions: list[Suggestion]) -> list[str]:
    """The 'Eval suggestions' section for the ``reflect report`` Markdown."""
    ranked = rank_suggestions(suggestions)
    lines: list[str] = [f"## Eval suggestions ({len(ranked)})"]
    if not ranked:
        lines.append("(none — run `zicato inspect reflection suggest` to synthesise)")
        lines.append("")
        return lines
    for s in ranked:
        lines.append(f"### [{s.suggestion_type}] {s.subject} → {s.target_slice}")
        lines.append(s.summary)
        if s.rationale:
            lines.append(f"- rationale: {s.rationale}")
        lines.append(f"- admission: {format_admission(s.admission)}")
        lines.append(f"- admission (compact): {format_admission_compact(s.admission)}")
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
                "- apply with: `zicato inspect reflection apply "
                f"{{reflection_id}} {s.suggestion_id}`"
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
    "format_admission_compact",
    "plan_cost",
    "rank_suggestions",
    "read_suggestions",
    "render_suggestions_md",
    "render_suggestions_table",
    "resolve_admit",
    "resolve_synthesize",
    "suggestion_id",
    "write_suggestions",
]
