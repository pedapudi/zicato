// variants/F/views/styles.js — Screen: TOURNAMENT-STYLE TOPOLOGY SWITCHER (theme 4).
//
// THE showcase. The SAME candidate set, re-laid-out under five different
// selection structures, each a DIFFERENT graph topology:
//
//   gauntlet  — star/hub (REAL: champion centre, real /api/tournaments
//               matchups; clicking a spoke opens the paired per-board
//               duel grid from /api/matchup-grid).
//   single    — binary bracket tree (illustrative).
//   double    — two coupled trees, winners'/losers' (illustrative).
//   swiss     — round-by-round bipartite pairing (illustrative).
//   racing    — parallel lanes with cut-lines (illustrative).
//
// A style switcher re-runs layout on the same node ids. Only the gauntlet
// has real per-round data; the others are honest conceptual overlays
// (SELECTION.md §2/§5/§6) — each is labelled accordingly.
//
// Data: /api/tournaments (real matchups + decisions/deltas),
//       /api/matchup-grid/{e}/{champ}/{chall} (the paired per-board duel),
//       /api/lineage (the candidate set the layouts share).

import { el, svgEl, clearChildren } from '../../../core/dom.js';
import { fetchJson } from '../../../core/api.js';
import { createSurface, createSurfaceControls } from '../diagram/surface.js';
import { verdictClass, edgeEl, flowPath } from '../diagram/primitives.js';
import { TOURNAMENT_STYLES } from '../diagram/topology.js';
import { candidatesOf, championOf, matchupGridRows } from '../model.js';
import { openDrawer } from '../chrome.js';
import { fmtScalar, fmtDelta } from '../../../core/format.js';
import { pairedSlopegraph } from '../lib/svg.js';

const _tourCache = new Map();   // epoch -> tournaments payload
const _tourLoading = new Set();
const _gridCache = new Map();   // epoch/champ/chall -> rows
const _gridLoading = new Set();
const _styleByEpoch = new Map(); // epoch -> selected style id (sticky)
let _lastDigest = null;

export function resetStylesCaches() {
  _tourCache.clear(); _tourLoading.clear();
  _gridCache.clear(); _gridLoading.clear();
  _styleByEpoch.clear();
  _lastDigest = null;
}

