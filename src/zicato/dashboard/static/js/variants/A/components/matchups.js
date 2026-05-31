// variants/A/components/matchups.js — Theme 4: match-ups across styles.
//
// zicato actually runs a king-of-the-hill GAUNTLET (one reigning champion
// per epoch, one challenger per round, paired common-random-number board
// comparison; SELECTION.md §3). That real ladder is rendered first, with
// each challenger's per-board duel from /api/matchup-grid.
//
// Then a style switcher re-renders the SAME candidate set under the other
// documented structures — each clearly labelled "conceptual — not how
// zicato ran this epoch" — using a DIFFERENT visual topology per style:
//   * single-elim   → a left-to-right bracket tree
//   * double-elim   → a winners' rail + a losers' bracket below
//   * swiss         → a pairing table with running scores
//   * racing        → race lanes with an elimination cut-line
// (SELECTION.md §2 Family ①②③, §5 spectrum, §6 the explicit verdict.)
//
// Pure builders: (data) -> DOM node. No fetch, no module state.

import { el, svgEl } from '../../../core/dom.js';
import { chip, empty } from './instruments.js';

function fmt(v, d = 2) { return (typeof v === 'number' && isFinite(v)) ? v.toFixed(d) : '—'; }
function signed(v, d = 2) { return (typeof v === 'number' && isFinite(v)) ? (v > 0 ? '+' : '') + v.toFixed(d) : '—'; }

// --------------------------------------------------------------------
// The REAL gauntlet ladder (king of the hill).
//   champion: id (defending at top)
//   matchups: [{ champion, challenger, decision, delta_scalar, ... }]
//   grids: Map<challengerId, entry_grid[]>  (paired per-board duels)
//   onSelectGrid(challengerId): expand/collapse the per-board duel
//   expanded: challengerId currently expanded (or null)
// --------------------------------------------------------------------
export function gauntletLadder({ champion, matchups, grids, expanded, onSelectGrid }) {
  matchups = Array.isArray(matchups) ? matchups : [];
  grids = grids || new Map();
  const wrap = el('div', { class: 'mcA-ladder' });

  // the defending king
  wrap.appendChild(el('div', { class: 'mcA-ladder-king' }, [
    el('span', { class: 'mcA-ladder-king-crown' }, ['♚']),
    el('span', { class: 'mcA-ladder-king-label' }, ['king of the hill']),
    el('span', { class: 'mcA-ladder-king-id mono' }, [champion || '?']),
    el('span', { class: 'mcA-ladder-king-tag mono' }, ['defending']),
  ]));

  if (!matchups.length) {
    wrap.appendChild(empty('No challengers have mounted the hill yet.'));
    return wrap;
  }

  matchups.forEach((m, i) => {
    const promoted = String(m.decision || '').toLowerCase().includes('promot');
    const light = promoted ? 'go' : 'stop';
    const isOpen = expanded === m.challenger;
    const rung = el('div', { class: 'mcA-ladder-rung' + (isOpen ? ' is-open' : '') }, [
      el('div', {
        class: 'mcA-ladder-rung-head' + (onSelectGrid ? ' is-clickable' : ''),
        role: onSelectGrid ? 'button' : null,
        tabindex: onSelectGrid ? '0' : null,
      }, [
        el('span', { class: 'mcA-ladder-step mono' }, ['R' + (i + 1)]),
        el('span', { class: 'mcA-ladder-vs mono' }, [
          el('b', null, [m.champion || champion || '?']),
          ' vs ',
          el('b', { class: 'mcA-ladder-chal' }, [m.challenger || '?']),
        ]),
        el('span', { class: 'mcA-ladder-delta mono ' + (typeof m.delta_scalar === 'number' && m.delta_scalar < 0 ? 'mcA-tag-good' : 'mcA-tag-bad') },
          ['Δ ' + signed(m.delta_scalar)]),
        chip(promoted ? 'challenger crowned' : 'champion holds', light),
      ]),
    ]);
    const headEl = rung.firstChild;
    if (onSelectGrid && m.challenger) {
      headEl.addEventListener('click', () => onSelectGrid(isOpen ? null : m.challenger));
      headEl.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') onSelectGrid(isOpen ? null : m.challenger); });
    }
    if (isOpen) {
      rung.appendChild(perBoardDuel(grids.get(m.challenger), m.champion || champion, m.challenger));
    } else if (m.rejection_reason || m.hypothesis_core_idea) {
      rung.appendChild(el('div', { class: 'mcA-ladder-note' }, [
        m.hypothesis_core_idea ? el('div', { class: 'mcA-ladder-hyp' }, ['idea · ' + m.hypothesis_core_idea]) : null,
        m.rejection_reason ? el('div', { class: 'mcA-ladder-rej mono' }, ['↳ ' + m.rejection_reason]) : null,
      ]));
    }
    wrap.appendChild(rung);
  });
  return wrap;
}

