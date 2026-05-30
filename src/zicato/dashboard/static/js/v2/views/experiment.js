// js/v2/views/experiment.js — the Experiment (generation) view.
//
// DASHBOARD-V2 §4.4: "was the bet right, and why?" — ONE dense screen
// that recomposes v1's good atoms around the question, not the entity.
// It is the post-hoc resting place a finished tournament lands on (the
// Bench's "jump to decision" resolves here once the run completes).
//
// Comparison is the DEFAULT unit of meaning (§2 principle 2): the
// challenger is always shown AGAINST its champion (= its lineage
// parent), with no opt-in "compare" toggle. The parent is resolved from
// the lineage spine; the picker (a later affordance) would only choose
// an *alternate* comparison — the parent is never optional.
//
// The screen, top to bottom:
//   1. Header        — champion → challenger ids + the big verdict glyph.
//   2. Hypothesis→outcome — ONE comparative figure: the bet (core idea
//      + why), predicted-vs-actual drift as a divergingBar, an alignment
//      verdict (did the predicted band contain the actual?).
//   3. Gate ladder   — the legible "why" (the ONE rule that fired).
//   4. Scalar waterfall — which component moved the loss, champion→child.
//   5. Per-entry A/B — challenger−champion drift per board entry, with
//      pass→fail flips flagged; every row drills to the Run view.
//   6. Primary driver + per-judge attribution.
//   7. Patches       — the exact change the experiment made.
//
// Every async section renders through stateBlock — the four honest
// states (not_yet / running / empty / broken), never a bare "No data".
// A fetch failure degrades ONLY its own section: the screen stays up.

import { el, clearChildren } from '../../core/dom.js';
import { state } from '../../core/state.js';
import { fetchJson } from '../../core/api.js';
import { v2Router } from '../router.js';

import { stateBlock } from '../components/stateBlock.js';
import { dataTable, deltaCell } from '../components/dataTable.js';

// v1 atoms — reused by direct path (DASHBOARD-V2 §5: keep their factory
// contracts). They carry their own CSS (css/decision.css, already
// linked); this view only adds layout CSS.
import { gateLadder } from '../../components/gate_ladder.js';
import { divergingBar } from '../../components/diverging_bar.js';
import { scalarWaterfall } from '../../components/scalar_waterfall.js';
import { verdictGlyph } from '../../components/verdict_glyph.js';

// Self-inject the scoped stylesheet once (the view is an ES module that
// owns its own layout CSS; index.html is not edited). No-op when there
// is no document head (the node test harness) or when already present.
const _CSS_HREF = 'css/v2/experiment.css';
function ensureCss() {
  if (typeof document === 'undefined') return;
  const head = document.head || document.getElementsByTagName?.('head')?.[0];
  if (!head || typeof head.appendChild !== 'function') return;
  if (document.getElementById && document.getElementById('v2-experiment-css')) return;
  const link = el('link', { id: 'v2-experiment-css', rel: 'stylesheet', href: _CSS_HREF });
  head.appendChild(link);
}

// ---------------------------------------------------------------------------
// Lineage helpers — resolve the champion (parent) + epoch for a gen.
// ---------------------------------------------------------------------------

function _gens() {
  const lin = state.lineage || {};
  return Array.isArray(lin.generations) ? lin.generations : [];
}

function _genId(g) {
  if (!g) return null;
  const id = g.id != null ? g.id : g.generation_id;
  return id != null ? String(id) : null;
}

function _parentId(g) {
  if (!g) return null;
  const p = g.parent_id != null ? g.parent_id
    : (g.parentId != null ? g.parentId : g.parent_generation_id);
  return p != null ? String(p) : null;
}

function _findGen(genId) {
  if (genId == null) return null;
  const want = String(genId);
  for (const g of _gens()) if (_genId(g) === want) return g;
  return null;
}

// The experiment record (hypothesis + outcome + patches) for a gen,
// from the epoch contract's `experiments` list.
function _experimentFor(genId) {
  const def = state.epochDef || {};
  const exps = Array.isArray(def.experiments) ? def.experiments
    : (Array.isArray(state.experiments) ? state.experiments : []);
  const want = String(genId);
  for (const e of exps) {
    if (e && String(e.generation_id != null ? e.generation_id : e.id) === want) return e;
  }
  return null;
}