export function renderStyles(ctx) {
  const { stage, state, params, chrome, repaint } = ctx;
  const epochId = params.epochId || (state.epochDef && state.epochDef.epoch_id) || championOfEpochId(state);

  // Digest gate: epoch, candidate set, the loaded tournaments, AND the
  // selected topology (so a tab click rebuilds; rebuild ONLY when selection
  // changes, render-discipline rule 5). No timestamps.
  const candsPre = candidatesOf(state, epochId);
  const tourPre = _tourCache.get(epochId) || null;
  const digest = JSON.stringify({
    epochId,
    selected: _styleByEpoch.get(epochId) || 'gauntlet',
    cands: candsPre.map((c) => [c.id, c.parent, c.promoted]),
    matchups: tourPre && Array.isArray(tourPre.matchups)
      ? tourPre.matchups.map((m) => [m.champion, m.challenger, m.decision, m.delta_scalar]) : null,
  });
  if (digest === _lastDigest && stage.firstChild) return;
  _lastDigest = digest;

  clearChildren(stage);

  stage.appendChild(el('div', { class: 'cz-screen-head' }, [
    el('div', { class: 'cz-epoch-eyebrow' }, ['MATCH-UPS', el('span', { class: 'cz-mono' }, [epochId || '—'])]),
    el('h1', { class: 'cz-screen-title' }, ['Tournament-style match-ups']),
    el('p', { class: 'cz-screen-sub' }, [
      'The same candidate set, re-drawn under five selection structures — a different graph topology each. '
      + 'Only the gauntlet carries real per-round data; the rest are honest conceptual overlays (see SELECTION.md).',
    ]),
  ]));

  const cands = candidatesOf(state, epochId);
  if (cands.length === 0) {
    stage.appendChild(el('div', { class: 'cz-empty' }, ['No candidates in this epoch yet — the topologies fill in as generations are scored.']));
    return;
  }

  ensureTournaments(epochId, repaint);
  const tour = _tourCache.get(epochId) || null;

  // The switcher.
  const selected = _styleByEpoch.get(epochId) || 'gauntlet';
  const switcher = el('div', { class: 'cz-style-switch', role: 'tablist', 'aria-label': 'Tournament style' });
  for (const s of TOURNAMENT_STYLES) {
    const on = s.id === selected;
    switcher.appendChild(el('button', {
      type: 'button', class: 'cz-style-tab' + (on ? ' is-active' : ''),
      'data-cz': 'style-tab', 'data-style': s.id, role: 'tab', 'aria-selected': on ? 'true' : 'false',
      onclick: () => { _styleByEpoch.set(epochId, s.id); if (typeof repaint === 'function') repaint(); },
    }, [
      el('span', { class: 'cz-style-tab-label' }, [s.label]),
      el('span', { class: 'cz-style-tab-topo' }, [s.topology]),
      s.real ? el('span', { class: 'cz-style-badge cz-style-real' }, ['real'])
        : el('span', { class: 'cz-style-badge cz-style-illus' }, ['illustrative']),
    ]));
  }
  stage.appendChild(switcher);

  const style = TOURNAMENT_STYLES.find((s) => s.id === selected) || TOURNAMENT_STYLES[0];
  stage.appendChild(el('div', { class: 'cz-style-blurb ' + (style.real ? 'cz-style-blurb-real' : 'cz-style-blurb-illus') }, [
    el('span', { class: 'cz-style-blurb-tag' }, [style.real ? 'REAL DATA' : 'CONCEPTUAL OVERLAY']),
    el('span', {}, [style.blurb]),
  ]));

  // Build the shared candidate node specs, enriched with real decisions
  // for the gauntlet so the hub edges carry true verdicts + deltas.
  const champId = championOf(state, epochId);
  const matchByChall = new Map();
  if (tour && Array.isArray(tour.matchups)) {
    for (const m of tour.matchups) matchByChall.set(m.challenger, m);
  }
  const specs = cands.map((c) => {
    const m = matchByChall.get(c.id);
    const isChamp = c.id === champId;
    const cls = isChamp ? 'cz-v-promoted'
      : (m ? (m.decision === 'promoted' ? 'cz-v-promoted' : 'cz-v-rejected')
        : (c.promoted === true ? 'cz-v-promoted' : (c.promoted === false ? 'cz-v-rejected' : 'cz-v-running')));
    return {
      id: c.id,
      role: isChamp ? 'champion' : 'challenger',
      promoted: c.promoted === true,
      cls,
      decision: m ? m.decision : null,
      deltaLabel: m && typeof m.delta_scalar === 'number' ? fmtDelta(m.delta_scalar) : null,
    };
  });

  stage.appendChild(buildTopology(style, specs, epochId, champId, chrome, repaint));

  // The real gauntlet round table beneath (only meaningful for real data).
  if (style.real && tour) stage.appendChild(buildRoundTable(tour, epochId, champId, chrome, repaint));
}

