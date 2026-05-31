// variants/B/views/tournament.js — the Lineage view.
//
// The lineage as an elegant, non-colliding slopegraph/trajectory of the
// whole climb: the promoted spine is a bold through-line, rejected
// challengers branch faintly off their parent, the live node pulses. Every
// node is clickable → its experiment. Beneath the figure, a quiet legend
// and a chronological roll of the verdicts (as prose, not a table).
//
// Data: state.lineage (folded by the env poll) + state.activeTournament.

import { el, clearChildren } from '../../../core/dom.js';
import { bRouter } from '../router.js';
import { registerBView } from '../shell.js';
import { lineageNodes } from '../lib/data.js';
import { section, note, verdictBadge } from '../lib/prose.js';
import { trajectoryStory, fmtNum, fin } from '../lib/charts.js';

export function renderTournament(host, route) {
  if (!host) return;
  clearChildren(host);
  const nodes = lineageNodes();
  const focus = route && route.params && route.params.generationId;

  host.appendChild(el('div', { class: 'vb-tourn-lead' }, [
    el('p', { class: 'vb-eyebrow' }, ['The climb']),
    el('h1', { class: 'vb-page-title' }, ['The lineage, end to end']),
    el('p', { class: 'vb-env-dek' }, [
      'Every generation as a point on the optimization curve. The bold line is the promoted '
      + 'champion spine; rejected challengers branch off where they were tried.',
    ]),
  ]));

  if (!nodes.length) {
    host.appendChild(note('empty', {
      label: 'No generations yet',
      detail: 'The lineage begins with the first run of the first epoch.',
    }));
    return;
  }

  host.appendChild(section('Trajectory', [
    trajectoryStory(nodes, {
      height: 280,
      onSelect: (id) => bRouter.go('experiment', id),
      ariaLabel: 'lineage slopegraph',
    }),
    el('div', { class: 'vb-legend' }, [
      el('span', { class: 'vb-legend-item' }, [el('span', { class: 'vb-legend-dot vb-improve' }), 'promoted']),
      el('span', { class: 'vb-legend-item' }, [el('span', { class: 'vb-legend-dot vb-regress' }), 'rejected']),
      el('span', { class: 'vb-legend-item' }, [el('span', { class: 'vb-legend-dot vb-neutral' }), 'open']),
      el('span', { class: 'vb-legend-item' }, [el('span', { class: 'vb-legend-line vb-spine' }), 'champion spine']),
    ]),
  ], { sub: 'Lower is better. Click any node to open its experiment.' }));

  // Chronological roll of verdicts, newest last, as quiet prose rows.
  const roll = nodes.slice().map((n) => {
    const verdict = n.verdict === 'promoted' ? 'promoted' : n.verdict === 'rejected' ? 'rejected'
      : n.live ? 'running' : n.verdict === 'deferred' ? 'deferred' : 'open';
    const row = el('div', {
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
    return row;
  });
  host.appendChild(section('Verdicts', [el('div', { class: 'vb-roll' }, roll)], {
    sub: 'Each generation and how it resolved.',
  }));
}

registerBView('tournament', renderTournament);
