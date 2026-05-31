// variants/B/lib/fixtures.js — the tournament-style "fixture" figures.
//
// Theme 4 of the enrichment: the actual king-of-the-hill gauntlet zicato
// runs (real per-round data), plus a set of ILLUSTRATIVE alternative
// tournament structures laid over the SAME generation set — each drawn with
// a DIFFERENT diagram topology so the operator can read, at a glance, how the
// match-ups would arrange under another selection policy (SELECTION.md §2,
// §5, §6). These are conceptual overlays, never how the epoch actually ran;
// the views label them as such.
//
// Pure SVG/DOM factories, total (a degenerate field yields a labeled
// fallback, never a blank box or a throw). Editorial idiom: thin engraving
// lines, small-caps labels, hung captions. Color stays semantic (improve /
// regress / neutral) and is always redundant to a glyph or a label.

import { svgEl, el } from '../../../core/dom.js';
import { fin } from './charts.js';

// A field entry the fixtures consume: { id, label?, verdict?, loss? }.
// verdict ∈ promoted | rejected | open. loss lower-is-better.
function clean(field) {
  return (Array.isArray(field) ? field : []).filter((c) => c && c.id != null).map((c) => ({
    id: String(c.id), label: String(c.label != null ? c.label : c.id),
    verdict: c.verdict || 'open', loss: fin(c.loss) ? c.loss : null,
  }));
}
function toneOf(v) { return v === 'promoted' ? 'improve' : v === 'rejected' ? 'regress' : 'neutral'; }
function emptyFig(cls, label) {
  return el('figure', { class: 'vb-fixture ' + cls }, [
    el('figcaption', { class: 'vb-fig-empty' }, [label || 'No candidates to arrange.']),
  ]);
}

// ---------------------------------------------------------------------------
// gauntletFixture — the REAL structure: one reigning champion, each
// challenger mounted in turn, paired per board. Drawn as a head-to-head
// "ladder" engraving: the champion is a fixed spine on the left, each
// challenger hangs to the right with its verdict and Δscalar. This is the one
// fixture backed by real per-round data, so it carries no "illustrative" mark.
//
// `champion` is the reigning generation id; `rounds` is an array of
// { challenger, decision, deltaScalar, reason }. onSelect(id) drills in.
// ---------------------------------------------------------------------------
export function gauntletFixture(champion, rounds, opts = {}) {
  const list = (Array.isArray(rounds) ? rounds : []).filter((r) => r && r.challenger != null);
  if (!champion || list.length === 0) {
    return emptyFig('vb-fixture-gauntlet', 'No gauntlet rounds recorded for this epoch yet.');
  }
  const fig = el('figure', { class: 'vb-fixture vb-fixture-gauntlet' });
  const ladder = el('div', { class: 'vb-gauntlet-ladder' });
  // The champion spine.
  ladder.appendChild(el('div', { class: 'vb-gauntlet-champ' }, [
    el('span', { class: 'vb-gauntlet-crown', 'aria-hidden': 'true' }, ['♔']),
    el('span', { class: 'vb-gauntlet-champ-id vb-mono' }, [String(champion)]),
    el('span', { class: 'vb-gauntlet-champ-role' }, ['reigning champion']),
  ]));
  const rungs = el('ol', { class: 'vb-gauntlet-rungs' });
  list.forEach((r, i) => {
    const decision = String(r.decision || '').toLowerCase();
    const tone = decision.includes('promot') ? 'improve' : decision.includes('reject') ? 'regress' : 'neutral';
    const glyph = tone === 'improve' ? '✓' : tone === 'regress' ? '✗' : '○';
    const rung = el('li', {
      class: 'vb-gauntlet-rung vb-clickable', role: 'button', tabindex: '0',
      'aria-label': `round ${i + 1}: challenger ${r.challenger}`,
    }, [
      el('span', { class: 'vb-gauntlet-rung-no' }, [`R${i + 1}`]),
      el('span', { class: 'vb-gauntlet-connector', 'aria-hidden': 'true' }, ['┈┈┈⚔┈┈┈']),
      el('span', { class: `vb-gauntlet-chall vb-${tone}` }, [
        el('span', { class: 'vb-gauntlet-glyph', 'aria-hidden': 'true' }, [glyph]),
        el('span', { class: 'vb-mono' }, [String(r.challenger)]),
      ]),
      fin(r.deltaScalar)
        ? el('span', { class: `vb-gauntlet-delta vb-${r.deltaScalar < 0 ? 'improve' : r.deltaScalar > 0 ? 'regress' : 'neutral'}` }, [
            (r.deltaScalar >= 0 ? '+' : '') + r.deltaScalar.toFixed(2),
          ])
        : null,
    ].filter(Boolean));
    if (opts.onSelect) {
      const fire = () => opts.onSelect(String(r.challenger));
      rung.addEventListener('click', fire);
      rung.addEventListener('keydown', (ev) => { if (ev && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); fire(); } });
    }
    rungs.appendChild(rung);
  });
  ladder.appendChild(rungs);
  fig.appendChild(ladder);
  if (opts.caption) fig.appendChild(el('figcaption', { class: 'vb-fig-cap' }, Array.isArray(opts.caption) ? opts.caption : [opts.caption]));
  return fig;
}