function buildTopology(style, specs, epochId, champId, chrome, repaint) {
  const out = style.fn(specs, {});
  const w = Math.max(out.box.w, 720);
  const h = Math.max(out.box.h, 320);
  const surface = createSurface({ width: w, height: h, ariaLabel: style.label + ' topology' });
  const vp = surface.viewport;
  const edgeLayer = svgEl('g', { class: 'cz-edge-layer' });
  const nodeLayer = svgEl('g', { class: 'cz-node-layer' });

  for (const e of out.edges) {
    const path = edgeEl(flowPath(e.x1, e.y1, e.x2, e.y2), {
      cls: normalizeEdgeCls(e.cls), animated: e.kind === 'duel' && style.real && false, width: e.kind === 'lane' ? 5 : (e.kind === 'cut' ? 1.4 : 2),
    });
    if (e.kind === 'cut') path.classList.add('cz-cut-line');
    edgeLayer.appendChild(path);
    if (e.label) {
      const mx = (e.x1 + e.x2) / 2; const my = (e.y1 + e.y2) / 2;
      edgeLayer.appendChild(svgEl('text', { x: mx, y: my - 4, class: 'cz-edge-label cz-mono' }, [String(e.label)]));
    }
  }
  vp.appendChild(edgeLayer);

  for (const n of out.nodes) {
    let g;
    if (n.r != null) {
      // Circular node (gauntlet hub/spokes).
      const clickable = style.real && n.role === 'challenger';
      g = svgEl(clickable ? 'g' : 'g', {
        class: 'cz-node cz-topo-node ' + (n.cls || ''), 'data-cz': 'topo-node', 'data-key': n.id,
        tabindex: clickable ? '0' : null, role: clickable ? 'button' : 'group',
        'aria-label': `${n.label} ${n.sub || ''}`,
      }, [
        svgEl('circle', { cx: n.x, cy: n.y, r: n.r, class: 'cz-node-disc' }),
        svgEl('text', { x: n.x, y: n.y - 2, class: 'cz-node-gid' }, [n.label]),
        n.sub ? svgEl('text', { x: n.x, y: n.y + 13, class: 'cz-node-verdict' }, [clip(n.sub, 16)]) : null,
      ]);
      if (clickable) {
        const open = () => openDuelGrid(chrome, epochId, champId, n.id, repaint);
        g.classList.add('cz-clickable');
        g.addEventListener('click', open);
        g.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') open(); });
      }
    } else if (n.role === 'lane') {
      // Racing lane: a confidence band rectangle that narrows; the cut.
      const y = n.y;
      g = svgEl('g', { class: 'cz-node cz-topo-lane ' + (n.cls || ''), 'data-cz': 'topo-node', 'data-key': n.id }, [
        svgEl('rect', { x: n.x, y: y - n.h / 2, width: Math.max(8, n.w), height: n.h, rx: n.h / 2, class: 'cz-lane-band' }),
        svgEl('text', { x: n.x + 6, y: y - n.h / 2 - 6, class: 'cz-node-gid', 'text-anchor': 'start' }, [n.label]),
        n.sub ? svgEl('text', { x: n.x + 6, y: y + n.h / 2 + 14, class: 'cz-node-verdict', 'text-anchor': 'start' }, [n.sub]) : null,
      ]);
    } else {
      // Rectangular node (brackets / swiss).
      g = svgEl('g', { class: 'cz-node cz-topo-node ' + (n.cls || ''), 'data-cz': 'topo-node', 'data-key': n.id }, [
        svgEl('rect', { x: n.x, y: n.y, width: n.w, height: n.h, rx: 7, class: 'cz-node-box' }),
        svgEl('text', { x: n.x + n.w / 2, y: n.y + n.h / 2 - 3, class: 'cz-node-gid' }, [clip(n.label, 12)]),
        n.sub ? svgEl('text', { x: n.x + n.w / 2, y: n.y + n.h / 2 + 12, class: 'cz-node-verdict' }, [clip(n.sub, 16)]) : null,
      ]);
    }
    nodeLayer.appendChild(g);
  }
  vp.appendChild(nodeLayer);
  surface.fit(out.box);

  return el('div', { class: 'cz-canvas-wrap', 'data-cz': 'topo-canvas', 'data-style': style.id }, [
    createSurfaceControls(surface, el), surface.svg,
  ]);
}

// The real gauntlet rounds as a compact list with drill into the duel grid.
function buildRoundTable(tour, epochId, champId, chrome, repaint) {
  const matchups = Array.isArray(tour.matchups) ? tour.matchups : [];
  const wrap = el('div', {}, [
    el('h2', { class: 'cz-section-title' }, ['Gauntlet rounds']),
    el('p', { class: 'cz-section-sub' }, ['Each round is a paired duel: champion vs one challenger, the same board under both. Open a round for the per-board duel grid.']),
  ]);
  if (!matchups.length) {
    wrap.appendChild(el('div', { class: 'cz-empty' }, ['No rounds recorded.']));
    return wrap;
  }
  const list = el('div', { class: 'cz-board-cluster' });
  for (const m of matchups) {
    const dec = m.decision || 'running';
    list.appendChild(el('button', {
      type: 'button', class: 'cz-board-node cz-round-card ' + verdictClass(dec),
      'data-cz': 'round-card', 'data-key': m.challenger,
      onclick: () => openDuelGrid(chrome, epochId, m.champion || champId, m.challenger, repaint),
    }, [
      el('div', { class: 'cz-board-node-head' }, [
        el('span', { class: 'cz-board-id cz-mono' }, [(m.champion || champId) + ' vs ' + m.challenger]),
        el('span', { class: 'cz-board-weight' }, [typeof m.delta_scalar === 'number' ? fmtDelta(m.delta_scalar) : '—']),
      ]),
      el('div', { class: 'cz-board-kind' }, [String(dec)]),
      m.hypothesis_core_idea ? el('div', { class: 'cz-board-preview' }, [clip(String(m.hypothesis_core_idea), 110)]) : null,
    ]));
  }
  wrap.appendChild(list);
  return wrap;
}

