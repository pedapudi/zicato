"""The deterministic policy harness — NO LLM anywhere.

:class:`DeterministicPolicyAdapter` is a real
:class:`zicato.adapters.base.HarnessAdapter`-shaped object whose session
synthesises its output purely from the ``STYLE_RULES`` token list in the
generation snapshot it was loaded from. Because the snapshot IS the
input, the full evolve loop (propose → apply → subprocess tournament
worker → reduce → gate) is exercised with a scalar that is an exact,
hand-computable function of the remaining defect tokens:

* every remaining token emits ONE ``drift_detected`` frame at severity
  ``info`` (→ ``+1.0`` drift loss per run under the example contract's
  ``severity_weights``), and
* each KNOWN defect token suppresses one output feature, failing exactly
  one board predicate (see :mod:`.predicates`).

The session implements the rich ``run(entry, sinks, config)`` shape and
emits real goldfive lifecycle frames (``run_started`` …
``drift_detected`` × k … ``run_completed``) through the worker's sink
list, so the REAL reducer computes the loss from a real events file —
no telemetry stubs.

The adapter is subprocess-safe: ``worker_spec()`` returns the
``{"kind": "import", "factory": ...}`` shape both
:func:`zicato.adapter_factory.make_adapter_from_config` and the
tournament worker's ``_build_adapter`` reconstruct from a dotted path.

:class:`NoisyPolicyAdapter` (Tier 2) is the SEEDED-NOISE variant: true
quality stays the policy's token set, but each run's measured pass/drift
is a reproducible draw around it — the RNG seed derives only from
``(workspace seed, generation id, entry id, replicate index)``, so the
stochastic operating characteristics of the decision procedure can be
asserted in CI. See :func:`draw_measured_tokens`.
"""

from __future__ import annotations

import ast
import hashlib
import random
import time
from pathlib import Path
from typing import Any

from zicato.core import RunResult

#: Where the policy lives inside a generation snapshot. The workspace
#: registers the example's ``agent/`` directory as its mutable tree, so
#: the seeded ``v0`` snapshot (and every derived child) carries it under
#: the tree's basename.
POLICY_RELPATH = Path("agent") / "policy.py"

#: The defect tokens the harness understands, mapped to the feature they
#: suppress. Unknown tokens still count as one drift frame each but
#: suppress nothing — a generic defect.
KNOWN_DEFECTS = ("verbose-prose", "omit-summary", "skip-citations", "fabricate-metrics")

#: The filler paragraph appended while ``verbose-prose`` remains. Long
#: enough on its own to blow the ``is_concise`` predicate's character
#: budget (see :data:`.predicates.CONCISE_MAX_CHARS`).
_FILLER = "FILLER: " + "meandering prose that adds nothing " * 24

#: ``BoardEntry.context`` keys carrying run provenance to the session.
#: Kept in sync with the tournament runner's
#: ``zicato.tournament.worker_transport._GENERATION_ID_CONTEXT_KEY`` /
#: ``_REPLICATE_INDEX_CONTEXT_KEY`` — the two ends meet on these strings.
#: The runner stamps the generation id onto every worker entry, and the
#: replication loop stamps the replicate index for replicates > 0, so a
#: session can derive its noise seed from stable identifiers even though
#: it only ever sees an ephemeral snapshot copy with a throwaway name.
GENERATION_ID_CONTEXT_KEY = "generation_id"
REPLICATE_INDEX_CONTEXT_KEY = "replicate_index"

#: Prefix of the INTERMITTENT defect-token form the noisy harness
#: understands: ``sometimes-<pct>-<token>`` behaves as ``<token>`` with
#: probability ``pct/100`` per run draw, and is absent otherwise — a
#: defect that manifests intermittently, the real-world flaky behaviour
#: the decision procedure must resolve. Under the DETERMINISTIC adapter
#: the whole token is simply an unknown defect (one drift frame, no
#: feature suppressed), so deterministic boards are unaffected.
_INTERMITTENT_PREFIX = "sometimes-"


