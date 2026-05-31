// js/v2/views/experiment.js — the Experiment view as a CAUSAL NARRATIVE.
//
// DASHBOARD-V2 §3 (graphical & interactive) + §4.4. zicato is a causal
// instrument, and this view reads like the sentence it measures:
//
//     a CODE CHANGE  →  a BEHAVIORAL CHANGE  →  a VERDICT
//   (a patch to the   (drift movement across   (the promote
//    agent's instrs/   the board, by kind)      gate)
//    tools)
//
// rendered as THREE LINKED VISUAL PANELS, not an ACM text stack:
//
//   1. CAUSE   — the patch, as a real red/green diff viewer (the
//      centerpiece, restored): the instruction / tool-description text the
//      patch edited, labeled with the mutation-point id + op + rationale.
//      Data: the experiment's patches + GET …/patches + …/diff.
//   2. EFFECT  — drift movement, visual: a per-entry diverging A/B AND a
//      drift-KIND composition (off_topic / looping_reasoning / …) showing
//      which behaviors moved, plus the predicted-vs-actual bet and
//      pass→fail flips. Each entry row drills to its Run.
//   3. VERDICT — the gate, visual: the gate ladder (the decision flow) +
//      the scalar waterfall (champion→challenger per component).
//
// Interactivity (the point): hovering a diff hunk / mutation id highlights
// the drift kinds + entries it plausibly moved, and vice-versa; clicking a
// patch PINS the highlight; clicking an entry drills to its run.
//
// The seed (v0) is honest: a root generation has NO parent champion, so
// instead of red "no champion to compare" errors, the comparative panels
// render an honest seed panel ("the baseline — no matchup to compare").
//
// Every async section renders through stateBlock — the four honest states
// — and a fetch failure degrades ONLY its own section: the screen stays up.

import { el, clearChildren } from '../../core/dom.js';
import { state } from '../../core/state.js';
import { fetchJson } from '../../core/api.js';
import { bus } from '../../core/bus.js';
import { v2Router } from '../router.js';

import { stateBlock } from '../components/stateBlock.js';
import { dataTable } from '../components/dataTable.js';
import { mutationPatchCard, fileDiffCard } from '../components/patchDiff.js';
import { driftComposition } from '../components/driftComposition.js';

// v1 atoms — reused by direct path (DASHBOARD-V2 §5: keep their factory
// contracts). They carry their own CSS (css/decision.css, already linked).
import { gateLadder } from '../../components/gate_ladder.js';
import { divergingBar } from '../../components/diverging_bar.js';
import { scalarWaterfall } from '../../components/scalar_waterfall.js';
import { verdictGlyph } from '../../components/verdict_glyph.js';

// Self-inject the scoped stylesheet once (index.html also links it; this
// is a belt-and-braces no-op when present or when there is no head).
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

// The experiment record (hypothesis + outcome + patches) for a gen.
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

function _epochIdFor(genId, gen, exp) {
  const fromGen = gen && (gen.epoch_id || gen.epochId);
  if (fromGen) return String(fromGen);
  if (exp && exp.epoch_id) return String(exp.epoch_id);
  const def = state.epochDef || {};
  if (def.epoch_id) return String(def.epoch_id);
  if (state.epoch && state.epoch.id && state.epoch.id !== '—') return String(state.epoch.id);
  return null;
}

// A seed/root generation has no parent champion to compare against. We
// detect it from the resolved champion id (null) — the SINGLE source of
// "is this the baseline" so no comparative section guesses differently.
function _isSeed(championId) {
  return championId == null;
}