// ---------------------------------------------------------------------------
// matchupGridFigure — the heart of ONE gauntlet round: the paired
// (common-random-number) per-board duel, champion vs challenger, as a beautiful
// set typeset table-figure. Each row: entry, champion loss bar, challenger
// loss bar, Δ, won_by. Real data. `entryHeadToHead(champLoss, challLoss, opts)`
// is injected so the figure can reuse the charts toolkit without a cycle.
// ---------------------------------------------------------------------------
export function matchupGridFigure(grid, headToHead, opts = {}) {
  const rows = (grid && Array.isArray(grid.entry_grid)) ? grid.entry_grid : [];
  const champId = grid && grid.champion;
  const challId = grid && grid.challenger;
  if (rows.length === 0) {
    return emptyFig('vb-fixture-matchup', 'No paired per-board results for this round.');
  }
  const fig = el('figure', { class: 'vb-fixture vb-fixture-matchup' });
  const head = el('div', { class: 'vb-matchup-head' }, [
    el('span', { class: 'vb-matchup-col-entry' }, ['board entry']),
    el('span', { class: 'vb-matchup-col-bars' }, [
      el('span', { class: 'vb-mono' }, [String(champId || 'champion')]),
      el('span', { class: 'vb-matchup-vs' }, ['vs']),
      el('span', { class: 'vb-mono' }, [String(challId || 'challenger')]),
    ]),
    el('span', { class: 'vb-matchup-col-won' }, ['won by']),
  ]);
  const body = el('div', { class: 'vb-matchup-rows' }, rows.map((r) => {
    const verdict = String(r.verdict || '').toLowerCase();
    const tone = verdict === 'improved' ? 'improve' : verdict === 'regressed' ? 'regress' : 'neutral';
    const wonBy = r.won_by != null ? String(r.won_by) : null;
    const wonLabel = wonBy == null ? 'tie' : wonBy;
    const row = el('div', {
      class: 'vb-matchup-row' + (opts.onSelect ? ' vb-clickable' : ''),
      role: opts.onSelect ? 'button' : null, tabindex: opts.onSelect ? '0' : null,
      'aria-label': `entry ${r.entry_id}`,
    }, [
      el('span', { class: 'vb-matchup-col-entry vb-mono' }, [String(r.entry_id)]),
      el('span', { class: 'vb-matchup-col-bars' }, [
        headToHead(r.parent_drift_loss, r.child_drift_loss, { champId, challId, wonBy }),
        el('span', { class: `vb-matchup-delta vb-${tone}` }, [
          fin(r.delta) ? (r.delta >= 0 ? '+' : '') + r.delta.toFixed(1) : '—',
        ]),
      ]),
      el('span', { class: `vb-matchup-col-won vb-${tone}` }, [
        el('span', { class: 'vb-matchup-won-glyph', 'aria-hidden': 'true' }, [
          wonBy == null ? '=' : wonBy === String(challId) ? '↑' : '↓',
        ]),
        el('span', { class: 'vb-mono' }, [wonLabel]),
      ]),
    ]);
    if (opts.onSelect && r.entry_id != null) {
      const fire = () => opts.onSelect(String(r.entry_id));
      row.addEventListener('click', fire);
      row.addEventListener('keydown', (ev) => { if (ev && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); fire(); } });
    }
    return row;
  }));
  fig.appendChild(head);
  fig.appendChild(body);
  if (opts.caption) fig.appendChild(el('figcaption', { class: 'vb-fig-cap' }, Array.isArray(opts.caption) ? opts.caption : [opts.caption]));
  return fig;
}

