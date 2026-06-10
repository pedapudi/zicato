"""Parity CONTRACT-HASH gate helper.

The epoch contract hash is the load-bearing identity of an evaluation
contract: it is what ``evolve`` uses to decide whether the contract has
drifted and a new epoch must open. It is canonicalized so spurious edits
(row reordering, whitespace) do not move it — which means an UNCHANGED
contract must hash to the SAME value across any behavior-preserving
refactor. If a refactor moves the hash for an unchanged contract, every
operator's workspace would spuriously roll its epoch on the next run.

This gate pins the full contract hash AND every per-component hash for a
fixed fixture contract (the ``target_1_presentation`` example) to a
committed golden. The component breakdown localizes a regression to the
exact canonicalizer (board / brief / scoring / entrypoint / mutable_trees
/ proposer) that moved.

Run directly:  ``uv run python tools/parity/lib/contract_hash.py [--update]``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "contract_hash.json"

# The example contract, pinned with fixed entrypoint + mutable_trees so the
# hash depends only on committed file contents + these literals (never on
# anything host- or clock-derived).
_EXAMPLE = _REPO_ROOT / "examples" / "zicato_examples" / "target_1_presentation"
_ENTRYPOINT = "zicato_examples.target_1_presentation.agent:root_agent"
_MUTABLE_TREES = ("agent",)


def compute() -> dict[str, object]:
    from zicato.epoch.contract import (
        ContractInputs,
        compute_component_hashes,
        compute_contract_hash,
    )

    inputs = ContractInputs(
        board_path=_EXAMPLE / "board.jsonl",
        brief_path=_EXAMPLE / "rubric.md",
        scoring_path=_EXAMPLE / "scoring.json",
        entrypoint=_ENTRYPOINT,
        mutable_trees=_MUTABLE_TREES,
        proposer_path=None,
    )
    return {
        "fixture": "target_1_presentation",
        "entrypoint": _ENTRYPOINT,
        "mutable_trees": list(_MUTABLE_TREES),
        "contract_hash": compute_contract_hash(inputs),
        "component_hashes": compute_component_hashes(inputs),
    }


def _canonical(doc: dict[str, object]) -> str:
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    update = "--update" in argv
    text = _canonical(compute())
    if update:
        _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN.write_text(text, encoding="utf-8")
        print(f"wrote {_GOLDEN}")
        return 0
    if not _GOLDEN.exists():
        print(f"FAIL: golden missing at {_GOLDEN}", file=sys.stderr)
        return 1
    expected = _GOLDEN.read_text(encoding="utf-8")
    if text != expected:
        print(
            "FAIL: CONTRACT-HASH drift — an unchanged contract hashes "
            "differently. Refactor changed the contract canonicalizer.",
            file=sys.stderr,
        )
        sys.stderr.write("--- expected ---\n" + expected)
        sys.stderr.write("--- actual ---\n" + text)
        return 1
    print("OK: CONTRACT-HASH matches golden")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