def stable_noise_seed(
    workspace_seed: int,
    generation_key: str,
    entry_id: str,
    replicate_index: int,
) -> int:
    """Derive one run's RNG seed from its stable identifiers.

    ``sha256`` over the joined identifier tuple, truncated to 64 bits.
    Deterministic across processes and interpreter versions (no
    ``hash()`` randomisation, no wall clock, no global RNG), so a run is
    exactly reproducible given the same ``(workspace seed, generation,
    entry, replicate)`` coordinate — and two coordinates that differ in
    ANY component draw independently.
    """
    material = f"{workspace_seed}|{generation_key}|{entry_id}|{replicate_index}"
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def parse_intermittent_token(token: str) -> tuple[str, float] | None:
    """Parse a ``sometimes-<pct>-<token>`` form, or ``None`` for any other.

    Returns ``(inner_token, probability)`` with probability in ``[0, 1]``.
    A malformed percentage yields ``None`` (the token then counts as a
    plain unknown defect — one drift frame, nothing suppressed — rather
    than raising inside a scoring run).
    """
    if not token.startswith(_INTERMITTENT_PREFIX):
        return None
    rest = token[len(_INTERMITTENT_PREFIX) :]
    pct_text, sep, inner = rest.partition("-")
    if not sep or not inner:
        return None
    try:
        pct = float(pct_text)
    except ValueError:
        return None
    if not 0.0 <= pct <= 100.0:
        return None
    return inner, pct / 100.0


def draw_measured_tokens(
    tokens: list[str],
    rng: random.Random,
    noise_sigma: float,
) -> list[str]:
    """One run's MEASURED defect-token draw around the policy's true tokens.

    Two stochastic layers, both driven by the caller's seeded ``rng`` in a
    fixed order (so the draw is a pure function of the seed):

    1. **Intermittent manifestation** — each ``sometimes-<pct>-<token>``
       manifests as its inner token with probability ``pct/100`` (in
       token-list order). This is TRUE behaviour variance: the policy's
       expected quality genuinely sits between "defect present" and
       "defect fixed".
    2. **Measurement flips** — each KNOWN defect's measured presence is
       flipped with probability ``noise_sigma`` (in :data:`KNOWN_DEFECTS`
       order). This is OBSERVATION noise: the run degrades a feature the
       policy would have produced, or produces one it would have
       suppressed, exactly like a stochastic agent/judge would.

    Unknown tokens pass through deterministically (one drift frame each,
    no flip), so a planted noise-free effect stays available to the
    operating-characteristics tests. Returned order is KNOWN defects
    first (:data:`KNOWN_DEFECTS` order) then the surviving unknown tokens
    in their original order.
    """
    manifested: list[str] = []
    for token in tokens:
        intermittent = parse_intermittent_token(token)
        if intermittent is None:
            manifested.append(token)
            continue
        inner, prob = intermittent
        if rng.random() < prob:
            manifested.append(inner)
    measured: list[str] = []
    present = set(manifested)
    for known in KNOWN_DEFECTS:
        is_present = known in present
        if rng.random() < noise_sigma:
            is_present = not is_present
        if is_present:
            measured.append(known)
    measured.extend(t for t in manifested if t not in KNOWN_DEFECTS)
    return measured