// ---------------------------------------------------------------------------
// bracketFixture — ILLUSTRATIVE single-elimination engraving. The field is
// seeded into a tree; winners (lower loss) advance up the bracket. A bracket
// tree topology — pairs join at vertical "elbows". Drawn as an SVG engraving.
// ---------------------------------------------------------------------------
export function bracketFixture(field, opts = {}) {
  const cand = clean(field);
  if (cand.length === 0) return emptyFig('vb-fixture-bracket', 'No field to seed into a bracket.');
  // Pad up to the next power of two with byes so the tree is balanced.
  let size = 1; while (size < cand.length) size *= 2;
  const seeds = cand.slice();
  while (seeds.length < size) seeds.push(null); // bye
  const rounds = Math.log2(size);
  const width = opts.width || 600;
  const laneH = 30;
  const height = 24 + size * laneH;
  const svg = svgEl('svg', {
    class: 'vb-bracket-svg', width: '100%', height, viewBox: `0 0 ${width} ${height}`,
    role: 'img', 'aria-label': opts.ariaLabel || 'single-elimination bracket (illustrative)',
  });
  const colW = (width - 40) / (rounds + 1);
  // Round 0 — the leaves.
  let layer = seeds.map((c, i) => ({ c, y: 24 + i * laneH + laneH / 2 }));
  for (let round = 0; round <= rounds; round++) {
    const x = 20 + round * colW;
    layer.forEach((slot) => {
      const c = slot.c;
      svg.appendChild(svgEl('text', {
        x: x + 6, y: slot.y - 5, 'text-anchor': 'start',
        class: 'vb-bracket-id vb-mono vb-' + (c ? toneOf(c.verdict) : 'neutral'),
      }, [c ? c.label : 'bye']));
      svg.appendChild(svgEl('line', {
        x1: x, y1: slot.y, x2: x + colW * 0.7, y2: slot.y, class: 'vb-bracket-line',
      }));
    });
    if (round === rounds) break;
    // Join pairs into the next layer; the winner is the lower-loss candidate.
    const next = [];
    for (let i = 0; i < layer.length; i += 2) {
      const a = layer[i]; const b = layer[i + 1];
      const jx = x + colW * 0.7;
      const midY = (a.y + b.y) / 2;
      svg.appendChild(svgEl('path', {
        d: `M${jx} ${a.y} V${b.y}`, class: 'vb-bracket-elbow', fill: 'none',
      }));
      const winner = pickWinner(a.c, b.c);
      next.push({ c: winner, y: midY });
    }
    layer = next;
  }
  const fig = el('figure', { class: 'vb-fixture vb-fixture-bracket' }, [
    illustrativeTag(), svg,
  ]);
  if (opts.caption) fig.appendChild(el('figcaption', { class: 'vb-fig-cap' }, Array.isArray(opts.caption) ? opts.caption : [opts.caption]));
  return fig;
}
function pickWinner(a, b) {
  if (!a) return b; if (!b) return a;
  if (a.loss == null && b.loss == null) return a;
  if (a.loss == null) return b; if (b.loss == null) return a;
  return a.loss <= b.loss ? a : b;
}