// Resolve the epoch id for a generation: the gen row, else the
// experiment record, else the current epoch.
function _epochIdFor(genId, gen, exp) {
  const fromGen = gen && (gen.epoch_id || gen.epochId);
  if (fromGen) return String(fromGen);
  if (exp && exp.epoch_id) return String(exp.epoch_id);
  const def = state.epochDef || {};
  if (def.epoch_id) return String(def.epoch_id);
  if (state.epoch && state.epoch.id && state.epoch.id !== '—') return String(state.epoch.id);
  return null;
}

// ---------------------------------------------------------------------------
// Async section helper. Mounts a stateBlock, fires the fetch, and swaps
// in the rendered body on resolve — degrading to a broken state with the
// verbatim reason on failure. Each section is independent: one failure
// never takes down the screen.
// ---------------------------------------------------------------------------

function asyncSection(host, fetchFn, renderFn, opts) {
  const o = opts || {};
  clearChildren(host);
  host.appendChild(stateBlock('running', { label: o.runningLabel || 'Loading', detail: o.runningDetail }));
  Promise.resolve()
    .then(fetchFn)
    .then((data) => {
      clearChildren(host);
      let body;
      try {
        body = renderFn(data);
      } catch (err) {
        host.appendChild(stateBlock('broken', { reason: String(err && err.message ? err.message : err) }));
        return;
      }
      if (body == null) {
        host.appendChild(stateBlock('empty', { label: o.emptyLabel || 'Nothing here', detail: o.emptyDetail }));
        return;
      }
      host.appendChild(body);
    })
    .catch((err) => {
      clearChildren(host);
      host.appendChild(stateBlock('broken', { reason: String(err && err.message ? err.message : err) }));
    });
}

// ---------------------------------------------------------------------------
// Section: header — champion → challenger + the verdict glyph.
// ---------------------------------------------------------------------------

function sectionHeader(genId, championId, decision) {
  const ids = el('div', { class: 'v2-exp-ids' }, [
    championId != null
      ? el('span', { class: 'v2-exp-champ v2-mono', title: 'champion (parent)' }, [String(championId)])
      : el('span', { class: 'v2-exp-champ v2-exp-champ-none v2-mono', title: 'no parent — root generation' }, ['root']),
    el('span', { class: 'v2-exp-arrow', 'aria-hidden': 'true' }, ['→']),
    el('span', { class: 'v2-exp-chall v2-mono', title: 'challenger' }, [String(genId)]),
  ]);
  const head = el('header', { class: 'v2-exp-head' }, [
    el('div', { class: 'v2-exp-head-l' }, [
      el('div', { class: 'v2-exp-kicker' }, ['Experiment · was the bet right?']),
      ids,
    ]),
    el('div', { class: 'v2-exp-verdict' }, [verdictGlyph(decision, { withLabel: true })]),
  ]);
  return head;
}

// ---------------------------------------------------------------------------
// Section: hypothesis → outcome (the comparative figure).
//
// The bet: core_idea + why. The drift figure: predicted vs actual drift
// movements as one divergingBar (predicted shown as ghost rows, actual
// as solid), and an alignment verdict — did the actual land inside the
// predicted band? Sources: the experiment record (hypothesis) +
// /api/drift-movements/{gen} (actual champion→challenger movement).
// ---------------------------------------------------------------------------

function _predictedDriftRows(hyp) {
  const moves = hyp && Array.isArray(hyp.expected_drift_movements) ? hyp.expected_drift_movements : [];
  // Map a qualitative prediction to a signed magnitude for the bar: a
  // "fewer/down/improved" kind predicts a negative delta, "more/up/
  // worsened" a positive one. Magnitude defaults to 1 so a directional-
  // only prediction still draws.
  const out = [];
  for (const m of moves) {
    if (!m || !m.kind) continue;
    const dir = String(m.direction || '').toLowerCase();
    const sign = (dir.includes('few') || dir.includes('down') || dir.includes('improv') || dir.includes('less') || dir.includes('reduc'))
      ? -1
      : ((dir.includes('more') || dir.includes('up') || dir.includes('wors') || dir.includes('increa')) ? 1 : 0);
    let mag = Number(m.magnitude);
    if (!isFinite(mag) || mag <= 0) mag = 1;
    out.push({
      label: String(m.kind),
      delta: sign * mag,
      annotation: { glyph: '◇', title: 'predicted' },
    });
  }
  return out;
}