def parse_style_tokens(policy_source: str) -> list[str]:
    """Extract the ``STYLE_RULES`` token list from policy-module source.

    Parses the module with :mod:`ast` (never imports it — the snapshot
    under evaluation is untrusted, proposer-patched code) and reads the
    string assigned to ``STYLE_RULES``. Tokens are ``;``-separated,
    whitespace-stripped, empties dropped, order preserved.

    An OPTIONAL ``STYLE_RULES_EXTRA`` assignment (additive; the WS-REC
    two-marker recombination OC splits its defects across two mutation
    points so two single-fix challengers touch DISJOINT ids) contributes
    its tokens APPENDED after ``STYLE_RULES``'s. A policy without the
    variable — every shipped example — parses byte-identically to before
    the variable existed.

    A policy that no longer parses, lost its ``STYLE_RULES`` assignment,
    or assigns a non-string to either variable, yields the sentinel token
    ``["broken-policy"]`` — one generic defect — rather than raising, so
    a destructive patch that somehow survives validation still scores
    (badly) instead of crashing the worker.
    """
    try:
        tree = ast.parse(policy_source)
    except SyntaxError:
        return ["broken-policy"]

    def _tokens_of(node: ast.Assign) -> list[str] | None:
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return [t for t in (t.strip() for t in value.value.split(";")) if t]
        return None

    found: dict[str, list[str] | None] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                name = target.id if isinstance(target, ast.Name) else ""
                if name in ("STYLE_RULES", "STYLE_RULES_EXTRA") and name not in found:
                    found[name] = _tokens_of(node)
    base = found.get("STYLE_RULES")
    if base is None:
        return ["broken-policy"]
    if "STYLE_RULES_EXTRA" in found:
        extra = found["STYLE_RULES_EXTRA"]
        if extra is None:
            return ["broken-policy"]
        return base + extra
    return base


def synthesize_output(entry_input: str, tokens: list[str]) -> str:
    """Deterministically render the run's ``final_output`` from the tokens.

    The base note always opens with ``NOTE:`` (the stable control
    feature). Each KNOWN defect token then suppresses — or injects —
    exactly one feature:

    * no ``omit-summary``     → a ``SUMMARY:`` line is present.
    * no ``skip-citations``   → a ``[source: ...]`` citation is present.
    * ``verbose-prose``       → the long filler paragraph is appended.
    * ``fabricate-metrics``   → an unverified ``METRIC-CLAIM:`` line is
      appended.
    """
    parts = [f"NOTE: {entry_input.strip()} — handled deterministically."]
    if "omit-summary" not in tokens:
        parts.append("SUMMARY: key points captured.")
    if "skip-citations" not in tokens:
        parts.append("[source: workspace-records]")
    if "fabricate-metrics" in tokens:
        parts.append("METRIC-CLAIM: growth 99.9% (unverified).")
    if "verbose-prose" in tokens:
        parts.append(_FILLER)
    return "\n".join(parts)


def _run_identifier(entry: Any) -> str:
    """One run's stable id, unique per ``(generation, entry, replicate)``.

    The historical ``conv-<entry>`` id was REUSED across generations and
    replicates, so the analytical index's ``runs`` rows (PRIMARY KEY
    ``run_id``) were silently overwritten as the lineage advanced — only
    the last generation's runs survived (task #11). The generation id and
    replicate index are recovered from the same ``entry.context`` keys the
    noisy session already reads (the runner stamps the generation onto
    every worker entry; the replication loop stamps replicates > 0), so
    the id is a pure function of the run's stable coordinate:
    ``conv-<generation>-<entry>[-r<replicate>]``. An ad-hoc drive outside
    the worker (no generation in context) keeps the historical
    ``conv-<entry>`` form.
    """
    context = dict(getattr(entry, "context", {}) or {})
    generation = str(context.get(GENERATION_ID_CONTEXT_KEY, "") or "")
    try:
        replicate = int(context.get(REPLICATE_INDEX_CONTEXT_KEY, "0") or 0)
    except (TypeError, ValueError):
        replicate = 0
    parts = ["conv"]
    if generation:
        parts.append(generation)
    parts.append(str(entry.id))
    if replicate:
        parts.append(f"r{replicate}")
    return "-".join(parts)