// ---------------------------------------------------------------------------
// doubleElimFixture — ILLUSTRATIVE coupled winners'/losers' figure. Two
// engraved lanes: a winners' bracket on top, a losers' bracket beneath, with a
// "drops to losers" connector. A DIFFERENT topology from the single bracket:
// two parallel rails with a crossing arrow.
// ---------------------------------------------------------------------------
export function doubleElimFixture(field, opts = {}) {
  const cand = clean(field);
  if (cand.length === 0) return emptyFig('vb-fixture-double', 'No field for a double-elimination figure.');
  const winners = cand.slice();
  const sorted = cand.slice().sort((a, b) => (a.loss == null ? 1e9 : a.loss) - (b.loss == null ? 1e9 : b.loss));
  const losers = sorted.slice(1); // everyone but the leader gets a second life
  const rail = (title, members, hint) => el('div', { class: 'vb-double-rail' }, [
    el('p', { class: 'vb-double-rail-title' }, [title, hint ? el('span', { class: 'vb-double-rail-hint vb-muted' }, [' ' + hint]) : null].filter(Boolean)),
    el('div', { class: 'vb-double-track' }, members.length
      ? members.map((c) => el('span', { class: `vb-double-node vb-${toneOf(c.verdict)}` }, [
          el('span', { class: 'vb-mono' }, [c.label]),
        ]))
      : [el('span', { class: 'vb-muted' }, ['—'])]),
  ]);
  const fig = el('figure', { class: 'vb-fixture vb-fixture-double' }, [
    illustrativeTag(),
    rail("Winners' bracket", winners, '(every candidate enters)'),
    el('div', { class: 'vb-double-drop', 'aria-hidden': 'true' }, ['↓ a single loss drops a candidate, not eliminates it ↓']),
    rail("Losers' bracket", losers, '(a second life — the variance-victim path)'),
  ]);
  if (opts.caption) fig.appendChild(el('figcaption', { class: 'vb-fig-cap' }, Array.isArray(opts.caption) ? opts.caption : [opts.caption]));
  return fig;
}

// ---------------------------------------------------------------------------
// roundRobinFixture — ILLUSTRATIVE round-robin matrix: every candidate plays
// every other. A square grid-matrix topology with the winner of each cell
// marked. The diagonal is hatched (no self-game).
// ---------------------------------------------------------------------------
export function roundRobinFixture(field, opts = {}) {
  const cand = clean(field);
  if (cand.length === 0) return emptyFig('vb-fixture-rr', 'No field for a round-robin matrix.');
  const n = cand.length;
  const fig = el('figure', { class: 'vb-fixture vb-fixture-rr' }, [illustrativeTag()]);
  const grid = el('div', { class: 'vb-rr-grid', style: `grid-template-columns: 4.5rem repeat(${n}, 1fr);` });
  // Header row.
  grid.appendChild(el('span', { class: 'vb-rr-corner', 'aria-hidden': 'true' }));
  for (const c of cand) grid.appendChild(el('span', { class: 'vb-rr-colhead vb-mono' }, [c.label]));
  // Body rows.
  for (let i = 0; i < n; i++) {
    grid.appendChild(el('span', { class: 'vb-rr-rowhead vb-mono' }, [cand[i].label]));
    for (let j = 0; j < n; j++) {
      if (i === j) { grid.appendChild(el('span', { class: 'vb-rr-cell vb-rr-diag', 'aria-hidden': 'true' }, ['·'])); continue; }
      const a = cand[i]; const b = cand[j];
      const winner = pickWinner(a, b);
      const rowWon = winner && winner.id === a.id;
      grid.appendChild(el('span', {
        class: `vb-rr-cell vb-${rowWon ? 'improve' : 'regress'}`,
        'aria-label': `${a.label} vs ${b.label}: ${rowWon ? a.label : b.label} wins`,
      }, [rowWon ? 'W' : 'L']));
    }
  }
  fig.appendChild(grid);
  if (opts.caption) fig.appendChild(el('figcaption', { class: 'vb-fig-cap' }, Array.isArray(opts.caption) ? opts.caption : [opts.caption]));
  return fig;
}