// ---------------------------------------------------------------------------
// The view-local highlight bus. Hovering a patch (a mutation id) lights up
// the drift kinds + entries it plausibly moved; hovering a drift kind /
// entry lights the patches back. A click PINS the highlight (so the
// operator can study the linked rows without holding the mouse still). One
// instance per render; nodes register their (key → element[]) maps with it.
// ---------------------------------------------------------------------------
function makeHighlighter() {
  const groups = new Map(); // groupName -> { keyOf(el)->key, nodes: Map<key, Set<el>> }
  let pinned = null;        // { group, key } | null

  function register(group, node, key) {
    if (key == null) return;
    let g = groups.get(group);
    if (!g) { g = new Map(); groups.set(group, g); }
    let set = g.get(String(key));
    if (!set) { set = new Set(); g.set(String(key), set); }
    set.add(node);
  }

  function _setClass(group, key, on) {
    const g = groups.get(group);
    if (!g) return;
    const set = g.get(String(key));
    if (!set) return;
    for (const node of set) {
      if (node && node.classList) node.classList.toggle('v2-exp-lit', on);
    }
  }

  // The cross-panel mapping: a patch (mutation id) plausibly moved every
  // drift kind + every entry (we cannot know the precise causal subset
  // without per-kind attribution, so a patch lights the whole EFFECT
  // panel's movement; a drift kind / entry lights the whole CAUSE panel).
  // This is honest: it says "this change is the cause under study", not a
  // false-precise claim. Each group keys on its own ids and additionally
  // toggles the sibling groups' "linked" wash.
  function _siblings(group) {
    // Hovering any group washes the OTHER two panels so the eye follows
    // the cause→effect link. The hovered group highlights only its own key.
    return ['cause', 'effect-kind', 'effect-entry'].filter((s) => s !== group);
  }

  function highlight(group, key) {
    _setClass(group, key, true);
    for (const sib of _siblings(group)) {
      const g = groups.get(sib);
      if (!g) continue;
      for (const k of g.keys()) _setClass(sib, k, true);
    }
  }
  function clear(group, key) {
    if (pinned) return; // a pin holds the highlight until cleared
    _setClass(group, key, false);
    for (const sib of _siblings(group)) {
      const g = groups.get(sib);
      if (!g) continue;
      for (const k of g.keys()) _setClass(sib, k, false);
    }
  }
  function _clearAll() {
    for (const [group, g] of groups) for (const k of g.keys()) _setClass(group, k, false);
  }
  function pin(group, key) {
    if (pinned && pinned.group === group && pinned.key === String(key)) {
      pinned = null; _clearAll(); return;
    }
    pinned = { group, key: String(key) };
    _clearAll();
    highlight(group, key);
  }
  return { register, highlight, clear, pin };
}

// ---------------------------------------------------------------------------
// Async section helper.
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

// A honest "seed / baseline" panel — the explicit, non-error resting place
// for v0's comparative sections (there is no champion to compare against).
function seedPanel(detail) {
  return stateBlock('empty', {
    label: 'Seed generation — the baseline',
    detail: detail || 'No parent champion, so there is no matchup to compare against. '
      + 'The seed defines the starting behavior every later challenger is measured against.',
  });
}

// ---------------------------------------------------------------------------
// Header — champion → challenger + the verdict glyph + the causal kicker.
// ---------------------------------------------------------------------------
function sectionHeader(genId, championId, decision) {
  const seed = _isSeed(championId);
  const ids = el('div', { class: 'v2-exp-ids' }, [
    seed
      ? el('span', { class: 'v2-exp-champ v2-exp-champ-none v2-mono', title: 'no parent — seed / baseline generation' }, ['seed'])
      : el('span', { class: 'v2-exp-champ v2-mono', title: 'champion (parent)' }, [String(championId)]),
    el('span', { class: 'v2-exp-arrow', 'aria-hidden': 'true' }, ['→']),
    el('span', { class: 'v2-exp-chall v2-mono', title: 'challenger' }, [String(genId)]),
  ]);
  return el('header', { class: 'v2-exp-head' }, [
    el('div', { class: 'v2-exp-head-l' }, [
      el('div', { class: 'v2-exp-kicker' }, [
        seed ? 'Experiment · the baseline' : 'Experiment · a code change → a behavioral change → a verdict',
      ]),
      ids,
    ]),
    el('div', { class: 'v2-exp-verdict' }, [verdictGlyph(decision, { withLabel: true })]),
  ]);
}

// ---------------------------------------------------------------------------
// PANEL 1 — CAUSE: the patch diff. Joins the structured patches (intent:
// mutation id + op + rationale) to the realized file diffs (red/green
// line diff) by target path, and renders each as a themed diff card. Each
// card hovers → lights the EFFECT panel (the behaviors it plausibly moved).
// ---------------------------------------------------------------------------