def _drift_event(run_id: str, sequence: int, token: str) -> Any:
    """Build one ``drift_detected`` frame for a remaining defect token.

    Constructed directly on the proto (via :func:`goldfive.events.new_event`
    plus the ``goldfive.v1`` enum values) so the ``kind`` / ``severity``
    fields land on the wire exactly as the reducer's normaliser expects:
    kind ``unexpected_output``, severity ``info`` — a ``1.0``
    contribution per frame under the example contract's
    ``severity_weights`` (and any contract keeping ``info`` at ``1.0``).
    """
    from goldfive.events import new_event  # noqa: PLC0415
    from goldfive.pb.goldfive.v1 import types_pb2  # noqa: PLC0415

    evt = new_event(run_id, sequence)
    evt.drift_detected.kind = types_pb2.DriftKind.Value("DRIFT_KIND_UNEXPECTED_OUTPUT")
    evt.drift_detected.severity = types_pb2.DriftSeverity.Value("DRIFT_SEVERITY_INFO")
    evt.drift_detected.detail = f"planted defect token: {token}"
    return evt


#: Map a judge verdict's severity string to the goldfive proto enum name.
#: A judge declares its severity as ``info`` / ``warning`` / ``critical``;
#: the paired ``custom`` drift frame carries it onto the wire so the
#: reducer weighs it through ``severity_weights`` exactly like a real run.
_JUDGE_SEVERITY_TO_PROTO = {
    "info": "DRIFT_SEVERITY_INFO",
    "warning": "DRIFT_SEVERITY_WARNING",
    "critical": "DRIFT_SEVERITY_CRITICAL",
}


def _judge_drift_event_pair(
    run_id: str,
    judgement_seq: int,
    drift_seq: int,
    judge_name: str,
    severity_str: str,
    detail: str,
) -> tuple[Any, Any]:
    """Build the paired ``judgement_emitted`` + ``custom`` ``drift_detected`` frames.

    A process judge that finds a violation emits a drift-flavoured
    ``JudgementEmitted`` (``verdict_kind="drift"``, carrying ``judge_name``)
    IMMEDIATELY followed by a ``custom``-kind ``DriftDetected`` — the exact
    contiguous pair the reducer folds into a ``custom:<judge_name>``
    :class:`~zicato.core.types.DriftCount` (see
    :func:`zicato.telemetry.reducer.reduce_loss`). This is what goldfive
    publishes on a real ADK run; the deterministic harness synthesises the
    identical wire shape so declared judges are exercised end-to-end with
    NO live LLM.
    """
    from goldfive.events import new_event  # noqa: PLC0415
    from goldfive.pb.goldfive.v1 import types_pb2  # noqa: PLC0415

    sev_enum = types_pb2.DriftSeverity.Value(
        _JUDGE_SEVERITY_TO_PROTO.get(severity_str, "DRIFT_SEVERITY_INFO")
    )
    custom_kind = types_pb2.DriftKind.Value("DRIFT_KIND_CUSTOM")

    # On ``judgement_emitted`` these are STRING fields (the reducer pairs on
    # ``verdict_kind`` + ``judge_name``); on ``drift_detected`` they are the
    # DriftKind / DriftSeverity ENUMs (like every other drift frame).
    judgement = new_event(run_id, judgement_seq)
    judgement.judgement_emitted.judge_name = judge_name
    judgement.judgement_emitted.verdict_kind = "drift"
    judgement.judgement_emitted.drift_kind = "custom"
    judgement.judgement_emitted.severity = severity_str
    if detail:
        judgement.judgement_emitted.detail = detail

    drift = new_event(run_id, drift_seq)
    drift.drift_detected.kind = custom_kind
    drift.drift_detected.severity = sev_enum
    if detail:
        drift.drift_detected.detail = detail
    return judgement, drift