function _actualDriftRows(movements) {
  const list = Array.isArray(movements) ? movements : [];
  return list
    .filter((m) => m && m.kind != null && typeof m.delta === 'number' && isFinite(m.delta))
    .map((m) => ({
      label: String(m.kind),
      // Drift count delta: fewer drift events on the challenger (negative)
      // is the improvement — matches divergingBar's goodWhenNegative.
      delta: m.delta,
    }));
}

// Alignment verdict: for each predicted kind, did the actual move in the
// predicted DIRECTION? Returns { aligned, total, kinds: [{kind, ok}] }.
function _alignment(predictedRows, actualRows) {
  const actualByKind = new Map();
  for (const r of actualRows) actualByKind.set(r.label, r.delta);
  const kinds = [];
  let aligned = 0;
  for (const p of predictedRows) {
    if (p.delta === 0) continue; // directionless prediction — not scorable
    const a = actualByKind.get(p.label);
    if (typeof a !== 'number') { kinds.push({ kind: p.label, ok: null }); continue; }
    const ok = (p.delta < 0 && a < 0) || (p.delta > 0 && a > 0) || (a === 0 && p.delta === 0);
    if (ok) aligned += 1;
    kinds.push({ kind: p.label, ok });
  }
  const scorable = kinds.filter((k) => k.ok !== null).length;
  return { aligned, scorable, kinds };
}

function sectionBet(exp, movements) {
  const hyp = (exp && typeof exp.hypothesis === 'object' && exp.hypothesis) || {};
  const outcome = (exp && typeof exp.outcome === 'object' && exp.outcome) || null;

  const wrap = el('div', { class: 'v2-exp-bet' });

  // The bet — core idea + why (prose face).
  const bet = el('div', { class: 'v2-exp-bet-prose' });
  if (typeof hyp.core_idea === 'string' && hyp.core_idea.trim()) {
    bet.appendChild(el('p', { class: 'v2-exp-core' }, [hyp.core_idea]));
  }
  if (typeof hyp.why === 'string' && hyp.why.trim()) {
    bet.appendChild(el('p', { class: 'v2-exp-why' }, [
      el('span', { class: 'v2-exp-lead' }, ['Why. ']), hyp.why,
    ]));
  }
  if (typeof hyp.expected_pass_rate_delta === 'string' && hyp.expected_pass_rate_delta.trim()) {
    bet.appendChild(el('p', { class: 'v2-exp-pred-pass' }, [
      el('span', { class: 'v2-exp-lead' }, ['Predicted pass-rate Δ. ']),
      el('span', { class: 'v2-mono' }, [hyp.expected_pass_rate_delta]),
    ]));
  }
  if (!bet.firstChild) {
    bet.appendChild(el('p', { class: 'v2-exp-empty-prose' }, ['No structured hypothesis recorded for this generation.']));
  }
  wrap.appendChild(bet);

  // Predicted vs actual drift, as one comparative figure.
  const predicted = _predictedDriftRows(hyp);
  const actual = _actualDriftRows(movements);

  const fig = el('div', { class: 'v2-exp-driftfig' });
  fig.appendChild(el('h4', { class: 'v2-exp-subh' }, ['Predicted vs actual drift movement']));

  const cols = el('div', { class: 'v2-exp-driftcols' });

  const predCol = el('div', { class: 'v2-exp-driftcol' }, [
    el('div', { class: 'v2-exp-driftcol-h' }, ['Predicted']),
  ]);
  predCol.appendChild(predicted.length
    ? divergingBar({ rows: predicted, goodWhenNegative: true })
    : el('p', { class: 'v2-exp-empty-prose' }, ['No drift movement predicted.']));

  const actCol = el('div', { class: 'v2-exp-driftcol' }, [
    el('div', { class: 'v2-exp-driftcol-h' }, ['Actual (challenger − champion)']),
  ]);
  actCol.appendChild(actual.length
    ? divergingBar({ rows: actual, goodWhenNegative: true })
    : stateBlock('empty', { label: 'No drift movement recorded', detail: 'The index has no per-kind drift counts for this round.' }));

  cols.appendChild(predCol);
  cols.appendChild(actCol);
  fig.appendChild(cols);

  // Alignment verdict — did the bet's predicted direction hold?
  const align = _alignment(predicted, actual);
  const verdictRow = el('div', { class: 'v2-exp-align' });
  if (align.scorable === 0) {
    verdictRow.appendChild(el('span', { class: 'v2-exp-align-label v2-exp-align-na' }, [
      'Alignment — unscorable (no directional prediction overlapped a recorded movement).',
    ]));
  } else {
    const hit = align.aligned === align.scorable;
    const partial = align.aligned > 0 && !hit;
    const cls = hit ? 'v2-exp-align-hit' : (partial ? 'v2-exp-align-partial' : 'v2-exp-align-miss');
    const glyph = hit ? '✓' : (partial ? '≈' : '✗');
    const word = hit ? 'Bet held' : (partial ? 'Bet partly held' : 'Bet missed');
    verdictRow.appendChild(el('span', { class: 'v2-exp-align-badge ' + cls }, [
      el('span', { class: 'v2-exp-align-glyph', 'aria-hidden': 'true' }, [glyph]),
      el('span', { class: 'v2-exp-align-word' }, [word]),
    ]));
    verdictRow.appendChild(el('span', { class: 'v2-exp-align-detail v2-mono' }, [
      `${align.aligned}/${align.scorable} predicted directions matched`,
    ]));
  }
  // Per-kind chips so the alignment is legible, not just a ratio.
  if (align.kinds.length) {
    const chips = el('div', { class: 'v2-exp-align-chips' });
    for (const k of align.kinds) {
      const cls = k.ok === true ? 'v2-exp-chip-hit' : (k.ok === false ? 'v2-exp-chip-miss' : 'v2-exp-chip-na');
      const g = k.ok === true ? '✓' : (k.ok === false ? '✗' : '·');
      chips.appendChild(el('span', { class: 'v2-exp-chip ' + cls, title: k.ok === null ? 'no recorded movement' : (k.ok ? 'matched' : 'opposed') }, [
        el('span', { 'aria-hidden': 'true' }, [g]), ' ', el('span', { class: 'v2-mono' }, [k.kind]),
      ]));
    }
    verdictRow.appendChild(chips);
  }
  fig.appendChild(verdictRow);

  // Outcome summary line (the after-the-fact prose) when present.
  if (outcome && typeof outcome.summary === 'string' && outcome.summary.trim()) {
    fig.appendChild(el('p', { class: 'v2-exp-outcome-summary' }, [
      el('span', { class: 'v2-exp-lead' }, ['Outcome. ']), outcome.summary,
    ]));
  }

  wrap.appendChild(fig);
  return wrap;
}