// Pull the structured patches off the experiment record (preferred) or the
// /patches endpoint payload. Tolerant of the dict-keyed-by-mutation form
// AND the list-of-Patch form.
function _patchList(exp, patchesPayload) {
  const out = [];
  const fromPayload = patchesPayload && Array.isArray(patchesPayload.patches) ? patchesPayload.patches : null;
  if (fromPayload && fromPayload.length) {
    for (const p of fromPayload) if (p) out.push(p);
    return out;
  }
  const raw = exp && exp.patches;
  if (Array.isArray(raw)) {
    for (const p of raw) if (p) out.push(p);
  } else if (raw && typeof raw === 'object') {
    for (const mid of Object.keys(raw)) {
      const p = raw[mid] || {};
      out.push(Object.assign({ mutation_id: p.mutation_id || mid }, p));
    }
  }
  return out;
}

// Index the diff endpoint's files by path so a patch can show its realized
// red/green diff. The patch's `target` (or `path`/`file`) names the file.
function _diffByPath(diffPayload) {
  const m = new Map();
  const files = diffPayload && Array.isArray(diffPayload.files) ? diffPayload.files : [];
  for (const f of files) if (f && f.path != null) m.set(String(f.path), f);
  return m;
}
function _patchTarget(p) {
  return p && (p.target || p.path || p.file || p.mutation_point) ? String(p.target || p.path || p.file || p.mutation_point) : '';
}

function sectionCause(exp, patchesPayload, diffPayload, hl) {
  const patches = _patchList(exp, patchesPayload);
  const diffByPath = _diffByPath(diffPayload);
  const looseFiles = diffPayload && Array.isArray(diffPayload.files) ? diffPayload.files : [];

  // Nothing changed at all (seed with empty tree is handled upstream by
  // the file fallback) — honest empty, not a blank.
  if (patches.length === 0 && looseFiles.length === 0) return null;

  const wrap = el('div', { class: 'v2-exp-cause' });
  const hooks = {
    onHover: () => hl.highlight('cause', '*'),
    onHoverEnd: () => hl.clear('cause', '*'),
  };

  if (patches.length) {
    const matched = new Set();
    for (const p of patches) {
      const target = _patchTarget(p);
      const fileDiff = target ? diffByPath.get(target) : null;
      if (fileDiff) matched.add(target);
      const card = mutationPatchCard(p, fileDiff, hooks);
      // Pin the highlight on click so the linked behaviors stay lit.
      card.addEventListener('click', () => hl.pin('cause', '*'));
      hl.register('cause', card, '*');
      wrap.appendChild(card);
    }
    // Realized file diffs not claimed by a structured patch still belong to
    // the change — render them so the CAUSE is complete, not partial.
    for (const f of looseFiles) {
      if (f && f.path != null && matched.has(String(f.path))) continue;
      const card = fileDiffCard(f, hooks);
      card.addEventListener('click', () => hl.pin('cause', '*'));
      hl.register('cause', card, '*');
      wrap.appendChild(card);
    }
  } else {
    // No structured patches recorded, but the diff endpoint has the files
    // the generation changed — render those as the CAUSE.
    for (const f of looseFiles) {
      const card = fileDiffCard(f, hooks);
      card.addEventListener('click', () => hl.pin('cause', '*'));
      hl.register('cause', card, '*');
      wrap.appendChild(card);
    }
  }
  return wrap;
}

// ---------------------------------------------------------------------------
// PANEL 2 — EFFECT: drift movement. The bet (predicted vs actual), the
// drift-KIND composition, and the per-entry A/B (with pass→fail flips).
// ---------------------------------------------------------------------------

