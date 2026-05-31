// variants/B/views/environment.js — the Environment view (the home).
//
// The whole workspace told as a story, across epochs. The current UI loses
// the environment-as-a-whole; here it is the lead. The page opens with a
// quiet masthead line (the workspace identity + a one-glance health read),
// the full lineage as an elegant optimization curve, then each epoch as a
// numbered chapter — its objective rendered as a readable statement, a
// refined loss sparkline, and its outcome (promotions / generations) — laid
// out as a flowing editorial column, NOT a table.
//
// Data: /api/workspace (epoch rows + sparkline), /api/health-report
// (folded into state.healthReport), /api/active-tournament (state).

import { el, clearChildren } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import { bRouter } from '../router.js';
import { registerBView } from '../shell.js';
import { makeCache, lineageNodes, scalarOf, gens, genId, parentId, verdictKey } from '../lib/data.js';
import { section, note, stat, verdictBadge } from '../lib/prose.js';
import { trajectoryStory, sparkline, fin, fmtNum } from '../lib/charts.js';

let _wsCache = null;
function repaint() {
  const host = document.getElementById('vb-page');
  if (host && bRouter.current().view === 'environment') renderEnvironment(host, bRouter.current());
}
function caches() {
  if (!_wsCache) _wsCache = makeCache(repaint);
  return _wsCache;
}
export function resetEnvironmentView() { _wsCache = null; }

// Best (lowest) scalar per epoch, in epoch order, from the per-generation
// lineage when /api/workspace has not landed — so the page is never blank.
function epochsFromLineage() {
  const byEpoch = new Map();
  for (const g of gens()) {
    const ep = g.epoch_id || g.epochId;
    if (!ep) continue;
    const s = scalarOf(g);
    if (!byEpoch.has(ep)) byEpoch.set(ep, { epoch_id: ep, best_scalar: null, generation_count: 0, promoted_count: 0 });
    const row = byEpoch.get(ep);
    row.generation_count += 1;
    if (g.promoted === true || verdictKey(g.verdict || g.outcome) === 'promoted') row.promoted_count += 1;
    if (s != null && (row.best_scalar == null || s < row.best_scalar)) row.best_scalar = s;
  }
  return [...byEpoch.values()];
}

// Per-epoch loss sparkline points: the per-generation best scalars in id
// order, for a refined within-epoch trajectory.
function epochSparkPoints(epochId) {
  return gens()
    .filter((g) => (g.epoch_id || g.epochId) === epochId)
    .map((g) => scalarOf(g));
}

function healthLine() {
  const hr = state.healthReport;
  if (!hr) return note('running', { label: 'Reading loop health' });
  const findings = Array.isArray(hr.findings) ? hr.findings : [];
  if (hr.healthy && findings.length === 0) {
    return el('p', { class: 'vb-env-health vb-improve' }, [
      el('span', { class: 'vb-env-health-dot vb-improve', 'aria-hidden': 'true' }),
      'The loop is healthy — no findings on the latest round.',
    ]);
  }
  const tone = hr.healthy ? 'caution' : 'regress';
  return el('div', { class: 'vb-env-health vb-' + tone }, [
    el('p', null, [
      el('span', { class: `vb-env-health-dot vb-${tone}`, 'aria-hidden': 'true' }),
      hr.healthy ? `${findings.length} advisory finding${findings.length === 1 ? '' : 's'} on the latest round.`
        : `The watchdog flagged ${findings.length} finding${findings.length === 1 ? '' : 's'}.`,
    ]),
    findings.length ? el('ul', { class: 'vb-env-findings' },
      findings.slice(0, 4).map((f) => el('li', null, [
        typeof f === 'string' ? f : (f && (f.message || f.detail || f.kind) || JSON.stringify(f)),
      ]))) : null,
  ].filter(Boolean));
}

