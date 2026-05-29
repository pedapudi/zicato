---
name: zicato-watch-dashboard
description: Open and read zicato's live competition dashboard during an evolve run — navigate L0 workspace → L1 epoch → L2 generation → L3 round → L4 run, screenshot it with browser-use, narrate what the loop is doing, and follow harmonograf deep-links per board run. Use whenever you need to observe an in-flight (or post-mortem) epoch.
---

# Watch the zicato dashboard

The dashboard is zicato's **competition view**: one epoch, many runs across
many generations — the bracket, the lineage tree, the score trajectory, the
drift heatmap, and the loop-health panel. It is the cadence *above*
harmonograf (which is the per-run **execution view**). See
[../../docs/design/DASHBOARD.md](../../docs/design/DASHBOARD.md).

## 1. Find the URL

`zicato evolve` launches the dashboard automatically and prints its URL once
on startup — bound to **`127.0.0.1:7892`** (auto +1 on a port clash). Per
project policy, always report that URL to the user; never bind `0.0.0.0`.

```sh
# evolve prints, e.g.:
#   [evolve] dashboard:  http://127.0.0.1:7892/  (live for this epoch)
```

To serve the dashboard standalone against an existing workspace (post-mortem
of a closed epoch, or attach to a workspace another `evolve` is driving):

```sh
.venv/bin/zicato dashboard --workspace .zicato        # foreground; Ctrl-C to stop
.venv/bin/zicato dashboard --port 7892                # default host 127.0.0.1
```

Real flags only — `zicato dashboard` exposes `--workspace`, `--host`,
`--port`. (The design doc mentions `--read-only` / `--daemon` standalone
modes; those are a later phase and are **not** in the shipped `--help`. Do
not pass flags that `--help` does not list.) Do not bind ports yourself in an
agent run — read the URL `evolve` already printed.

## 2. The navigation levels (what to look at, in order)

The single page routes by URL fragment. Walk it top-down — this is the L0→L4
drill path:

| Level | Where | What it shows / what to look for |
|---|---|---|
| **L0 workspace** | header (always visible) | epoch · generation · round · elapsed · phase pill (`PROPOSING`/`TOURNAMENT RUNNING`/`PAUSED`/`STALLED`). A `STALLED` pill or stale heartbeat means the orchestrator stopped bumping `heartbeat.json`. |
| **L1 epoch** | `#/epoch` | the evaluation contract: scoring weights (incl. nested `per_judge_weights`), the board, the proposer brief, mutation paths. Confirm the loop has teeth *before* trusting any result. |
| **L2 generation** | `#/tree` | cross-epoch lineage graph **including the in-flight candidate** (drawn mid-run, `promoted: null`), plus the score trajectory. Click a node for its `experiment.json` (hypothesis, outcome, per-entry deltas). |
| **L3 round** | `#/tournament` | the bracket (champion spine + challengers, `PROM`/`DISCARD`, Δ-drift) for resolved rounds **and the in-progress round rendered live**. Click a round → matchup detail. |
| **L4 run** | active matchup / active-runs row | per board entry × (parent champion, candidate challenger): status glyph, drift loss, pass/fail, running aggregate, and the **predicted gate verdict** (best/worst/current-trend, deterministic, no LLM). Click a run row → per-run drill-down. |

The **Overview** view (`#/overview`) composes the active matchup + log tail —
the "what is happening right now" landing view.

Read the loop-health panel every time (see
[zicato-diagnose-health](../zicato-diagnose-health/SKILL.md)): a red border
means the eval is toothless and the whole bracket is meaningless.

Status glyphs in the matchup: `[⋯] queued`, `[▶ N%] running` (N% is the
**deadline-elapsed fraction** — how far through its wall-clock budget, NOT
task progress), `[✓] done`, `[!] aborted`, `[✗] killed`.

## 3. Screenshot it with browser-use

The browser-use MCP tools are **deferred** — load their schemas before
calling, or the call fails with `InputValidationError`:

```text
ToolSearch  query: select:mcp__browser-use__browser_navigate,mcp__browser-use__browser_screenshot,mcp__browser-use__browser_get_state,mcp__browser-use__browser_click
```

Then drive the four views (the URL fragment switches view without a reload):

```text
mcp__browser-use__browser_navigate   url: http://127.0.0.1:7892/#/overview
mcp__browser-use__browser_screenshot                          # the active matchup + log tail
mcp__browser-use__browser_navigate   url: http://127.0.0.1:7892/#/tournament
mcp__browser-use__browser_screenshot                          # the bracket
mcp__browser-use__browser_navigate   url: http://127.0.0.1:7892/#/tree
mcp__browser-use__browser_screenshot                          # lineage + trajectory
mcp__browser-use__browser_navigate   url: http://127.0.0.1:7892/#/epoch
mcp__browser-use__browser_screenshot                          # the contract
```

Use `mcp__browser-use__browser_get_state` to read the DOM/text when you need
to narrate exact numbers rather than just capture a picture, and
`mcp__browser-use__browser_click` to open a round's matchup detail or a
generation node before screenshotting. Surface the saved PNGs to the user
with the file-send tool when a picture is the deliverable.

## 4. Follow the harmonograf deep-link per board run

Each L4 run drill-down carries an **Open in harmonograf** link — this is the
hand-off from the competition view *across* into the per-run execution view.
The href is built as `<harmonograf_url>/#/session/<adk_session_id>`, where
`adk_session_id` is the run's session (per-board-run sessions are named
`<gen_id>--<entry_id>`). There is **one** harmonograf server with many
sessions. The link only renders when the run has a known `harmonograf_url`
and `adk_session_id`. Click through to inspect the Gantt / drift trace of
that single run, then come back up to the tournament. For how the sessions
relate (meta-loop vs per-board-run), see
[zicato-read-telemetry](../zicato-read-telemetry/SKILL.md).

## 5. No-browser snapshot

There is **no `zicato status` command** (not implemented — `zicato --help`
lists no such subcommand). For a no-browser snapshot, read the canonical
runtime files directly or hit the dashboard's JSON endpoints:

```sh
.venv/bin/zicato health --workspace .zicato           # loop-health report (no browser)
# or curl the live server if it is up:
curl -s http://127.0.0.1:7892/api/state | head
curl -s http://127.0.0.1:7892/api/active-tournament
```

`/api/state`, `/api/active-tournament`, `/api/active-runs`, `/api/lineage`,
`/api/run-log` are file-backed and live (files-canonical); `/api/tournaments`
reads the lagging analytical index and only covers **closed** rounds.

## Guardrails

- Report the dashboard URL to the user; bind only to `127.0.0.1` (no
  `0.0.0.0`, no auth on the dashboard).
- Do not start a live `zicato evolve` to get a dashboard — only the user
  starts live runs. Attach to an existing workspace with `zicato dashboard`
  or read a finished epoch post-mortem.
- Cite only flags that appear in real `--help` output.
- Files are canonical; the live panels read JSON/JSONL, the index is derived
  and lags to generation boundaries — never trust the index for in-flight
  state.
