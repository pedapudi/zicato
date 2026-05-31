// variants/B/views/tournament.js — the Lineage + Fixtures view.
//
// Two movements:
//
//   The climb. The lineage as an elegant, non-colliding trajectory: the
//   promoted spine is a bold through-line, rejected challengers branch faintly
//   off their parent, the live node pulses. Every node clickable → its
//   experiment. A quiet legend + a chronological roll of verdicts as prose.
//
//   Theme 4 — match-ups across tournament styles. The REAL king-of-the-hill
//   gauntlet zicato runs (one champion, each challenger mounted in turn) drawn
//   as a head-to-head ladder, with the heart of a single round — the paired
//   per-board matchup grid — as a beautifully set table-figure. Then a set of
//   ILLUSTRATIVE alternative structures over the SAME generation set, each a
//   DIFFERENT diagram with an explanatory caption drawn from SELECTION.md and
//   clearly marked "not how this epoch ran": a single-elimination bracket
//   engraving, a coupled double-elimination figure, a round-robin matrix, a
//   Swiss pairing ledger, and a racing / successive-halving lane chart.
//
// Data: state.lineage, state.bracket (/api/tournaments),
// /api/matchup-grid/{e}/{champ}/{chall}.

import { el, clearChildren } from '../../../core/dom.js';
import { bRouter } from '../router.js';
import { registerBView } from '../shell.js';
import { lineageNodes, gauntlet, fixtureField, makeCache } from '../lib/data.js';
import { section, note, verdictBadge } from '../lib/prose.js';
import { trajectoryStory, fmtNum, fin, headToHead } from '../lib/charts.js';
import {
  gauntletFixture, matchupGridFigure, bracketFixture, doubleElimFixture,
  roundRobinFixture, swissFixture, raceFixture,
} from '../lib/fixtures.js';

let _gridCache = null;
function repaint() {
  const host = document.getElementById('vb-page');
  if (host && bRouter.current().view === 'tournament') renderTournament(host, bRouter.current());
}
function caches() { if (!_gridCache) _gridCache = makeCache(repaint); return _gridCache; }
export function resetTournamentView() { _gridCache = null; }

// The illustrative fixtures, each with a SELECTION.md-grounded caption.
function alternativeFixtures(field) {
  const blocks = [];

  blocks.push(el('div', { class: 'vb-altfix' }, [
    el('h3', { class: 'vb-altfix-name' }, ['Single-elimination bracket']),
    bracketFixture(field, {
      caption: 'Seed the field into a tree; the lower-loss candidate advances each round. '
        + 'Cheap triage for a large field — but noise-fragile at the boundary (SELECTION.md §2 Family ③): '
        + 'a strong candidate can die to one unlucky pairing.',
    }),
  ]));

  blocks.push(el('div', { class: 'vb-altfix' }, [
    el('h3', { class: 'vb-altfix-name' }, ['Double-elimination']),
    doubleElimFixture(field, {
      caption: 'A losers’ bracket buys a variance victim a second life. Its one benefit — robustness '
        + 'to a single bad match — is delivered more directly and cheaply by replication (SELECTION.md §6); '
        + 'not recommended for zicato’s few, expensive, noisy candidates.',
    }),
  ]));

  blocks.push(el('div', { class: 'vb-altfix' }, [
    el('h3', { class: 'vb-altfix-name' }, ['Round-robin']),
    roundRobinFixture(field, {
      caption: 'Every candidate plays every other; a full ranking with no single-loss death. '
        + 'The right instinct, but full round-robin is the most expensive point on the curve '
        + '(SELECTION.md §5) — many comparisons spent even where the ranking is already certain.',
    }),
  ]));

  blocks.push(el('div', { class: 'vb-altfix' }, [
    el('h3', { class: 'vb-altfix-name' }, ['Swiss pairing']),
    swissFixture(field, {
      caption: 'Fixed rounds; each round pairs candidates on equal running scores. A full ranking '
        + 'without elimination fragility — but iterated racing supersedes it (SELECTION.md §6): '
        + 'Swiss made adaptive, spending comparisons only where the ranking is still uncertain.',
    }),
  ]));

  blocks.push(el('div', { class: 'vb-altfix' }, [
    el('h3', { class: 'vb-altfix-name' }, ['Racing / successive-halving']),
    raceFixture(field, {
      caption: 'Give everyone a little budget, cut the worst fraction, give survivors more, repeat — '
        + 'resources concentrate on what looks promising (SELECTION.md §2 Family ③, §5). '
        + 'The convergent recommendation pairs this with replication so the cuts are noise-aware.',
    }),
  ]));

  return blocks;
}

