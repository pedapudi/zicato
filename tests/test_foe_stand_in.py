"""The Foe stand-in speaks the protocol the pinned `foe` package reads.

Every proposer test drives the stand-in rather than the real binary, so
what the stand-in gets wrong the proposer suite cannot see. These cases
hold it against the package itself: each of the four outcomes, a host
tool call answered from Python, the built-in edit loop bounded by the
document's write grant, the fingerprint `foe plan --json` reports, and
the version refusal.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import foe
import pytest

from tests._foe_support import (
    block_turn,
    call_turn,
    error_turn,
    fake_foe_binary,
    read_episode_log,
    return_turn,
    scripted_model,
    text_turn,
)


def _contract(tmp_path: Path, **overrides: object) -> foe.ExecutionContract:
    read_root = tmp_path / "snapshot"
    read_root.mkdir(exist_ok=True)
    write_root = tmp_path / "scratch"
    write_root.mkdir(exist_ok=True)
    fields: dict[str, object] = {
        "name": "stand-in",
        "instructions": {"10-charter": "Do the work."},
        "tools": ["read", "edit", "block"],
        "grants": foe.Grants(read=[read_root], write=[write_root]),
        "budget": foe.Budget(model_calls=4),
        "model": foe.Model(provider="fixture", model="scripted", options={}),
    }
    fields.update(overrides)
    return foe.ExecutionContract(**fields)  # type: ignore[arg-type]


def _run(
    contract: foe.ExecutionContract, tmp_path: Path, turns: list[dict[str, object]]
) -> foe.Outcome:
    contract.model = foe.Model(**scripted_model(tmp_path / "bin", turns))
    return asyncio.run(
        contract.run(
            task="propose something",
            binary=fake_foe_binary(tmp_path / "bin"),
            log_dir=tmp_path / "episode",
        )
    )


def test_a_turn_with_no_tool_calls_completes_with_its_text(tmp_path: Path) -> None:
    outcome = _run(_contract(tmp_path), tmp_path, [text_turn("done here")])
    assert outcome == foe.Completed("done here")


def test_the_block_tool_ends_the_episode_with_its_closed_set_code(tmp_path: Path) -> None:
    outcome = _run(
        _contract(tmp_path), tmp_path, [block_turn("goal-unreachable", "nothing to change")]
    )
    assert outcome == foe.Blocked("goal-unreachable", "nothing to change")


def test_a_spent_model_call_budget_ends_the_episode_exhausted(tmp_path: Path) -> None:
    contract = _contract(tmp_path, budget=foe.Budget(model_calls=2))
    outcome = _run(contract, tmp_path, [call_turn(("read", {"path": "absent"}))])
    assert outcome == foe.Exhausted("model_calls")


def test_a_model_error_ends_the_episode_failed(tmp_path: Path) -> None:
    outcome = _run(_contract(tmp_path), tmp_path, [error_turn("the provider is down")])
    assert isinstance(outcome, foe.Failed)
    assert "the provider is down" in outcome.error


def test_a_binary_that_dies_before_episode_end_is_a_failed_outcome(tmp_path: Path) -> None:
    contract = _contract(tmp_path)
    contract.model = foe.Model(**scripted_model(tmp_path / "bin", [text_turn("never reached")]))
    outcome = asyncio.run(
        contract.run(
            task="propose something",
            binary=fake_foe_binary(tmp_path / "bin", die_after=2),
            log_dir=tmp_path / "episode",
        )
    )
    assert isinstance(outcome, foe.Failed)
    assert "before episode/end" in outcome.error


def test_an_edit_inside_the_write_grant_moves_bytes(tmp_path: Path) -> None:
    target = tmp_path / "scratch" / "prompt.txt"
    outcome = _run(
        _contract(tmp_path),
        tmp_path,
        [
            call_turn(("edit", {"path": str(target), "content": "rewritten"})),
            text_turn("edited"),
        ],
    )
    assert outcome == foe.Completed("edited")
    assert target.read_text(encoding="utf-8") == "rewritten"


def test_an_edit_outside_the_write_grant_is_refused(tmp_path: Path) -> None:
    contract = _contract(tmp_path)
    target = tmp_path / "snapshot" / "prompt.txt"
    target.write_text("original", encoding="utf-8")
    _run(
        contract,
        tmp_path,
        [call_turn(("edit", {"path": str(target), "content": "clobbered"})), text_turn("tried")],
    )
    assert target.read_text(encoding="utf-8") == "original"
    results = [e for e in read_episode_log(tmp_path / "episode") if e["type"] == "tool/result"]
    assert results[0]["data"]["is_error"] is True
    assert "outside every granted root" in results[0]["data"]["rendered"]


def test_a_host_tool_call_is_answered_by_the_python_function(tmp_path: Path) -> None:
    @foe.tool
    def mutation_usage(mutation_id: str) -> dict[str, object]:
        """Report where a mutation point is referenced."""
        return {"id": mutation_id, "count": 2}

    contract = _contract(tmp_path, tools=["read", "edit", "block", mutation_usage])
    outcome = _run(
        contract,
        tmp_path,
        [call_turn(("mutation_usage", {"mutation_id": "p1"})), text_turn("grounded")],
    )
    assert outcome == foe.Completed("grounded")
    results = [e for e in read_episode_log(tmp_path / "episode") if e["type"] == "tool/result"]
    assert results[0]["data"]["value"] == {"id": "p1", "count": 2}


def test_a_returns_schema_completes_with_the_unwrapped_value(tmp_path: Path) -> None:
    schema = {"type": "object", "properties": {"idea": {"type": "string"}}, "required": ["idea"]}
    contract = _contract(tmp_path, done_when=foe.Returns(schema))
    outcome = _run(contract, tmp_path, [return_turn({"idea": "shorten the prompt"})])
    assert outcome == foe.Completed({"idea": "shorten the prompt"})


def test_the_fingerprint_excludes_the_model_and_the_grant_paths(tmp_path: Path) -> None:
    binary = fake_foe_binary(tmp_path / "bin")
    base = _contract(tmp_path).fingerprint(binary)

    moved = tmp_path / "elsewhere"
    moved.mkdir()
    relocated = _contract(tmp_path)
    relocated.grants = foe.Grants(read=[moved], write=[tmp_path / "scratch"])
    assert relocated.fingerprint(binary) == base

    reworded = _contract(tmp_path, instructions={"10-charter": "Do the work carefully."})
    assert reworded.fingerprint(binary) != base


def test_a_binary_stating_another_log_format_version_is_refused(tmp_path: Path) -> None:
    contract = _contract(tmp_path)
    contract.model = foe.Model(**scripted_model(tmp_path / "bin", [text_turn("unreachable")]))
    with pytest.raises(foe.CompatibilityError, match="log format"):
        asyncio.run(
            contract.run(
                task="propose something",
                binary=fake_foe_binary(tmp_path / "bin", log_version=99),
                log_dir=tmp_path / "episode",
            )
        )