async def _emit_declared_judge_drifts(
    *,
    entry: Any,
    config: Any,
    final_output: str,
    run_id: str,
    seq: int,
    emit: Any,
    sink_list: list[Any],
) -> int:
    """Invoke each of ``entry.judges`` on the run and emit paired frames.

    The deterministic harness historically ignored ``entry.judges`` — it
    hand-emitted only its own token drifts — so a board that declared a
    process judge produced ZERO ``custom:<name>`` counts and loop-health
    flagged the judge "never fired" even though the harness never invoked
    it. This closes that invocation gap: every declared judge is built
    through the SAME :func:`zicato.judge_runtime.judge_spec_to_goldfive`
    seam a real ADK run uses and evaluated once over the synthesised
    output; a drift-flavoured verdict is written to the wire as the paired
    ``judgement_emitted`` + ``custom`` ``drift_detected`` frames.

    Additive by construction: an entry with no declared judges skips the
    whole block, so a judge-free board (the convergence oracle's) emits a
    byte-identical event stream. Best-effort — a judge that raises never
    breaks the deterministic run. Returns the advanced sequence counter.

    FIDELITY CAVEAT — this proves judge *plumbing*, not *firing fidelity*.
    target_0 evaluates each judge ONCE over the synthesised ``final_output``;
    a real ADK run dispatches judges per reasoning observation over incremental
    chain-of-thought (which the deterministic harness has none of). So a judge
    that fires here need not fire in a real run (and vice versa), and the count
    is ≤1 per run where a real (esp. multi-turn) run can accumulate N>1. Use
    target_0 to verify a declared judge is WIRED (invoked → paired frames →
    reducer folds a ``custom:<name>`` metric), NOT to validate a judge's
    firing decision, per-judge weighting, or severity/count magnitude. Note
    also that an INLINE judge here consults ``config.effective_judge_call_llm``
    — a live model in a real evolve run; the oracle board stays judge-free /
    python-mode to keep G3's no-LLM guarantee.
    """
    judges = tuple(getattr(entry, "judges", ()) or ())
    if not judges:
        return seq

    # Lazy imports: only a board that declares a judge pays for them, so
    # `zicato --help` and the judge-free oracle path stay untouched.
    from goldfive.judges import JudgeContext  # noqa: PLC0415

    from zicato.judge_runtime import judge_spec_to_goldfive  # noqa: PLC0415

    # Inline judges audit reasoning text via the evaluation/judge endpoint;
    # python judges are deterministic and ignore it. A missing accessor
    # (an ad-hoc config) degrades to no aux — inline judges then no-signal
    # (they catch their own errors), python judges still fire.
    aux_accessor = getattr(config, "effective_judge_call_llm", None)
    aux_call_llm = aux_accessor() if callable(aux_accessor) else None
    ctx = JudgeContext(reasoning_text=final_output, transcript=(final_output,))

    for spec in judges:
        try:
            live_judge = judge_spec_to_goldfive(spec, aux_call_llm)
            verdict = await live_judge.evaluate(ctx)
        except Exception as exc:  # noqa: BLE001 — a judge must never break a run
            import logging  # noqa: PLC0415

            logging.getLogger(__name__).debug(
                "target_0 harness: judge %r evaluate raised %s; skipping",
                getattr(spec, "name", "?"),
                exc,
            )
            continue
        if not getattr(verdict, "drift_emitted", False):
            continue  # the judge ran and found nothing — not "never fired"
        judge_name = str(getattr(live_judge, "name", "") or getattr(spec, "name", "") or "")
        severity_str = str(getattr(verdict, "severity", "") or "info")
        detail = str(getattr(verdict, "detail", "") or "")
        judgement_evt, drift_evt = _judge_drift_event_pair(
            run_id, seq + 1, seq + 2, judge_name, severity_str, detail
        )
        await emit(sink_list, judgement_evt)
        await emit(sink_list, drift_evt)
        seq += 2
    return seq