// ---------------------------------------------------------------------------
// Section: scalar waterfall — which component moved the loss.
// The gate's `scalar_components` carries champion + challenger per-
// component scalars; the waterfall wants the per-component DELTA.
// ---------------------------------------------------------------------------

function _waterfallComponents(scalarComponents) {
  const sc = scalarComponents && typeof scalarComponents === 'object' ? scalarComponents : {};
  const champ = sc.champion && typeof sc.champion === 'object' ? sc.champion : {};
  const chall = sc.challenger && typeof sc.challenger === 'object' ? sc.challenger : {};
  const names = new Set([...Object.keys(champ), ...Object.keys(chall)]);
  const out = [];
  for (const name of [...names].sort()) {
    const c = Number(champ[name]);
    const k = Number(chall[name]);
    const cOk = isFinite(c);
    const kOk = isFinite(k);
    if (!cOk && !kOk) continue;
    const delta = (cOk ? c : 0) === 0 && !cOk ? (kOk ? k : 0)
      : ((kOk ? k : 0) - (cOk ? c : 0));
    out.push({ name, delta });
  }
  return out;
}

// ---------------------------------------------------------------------------
// Section: per-entry A/B — challenger−champion drift per board entry.
// Pass→fail flips are flagged (annotation); every row drills to the run.
// ---------------------------------------------------------------------------

function _flip(parentPass, childPass) {
  // A regression flip: the champion passed and the challenger no longer
  // does. Returns 'regress' | 'recover' | null.
  if (parentPass === true && childPass === false) return 'regress';
  if (parentPass === false && childPass === true) return 'recover';
  return null;
}