function chapter(row, index, currentEpochId) {
  const epochId = row.epoch_id;
  const isCurrent = currentEpochId && epochId === currentEpochId;
  const points = epochSparkPoints(epochId).filter((p) => p != null);
  const promoted = row.promoted_count || 0;
  const total = row.generation_count || points.length || 0;

  const head = el('div', { class: 'vb-chapter-head' }, [
    el('span', { class: 'vb-chapter-no' }, [String(index + 1).padStart(2, '0')]),
    el('div', { class: 'vb-chapter-headings' }, [
      el('div', { class: 'vb-chapter-eyebrow' }, [
        el('span', { class: 'vb-chapter-id' }, [epochId]),
        isCurrent ? el('span', { class: 'vb-chapter-live' }, ['current']) : null,
      ].filter(Boolean)),
      el('h2', { class: 'vb-chapter-objective' }, [
        row.goal ? row.goal : el('span', { class: 'vb-muted' }, ['(no recorded objective)']),
      ]),
    ]),
  ]);

  const fig = el('div', { class: 'vb-chapter-fig' }, [
    points.length
      ? sparkline(points, { width: 280, height: 60, ariaLabel: `loss trajectory for ${epochId}` })
      : el('p', { class: 'vb-muted vb-chapter-noscalar' }, ['No scored generations yet.']),
    points.length ? el('p', { class: 'vb-chapter-fig-cap' }, [
      'best loss ', el('strong', null, [fmtNum(Math.min(...points), 3)]),
      ' across ', String(total), ' generation', total === 1 ? '' : 's',
    ]) : null,
  ].filter(Boolean));

  const meta = el('div', { class: 'vb-chapter-meta' }, [
    stat(total, 'generations'),
    stat(promoted, 'promoted', { tone: promoted > 0 ? 'improve' : 'neutral' }),
    fin(row.best_scalar) ? stat(fmtNum(row.best_scalar, 3), 'best loss', { tone: 'improve' }) : null,
    el('div', { class: 'vb-chapter-actions' }, [
      el('a', {
        class: 'vb-link-arrow', href: '#/B/epoch/' + encodeURIComponent(epochId),
        onclick: (ev) => { if (ev && ev.preventDefault) ev.preventDefault(); bRouter.go('epoch', epochId); },
      }, ['Read the chapter →']),
    ]),
  ].filter(Boolean));

  return el('article', {
    class: 'vb-chapter' + (isCurrent ? ' vb-chapter-current' : ''),
  }, [head, el('div', { class: 'vb-chapter-body' }, [fig, meta])]);
}

export function renderEnvironment(host, _route) {
  if (!host) return;
  const c = caches();
  c.ensure('ws', '/api/workspace', { epochs: [], sparkline: [], current_epoch_id: null });

  const ws = c.get('ws') || {};
  let epochs = Array.isArray(ws.epochs) && ws.epochs.length ? ws.epochs : epochsFromLineage();
  const currentEpochId = ws.current_epoch_id
    || (state.heartbeat && state.heartbeat.epoch_id) || null;
  // Sparkline across epochs (best loss per epoch) — the workspace-level arc.
  const sparkPoints = (Array.isArray(ws.sparkline) && ws.sparkline.length)
    ? ws.sparkline.map((s) => (fin(s.scalar) ? s.scalar : null))
    : epochs.map((e) => (fin(e.best_scalar) ? e.best_scalar : null));

  clearChildren(host);

  // Masthead block.
  const lead = el('div', { class: 'vb-env-lead' }, [
    el('p', { class: 'vb-eyebrow' }, ['The workspace']),
    el('h1', { class: 'vb-page-title vb-env-title' }, [
      'An automated-science notebook, read across ',
      el('em', null, [String(epochs.length)]),
      epochs.length === 1 ? ' epoch.' : ' epochs.',
    ]),
    el('p', { class: 'vb-env-dek' }, [
      'zicato mutates an agent, runs it over a board, measures the drift it causes, '
      + 'and a gate decides. Each epoch below is a chapter in that experiment.',
    ]),
    healthLine(),
  ]);
  host.appendChild(lead);

  // The lineage as the hero figure.
  const nodes = lineageNodes();
  host.appendChild(section('Lineage', [
    nodes.length
      ? trajectoryStory(nodes, {
          onSelect: (id) => bRouter.go('experiment', id),
          ariaLabel: 'workspace lineage — every generation, y = loss',
        })
      : note('empty', {
          label: 'No generations yet',
          detail: 'The trajectory begins with the first run.',
        }),
    sparkPoints.filter((p) => p != null).length > 1
      ? el('div', { class: 'vb-env-arc' }, [
          el('span', { class: 'vb-env-arc-label' }, ['cross-epoch best loss']),
          sparkline(sparkPoints, { width: 360, height: 40, ariaLabel: 'best loss per epoch' }),
        ])
      : null,
  ].filter(Boolean), { sub: 'The optimization curve being climbed. Click a node to open its experiment.' }));

  // The epochs as chapters.
  const chaptersBody = epochs.length
    ? epochs.map((row, i) => chapter(row, i, currentEpochId))
    : [note('empty', {
        label: 'No epochs on disk yet',
        detail: 'Start an evolve run — the first epoch will appear here as a chapter.',
      })];
  host.appendChild(section('Chapters', chaptersBody, {
    sub: 'Each epoch in order — its objective, its loss trajectory, its verdicts.',
  }));
}

registerBView('environment', renderEnvironment);