export function renderTournament(host, route) {
  if (!host) return;
  clearChildren(host);
  const nodes = lineageNodes();
  const focus = route && route.params && route.params.generationId;
  const g = gauntlet();
  const c = caches();

  host.appendChild(el('div', { class: 'vb-tourn-lead' }, [
    el('p', { class: 'vb-eyebrow' }, ['The climb']),
    el('h1', { class: 'vb-page-title' }, ['The lineage, and the match-ups']),
    el('p', { class: 'vb-env-dek' }, [
      'Every generation as a point on the optimization curve, the bold line the promoted champion spine. '
      + 'Below, the gauntlet zicato actually ran — and how the same field would arrange under other policies.',
    ]),
  ]));

  if (!nodes.length) {
    host.appendChild(note('empty', {
      label: 'No generations yet',
      detail: 'The lineage begins with the first run of the first epoch.',
    }));
    return;
  }

  // --- The climb (trajectory + roll) ---
  host.appendChild(section('Trajectory', [
    trajectoryStory(nodes, {
      height: 280, onSelect: (id) => bRouter.go('experiment', id), ariaLabel: 'lineage slopegraph',
    }),
    el('div', { class: 'vb-legend' }, [
      el('span', { class: 'vb-legend-item' }, [el('span', { class: 'vb-legend-dot vb-improve' }), 'promoted']),
      el('span', { class: 'vb-legend-item' }, [el('span', { class: 'vb-legend-dot vb-regress' }), 'rejected']),
      el('span', { class: 'vb-legend-item' }, [el('span', { class: 'vb-legend-dot vb-neutral' }), 'open']),
      el('span', { class: 'vb-legend-item' }, [el('span', { class: 'vb-legend-line vb-spine' }), 'champion spine']),
    ]),
  ], { sub: 'Lower is better. Click any node to open its experiment.' }));

  const roll = nodes.slice().map((n) => {
    const verdict = n.verdict === 'promoted' ? 'promoted' : n.verdict === 'rejected' ? 'rejected'
      : n.live ? 'running' : n.verdict === 'deferred' ? 'deferred' : 'open';
    return el('div', {
      class: 'vb-roll-row vb-clickable' + (focus && n.id === String(focus) ? ' vb-roll-focus' : ''),
      role: 'button', tabindex: '0', 'aria-label': `generation ${n.id}`,
      onclick: () => bRouter.go('experiment', n.id),
      onkeydown: (ev) => { if (ev && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); bRouter.go('experiment', n.id); } },
    }, [
      el('span', { class: 'vb-mono vb-roll-gen' }, [String(n.label || n.id)]),
      verdictBadge(verdict),
      el('span', { class: 'vb-roll-scalar vb-mono' }, [fin(n.scalar) ? fmtNum(n.scalar, 3) : '—']),
      n.parentId ? el('span', { class: 'vb-roll-parent vb-muted' }, ['from ' + n.parentId]) : null,
    ].filter(Boolean));
  });
  host.appendChild(section('Verdicts', [el('div', { class: 'vb-roll' }, roll)], {
    sub: 'Each generation and how it resolved.',
  }));

  // --- Theme 4: the REAL gauntlet ---
  host.appendChild(section('The gauntlet', [
    gauntletFixture(g.champion, g.rounds, {
      onSelect: (id) => bRouter.go('experiment', id),
      caption: [
        'How this epoch actually ran: a ', el('strong', null, ['king-of-the-hill gauntlet']),
        ' — one reigning champion, each challenger mounted against it in turn, scored on the same board '
        + '(common random numbers). ', el('strong', null, ['Real data.']),
      ],
    }),
  ], { sub: 'The single shipped mechanism (SELECTION.md §3). Click a challenger to read its experiment.' }));

  // --- Theme 4: the heart of one round — the paired matchup grid ---
  if (g.rounds.length && g.epochId) {
    // Default to the most recent decided round.
    const round = g.rounds.slice().reverse().find((r) => r.champion && r.challenger) || g.rounds[g.rounds.length - 1];
    if (round && round.champion && round.challenger) {
      const key = `${round.champion}->${round.challenger}`;
      c.ensure(key, '/api/matchup-grid/' + [g.epochId, round.champion, round.challenger].map(encodeURIComponent).join('/'));
      const grid = c.get(key);
      let body;
      if (grid === undefined) {
        body = note('running', { label: 'Reading the paired per-board grid' });
      } else if (grid && !grid.__broken) {
        if (!grid.champion) grid.champion = round.champion;
        if (!grid.challenger) grid.challenger = round.challenger;
        body = matchupGridFigure(grid, headToHead, {
          onSelect: (entryId) => bRouter.go('run', entryId, round.challenger),
          caption: [
            'One round, board by board: ', el('span', { class: 'vb-mono' }, [String(round.champion)]),
            ' vs ', el('span', { class: 'vb-mono' }, [String(round.challenger)]),
            '. Paired losses (lower is better) with the per-entry winner. ',
            el('strong', null, ['Real data.']), ' Click a row to read that run.',
          ],
        });
      } else {
        body = note('broken', { reason: 'matchup grid unavailable for this round' });
      }
      host.appendChild(section('Inside a round', [body], {
        sub: 'The paired (common-random-number) per-board duel — the heart of a single gauntlet round.',
      }));
    }
  }

  // --- Theme 4: the illustrative alternatives ---
  const field = fixtureField();
  host.appendChild(section('Other structures', [
    el('p', { class: 'vb-fig-lead' }, [
      'The same generations, arranged under the selection policies zicato considered and rejected '
      + '(SELECTION.md §2, §5, §6). Each is a different diagram — and a conceptual overlay, ',
      el('em', null, ['not']), ' how this epoch ran.',
    ]),
    el('div', { class: 'vb-altfix-grid' }, alternativeFixtures(field)),
  ], { sub: 'Why the gauntlet — and what the alternatives would have looked like.' }));
}

registerBView('tournament', renderTournament);