// The paired per-board duel grid (the heart of one real round) — a
// slopegraph: champion loss on the left, challenger on the right, one
// line per board entry, coloured by who won.
async function openDuelGrid(chrome, epochId, champId, challId, repaint) {
  const body = el('div', { class: 'cz-duel' });
  body.appendChild(el('p', { class: 'cz-brief-meta' }, [
    'Paired per-board duel — ', el('span', { class: 'cz-mono' }, [String(champId)]), ' vs ',
    el('span', { class: 'cz-mono' }, [String(challId)]),
    '. Common-random-number: the same board runs under both sides, so shared difficulty cancels.',
  ]));
  const slot = el('div', { 'data-cz': 'duel-slot' }, [el('div', { class: 'cz-section-sub' }, ['Loading duel grid…'])]);
  body.appendChild(slot);
  openDrawer(chrome, 'Duel · ' + champId + ' vs ' + challId, body);

  const rows = await ensureGrid(epochId, champId, challId);
  paintDuel(slot, rows, champId, challId);
  void repaint;
}

function paintDuel(slot, rows, champId, challId) {
  clearChildren(slot);
  if (!rows || !rows.length) {
    slot.appendChild(el('div', { class: 'cz-section-sub' }, ['No paired grid available for this round.']));
    return;
  }
  // D's non-colliding paired slopegraph: champion loss left, challenger
  // right, one line per board entry, de-collided labels + jittered nodes.
  slot.appendChild(pairedSlopegraph({
    width: 520, height: Math.max(160, rows.length * 42 + 60), labelGap: 150,
    left: { title: champId }, right: { title: challId },
    goodDirection: 'down',
    series: rows.map((r) => ({
      label: r.entryId, id: r.entryId,
      a: r.championLoss, b: r.challengerLoss, verdict: r.verdict,
    })),
  }));
  void svgEl; void fmtScalar; void clip;

  // A who-won strip.
  let cWon = 0; let chWon = 0; let flat = 0;
  for (const r of rows) {
    if (r.verdict === 'improved') chWon++;
    else if (r.verdict === 'regressed') cWon++;
    else flat++;
  }
  slot.appendChild(el('div', { class: 'cz-legend', 'data-cz': 'duel-tally' }, [
    el('span', { class: 'cz-legend-chip' }, [el('span', { class: 'cz-legend-dot cz-v-rejected' }), `${champId} won ${cWon}`]),
    el('span', { class: 'cz-legend-chip' }, [el('span', { class: 'cz-legend-dot cz-v-promoted' }), `${challId} won ${chWon}`]),
    el('span', { class: 'cz-legend-chip' }, [el('span', { class: 'cz-legend-dot cz-v-running' }), `flat ${flat}`]),
  ]));
}

// -- helpers ----------------------------------------------------------
function normalizeEdgeCls(cls) {
  // Topology edges carry verdict classes (cz-v-*) OR edge classes
  // (cz-edge-*). Map verdict → edge so the stroke colour applies.
  if (cls === 'cz-v-promoted') return 'cz-edge-promoted';
  if (cls === 'cz-v-rejected') return 'cz-edge-rejected';
  if (cls === 'cz-v-running') return 'cz-edge-running';
  return cls || 'cz-edge';
}
function championOfEpochId(state) {
  const eps = Array.isArray(state.epochs) ? state.epochs : [];
  return eps.length ? eps[eps.length - 1].epoch_id : null;
}
function gkey(e, c, h) { return e + '/' + c + '/' + h; }
function clip(s, n) { s = String(s || ''); return s.length > n ? s.slice(0, n - 1) + '…' : s; }

async function ensureTournaments(epochId, repaint) {
  if (!epochId || _tourCache.has(epochId) || _tourLoading.has(epochId)) return;
  _tourLoading.add(epochId);
  try {
    _tourCache.set(epochId, await fetchJson('/api/tournaments'));
  } catch {
    _tourCache.set(epochId, { matchups: [] });
  } finally {
    _tourLoading.delete(epochId);
    if (typeof repaint === 'function') repaint();
  }
}

async function ensureGrid(epochId, champId, challId) {
  const k = gkey(epochId, champId, challId);
  if (_gridCache.has(k)) return _gridCache.get(k);
  if (_gridLoading.has(k)) return [];
  _gridLoading.add(k);
  try {
    const d = await fetchJson('/api/matchup-grid/' + encodeURIComponent(epochId) + '/' + encodeURIComponent(champId) + '/' + encodeURIComponent(challId));
    const rows = matchupGridRows(d);
    _gridCache.set(k, rows);
    return rows;
  } catch {
    _gridCache.set(k, []);
    return [];
  } finally {
    _gridLoading.delete(k);
  }
}