// The paired per-board duel for one round (the heart of a matchup).
//   grid: entry_grid[] from /api/matchup-grid
function perBoardDuel(grid, champId, chalId) {
  grid = Array.isArray(grid) ? grid : [];
  if (!grid.length) return el('div', { class: 'mcA-ladder-duel' }, [empty('Per-board duel not loaded.')]);
  const strip = el('div', { class: 'mcA-duel' });
  strip.appendChild(el('div', { class: 'mcA-duel-headrow mono' }, [
    el('span', null, ['board entry']),
    el('span', null, [champId || 'champ']),
    el('span', null, [chalId || 'chal']),
    el('span', null, ['won by']),
  ]));
  for (const r of grid) {
    const verdict = String(r.verdict || '').toLowerCase();
    const wonChild = r.won_by === chalId;
    const wonChamp = r.won_by === champId;
    strip.appendChild(el('div', { class: 'mcA-duel-row' }, [
      el('span', { class: 'mcA-duel-entry mono' }, [r.entry_id || '?']),
      duelBar(r.parent_drift_loss, r.child_drift_loss, 'parent', wonChamp),
      duelBar(r.child_drift_loss, r.parent_drift_loss, 'child', wonChild),
      el('span', {
        class: 'mcA-duel-won mono ' + (verdict === 'improved' ? 'mcA-tag-good' : verdict === 'regressed' ? 'mcA-tag-bad' : ''),
      }, [r.won_by || (verdict === 'flat' ? 'tie' : '—')]),
    ]));
  }
  return el('div', { class: 'mcA-ladder-duel' }, [strip]);
}

function duelBar(loss, other, side, won) {
  const have = typeof loss === 'number' && isFinite(loss);
  const max = Math.max(have ? loss : 0, typeof other === 'number' ? other : 0) || 1;
  const frac = have ? Math.max(0.04, loss / max) : 0;
  return el('div', { class: 'mcA-duel-cell' }, [
    el('div', { class: 'mcA-duel-track' }, [
      el('div', {
        class: 'mcA-duel-fill' + (won ? ' is-won' : ''),
        'data-side': side,
        style: `width:${(frac * 100).toFixed(1)}%`,
      }),
    ]),
    el('span', { class: 'mcA-duel-val mono' + (have ? '' : ' is-muted') }, [have ? fmt(loss, 1) : '—']),
  ]);
}

// --------------------------------------------------------------------
// Style switcher — the SAME candidate set under alternative structures.
// Each renders a DIFFERENT topology and is labelled conceptual.
//
//   candidates: [{ id, scalar, role }]  (champion first; challengers after)
//   style: 'gauntlet' | 'single_elim' | 'double_elim' | 'swiss' | 'racing'
// --------------------------------------------------------------------
export const STYLES = [
  { key: 'gauntlet', label: 'Gauntlet', real: true, note: 'how zicato actually ran this epoch — king of the hill, paired board.' },
  { key: 'single_elim', label: 'Single-elim', real: false, note: 'conceptual — a bracket tree; SELECTION.md §2③ calls this the wrong primitive (noise-fragile).' },
  { key: 'double_elim', label: 'Double-elim', real: false, note: 'conceptual — a losers’ bracket buys a second life; §6: replication dominates bracket position.' },
  { key: 'swiss', label: 'Swiss', real: false, note: 'conceptual — fixed-round pairing by running score; §6: superseded by iterated racing.' },
  { key: 'racing', label: 'Racing', real: false, note: 'conceptual — race lanes with an elimination cut; §5: the convergent recommendation (replicate survivors).' },
];

export function styleSwitcher(active, onPick) {
  const row = el('div', { class: 'mcA-stylesw', role: 'tablist', 'aria-label': 'tournament style' });
  for (const s of STYLES) {
    const btn = el('button', {
      class: 'mcA-stylesw-btn' + (s.key === active ? ' is-active' : '') + (s.real ? ' is-real' : ''),
      type: 'button',
      role: 'tab',
      'aria-selected': s.key === active ? 'true' : 'false',
    }, [
      s.real ? el('span', { class: 'mcA-stylesw-realdot' }) : null,
      s.label,
    ]);
    if (onPick) btn.addEventListener('click', () => onPick(s.key));
    row.appendChild(btn);
  }
  return row;
}