// ---------------------------------------------------------------------------
// swissFixture — ILLUSTRATIVE Swiss pairing ledger: a ruled accounting ledger
// of rounds, pairing nearest-ranked candidates each round, accumulating a
// running score. A ledger/table-as-prose topology (NOT the rejected ugly
// grid — a typeset accounting register with hung rules).
// ---------------------------------------------------------------------------
export function swissFixture(field, opts = {}) {
  const cand = clean(field);
  if (cand.length === 0) return emptyFig('vb-fixture-swiss', 'No field for a Swiss ledger.');
  // Rank by loss; pair adjacently each round; the lower-loss member scores.
  const ranked = cand.slice().sort((a, b) => (a.loss == null ? 1e9 : a.loss) - (b.loss == null ? 1e9 : b.loss));
  const score = new Map(ranked.map((c) => [c.id, 0]));
  const rounds = Math.max(1, Math.min(3, Math.ceil(Math.log2(cand.length || 1)) || 1));
  const ledger = el('div', { class: 'vb-swiss-ledger' });
  let order = ranked.slice();
  for (let r = 0; r < rounds; r++) {
    const roundBlock = el('div', { class: 'vb-swiss-round' }, [
      el('p', { class: 'vb-swiss-round-no' }, [`Round ${r + 1}`]),
    ]);
    const pairs = el('ul', { class: 'vb-swiss-pairs' });
    for (let i = 0; i < order.length; i += 2) {
      const a = order[i]; const b = order[i + 1];
      if (!b) {
        score.set(a.id, (score.get(a.id) || 0) + 1); // bye
        pairs.appendChild(el('li', { class: 'vb-swiss-pair' }, [
          el('span', { class: 'vb-mono' }, [a.label]),
          el('span', { class: 'vb-swiss-bye vb-muted' }, ['— bye (+1)']),
        ]));
        continue;
      }
      const winner = pickWinner(a, b);
      if (winner) score.set(winner.id, (score.get(winner.id) || 0) + 1);
      pairs.appendChild(el('li', { class: 'vb-swiss-pair' }, [
        el('span', { class: 'vb-mono vb-' + (winner && winner.id === a.id ? 'improve' : 'neutral') }, [a.label]),
        el('span', { class: 'vb-swiss-v', 'aria-hidden': 'true' }, ['v']),
        el('span', { class: 'vb-mono vb-' + (winner && winner.id === b.id ? 'improve' : 'neutral') }, [b.label]),
        el('span', { class: 'vb-swiss-pt vb-muted' }, [winner ? `${winner.label} +1` : 'draw']),
      ]));
    }
    roundBlock.appendChild(pairs);
    ledger.appendChild(roundBlock);
    // Re-pair by running score for the next round (Swiss: near scores meet).
    order = order.slice().sort((a, b) => (score.get(b.id) || 0) - (score.get(a.id) || 0));
  }
  // Standings tail.
  const standings = el('ol', { class: 'vb-swiss-standings' },
    ranked.slice().sort((a, b) => (score.get(b.id) || 0) - (score.get(a.id) || 0)).map((c) => el('li', { class: 'vb-swiss-standing' }, [
      el('span', { class: 'vb-mono' }, [c.label]),
      el('span', { class: 'vb-swiss-score vb-muted' }, [`${score.get(c.id) || 0} pt`]),
    ])));
  const fig = el('figure', { class: 'vb-fixture vb-fixture-swiss' }, [
    illustrativeTag(), ledger,
    el('p', { class: 'vb-swiss-standings-title' }, ['Final standings']),
    standings,
  ]);
  if (opts.caption) fig.appendChild(el('figcaption', { class: 'vb-fig-cap' }, Array.isArray(opts.caption) ? opts.caption : [opts.caption]));
  return fig;
}

