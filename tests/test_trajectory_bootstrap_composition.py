"""THE UN-MOCKED COMPOSITION TEST — TRAJECTORY-BOOTSTRAP.md §9 (the named deliverable).

The whole chain, end to end, with ZERO resolver monkeypatching:

    a real foreign-trace fixture dir
      → real ``reflect suggest --from-trajectories``
      → real ``import_trajectories``
      → real ``mine_episodes`` (imported source folded in)
      → REAL bootstrap synthesis (WS-BOOT's ``synthesize`` + tier)
      → persisted ``suggestions.json`` with non-empty ``draft_artifact`` + ``proposed_op``
      → real ``reflect apply`` staging a bootstrap entry into a builder draft
        (the sealed contract byte-unchanged)
      → the builder inbox feed sees it.

Plus the goldfive-optional assertion: the whole flow on a workspace whose trace
dir carries ZERO goldfive artifacts (an adk_events + transcript dir).

**The guard mechanism.** This module is RED-PROOF on the WS-WIRE branch and LIVE
at the integration merge: it activates only when WS-BOOT's real §7 bootstrap
capability is present — ``synthesize_bootstrap_suggestions`` exists AND
``synthesize`` accepts ``imported_traces=``. Before WS-BOOT integrates, the
mechanical ``synthesize`` cannot draft a bootstrap entry (the ``bootstrap_*``
hints route to a tier that does not exist yet), so the module SKIPS with a loud
reason rather than failing. It is a capability guard on the real symbol — NEVER
a monkeypatch of the resolver.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from tests._cli_support import registered_workspace
from zicato.cli.discovery import build_cli_root
from zicato.core.workspace import board_path, reflection_suggestions_path

_FIXTURES = Path(__file__).parent / "fixtures"
_TRAJ_DIR = _FIXTURES / "trajectories"
_TRAJ_GF_FREE = _FIXTURES / "trajectories_goldfive_free"


def _bootstrap_capability_present() -> bool:
    """True iff WS-BOOT's real §7 bootstrap tier is integrated on this checkout."""
    try:
        from zicato.reflection import synthesis
    except ImportError:  # pragma: no cover - synthesis always importable here
        return False
    if not hasattr(synthesis, "synthesize_bootstrap_suggestions"):
        return False
    try:
        params = inspect.signature(synthesis.synthesize).parameters
    except (TypeError, ValueError):  # pragma: no cover
        return False
    return "imported_traces" in params


pytestmark = pytest.mark.skipif(
    not _bootstrap_capability_present(),
    reason=(
        "TRAJECTORY-BOOTSTRAP.md §9 un-mocked composition test: WS-BOOT's "
        "synthesize_bootstrap_suggestions + synthesize(imported_traces=) is NOT yet "
        "integrated on this branch — the mechanical synthesise cannot draft a bootstrap "
        "entry, so the whole-chain proof is skipped here (red-proof) and ACTIVATES at the "
        "integration merge (live there). This is a capability guard on the real §7 symbol, "
        "never a resolver monkeypatch."
    ),
)


def _make_workspace(tmp_path: Path) -> tuple[Path, str]:
    return registered_workspace(tmp_path, "boot")


def _run(args: list[str]) -> object:
    return CliRunner(mix_stderr=False).invoke(build_cli_root(), args)


def _first_entry_suggestion(persisted: dict[str, object]) -> dict[str, object]:
    suggestions = persisted["suggestions"]  # type: ignore[index]
    assert isinstance(suggestions, list) and suggestions, "bootstrap produced no suggestions"
    for s in suggestions:
        op = s.get("proposed_op")  # type: ignore[union-attr]
        if isinstance(op, dict) and op.get("op") == "add_board_entry":
            return s  # type: ignore[return-value]
    raise AssertionError("no add_board_entry bootstrap suggestion in the persisted output")


def test_full_chain_unmocked_bootstrap_to_builder_draft(tmp_path: Path) -> None:
    ws, epoch_id = _make_workspace(tmp_path)
    reflection_id = "refl-composition"

    # 1–4: real import → real mine → REAL bootstrap synthesis → persist.
    result = _run(
        [
            "inspect",
            "reflection",
            "suggest",
            "--workspace",
            str(ws),
            "--reflection",
            reflection_id,
            "--from-trajectories",
            str(_TRAJ_DIR),
        ]
    )
    assert result.exit_code == 0, result.output

    persisted = json.loads(
        reflection_suggestions_path(ws, epoch_id, reflection_id).read_text(encoding="utf-8")
    )
    entry_sug = _first_entry_suggestion(persisted)

    # Non-empty draft_artifact + proposed_op (the §9 assertion).
    draft_artifact = entry_sug["draft_artifact"]
    assert isinstance(draft_artifact, dict) and draft_artifact, "empty draft_artifact"
    assert draft_artifact.get("id"), "the drafted entry has no id"
    proposed_op = entry_sug["proposed_op"]
    assert isinstance(proposed_op, dict) and proposed_op.get("op") == "add_board_entry"

    # The foreign-source provenance rode the whole way.
    foreign = entry_sug["provenance"]["foreign_source"]  # type: ignore[index]
    assert foreign["dialect"] in {"goldfive", "adk_events", "transcript"}
    assert foreign["source_file"]

    # 5: real apply stages the bootstrap entry into a builder draft; the SEALED
    # contract is byte-unchanged.
    before = board_path(ws, epoch_id).read_bytes()
    from zicato.reflection.apply import apply_suggestion_to_draft

    applied = apply_suggestion_to_draft(
        workspace_root=ws,
        epoch_id=epoch_id,
        reflection_id=reflection_id,
        suggestion_id=str(entry_sug["suggestion_id"]),
    )
    assert applied.op == "add_board_entry"
    assert "board" in applied.diff["changed_components"]
    assert board_path(ws, epoch_id).read_bytes() == before  # sealed contract untouched

    # 6: the builder inbox feed sees the persisted bootstrap suggestion.
    from zicato.builder.api import _read_suggestions_feed

    feed = _read_suggestions_feed(ws)
    feed_ids = {s["suggestion_id"] for s in feed["suggestions"]}
    assert str(entry_sug["suggestion_id"]) in feed_ids
    feed_sug = next(
        s for s in feed["suggestions"] if s["suggestion_id"] == entry_sug["suggestion_id"]
    )
    assert feed_sug["provenance"]["foreign_source"]["source_file"] == foreign["source_file"]


def test_full_chain_is_goldfive_optional(tmp_path: Path) -> None:
    # The goldfive-optional proof (§1.1 / §9): the whole flow on a trace dir with
    # ZERO goldfive artifacts (an adk_events + transcript dir) still yields a
    # persisted bootstrap suggestion with a non-empty drafted entry.
    ws, epoch_id = _make_workspace(tmp_path)
    reflection_id = "refl-gf-free"

    result = _run(
        [
            "inspect",
            "reflection",
            "suggest",
            "--workspace",
            str(ws),
            "--reflection",
            reflection_id,
            "--from-trajectories",
            str(_TRAJ_GF_FREE),
        ]
    )
    assert result.exit_code == 0, result.output

    persisted = json.loads(
        reflection_suggestions_path(ws, epoch_id, reflection_id).read_text(encoding="utf-8")
    )
    entry_sug = _first_entry_suggestion(persisted)
    assert entry_sug["draft_artifact"], "goldfive-optional path drafted no entry"
    dialect = entry_sug["provenance"]["foreign_source"]["dialect"]  # type: ignore[index]
    assert dialect in {"adk_events", "transcript"}  # no goldfive artifact anywhere
