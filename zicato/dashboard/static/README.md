# supervisor/static — dashboard UI bundle

Vanilla HTML / CSS / JS source for the zicato supervisor dashboard.
The Rust supervisor binary bundles these files at compile time via
`include_dir!` and serves them from `/` and `/static/...`.

No build step. No framework. No external network. Everything in this
directory must remain self-contained — no Google Fonts, no CDN, no
remote scripts. The renderer mirrors the palette and typography of
`zicato/epoch/html_report.py` so `analysis.html` and the live
dashboard read as siblings.

## Files

- `index.html` — single-page shell. Top-level `<svg>` elements are
  declared here so the JS module can populate them via
  `createElementNS`. Loads `style.css` and `app.js`.
- `style.css` — all styling. CSS custom properties drive light + dark
  themes; the dark branch lives under `@media (prefers-color-scheme:
  dark)`. Includes a print stylesheet (snapshot-only).
- `app.js` — ES2022 module. Owns the `AppState`, connects to
  `EventSource('/events')`, renders every panel. Pure DOM — no
  framework runtime. The `predictedGateVerdict` function is the
  deterministic gate-projection calculator used by the active
  tournament panel.
- `icons.svg` — inline-able sprite. Reference via
  `<use href="/static/icons.svg#icon-name"/>`.

## Server contract

The dashboard talks to the supervisor over these endpoints. Anything
not listed must NOT be requested.

```
GET  /                              — index.html
GET  /static/{path}                 — style.css, app.js, icons.svg, ...
GET  /api/state                     — full snapshot
GET  /api/lineage                   — { generations, experiments }
GET  /api/active-runs               — [ run, ... ]
GET  /api/active-tournament         — current tournament or null
GET  /events                        — SSE
                                      events: snapshot, state_change, log, heartbeat
POST /api/control/pause             — v1.3
POST /api/control/skip-round        — v1.3
POST /api/control/kill/{run_id}     — v1.3
POST /api/control/promote/{gen_id}  — v1.3
POST /api/control/reject/{gen_id}   — v1.3
POST /api/control/brief             — v1.3 — body: new proposer brief text
```

For v1.2 the action buttons render but are disabled with a tooltip
explaining they land in v1.3. The POST handlers are wired in
`app.js#postControl` so the v1.3 change is "remove `disabled`".

## Snapshot shape (consumed by `app.js`)

```jsonc
{
  "epoch":      { "id": "initial", "generation": "v3", "round": "4",
                  "startedAt": "2026-05-14T12:30:27Z" },
  "heartbeat":  { "timestamp": "...", "pid": 12345, "instance_id": "..." },
  "supervisor": { "version": "1.2.0", "port": "7892", "build": "..." },
  "scoring":    { "margin": 0.05 },
  "active_runs": [
    { "run_id": "r-9c2a", "entry_id": "...", "generation_id": "v4",
      "started_at": "...", "budget_seconds": 180, "percent": 23 }
  ],
  "active_tournament": {
    "round": 4, "total_rounds": 5,
    "parent_id": "v3", "child_id": "v4",
    "elapsed_seconds": 263,
    "hypothesis": { "core_idea": "...", "modulating": ["..."] },
    "entries": [
      { "entry_id": "...", "status": "done|running|queued|fail",
        "parent": { "drift_loss": 0.23, "pass": true },
        "child":  { "drift_loss": 0.18, "pass": true,
                    "drift_kinds": { "off_topic": 2 } },
        "runtime": { "elapsed_seconds": 42, "budget_seconds": 180, "percent": 23 },
        "run_id": "r-9c2a" }
    ]
  },
  "lineage": {
    "generations": [{ "id": "v0", "parent_id": null }, ...],
    "experiments": [{ "generation_id": "v1", "hypothesis": {...},
                      "outcome": { "tournament_decision": "promoted",
                                   "scalar_score_delta": -0.080,
                                   "drift_movements": [
                                     { "kind": "off_topic",
                                       "from_rate": 0.4, "to_rate": 0.2 }
                                   ]}}]
  },
  "experiments": [ /* full experiment objects with patches */ ],
  "log_tail": [{ "ts": "12:34:50", "level": "info", "message": "..." }]
}
```

## Mock mode

For offline preview without a running supervisor:

```
file:///path/to/supervisor/static/index.html?mock=1
```

A hardcoded `mockSnapshot()` populates `AppState` and every panel
renders normally. SSE is not opened. Useful for design iteration and
for the structural test in `tests/test_dashboard_ui.py`.

## Size envelope

Total bundle (index.html + style.css + app.js + icons.svg) must stay
under 80 KB uncompressed. The structural test enforces this.

## Accessibility

- `role` and `aria-label` on every interactive region
- skip-link at the top of the page (visible on focus)
- keyboard activation on every clickable lineage node, run card, and
  tournament entry row (Enter / Space)
- `aria-live="polite"` on the log tail so screen readers announce new
  lines without interrupting
- focus outlines preserved on every focusable element
- `Escape` closes the drill-down side panel
- print stylesheet hides live-only panels and shows the snapshot
