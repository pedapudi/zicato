#!/usr/bin/env bash
# tools/parity.sh — the behavior-preserving refactor oracle.
#
# Runs a fixed set of gates and reports PASS/FAIL for each, then an overall
# verdict. Each gate either runs the full suite (PYTEST) or diffs a freshly
# computed artifact against a committed golden under tools/parity/golden/.
#
# The contract: on UNCHANGED behavior every gate is GREEN. The refactor is
# validated as isomorphic to the feature-complete integration base by
# keeping every gate GREEN throughout. The goldens were captured from that
# base, so a refactor that moves any observable behavior turns a gate RED.
#
# Gates
# -----
#   PYTEST         the full test suite — BOTH tiers, ~5990 tests — as the
#                  primary behavioral characterization. Must pass. The
#                  explicit -m below restates the pyproject selector minus
#                  its `not slow` term, because a command-line -m REPLACES
#                  that selector: a bare `pytest` here would silently gate
#                  on the default tier alone and skip the thirteen
#                  statistical and end-to-end tests the oracles live in.
#   CONTRACT-HASH  the epoch contract hash (+ per-component hashes) for a
#                  fixed fixture contract is byte-identical to the golden.
#   CLI-HELP       `zicato --help` and every subcommand `--help` is
#                  byte-identical to the golden.
#   REINDEX-DUMP   the SQLite index, rebuilt from a fixture workspace and
#                  dumped to stable text, is byte-identical to the golden.
#   MOCK-GOLDEN    a deterministic, no-live-LLM racing (field 4) mock evolve
#                  under --mode full produces gen_score.json /
#                  experiment.json / loss.json / round_log.jsonl / the
#                  settled field-tournament snapshot / lineage.json
#                  artifacts byte-identical (after masking wall-clock noise)
#                  to the golden.
#   MOCK-GOLDEN-GAUNTLET
#                  the same capture with a single challenger under --mode
#                  full: the field-size-1 selector and the crowning holdout
#                  confirmation a gauntlet round runs.
#   MOCK-GOLDEN-GAUNTLET-FAST
#                  a single challenger under --mode fast: cache-first slot
#                  resolution under a one-challenger field.
#   MOCK-GOLDEN-RACING-FAST
#                  the racing field under --mode fast: every rung resolves
#                  both competitors through the unit cache.
#   MOCK-GOLDEN-TWO-ROUND-RACING
#                  the racing field under --mode full for TWO rounds: the
#                  promoted head advances, the crowned generation defends
#                  the next round from its own patched snapshot, and the
#                  epoch's round directories number on from 0.
#   MOCK-GOLDEN-SWISS
#                  a four-challenger Swiss field under --mode full: fixed
#                  pairings over champion + challengers, Copeland
#                  standings, and the leader's champion-gate confirmation.
#   MOCK-GOLDEN-SINGLE-ELIM
#                  a four-challenger single-elimination bracket under
#                  --mode full: challenger-vs-challenger nodes, then the
#                  champion-vs-survivor final.
#   MOCK-GOLDEN-DOUBLE-ELIM
#                  a four-challenger double-elimination field under --mode
#                  full: winners' bracket, losers' bracket, grand final,
#                  then the champion gate.
#   MYPY           the mypy error count is not worse than the committed
#                  baseline (a refactor should reduce it).
#
# Usage
# -----
#   bash tools/parity.sh                 # run every gate
#   bash tools/parity.sh --update        # (re)capture every golden baseline
#   bash tools/parity.sh --only PYTEST   # run a gate (repeatable, or A,B)
#   bash tools/parity.sh --skip PYTEST   # skip a gate (repeatable, or A,B)
#
# Exit code is 0 only if every selected gate passed.

set -u -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GOLDEN_DIR="$REPO_ROOT/tools/parity/golden"
LIB="$REPO_ROOT/tools/parity/lib"

UPDATE=0
ONLY=()
SKIP=()
while [ $# -gt 0 ]; do
  case "$1" in
    --update) UPDATE=1; shift ;;
    # Split a comma list without an unquoted expansion: an unquoted $() is
    # subject to pathname expansion, so `--only '*'` would glob the cwd.
    --only) IFS=',' read -r -a _vals <<< "$2"; ONLY+=(${_vals[@]+"${_vals[@]}"}); shift 2 ;;
    --skip) IFS=',' read -r -a _vals <<< "$2"; SKIP+=(${_vals[@]+"${_vals[@]}"}); shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# Single-process pytest: the suite's default addopts is `-n auto`; the
# golden-capture gates must run serially in one interpreter.
PYTEST_SERIAL=(uv run pytest -n0 -q)

RESULTS=()  # "GATE\tPASS|FAIL"
OVERALL=0