class _PolicySession:
    """One loaded generation: synthesise output + frames from the policy."""

    def __init__(self, generation_root: Path) -> None:
        self._generation_root = Path(generation_root)

    async def run(self, entry: Any, sinks: Any, config: Any) -> RunResult:
        """Drive one board entry deterministically (rich session shape).

        Reads ``agent/policy.py`` from THIS session's generation root —
        the per-run snapshot copy the worker mounted — so the output is
        a pure function of the generation under evaluation. Emits the
        real goldfive lifecycle frames through ``sinks`` (the worker's
        JSONL persistence sink), then returns the :class:`RunResult`
        the worker evaluates the entry's predicate expectation against.
        """
        started = time.monotonic()
        run_id = _run_identifier(entry)

        policy_path = self._generation_root / POLICY_RELPATH
        try:
            policy_source = policy_path.read_text(encoding="utf-8")
        except OSError:
            policy_source = ""
        tokens = self._measured_tokens(
            entry, config, parse_style_tokens(policy_source), policy_source
        )
        final_output = synthesize_output(str(getattr(entry, "input", "") or ""), tokens)

        # Emit the lifecycle frames: run_started, one drift_detected per
        # remaining defect token, run_completed. Guarded on goldfive being
        # importable so the adapter degrades (no frames, zero drift) in a
        # stripped environment where the worker attached no sinks anyway.
        try:
            from goldfive.events import (  # noqa: PLC0415
                emit,
                run_completed_event,
                run_started_event,
            )

            sink_list = list(sinks or [])
            if sink_list:
                seq = 1
                await emit(
                    sink_list,
                    run_started_event(
                        run_id=run_id,
                        sequence=seq,
                        goal_summary=str(getattr(entry, "input", "") or ""),
                    ),
                )
                for token in tokens:
                    seq += 1
                    await emit(sink_list, _drift_event(run_id, seq, token))
                # Invoke any board-declared process judges over the run's
                # synthesised output and emit their paired judgement +
                # custom-drift frames. No-op (byte-identical) when the entry
                # declares no judges, so the convergence oracle is unaffected.
                seq = await _emit_declared_judge_drifts(
                    entry=entry,
                    config=config,
                    final_output=final_output,
                    run_id=run_id,
                    seq=seq,
                    emit=emit,
                    sink_list=sink_list,
                )
                seq += 1
                await emit(
                    sink_list,
                    run_completed_event(
                        run_id=run_id,
                        sequence=seq,
                        outcome_summary=f"deterministic note with {len(tokens)} defect(s)",
                    ),
                )
        except ModuleNotFoundError:
            pass

        runtime_ms = max(1, int((time.monotonic() - started) * 1000))
        return RunResult(
            run_id=run_id,
            entry_id=str(entry.id),
            final_output=final_output,
            transcript=(final_output,),
            runtime_ms=runtime_ms,
        )

    def _measured_tokens(
        self,
        entry: Any,
        config: Any,
        tokens: list[str],
        policy_source: str,
    ) -> list[str]:
        """The token set THIS run measures — identity for the deterministic base.

        The single seam the noisy session overrides: the base session
        measures exactly the true tokens (byte-identical to the Tier-1
        behaviour), so output, drift frames, and predicate outcomes stay
        an exact function of the policy.
        """
        del entry, config, policy_source
        return tokens


class _NoisyPolicySession(_PolicySession):
    """A :class:`_PolicySession` whose measurement is a seeded noisy draw.

    True quality stays the policy's token set; the MEASURED pass/drift of
    each run is a draw around it (see :func:`draw_measured_tokens`). The
    draw's RNG seed derives ONLY from stable identifiers — the workspace
    seed (``config.seed``), the generation id (stamped onto
    ``entry.context`` by the runner; content digest of the policy as the
    ad-hoc fallback), the entry id, and the replicate index — so a run is
    exactly reproducible in CI yet varies across replicates, generations,
    and workspace seeds exactly like real noise. No wall clock, no global
    RNG.
    """

    def __init__(self, generation_root: Path, noise_sigma: float) -> None:
        super().__init__(generation_root)
        self._noise_sigma = float(noise_sigma)

    def _measured_tokens(
        self,
        entry: Any,
        config: Any,
        tokens: list[str],
        policy_source: str,
    ) -> list[str]:
        context = dict(getattr(entry, "context", {}) or {})
        generation_key = str(context.get(GENERATION_ID_CONTEXT_KEY, "") or "")
        if not generation_key:
            # Ad-hoc drive outside the worker: fall back to the policy's
            # content digest — still a stable identifier, never the
            # ephemeral snapshot path.
            generation_key = hashlib.sha256(policy_source.encode("utf-8")).hexdigest()
        try:
            replicate_index = int(context.get(REPLICATE_INDEX_CONTEXT_KEY, "0") or 0)
        except (TypeError, ValueError):
            replicate_index = 0
        seed = stable_noise_seed(
            workspace_seed=int(getattr(config, "seed", None) or 0),
            generation_key=generation_key,
            entry_id=str(entry.id),
            replicate_index=replicate_index,
        )
        return draw_measured_tokens(tokens, random.Random(seed), self._noise_sigma)