// dispatch to the right topology builder.
export function styleView(style, candidates) {
  candidates = Array.isArray(candidates) ? candidates : [];
  const meta = STYLES.find((s) => s.key === style) || STYLES[0];
  const banner = el('div', {
    class: 'mcA-style-banner' + (meta.real ? ' is-real' : ' is-concept'),
  }, [
    el('span', { class: 'mcA-style-banner-tag mono' }, [meta.real ? 'REAL' : 'CONCEPTUAL']),
    el('span', null, [meta.note]),
  ]);
  let viz;
  switch (style) {
    case 'single_elim': viz = singleElim(candidates); break;
    case 'double_elim': viz = doubleElim(candidates); break;
    case 'swiss': viz = swiss(candidates); break;
    case 'racing': viz = racing(candidates); break;
    default: viz = empty('Select a style.');
  }
  return el('div', { class: 'mcA-style' }, [banner, viz]);
}

// names for the conceptual brackets (use the candidate ids)
function ids(cands) { return cands.map((c) => c.id || '?'); }
function scalarOf(c) { return typeof c.scalar === 'number' && isFinite(c.scalar) ? c.scalar : null; }
function betterOf(a, b) {
  // lower scalar (loss) wins; unknown loses to known; tie → a
  const sa = scalarOf(a), sb = scalarOf(b);
  if (sa == null && sb == null) return a;
  if (sa == null) return b;
  if (sb == null) return a;
  return sa <= sb ? a : b;
}