function _predictedDriftRows(hyp) {
  const moves = hyp && Array.isArray(hyp.expected_drift_movements) ? hyp.expected_drift_movements : [];
  const out = [];
  for (const m of moves) {
    if (!m || !m.kind) continue;
    const dir = String(m.direction || '').toLowerCase();
    const sign = (dir.includes('few') || dir.includes('down') || dir.includes('improv') || dir.includes('less') || dir.includes('reduc'))
      ? -1
      : ((dir.includes('more') || dir.includes('up') || dir.includes('wors') || dir.includes('increa')) ? 1 : 0);
    let mag = Number(m.magnitude);
    if (!isFinite(mag) || mag <= 0) mag = 1;
    out.push({ label: String(m.kind), delta: sign * mag, annotation: { glyph: '◇', title: 'predicted' } });
  }
  return out;
}
function _actualDriftRows(movements) {
  const list = Array.isArray(movements) ? movements : [];
  return list
    .filter((m) => m && m.kind != null && typeof m.delta === 'number' && isFinite(m.delta))
    .map((m) => ({ label: String(m.kind), delta: m.delta }));
}
function _alignment(predictedRows, actualRows) {
  const actualByKind = new Map();
  for (const r of actualRows) actualByKind.set(r.label, r.delta);
  const kinds = [];
  let aligned = 0;
  for (const p of predictedRows) {
    if (p.delta === 0) continue;
    const a = actualByKind.get(p.label);
    if (typeof a !== 'number') { kinds.push({ kind: p.label, ok: null }); continue; }
    const ok = (p.delta < 0 && a < 0) || (p.delta > 0 && a > 0);
    if (ok) aligned += 1;
    kinds.push({ kind: p.label, ok });
  }
  const scorable = kinds.filter((k) => k.ok !== null).length;
  return { aligned, scorable, kinds };
}

// The bet sub-figure: prose + predicted/actual diverging bars + alignment.
function betFigure(exp, movements) {
  const hyp = (exp && typeof exp.hypothesis === 'object' && exp.hypothesis) || {};
  const outcome = (exp && typeof exp.outcome === 'object' && exp.outcome) || null;

  const wrap = el('div', { class: 'v2-exp-bet' });
  const prose = el('div', { class: 'v2-exp-bet-prose' });
  if (typeof hyp.core_idea === 'string' && hyp.core_idea.trim()) {
    prose.appendChild(el('p', { class: 'v2-exp-core' }, [hyp.core_idea]));
  }
  if (typeof hyp.why === 'string' && hyp.why.trim()) {
    prose.appendChild(el('p', { class: 'v2-exp-why' }, [
      el('span', { class: 'v2-exp-lead' }, ['Why. ']), hyp.why,
    ]));
  }
  if (typeof hyp.expected_pass_rate_delta === 'string' && hyp.expected_pass_rate_delta.trim()) {
    prose.appendChild(el('p', { class: 'v2-exp-pred-pass' }, [
      el('span', { class: 'v2-exp-lead' }, ['Predicted pass-rate Δ. ']),
      el('span', { class: 'v2-mono' }, [hyp.expected_pass_rate_delta]),
    ]));
  }
  if (!prose.firstChild) {
    prose.appendChild(el('p', { class: 'v2-exp-empty-prose' }, ['No structured hypothesis recorded for this generation.']));
  }
  wrap.appendChild(prose);

  const predicted = _predictedDriftRows(hyp);
  const actual = _actualDriftRows(movements);

  const fig = el('div', { class: 'v2-exp-driftfig' });
  fig.appendChild(el('h4', { class: 'v2-exp-subh' }, ['Predicted vs actual drift movement']));
  const cols = el('div', { class: 'v2-exp-driftcols' });
  const predCol = el('div', { class: 'v2-exp-driftcol' }, [el('div', { class: 'v2-exp-driftcol-h' }, ['Predicted'])]);
  predCol.appendChild(predicted.length
    ? divergingBar({ rows: predicted, goodWhenNegative: true })
    : el('p', { class: 'v2-exp-empty-prose' }, ['No drift movement predicted.']));
  const actCol = el('div', { class: 'v2-exp-driftcol' }, [el('div', { class: 'v2-exp-driftcol-h' }, ['Actual (challenger − champion)'])]);
  actCol.appendChild(actual.length
    ? divergingBar({ rows: actual, goodWhenNegative: true })
    : stateBlock('empty', { label: 'No drift movement recorded', detail: 'The index has no per-kind drift counts for this round.' }));
  cols.appendChild(predCol);
  cols.appendChild(actCol);
  fig.appendChild(cols);

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

  if (outcome && typeof outcome.summary === 'string' && outcome.summary.trim()) {
    fig.appendChild(el('p', { class: 'v2-exp-outcome-summary' }, [
      el('span', { class: 'v2-exp-lead' }, ['Outcome. ']), outcome.summary,
    ]));
  }
  wrap.appendChild(fig);
  return wrap;
}