function sectionEntries(grid, genId) {
  const rows = Array.isArray(grid && grid.entry_grid) ? grid.entry_grid : [];
  if (!rows.length) return null; // → asyncSection renders the empty state

  const table = dataTable({
    ariaLabel: 'per-entry challenger vs champion drift',
    rows,
    rowKey: (r) => r.entry_id,
    onRowClick: (r) => {
      if (r && r.entry_id != null) v2Router.go('run', r.entry_id, genId);
    },
    columns: [
      {
        key: 'entry_id',
        header: 'entry',
        mono: true,
        render: (r) => {
          const flip = _flip(r.parent_pass, r.child_pass);
          const children = [el('span', { class: 'v2-mono' }, [String(r.entry_id == null ? '' : r.entry_id)])];
          if (flip) {
            children.push(el('span', {
              class: 'v2-exp-flip v2-exp-flip-' + flip,
              title: flip === 'regress' ? 'pass → fail (regression)' : 'fail → pass (recovered)',
            }, [flip === 'regress' ? '⚠ pass→fail' : '✓ fail→pass']));
          }
          return el('span', { class: 'v2-exp-entrycell' }, children);
        },
        sortValue: (r) => String(r.entry_id == null ? '' : r.entry_id),
      },
      {
        key: 'parent_drift_loss', header: 'champ drift', mono: true,
        render: (r) => el('span', { class: 'v2-mono' }, [
          typeof r.parent_drift_loss === 'number' ? r.parent_drift_loss.toFixed(2) : '—',
        ]),
        sortValue: (r) => (typeof r.parent_drift_loss === 'number' ? r.parent_drift_loss : null),
      },
      {
        key: 'child_drift_loss', header: 'chall drift', mono: true,
        render: (r) => el('span', { class: 'v2-mono' }, [
          typeof r.child_drift_loss === 'number' ? r.child_drift_loss.toFixed(2) : '—',
        ]),
        sortValue: (r) => (typeof r.child_drift_loss === 'number' ? r.child_drift_loss : null),
      },
      {
        key: 'delta', header: 'Δ drift', semantic: 'delta', improveWhenNegative: true, digits: 2,
        value: (r) => (typeof r.delta === 'number' ? r.delta : null),
      },
    ],
    sort: { key: 'delta', dir: 'asc' },
  });
  return table;
}

// ---------------------------------------------------------------------------
// Section: primary driver + per-judge attribution.
// ---------------------------------------------------------------------------

function sectionJudges(comparison) {
  const judges = Array.isArray(comparison && comparison.judges) ? comparison.judges : [];
  const primary = comparison && comparison.primary_driver != null ? String(comparison.primary_driver) : null;
  if (!judges.length) return null;

  const wrap = el('div', { class: 'v2-exp-judges' });
  if (primary) {
    const driverRow = judges.find((j) => j && j.judge_name === primary);
    const d = driverRow && typeof driverRow.delta === 'number' ? driverRow.delta : null;
    wrap.appendChild(el('div', { class: 'v2-exp-driver' }, [
      el('span', { class: 'v2-exp-driver-label' }, ['Primary driver']),
      el('span', { class: 'v2-exp-driver-judge v2-mono' }, [primary]),
      d != null ? deltaCell(d, { improveWhenNegative: true, digits: 3 }) : null,
    ]));
  }

  const table = dataTable({
    ariaLabel: 'per-judge weighted-loss comparison',
    rows: judges,
    rowKey: (r) => r.judge_name,
    columns: [
      { key: 'judge_name', header: 'judge', mono: true, value: (r) => r.judge_name },
      {
        key: 'champion_weighted_loss', header: 'champ', mono: true,
        render: (r) => el('span', { class: 'v2-mono' }, [
          typeof r.champion_weighted_loss === 'number' ? r.champion_weighted_loss.toFixed(3) : '—',
        ]),
        sortValue: (r) => (typeof r.champion_weighted_loss === 'number' ? r.champion_weighted_loss : null),
      },
      {
        key: 'challenger_weighted_loss', header: 'chall', mono: true,
        render: (r) => el('span', { class: 'v2-mono' }, [
          typeof r.challenger_weighted_loss === 'number' ? r.challenger_weighted_loss.toFixed(3) : '—',
        ]),
        sortValue: (r) => (typeof r.challenger_weighted_loss === 'number' ? r.challenger_weighted_loss : null),
      },
      {
        key: 'delta', header: 'Δ', semantic: 'delta', improveWhenNegative: true, digits: 3,
        value: (r) => (typeof r.delta === 'number' ? r.delta : null),
      },
    ],
    sort: { key: 'delta', dir: 'asc' },
  });
  wrap.appendChild(table);
  return wrap;
}