class DeterministicPolicyAdapter:
    """Adapter whose sessions score a snapshot's policy deterministically.

    ``load`` captures the generation root it is handed (the worker's
    per-run ephemeral snapshot copy) and passes it to the session, so
    each run reads the policy of exactly the generation under
    evaluation. ``mutation_points()`` returns the empty list — the
    orchestrator enumerates the ``# zicato:mutable`` markers from the
    snapshot itself.
    """

    name = "deterministic_policy"

    def load(self, generation_root: Path) -> _PolicySession:
        return _PolicySession(generation_root)

    def mutation_points(self, source_roots: Any = None) -> list[Any]:
        del source_roots
        return []

    def worker_spec(self) -> dict[str, Any]:
        """The subprocess-worker reconstruction spec (kind='import').

        The same shape :func:`zicato.adapter_factory.make_adapter_from_config`
        accepts in ``config.json``, so the workspace declares this
        adapter honestly and the worker rebuilds the identical object.
        """
        return {
            "kind": "import",
            "factory": "zicato_examples.target_0_convergence.harness:make_adapter",
        }


def make_adapter() -> DeterministicPolicyAdapter:
    """Module-level factory for the ``import`` adapter spec."""
    return DeterministicPolicyAdapter()


class NoisyPolicyAdapter(DeterministicPolicyAdapter):
    """The seeded-noise variant of :class:`DeterministicPolicyAdapter`.

    Same policy parsing, output synthesis, frames, and predicates — only
    the per-run MEASUREMENT is a seeded draw around the true token set
    (see :class:`_NoisyPolicySession`). ``noise_sigma`` is the per-known-
    defect flip probability; ``0.0`` reproduces the deterministic adapter
    exactly. The spec round-trips through ``worker_spec()`` with its
    ``args`` payload, so the subprocess worker reconstructs an adapter
    with the SAME sigma — the noise level is part of the honest adapter
    declaration, not ambient state.
    """

    name = "noisy_deterministic_policy"

    def __init__(self, noise_sigma: float) -> None:
        self.noise_sigma = float(noise_sigma)

    def load(self, generation_root: Path) -> _PolicySession:
        return _NoisyPolicySession(generation_root, self.noise_sigma)

    def worker_spec(self) -> dict[str, Any]:
        return {
            "kind": "import",
            "factory": "zicato_examples.target_0_convergence.harness:make_noisy_adapter",
            "args": [{"noise_sigma": self.noise_sigma}],
        }


def make_noisy_adapter(options: dict[str, Any] | None = None) -> NoisyPolicyAdapter:
    """Module-level factory for the noisy ``import`` adapter spec.

    ``options`` is the single positional ``args`` element of the
    ``{"kind": "import", ...}`` spec: ``{"noise_sigma": <float>}``. An
    absent/empty options dict yields sigma ``0.0`` — byte-identical
    behaviour to :func:`make_adapter`.
    """
    sigma = float((options or {}).get("noise_sigma", 0.0))
    return NoisyPolicyAdapter(noise_sigma=sigma)


__all__ = [
    "DeterministicPolicyAdapter",
    "GENERATION_ID_CONTEXT_KEY",
    "KNOWN_DEFECTS",
    "NoisyPolicyAdapter",
    "POLICY_RELPATH",
    "REPLICATE_INDEX_CONTEXT_KEY",
    "draw_measured_tokens",
    "make_adapter",
    "make_noisy_adapter",
    "parse_intermittent_token",
    "parse_style_tokens",
    "stable_noise_seed",
    "synthesize_output",
]