// ---- single-elim: a left→right bracket TREE (SVG) -------------------
function singleElim(cands) {
  if (cands.length < 2) return empty('Need at least two candidates for a bracket.');
  // pair them up; winners advance. One round is enough for 2–3.
  const ROW_H = 44, COL_W = 150, PAD = 16, R = 13;
  const pairs = [];
  for (let i = 0; i < cands.length; i += 2) pairs.push([cands[i], cands[i + 1] || null]);
  const rounds = [cands.slice()];
  // build winner column(s)
  let cur = cands.slice();
  while (cur.length > 1) {
    const next = [];
    for (let i = 0; i < cur.length; i += 2) {
      const a = cur[i], b = cur[i + 1];
      next.push(b ? betterOf(a, b) : a);
    }
    rounds.push(next);
    cur = next;
  }
  const cols = rounds.length;
  const maxRows = rounds[0].length;
  const W = PAD * 2 + (cols - 1) * COL_W + 120;
  const H = PAD * 2 + maxRows * ROW_H;
  const svg = svgEl('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}`, class: 'mcA-bracket-svg', role: 'img', 'aria-label': 'single-elimination bracket' });
  const yOf = (col, idx) => {
    const span = Math.pow(2, col);
    return PAD + (idx * span + (span - 1) / 2) * ROW_H + ROW_H / 2;
  };
  // connectors
  for (let c = 0; c < cols - 1; c++) {
    const items = rounds[c];
    for (let i = 0; i < items.length; i += 2) {
      const x0 = PAD + c * COL_W + R;
      const x1 = PAD + (c + 1) * COL_W - R;
      const y0 = yOf(c, i), y1 = yOf(c, i + 1) || y0, ym = yOf(c + 1, i / 2);
      svg.appendChild(svgEl('path', {
        d: `M ${x0} ${y0} H ${(x0 + x1) / 2} V ${ym} H ${x1}` + (items[i + 1] ? ` M ${x0} ${y1} H ${(x0 + x1) / 2}` : ''),
        fill: 'none', stroke: 'var(--mc-line-2)', 'stroke-width': '1.4',
      }));
    }
  }
  // nodes
  rounds.forEach((items, c) => {
    items.forEach((cand, i) => {
      bracketNode(svg, PAD + c * COL_W, yOf(c, i), cand, R, c === cols - 1 ? 'go' : 'idle');
    });
  });
  return el('div', { class: 'mcA-bracket' }, [svg]);
}

function bracketNode(svg, x, y, cand, R, light) {
  const stroke = light === 'go' ? 'var(--mc-go)' : light === 'stop' ? 'var(--mc-stop)' : 'var(--mc-idle)';
  svg.appendChild(svgEl('circle', { cx: x, cy: y, r: R, fill: 'var(--mc-bg-2)', stroke, 'stroke-width': '2' }));
  const t = svgEl('text', { x: x + R + 8, y: y + 4, fill: 'var(--mc-text)', 'font-size': '12', 'font-family': 'var(--mc-mono)' });
  t.textContent = (cand && cand.id) || '?';
  svg.appendChild(t);
  if (cand && typeof cand.scalar === 'number') {
    const s = svgEl('text', { x: x + R + 8, y: y + 17, fill: 'var(--mc-text-3)', 'font-size': '10', 'font-family': 'var(--mc-mono)' });
    s.textContent = cand.scalar.toFixed(2);
    svg.appendChild(s);
  }
}

// ---- double-elim: winners' rail + losers' bracket below -------------
function doubleElim(cands) {
  if (cands.length < 2) return empty('Need at least two candidates for a bracket.');
  const winner = cands.reduce((best, c) => betterOf(best, c));
  const losers = cands.filter((c) => c !== winner);
  const railRow = (label, items, light) => el('div', { class: 'mcA-de-rail' }, [
    el('div', { class: 'mcA-de-rail-label mono' }, [label]),
    el('div', { class: 'mcA-de-rail-nodes' }, items.map((c) => el('span', {
      class: 'mcA-de-node mono', 'data-light': light,
    }, [
      el('b', null, [(c && c.id) || '?']),
      typeof c.scalar === 'number' ? el('span', { class: 'mcA-de-node-s' }, [c.scalar.toFixed(2)]) : null,
    ]))),
  ]);
  return el('div', { class: 'mcA-de' }, [
    railRow('winners', [winner], 'go'),
    el('div', { class: 'mcA-de-arrow mono' }, ['↓ a single loss drops you here — second life']),
    railRow('losers', losers.length ? losers : [{ id: '—' }], 'stop'),
  ]);
}

// ---- swiss: a pairing table with running scores ---------------------
function swiss(cands) {
  if (cands.length < 2) return empty('Need at least two candidates for Swiss pairing.');
  // one notional round: pair adjacent by seed (scalar), score 1 to the better.
  const seeded = cands.slice().sort((a, b) => {
    const sa = scalarOf(a), sb = scalarOf(b);
    if (sa == null) return 1; if (sb == null) return -1; return sa - sb;
  });
  const tbl = el('table', { class: 'mcA-table mcA-swiss' });
  tbl.appendChild(el('thead', null, [el('tr', null, [
    el('th', null, ['pairing']), el('th', null, ['scalars']), el('th', null, ['result']), el('th', null, ['score']),
  ])]));
  const tb = el('tbody');
  for (let i = 0; i < seeded.length; i += 2) {
    const a = seeded[i], b = seeded[i + 1];
    if (!b) {
      tb.appendChild(el('tr', null, [
        el('td', { class: 'mono' }, [(a.id || '?') + ' (bye)']),
        el('td', { class: 'mono' }, [fmt(scalarOf(a))]),
        el('td', null, ['bye']),
        el('td', { class: 'mono' }, ['1.0']),
      ]));
      continue;
    }
    const w = betterOf(a, b);
    tb.appendChild(el('tr', null, [
      el('td', { class: 'mono' }, [a.id + ' vs ' + b.id]),
      el('td', { class: 'mono' }, [fmt(scalarOf(a)) + ' / ' + fmt(scalarOf(b))]),
      el('td', null, [el('span', { class: 'mcA-tag-good mono' }, [(w.id || '?') + ' wins'])]),
      el('td', { class: 'mono' }, [w === a ? '1.0 / 0.0' : '0.0 / 1.0']),
    ]));
  }
  tbl.appendChild(tb);
  return el('div', { class: 'mcA-swiss-wrap' }, [tbl]);
}

// ---- racing: race lanes + an elimination cut-line -------------------
function racing(cands) {
  if (!cands.length) return empty('No candidates to race.');
  const scalars = cands.map(scalarOf).filter((v) => v != null);
  const min = scalars.length ? Math.min(...scalars) : 0;
  const max = scalars.length ? Math.max(...scalars) : 1;
  const span = (max - min) || 1;
  // cut-line: median scalar — lanes past it are eliminated (conceptually).
  const sorted = scalars.slice().sort((a, b) => a - b);
  const cut = sorted.length ? sorted[Math.floor(sorted.length / 2)] : null;
  const lanes = el('div', { class: 'mcA-race' });
  for (const c of cands) {
    const s = scalarOf(c);
    // progress = how far toward the BEST (lowest loss); leader is fullest.
    const prog = s == null ? 0 : 1 - (s - min) / span; // 1 = best
    const eliminated = cut != null && s != null && s > cut;
    lanes.appendChild(el('div', { class: 'mcA-race-lane' + (eliminated ? ' is-out' : '') }, [
      el('span', { class: 'mcA-race-name mono' }, [(c.id || '?')]),
      el('div', { class: 'mcA-race-track' }, [
        el('div', {
          class: 'mcA-race-car' + (eliminated ? ' is-out' : (prog >= 0.999 ? ' is-leader' : '')),
          style: `left:${(prog * 100).toFixed(1)}%`,
        }, ['▸']),
      ]),
      el('span', { class: 'mcA-race-s mono' }, [s == null ? '—' : s.toFixed(2)]),
    ]));
  }
  const board = el('div', { class: 'mcA-race-wrap' }, [lanes]);
  if (cut != null) {
    board.appendChild(el('div', { class: 'mcA-race-cut mono' }, [
      '╴╴ elimination cut-line @ ' + cut.toFixed(2) + ' — lanes past the cut are dropped; survivors keep racing (replicate)',
    ]));
  }
  return board;
}