// The drift-KIND composition (which behaviors moved), wired to the highlighter.
function driftKindFigure(movements, hl) {
  const rows = _actualDriftRows(movements);
  if (!rows.length) return null;
  const fig = el('div', { class: 'v2-exp-driftkind' });
  fig.appendChild(el('h4', { class: 'v2-exp-subh' }, ['Which behaviors moved — drift by kind']));
  const comp = driftComposition({
    movements,
    onHover: (k) => hl.highlight('effect-kind', k),
    onHoverEnd: (k) => hl.clear('effect-kind', k),
  });
  // Register each kind row with the highlighter so a CAUSE hover lights it.
  for (const row of comp.children || []) {
    const k = row.getAttribute && row.getAttribute('data-kind');
    if (k) {
      hl.register('effect-kind', row, k);
      row.addEventListener('click', () => hl.pin('effect-kind', k));
    }
  }
  fig.appendChild(comp);
  return fig;
}

function _flip(parentPass, childPass) {
  if (parentPass === true && childPass === false) return 'regress';
  if (parentPass === false && childPass === true) return 'recover';
  return null;
}

// The seed ran the full board too — it has no champion to compare against,
// but its ABSOLUTE per-entry results ARE the baseline every later challenger
// is measured against, so we show them (drift loss + pass/fail), not a Δ.
function seedEntriesTable(perEntry, genId) {
  const rows = Array.isArray(perEntry && perEntry.entries) ? perEntry.entries : [];
  if (!rows.length) return null;
  return dataTable({
    ariaLabel: 'baseline per-entry board results',
    rows,
    rowKey: (r) => r.entry_id,
    onRowClick: (r) => { if (r && r.entry_id != null) v2Router.go('run', r.entry_id, genId); },
    columns: [
      {
        key: 'entry_id', header: 'entry', mono: true,
        render: (r) => el('span', { class: 'v2-mono' }, [String(r.entry_id == null ? '' : r.entry_id)]),
        sortValue: (r) => String(r.entry_id == null ? '' : r.entry_id),
      },
      {
        key: 'drift_loss', header: 'drift loss', mono: true, align: 'right',
        render: (r) => el('span', { class: 'v2-mono' },
          [(typeof r.drift_loss === 'number' && isFinite(r.drift_loss)) ? r.drift_loss.toFixed(2) : '—']),
        sortValue: (r) => (typeof r.drift_loss === 'number' ? r.drift_loss : 0),
      },
      {
        key: 'pass_fail', header: 'verdict',
        render: (r) => {
          const p = r.pass_fail;
          const pass = p === true || p === 1;
          const fail = p === false || p === 0;
          return el('span', {
            class: 'v2-exp-' + (pass ? 'pass' : fail ? 'fail' : 'none'),
          }, [pass ? '✓ pass' : fail ? '✗ fail' : '—']);
        },
        sortValue: (r) => ((r.pass_fail === true || r.pass_fail === 1) ? 1 : 0),
      },
    ],
  });
}