_selected() {
  local gate="$1"
  if [ ${#ONLY[@]} -gt 0 ]; then
    local found=0
    for g in ${ONLY[@]+"${ONLY[@]}"}; do [ "$g" = "$gate" ] && found=1; done
    [ $found -eq 1 ] || return 1
  fi
  for g in ${SKIP[@]+"${SKIP[@]}"}; do [ "$g" = "$gate" ] && return 1; done
  return 0
}

_record() {
  local gate="$1" status="$2"
  RESULTS+=("$gate"$'\t'"$status")
  [ "$status" = "PASS" ] || OVERALL=1
}

_banner() { printf '\n========== %s ==========\n' "$1"; }

cd "$REPO_ROOT" || exit 2

# --- PYTEST -----------------------------------------------------------------
if _selected PYTEST; then
  _banner "PYTEST (full suite, both tiers — behavioral backbone)"
  if uv run pytest -q -m "not node and not cascade_oc"; then
    _record PYTEST PASS
  else
    _record PYTEST FAIL
  fi
fi

# --- CONTRACT-HASH ----------------------------------------------------------
if _selected CONTRACT-HASH; then
  _banner "CONTRACT-HASH"
  ARG=""; [ "$UPDATE" = "1" ] && ARG="--update"
  if uv run python "$LIB/contract_hash.py" $ARG; then _record CONTRACT-HASH PASS; else _record CONTRACT-HASH FAIL; fi
fi

# --- CLI-HELP ---------------------------------------------------------------
if _selected CLI-HELP; then
  _banner "CLI-HELP"
  ARG=""; [ "$UPDATE" = "1" ] && ARG="--update"
  if uv run python "$LIB/cli_help.py" $ARG; then _record CLI-HELP PASS; else _record CLI-HELP FAIL; fi
fi

# --- REINDEX-DUMP -----------------------------------------------------------
if _selected REINDEX-DUMP; then
  _banner "REINDEX-DUMP"
  if [ "$UPDATE" = "1" ]; then
    if ZICATO_PARITY_UPDATE=1 "${PYTEST_SERIAL[@]}" "$LIB/test_reindex_golden.py"; then _record REINDEX-DUMP PASS; else _record REINDEX-DUMP FAIL; fi
  else
    if "${PYTEST_SERIAL[@]}" "$LIB/test_reindex_golden.py"; then _record REINDEX-DUMP PASS; else _record REINDEX-DUMP FAIL; fi
  fi
fi

# --- MOCK-GOLDEN (one gate per capture lane) --------------------------------
# Each lane is one (tournament structure, runtime mode) pair with its own
# golden. They are separate gates because they execute different production
# branches, so a single combined verdict would not say which configuration
# moved. The selector is the lane name, matched by pytest -k; no lane name
# is a substring of another (see the lane table).
_mock_golden_lane() {
  local gate="$1" lane="$2"
  _selected "$gate" || return 0
  _banner "$gate"
  local env_prefix=()
  [ "$UPDATE" = "1" ] && env_prefix=(env ZICATO_PARITY_UPDATE=1)
  if "${env_prefix[@]}" "${PYTEST_SERIAL[@]}" "$LIB/test_mock_golden.py" -k "$lane"; then
    _record "$gate" PASS
  else
    _record "$gate" FAIL
  fi
}

_mock_golden_lane MOCK-GOLDEN racing_full
_mock_golden_lane MOCK-GOLDEN-GAUNTLET gauntlet_full
_mock_golden_lane MOCK-GOLDEN-GAUNTLET-FAST gauntlet_fast
_mock_golden_lane MOCK-GOLDEN-RACING-FAST racing_fast
_mock_golden_lane MOCK-GOLDEN-TWO-ROUND-RACING two_round_racing
_mock_golden_lane MOCK-GOLDEN-SWISS swiss_full
_mock_golden_lane MOCK-GOLDEN-SINGLE-ELIM single_elim_full
_mock_golden_lane MOCK-GOLDEN-DOUBLE-ELIM double_elim_full

# --- MYPY -------------------------------------------------------------------
if _selected MYPY; then
  _banner "MYPY (not-worse-than-baseline)"
  BASELINE_FILE="$GOLDEN_DIR/mypy_baseline.txt"
  MYPY_OUT="$(uv run mypy src/zicato/ 2>&1)"
  echo "$MYPY_OUT" | tail -3
  # Count "error:" lines; `Success: no issues` ⇒ 0.
  CURRENT="$(printf '%s\n' "$MYPY_OUT" | grep -c ': error:')"
  if [ "$UPDATE" = "1" ]; then
    printf '%s\n' "$CURRENT" > "$BASELINE_FILE"
    echo "wrote mypy baseline = $CURRENT"
    _record MYPY PASS
  else
    BASELINE="$(cat "$BASELINE_FILE" 2>/dev/null || echo 0)"
    echo "mypy errors: current=$CURRENT baseline=$BASELINE"
    if [ "$CURRENT" -le "$BASELINE" ]; then _record MYPY PASS; else _record MYPY FAIL; fi
  fi
fi

# --- VERDICT ----------------------------------------------------------------
_banner "PARITY VERDICT"
for r in ${RESULTS[@]+"${RESULTS[@]}"}; do
  gate="${r%%$'\t'*}"; status="${r##*$'\t'}"
  if [ "$status" = "PASS" ]; then
    printf '  \033[32mPASS\033[0m  %s\n' "$gate"
  else
    printf '  \033[31mFAIL\033[0m  %s\n' "$gate"
  fi
done
if [ "$OVERALL" = "0" ]; then
  printf '\n\033[32mPARITY: GREEN — behavior preserved.\033[0m\n'
else
  printf '\n\033[31mPARITY: RED — a gate failed; behavior moved.\033[0m\n'
fi
exit "$OVERALL"