// ---------------------------------------------------------------------------
// raceFixture — ILLUSTRATIVE racing / successive-halving lanes: each candidate
// runs in a horizontal lane, gets more "budget" (lane length) the longer it
// survives; the worst are cut at each rung (a dashed cut line). A race-lanes
// topology with elimination markers — distinct from every bracket/grid.
// ---------------------------------------------------------------------------
export function raceFixture(field, opts = {}) {
  const cand = clean(field);
  if (cand.length === 0) return emptyFig('vb-fixture-race', 'No field for a race chart.');
  // Rank by loss; survivors run further. Two cut rungs (halving).
  const ranked = cand.slice().sort((a, b) => (a.loss == null ? 1e9 : a.loss) - (b.loss == null ? 1e9 : b.loss));
  const n = ranked.length;
  const width = opts.width || 560;
  const laneH = 34;
  const height = 30 + n * laneH;
  const svg = svgEl('svg', {
    class: 'vb-race-svg', width: '100%', height, viewBox: `0 0 ${width} ${height}`,
    role: 'img', 'aria-label': opts.ariaLabel || 'racing / successive-halving lanes (illustrative)',
  });
  const x0 = 60; const xMax = width - 24;
  // Cut rungs at ~half and ~quarter survival.
  const rungs = [Math.ceil(n / 2), Math.ceil(n / 4)].filter((k) => k >= 1 && k < n);
  const rungX = (i) => x0 + ((i + 1) / (rungs.length + 1)) * (xMax - x0);
  rungs.forEach((survivors, i) => {
    const x = rungX(i);
    svg.appendChild(svgEl('line', { x1: x, y1: 18, x2: x, y2: height - 8, class: 'vb-race-cut' }));
    svg.appendChild(svgEl('text', { x, y: 12, 'text-anchor': 'middle', class: 'vb-race-cut-label' }, [`cut → ${survivors}`]));
  });
  ranked.forEach((c, idx) => {
    const y = 30 + idx * laneH + laneH / 2 - 6;
    // How far this lane runs: survives each rung whose survivor-count > its rank.
    let reach = xMax;
    for (let i = 0; i < rungs.length; i++) {
      if (idx >= rungs[i]) { reach = rungX(i); break; }
    }
    svg.appendChild(svgEl('text', { x: 8, y: y + 4, class: 'vb-race-id vb-mono vb-' + toneOf(c.verdict), 'text-anchor': 'start' }, [c.label]));
    svg.appendChild(svgEl('line', { x1: x0, y1: y, x2: reach, y2: y, class: 'vb-race-lane vb-' + toneOf(c.verdict) }));
    if (reach < xMax) {
      svg.appendChild(svgEl('text', { x: reach + 5, y: y + 4, class: 'vb-race-out', 'text-anchor': 'start' }, ['✗ cut']));
    } else {
      svg.appendChild(svgEl('circle', { cx: reach, cy: y, r: 4, class: 'vb-race-finish vb-' + toneOf(c.verdict) }));
    }
  });
  const fig = el('figure', { class: 'vb-fixture vb-fixture-race' }, [illustrativeTag(), svg]);
  if (opts.caption) fig.appendChild(el('figcaption', { class: 'vb-fig-cap' }, Array.isArray(opts.caption) ? opts.caption : [opts.caption]));
  return fig;
}

// A small, consistent "illustrative — not how this epoch ran" mark for every
// alternative fixture (the gauntlet + matchup grid omit it, being real).
export function illustrativeTag() {
  return el('p', { class: 'vb-illustrative' }, [
    el('span', { class: 'vb-illustrative-mark', 'aria-hidden': 'true' }, ['※']),
    'Illustrative — the same generations under a different selection policy, ',
    el('em', null, ['not']), ' how this epoch actually ran.',
  ]);
}