function entriesTable(grid, genId, hl) {
  const rows = Array.isArray(grid && grid.entry_grid) ? grid.entry_grid : [];
  if (!rows.length) return null;

  const table = dataTable({
    ariaLabel: 'per-entry challenger vs champion drift',
    rows,
    rowKey: (r) => r.entry_id,
    onRowClick: (r) => { if (r && r.entry_id != null) v2Router.go('run', r.entry_id, genId); },
    columns: [
      {
        key: 'entry_id', header: 'entry', mono: true,
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
        render: (r) => el('span', { class: 'v2-mono' }, [typeof r.parent_drift_loss === 'number' ? r.parent_drift_loss.toFixed(2) : '—']),
        sortValue: (r) => (typeof r.parent_drift_loss === 'number' ? r.parent_drift_loss : null),
      },
      {
        key: 'child_drift_loss', header: 'chall drift', mono: true,
        render: (r) => el('span', { class: 'v2-mono' }, [typeof r.child_drift_loss === 'number' ? r.child_drift_loss.toFixed(2) : '—']),
        sortValue: (r) => (typeof r.child_drift_loss === 'number' ? r.child_drift_loss : null),
      },
      { key: 'delta', header: 'Δ drift', semantic: 'delta', improveWhenNegative: true, digits: 2, value: (r) => (typeof r.delta === 'number' ? r.delta : null) },
    ],
    sort: { key: 'delta', dir: 'asc' },
  });
  // Wire each entry row into the highlighter (a CAUSE hover washes them).
  for (const tr of (table.querySelectorAll ? table.querySelectorAll('[data-key]') : [])) {
    const k = tr.getAttribute('data-key');
    if (k) hl.register('effect-entry', tr, k);
  }
  return table;
}

// ---------------------------------------------------------------------------
// PANEL 3 — VERDICT: the gate ladder + the scalar waterfall.
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
    const delta = (cOk ? c : 0) === 0 && !cOk ? (kOk ? k : 0) : ((kOk ? k : 0) - (cOk ? c : 0));
    out.push({ name, delta });
  }
  return out;
}

function gateFigure(g) {
  const rules = Array.isArray(g && g.rules) ? g.rules : [];
  const comps = _waterfallComponents(g && g.scalar_components);
  if (!rules.length && !comps.length) return null;

  const wrap = el('div', { class: 'v2-exp-verdict-panel' });
  if (g && g.reason) {
    wrap.appendChild(el('p', { class: 'v2-exp-gate-reason' }, [
      verdictGlyph(g.decision, { withLabel: true }),
      el('span', { class: 'v2-exp-gate-reason-text' }, [String(g.reason)]),
    ]));
  }
  const cols = el('div', { class: 'v2-exp-verdict-cols' });
  // Left: the gate ladder — the decision flow.
  const ladderCol = el('div', { class: 'v2-exp-verdict-col' }, [
    el('h4', { class: 'v2-exp-subh' }, ['Promote gate — the decision flow']),
  ]);
  ladderCol.appendChild(rules.length
    ? gateLadder({ rules })
    : el('p', { class: 'v2-exp-empty-prose' }, ['No gate rules recorded.']));
  // Right: the scalar waterfall — what moved the loss.
  const wfCol = el('div', { class: 'v2-exp-verdict-col' }, [
    el('h4', { class: 'v2-exp-subh' }, ['Scalar decomposition — what moved the loss']),
  ]);
  wfCol.appendChild(comps.length
    ? scalarWaterfall({ components: comps, label: 'champion → challenger, per component' })
    : el('p', { class: 'v2-exp-empty-prose' }, ['No scalar components recorded.']));
  cols.appendChild(ladderCol);
  cols.appendChild(wfCol);
  wrap.appendChild(cols);
  return wrap;
}

// ---------------------------------------------------------------------------
// Numbered panel frame: a big numbered title + a one-line causal caption.
// ---------------------------------------------------------------------------
function panelFrame(num, title, caption) {
  const sec = el('section', { class: 'v2-exp-panel', 'data-panel': String(num) });
  sec.appendChild(el('div', { class: 'v2-exp-panel-head' }, [
    el('span', { class: 'v2-exp-panel-num', 'aria-hidden': 'true' }, [String(num)]),
    el('div', { class: 'v2-exp-panel-headtext' }, [
      el('h3', { class: 'v2-exp-panel-title' }, [title]),
      caption ? el('p', { class: 'v2-exp-panel-caption' }, [caption]) : null,
    ]),
  ]));
  const body = el('div', { class: 'v2-exp-panel-body' });
  sec.appendChild(body);
  return { section: sec, body };
}