// ---------------------------------------------------------------------------
// Section: patches — the exact change the experiment made.
// ---------------------------------------------------------------------------

function sectionPatches(exp) {
  const patches = exp && typeof exp.patches === 'object' && exp.patches ? exp.patches : {};
  const ids = Object.keys(patches);
  if (!ids.length) return null;

  const wrap = el('div', { class: 'v2-exp-patches' });
  for (const mid of ids.sort()) {
    const p = patches[mid] || {};
    const target = p.target || p.path || p.file || p.mutation_point || '';
    const summary = p.summary || p.description || p.rationale || '';
    const card = el('div', { class: 'v2-exp-patch' }, [
      el('div', { class: 'v2-exp-patch-head' }, [
        el('span', { class: 'v2-exp-patch-id v2-mono' }, [String(mid)]),
        target ? el('span', { class: 'v2-exp-patch-target v2-mono' }, [String(target)]) : null,
      ]),
      summary ? el('p', { class: 'v2-exp-patch-summary' }, [String(summary)]) : null,
    ]);
    const diff = p.diff || p.patch || p.content;
    if (typeof diff === 'string' && diff.trim()) {
      card.appendChild(el('pre', { class: 'v2-exp-patch-diff v2-mono' }, [diff]));
    }
    wrap.appendChild(card);
  }
  return wrap;
}

// ---------------------------------------------------------------------------
// A titled section frame — a heading + a body host the async loader fills.
// ---------------------------------------------------------------------------

function sectionFrame(title, hint) {
  const sec = el('section', { class: 'v2-exp-section' });
  const head = el('div', { class: 'v2-exp-section-head' }, [
    el('h3', { class: 'v2-exp-section-title' }, [title]),
  ]);
  if (hint) head.appendChild(el('span', { class: 'v2-exp-section-hint' }, [hint]));
  sec.appendChild(head);
  const body = el('div', { class: 'v2-exp-section-body' });
  sec.appendChild(body);
  return { section: sec, body };
}

// ---------------------------------------------------------------------------
// The view entry. Renders into `host`. Re-renders are coarse: the whole
// screen rebuilds when the focused generation changes (the route key);
// within a generation the async sections own their own state.
// ---------------------------------------------------------------------------

let _lastKey = null;

