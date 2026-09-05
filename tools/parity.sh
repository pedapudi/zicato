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
#   MYPY           type checking must complete successfully.
#
# Usage
# -----
#   bash tools/parity.sh                 # run every gate
#   bash tools/parity.sh --update        # (re)capture every golden baseline
#   bash tools/parity.sh --only PYTEST   # run a gate (repeatable, or A,B)
#   bash tools/parity.sh --skip PYTEST   # skip a gate (repeatable, or A,B)
#
# Exit code is 0 only if a nonempty selection completed successfully.

set -u -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIB="$REPO_ROOT/tools/parity/lib"

# A capture name after ':' selects a single deterministic fixture. This table
# owns gate names, their default order, and each mock capture's selection.
GATES=(
  PYTEST CONTRACT-HASH CLI-HELP REINDEX-DUMP
  MOCK-GOLDEN:racing_full
  MOCK-GOLDEN-GAUNTLET:gauntlet_full
  MOCK-GOLDEN-GAUNTLET-FAST:gauntlet_fast
  MOCK-GOLDEN-RACING-FAST:racing_fast
  MOCK-GOLDEN-TWO-ROUND-RACING:two_round_racing
  MOCK-GOLDEN-SWISS:swiss_full
  MOCK-GOLDEN-SINGLE-ELIM:single_elim_full
  MOCK-GOLDEN-DOUBLE-ELIM:double_elim_full
  MYPY
)

UPDATE=0
ONLY=()
SKIP=()
while [ $# -gt 0 ]; do
  case "$1" in
    --update) UPDATE=1; shift ;;
    --only|--skip)
      option="$1"
      if [ $# -lt 2 ] || [[ "$2" == --* ]]; then
        echo "$option requires a gate name or comma-separated list" >&2
        exit 2
      fi
      case "$2" in
        ''|,*|*,|*,,*) echo "$option contains an empty gate name" >&2; exit 2 ;;
      esac
      IFS=',' read -r -a values <<< "$2"
      if [ "$option" = --only ]; then ONLY+=("${values[@]}"); else SKIP+=("${values[@]}"); fi
      shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

# The guarded expansions also support empty arrays under Bash 3.2 with nounset.
for requested in ${ONLY[@]+"${ONLY[@]}"} ${SKIP[@]+"${SKIP[@]}"}; do
  found=0
  for entry in "${GATES[@]}"; do
    [ "${entry%%:*}" = "$requested" ] && found=1
  done
  if [ "$found" -eq 0 ]; then
    echo "unknown gate: $requested" >&2
    exit 2
  fi
done

_selected() {
  local gate="$1" name found=0
  if [ ${#ONLY[@]} -gt 0 ]; then
    for name in ${ONLY[@]+"${ONLY[@]}"}; do [ "$name" = "$gate" ] && found=1; done
    [ "$found" -eq 1 ] || return 1
  fi
  for name in ${SKIP[@]+"${SKIP[@]}"}; do [ "$name" = "$gate" ] && return 1; done
  return 0
}

SELECTED=()
for entry in "${GATES[@]}"; do
  _selected "${entry%%:*}" && SELECTED+=("$entry")
done
if [ ${#SELECTED[@]} -eq 0 ]; then
  echo "no gates selected" >&2
  exit 2
fi
printf 'Selected gates: %s' "${SELECTED[0]%%:*}"
for entry in "${SELECTED[@]:1}"; do printf ', %s' "${entry%%:*}"; done
printf '\n'

# Golden captures run serially in one interpreter; the full suite uses its
# configured worker count and includes the required statistical tests.
PYTEST_SERIAL=(uv run pytest -n0 -q)
_run_gate() {
  local entry="$1" gate="${1%%:*}"
  local update_args=() env_args=()
  if [ "$UPDATE" -eq 1 ]; then
    update_args=(--update)
    env_args=(env ZICATO_PARITY_UPDATE=1)
  fi
  case "$gate" in
    PYTEST) uv run pytest -q -m "not node and not cascade_oc" ;;
    CONTRACT-HASH) uv run python "$LIB/contract_hash.py" ${update_args[@]+"${update_args[@]}"} ;;
    CLI-HELP) uv run python "$LIB/cli_help.py" ${update_args[@]+"${update_args[@]}"} ;;
    REINDEX-DUMP) ${env_args[@]+"${env_args[@]}"} "${PYTEST_SERIAL[@]}" "$LIB/test_reindex_golden.py" ;;
    MOCK-GOLDEN*) ${env_args[@]+"${env_args[@]}"} "${PYTEST_SERIAL[@]}" "$LIB/test_mock_golden.py" -k "${entry#*:}" ;;
    MYPY) uv run mypy src/zicato/ ;;
    *) echo "gate has no command: $gate" >&2; return 2 ;;
  esac
}

_banner() { printf '\n========== %s ==========\n' "$1"; }
RESULTS=()
OVERALL=0
cd "$REPO_ROOT" || exit 2
for entry in "${SELECTED[@]}"; do
  gate="${entry%%:*}"
  _banner "$gate"
  if _run_gate "$entry"; then
    RESULTS+=("$gate"$'\t'"PASS")
  else
    status=$?
    echo "$gate failed with exit status $status" >&2
    RESULTS+=("$gate"$'\t'"FAIL")
    OVERALL=1
  fi
done

_banner "PARITY VERDICT"
for result in "${RESULTS[@]}"; do
  gate="${result%%$'\t'*}"; status="${result##*$'\t'}"
  if [ "$status" = PASS ]; then
    printf '  \033[32mPASS\033[0m  %s\n' "$gate"
  else
    printf '  \033[31mFAIL\033[0m  %s\n' "$gate"
  fi
done
if [ "$OVERALL" -eq 0 ]; then
  printf '\n\033[32mPARITY: GREEN — all selected gates passed.\033[0m\n'
else
  printf '\n\033[31mPARITY: RED — a selected gate failed.\033[0m\n'
fi
exit "$OVERALL"