// A sub-block inside a panel (a labeled figure host the loader fills).
function subBlock(label, hint) {
  const wrap = el('div', { class: 'v2-exp-sub' });
  wrap.appendChild(el('div', { class: 'v2-exp-sub-head' }, [
    el('h4', { class: 'v2-exp-sub-title' }, [label]),
    hint ? el('span', { class: 'v2-exp-sub-hint' }, [hint]) : null,
  ]));
  const body = el('div', { class: 'v2-exp-sub-body' });
  wrap.appendChild(body);
  return { wrap, body };
}

// ---------------------------------------------------------------------------
// Deep-link contract hydration (mirrors epoch.js ensureEpoch).
// ---------------------------------------------------------------------------
let _contractLoading = false;
function ensureContract() {
  const def = state.epochDef;
  if (def && Array.isArray(def.experiments) && def.experiments.length) return;
  if (_contractLoading) return;
  _contractLoading = true;
  Promise.resolve()
    .then(() => fetchJson('/api/epoch'))
    .then((data) => {
      if (data && typeof data === 'object') {
        state.epochDef = Object.assign({}, state.epochDef || {}, data);
      }
    })
    .catch(() => { /* honest-state sections surface the failure */ })
    .finally(() => {
      _contractLoading = false;
      bus.emit('state:changed');
    });
}

// ---------------------------------------------------------------------------
// The view entry.
// ---------------------------------------------------------------------------
let _lastKey = null;