export function renderExperiment(host, route) {
  if (!host) return;
  ensureCss();

  const genId = route && route.params ? route.params.generationId : null;
  const key = 'experiment|' + String(genId == null ? '' : genId);
  if (key === _lastKey && host.firstChild) return; // no-op re-render
  _lastKey = key;

  clearChildren(host);

  if (genId == null) {
    host.appendChild(el('h1', { class: 'v2-view-title' }, ['Experiment']));
    host.appendChild(stateBlock('empty', {
      label: 'No generation selected',
      detail: 'Pick a generation from the lineage spine to see whether its bet was right.',
    }));
    return;
  }

  const gen = _findGen(genId);
  const exp = _experimentFor(genId);
  const championId = _parentId(gen) || (exp && exp.parent_generation_id ? String(exp.parent_generation_id) : null);
  const epochId = _epochIdFor(genId, gen, exp);

  // Decision: prefer the lineage row's verdict, fall back to the
  // experiment outcome. The gate fetch later confirms it authoritatively
  // (it owns the per-rule "why"), but the header glyph reads immediately.
  const rawDecision = (gen && (gen.verdict || gen.outcome || gen.tournament_decision))
    || (exp && exp.outcome && (exp.outcome.tournament_decision || exp.outcome.decision))
    || 'open';

  const screen = el('div', { class: 'v2-exp' });

  // 1. Header.
  screen.appendChild(sectionHeader(genId, championId, rawDecision));

  // 2. Hypothesis → outcome (the comparative figure). The bet is sync
  //    (from the experiment record); the actual drift is the async half.
  {
    const { section, body } = sectionFrame('Hypothesis → outcome', 'the bet vs what happened');
    asyncSection(
      body,
      () => fetchJson('/api/drift-movements/' + encodeURIComponent(genId)),
      (dm) => sectionBet(exp, dm && dm.movements),
      { runningLabel: 'Reading drift movements' },
    );
    screen.appendChild(section);
  }

  // The gate is the source of the legible "why" + the scalar components
  // both §3 and §4 need — fetch it ONCE and fan out to both sections.
  let _gatePromise = null;
  function gate() {
    if (_gatePromise) return _gatePromise;
    if (!epochId || championId == null) {
      _gatePromise = Promise.reject(new Error('no parent generation — gate needs a champion to compare against'));
      return _gatePromise;
    }
    const url = `/api/round/${encodeURIComponent(epochId)}/${encodeURIComponent(championId)}/${encodeURIComponent(genId)}/gate`;
    _gatePromise = fetchJson(url);
    return _gatePromise;
  }

  // 3. Gate ladder.
  {
    const { section, body } = sectionFrame('Promote gate', 'the legible why');
    asyncSection(
      body,
      gate,
      (g) => {
        const rules = Array.isArray(g && g.rules) ? g.rules : [];
        if (!rules.length) return null;
        const wrap = el('div', { class: 'v2-exp-gate' });
        // The gate's own decision + reason, then the ladder.
        if (g.reason) {
          wrap.appendChild(el('p', { class: 'v2-exp-gate-reason' }, [
            verdictGlyph(g.decision, { withLabel: true }),
            el('span', { class: 'v2-exp-gate-reason-text' }, [String(g.reason)]),
          ]));
        }
        wrap.appendChild(gateLadder({ rules }));
        return wrap;
      },
      { runningLabel: 'Evaluating gate' },
    );
    screen.appendChild(section);
  }

  // 4. Scalar waterfall (from the gate's scalar_components).
  {
    const { section, body } = sectionFrame('Scalar decomposition', 'which component moved the loss');
    asyncSection(
      body,
      gate,
      (g) => {
        const comps = _waterfallComponents(g && g.scalar_components);
        if (!comps.length) return null;
        return scalarWaterfall({ components: comps, label: 'champion → challenger, per component' });
      },
      { runningLabel: 'Decomposing scalar' },
    );
    screen.appendChild(section);
  }

  // 5. Per-entry A/B (matchup grid).
  {
    const { section, body } = sectionFrame('Per-entry A/B', 'challenger − champion drift · row → run');
    asyncSection(
      body,
      () => {
        if (!epochId || championId == null) {
          return Promise.reject(new Error('no parent generation — per-entry A/B needs a champion'));
        }
        const url = `/api/matchup-grid/${encodeURIComponent(epochId)}/${encodeURIComponent(championId)}/${encodeURIComponent(genId)}`;
        return fetchJson(url);
      },
      (grid) => sectionEntries(grid, genId),
      { runningLabel: 'Reading per-entry losses', emptyLabel: 'No per-entry losses recorded' },
    );
    screen.appendChild(section);
  }

  // 6. Primary driver + per-judge attribution.
  {
    const { section, body } = sectionFrame('Per-judge attribution', 'where the loss change came from');
    asyncSection(
      body,
      () => {
        if (!epochId || championId == null) {
          return Promise.reject(new Error('no parent generation — per-judge comparison needs a champion'));
        }
        const url = `/api/round/${encodeURIComponent(epochId)}/${encodeURIComponent(championId)}/${encodeURIComponent(genId)}/per-judge-comparison`;
        return fetchJson(url);
      },
      (cmp) => sectionJudges(cmp),
      { runningLabel: 'Comparing judges', emptyLabel: 'No per-judge losses recorded' },
    );
    screen.appendChild(section);
  }

  // 7. Patches (sync — from the experiment record).
  {
    const { section, body } = sectionFrame('Patches', 'the exact change');
    const patches = sectionPatches(exp);
    body.appendChild(patches || stateBlock('empty', {
      label: 'No patches recorded',
      detail: 'This generation has no mutation patches in the experiment record.',
    }));
    screen.appendChild(section);
  }

  host.appendChild(screen);
}

// Reset the coarse re-render key — tests share module state across cases.
export function resetExperimentView() { _lastKey = null; }

// Self-register so the shell routes `experiment` here once this module is
// imported. The import is wired by the views barrel (a later wave) or a
// test that imports this module directly.
import { registerView } from '../shell.js';
registerView('experiment', renderExperiment);