export function renderExperiment(host, route) {
  if (!host) return;
  ensureCss();

  const genId = route && route.params ? route.params.generationId : null;
  if (genId != null) ensureContract();

  const _expReady = _experimentFor(genId) != null;
  const key = 'experiment|' + String(genId == null ? '' : genId) + '|' + (_expReady ? '1' : '0');
  if (key === _lastKey && host.firstChild) return;
  _lastKey = key;

  clearChildren(host);

  if (genId == null) {
    host.appendChild(el('h1', { class: 'v2-view-title' }, ['Experiment']));
    host.appendChild(stateBlock('empty', {
      label: 'No generation selected',
      detail: 'Pick a generation from the lineage spine to see the change it made, what it moved, and the verdict.',
    }));
    return;
  }

  const gen = _findGen(genId);
  const exp = _experimentFor(genId);
  const championId = _parentId(gen) || (exp && exp.parent_generation_id ? String(exp.parent_generation_id) : null);
  const epochId = _epochIdFor(genId, gen, exp);
  const seed = _isSeed(championId);

  const rawDecision = (gen && (gen.verdict || gen.outcome || gen.tournament_decision))
    || (exp && exp.outcome && (exp.outcome.tournament_decision || exp.outcome.decision))
    || (seed ? 'promoted' : 'open');

  const hl = makeHighlighter();
  const screen = el('div', { class: 'v2-exp' });

  // Header.
  screen.appendChild(sectionHeader(genId, championId, rawDecision));

  // ---- PANEL 1 — CAUSE: the patch diff. ----
  {
    const { section, body } = panelFrame(1, 'The change', 'WHAT we changed — the patch to the agent\'s instructions / tools');
    asyncSection(
      body,
      () => {
        // The realized file diff is best-effort: it deepens the structured
        // patches into red/green line diffs. A failure (or a seed with no
        // parent tree) must NOT sink the panel — the structured patches
        // from the experiment record still render. So we always resolve.
        if (!epochId) return Promise.resolve(null);
        const base = `/api/files/${encodeURIComponent(epochId)}/${encodeURIComponent(genId)}`;
        return Promise.all([
          fetchJson(base + '/patches').catch(() => null),
          fetchJson(base + '/diff').catch(() => null),
        ]).then(([patches, diff]) => ({ patches, diff }));
      },
      (data) => {
        const d = data || {};
        const node = sectionCause(exp, d.patches, d.diff, hl);
        if (node) return node;
        return seed
          ? seedPanel('The seed is the starting source tree — there is no parent to diff it against, so there is no "change" to show here.')
          : null; // → honest empty
      },
      { runningLabel: 'Reading the patch', emptyLabel: 'No patches recorded', emptyDetail: 'This generation has no mutation patches in the experiment record.' },
    );
    screen.appendChild(section);
  }

  // The gate is the source of the VERDICT panel's ladder + scalar
  // components — fetched ONCE. For a seed there is no champion, so the
  // gate is not a failure — it is a non-event (handled in the panel).
  let _gatePromise = null;
  function gate() {
    if (_gatePromise) return _gatePromise;
    if (seed || !epochId || championId == null) {
      _gatePromise = Promise.resolve(null); // seed: honest non-event, not an error
      return _gatePromise;
    }
    const url = `/api/round/${encodeURIComponent(epochId)}/${encodeURIComponent(championId)}/${encodeURIComponent(genId)}/gate`;
    _gatePromise = fetchJson(url);
    return _gatePromise;
  }

  // ---- PANEL 2 — EFFECT: drift movement. ----
  {
    const { section, body } = panelFrame(2, 'What moved', 'the behavioral change — drift movement across the board, by kind and by entry');

    // 2a. The bet (predicted vs actual) — always present (sync prose +
    //     async actual drift). Renders for the seed too (predicted only).
    const bet = subBlock('Hypothesis → outcome', 'the bet vs what happened');
    asyncSection(
      bet.body,
      () => fetchJson('/api/drift-movements/' + encodeURIComponent(genId)),
      (dm) => betFigure(exp, dm && dm.movements),
      { runningLabel: 'Reading drift movements' },
    );
    body.appendChild(bet.wrap);

    // 2b. Which behaviors moved — the drift-KIND composition (interactive).
    const kinds = subBlock('Drift composition', 'which behaviors moved · hover to link the change');
    asyncSection(
      kinds.body,
      () => fetchJson('/api/drift-movements/' + encodeURIComponent(genId)),
      (dm) => driftKindFigure(dm && dm.movements, hl),
      { runningLabel: 'Composing drift', emptyLabel: 'No per-kind drift recorded', emptyDetail: 'The index has no per-kind drift counts for this round.' },
    );
    body.appendChild(kinds.wrap);

    // 2c. Per-entry results. For a challenger: the A/B matchup grid
    //     (challenger − champion drift). For the SEED: its own absolute
    //     board results — the baseline every challenger is measured against
    //     (it ran the full board; we show drift loss + pass/fail per entry).
    const entries = subBlock(
      seed ? 'Baseline board results' : 'Per-entry A/B',
      seed ? 'the starting drift on each board entry · row → run' : 'challenger − champion drift · row → run');
    if (seed && !epochId) {
      entries.body.appendChild(seedPanel());
    } else {
      asyncSection(
        entries.body,
        () => {
          if (seed) {
            return fetchJson(`/api/generation/${encodeURIComponent(epochId)}/${encodeURIComponent(genId)}/per-entry`);
          }
          if (!epochId || championId == null) {
            return Promise.reject(new Error('no parent generation — per-entry A/B needs a champion'));
          }
          const url = `/api/matchup-grid/${encodeURIComponent(epochId)}/${encodeURIComponent(championId)}/${encodeURIComponent(genId)}`;
          return fetchJson(url);
        },
        (data) => (seed ? seedEntriesTable(data, genId) : entriesTable(data, genId, hl)),
        { runningLabel: 'Reading per-entry losses', emptyLabel: 'No per-entry losses recorded' },
      );
    }
    body.appendChild(entries.wrap);

    screen.appendChild(section);
  }

  // ---- PANEL 3 — VERDICT: the gate. ----
  {
    const { section, body } = panelFrame(3, 'The verdict', 'the decision — the promote gate and the scalar that moved it');
    if (seed) {
      body.appendChild(seedPanel('The seed is admitted as the baseline — it is not gated against a champion. Later challengers are gated against it.'));
    } else {
      asyncSection(
        body,
        gate,
        (g) => {
          if (g == null) return null; // seed / no gate yet → honest empty
          return gateFigure(g);
        },
        { runningLabel: 'Evaluating gate', emptyLabel: 'No gate recorded', emptyDetail: 'The tournament for this generation has not been gated yet.' },
      );
    }
    screen.appendChild(section);
  }

  host.appendChild(screen);
}

export function resetExperimentView() { _lastKey = null; }

import { registerView } from '../shell.js';
registerView('experiment', renderExperiment);
