// variants/B/views/epoch.js — the Epoch view (the chapter).
//
// The chapter opens with the OBJECTIVE rendered as a prominent, readable
// statement — the thesis of the chapter, set large. Beneath it sits a real,
// well-typeset home for the PROPOSER BRIEF: it can be long and complex, so
// it is rendered as readable prose with a table-of-contents rail and
// collapsible sections (renderBrief), never a truncated line. When no brief
// endpoint text is present we surface the goal and say so plainly — but the
// brief's home is always designed.
//
// Then: the lineage as an elegant within-epoch trajectory (non-colliding),
// and the board's reasoning as a flowing list of small experiment cards —
// each a hypothesis line + a per-delta diverging glyph — NOT a raw table.
//
// Data: /api/epoch (goal + brief + experiments + patches), /api/contract-
// diff/{e}, and per-experiment outcome deltas already on epochDef.

import { el, clearChildren } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import { fetchJson } from '../../../core/api.js';
import { bRouter } from '../router.js';
import { registerBView } from '../shell.js';
import {
  makeCache, currentEpochId, lineageNodes, decisionOf, isBaselineSeed,
  hypothesisText, outcomeNum, genId,
} from '../lib/data.js';
import { renderBrief, section, note, pullQuote, verdictBadge, stat } from '../lib/prose.js';
import { trajectoryStory, divergingBars, fmtSigned, fin } from '../lib/charts.js';

let _epochCache = null;
let _diffCache = null;
function repaint() {
  const host = document.getElementById('vb-page');
  if (host && bRouter.current().view === 'epoch') renderEpoch(host, bRouter.current());
}
function caches() {
  if (!_epochCache) _epochCache = makeCache(repaint);
  if (!_diffCache) _diffCache = makeCache(repaint);
  return { epoch: _epochCache, diff: _diffCache };
}
export function resetEpochView() { _epochCache = null; _diffCache = null; }

// The current epoch is folded into state.epochDef by the env poll; prefer
// it when it matches so the first paint is never blank.
function epochDefFor(epochId, cache) {
  const def = state.epochDef;
  if (def && (!epochId || def.epoch_id === epochId)) return def;
  if (epochId && cache.has(epochId)) return cache.get(epochId);
  return null;
}

function contractLine(epochId, diff) {
  if (diff === undefined) return note('running', { label: 'Reading contract diff' });
  if (!diff || diff.__broken) return note('broken', { reason: 'contract diff unavailable' });
  if (!diff.predecessor_epoch_id) {
    return el('p', { class: 'vb-epoch-contract' }, [
      el('span', { class: 'vb-tag' }, ['first epoch']),
      ' No predecessor — the contract is the seed.',
    ]);
  }
  const comps = Array.isArray(diff.components) ? diff.components : [];
  const changed = comps.filter((c) => c && c.changed);
  if (changed.length === 0) {
    return el('p', { class: 'vb-epoch-contract' }, [
      'Contract unchanged — it carries over from ',
      el('span', { class: 'vb-mono' }, [String(diff.predecessor_epoch_id)]), '.',
    ]);
  }
  return el('p', { class: 'vb-epoch-contract' }, [
    'Rolled by ', el('strong', null, [String(changed.length)]),
    changed.length === 1 ? ' change' : ' changes', ' vs ',
    el('span', { class: 'vb-mono' }, [String(diff.predecessor_epoch_id)]), ': ',
    ...changed.map((c, i) => el('span', { class: 'vb-tag vb-tag-changed' }, [
      String(c.name),
    ])).flatMap((n, i) => i ? [' ', n] : [n]),
  ]);
}

// One experiment as a small editorial card: gen id + verdict, hypothesis
// line, and the three deltas as tiny diverging glyphs. Clickable → entry.
function experimentCard(exp) {
  const gid = exp.generation_id ? String(exp.generation_id) : '?';
  const decision = decisionOf(exp);
  const verdict = (isBaselineSeed(exp)) ? 'baseline'
    : decision === 'promoted' ? 'promoted'
      : decision === 'rejected' ? 'rejected'
        : decision === 'deferred' ? 'deferred' : 'open';
  const hyp = hypothesisText(exp);
  const deltas = [
    ['Δ loss', outcomeNum(exp, 'drift_loss_delta'), true],
    ['Δ scalar', outcomeNum(exp, 'scalar_score_delta'), true],
    ['Δ pass', outcomeNum(exp, 'pass_rate_delta'), false],
  ];
  const open = () => { if (gid !== '?') bRouter.go('experiment', gid); };
  const card = el('article', {
    class: 'vb-exp-card vb-clickable', role: 'button', tabindex: '0',
    'aria-label': `experiment ${gid}`,
  }, [
    el('div', { class: 'vb-exp-card-head' }, [
      el('span', { class: 'vb-mono vb-exp-card-gen' }, [gid]),
      verdictBadge(verdict),
    ]),
    el('p', { class: 'vb-exp-card-hyp' }, [
      hyp || el('span', { class: 'vb-muted' }, ['(no hypothesis recorded)']),
    ]),
    el('div', { class: 'vb-exp-card-deltas' }, deltas.map(([label, v, improveNeg]) => {
      const tone = v == null ? 'neutral'
        : (improveNeg ? (v < 0 ? 'improve' : v > 0 ? 'regress' : 'neutral')
          : (v > 0 ? 'improve' : v < 0 ? 'regress' : 'neutral'));
      return el('span', { class: `vb-exp-delta vb-${tone}` }, [
        el('span', { class: 'vb-exp-delta-label' }, [label]),
        el('span', { class: 'vb-exp-delta-val' }, [v == null ? '—' : fmtSigned(v, label === 'Δ pass' ? 2 : 3)]),
      ]);
    })),
  ]);
  card.addEventListener('click', open);
  card.addEventListener('keydown', (ev) => {
    if (ev && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); open(); }
  });
  return card;
}

export function renderEpoch(host, route) {
  if (!host) return;
  const epochId = (route && route.params && route.params.epochId) || currentEpochId();
  const c = caches();
  // Fetch a specific non-current epoch; the current one is in state.epochDef.
  if (epochId && (!state.epochDef || state.epochDef.epoch_id !== epochId)) {
    c.epoch.ensure(epochId, '/api/epoch', { epoch_id: epochId, experiments: [], __broken: true });
  }
  if (epochId) c.diff.ensure(epochId, '/api/contract-diff/' + encodeURIComponent(epochId));

  const def = epochDefFor(epochId, c.epoch);
  const experiments = (def && Array.isArray(def.experiments)) ? def.experiments : [];
  const diff = epochId ? c.diff.get(epochId) : undefined;

  clearChildren(host);

  if (!epochId && !def) {
    host.appendChild(el('h1', { class: 'vb-page-title' }, ['Epoch']));
    host.appendChild(note('empty', {
      label: 'No epoch yet',
      detail: 'Start an evolve run, or open an epoch from the Environment.',
    }));
    return;
  }

  const closed = !!(def && def.closed);
  const goal = (def && typeof def.goal === 'string') ? def.goal.trim() : '';
  const promoted = experiments.filter((e) => decisionOf(e) === 'promoted' || isBaselineSeed(e)).length;

  // The objective as the chapter thesis.
  host.appendChild(el('div', { class: 'vb-epoch-lead' }, [
    el('p', { class: 'vb-eyebrow' }, [
      el('span', { class: 'vb-mono' }, [String(epochId || '—')]),
      el('span', { class: `vb-epoch-state vb-epoch-state-${closed ? 'closed' : 'open'}` }, [
        closed ? 'closed' : 'open',
      ]),
    ]),
    goal
      ? pullQuote(goal, { class: 'vb-epoch-objective', attribution: 'the objective for this epoch' })
      : el('p', { class: 'vb-epoch-objective-empty vb-muted' }, [
          'This epoch has no recorded objective. Set it with ',
          el('code', { class: 'vb-code-inline' }, ['zicato epoch set-goal']),
          '.',
        ]),
    el('div', { class: 'vb-epoch-stats' }, [
      stat(experiments.length, 'generations'),
      stat(promoted, 'promoted', { tone: promoted > 0 ? 'improve' : 'neutral' }),
    ]),
    contractLine(epochId, diff),
  ]));

  // The proposer brief — its real, designed home.
  const briefText = (def && typeof def.brief === 'string') ? def.brief : '';
  const briefBody = briefText.trim()
    ? renderBrief(briefText, {
        onNavigate: (id) => {
          const target = document.getElementById('vb-brief-' + id);
          if (target) {
            if (target.tagName && target.tagName.toLowerCase() === 'details') target.open = true;
            if (typeof target.scrollIntoView === 'function') {
              target.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
          }
        },
      })
    : el('div', { class: 'vb-brief vb-brief-absent' }, [
        note('empty', {
          label: 'No proposer brief recorded for this epoch',
          detail: goal
            ? 'The objective above stands in for it. A brief.md in the epoch directory would render here as full prose.'
            : 'A brief.md in the epoch directory would render here as full prose with a table of contents.',
        }),
      ]);
  host.appendChild(section("Proposer's brief", briefBody, {
    sub: "The operator's brief to the proposer — the full instructions that shaped every hypothesis this epoch.",
  }));

  // The lineage within (and around) this epoch.
  const allNodes = lineageNodes();
  const epochNodes = allNodes.filter((n) => {
    const g = experiments.find((e) => genId({ id: e.generation_id }) === n.id);
    return g != null || allNodes.length <= 12;
  });
  const nodes = epochNodes.length ? epochNodes : allNodes;
  host.appendChild(section('Lineage', [
    nodes.length
      ? trajectoryStory(nodes, { onSelect: (id) => bRouter.go('experiment', id) })
      : note('empty', { label: 'No generations yet' }),
  ], { sub: 'The trajectory this chapter climbed. Click a node to open its experiment.' }));

  // The experiments as flowing cards.
  const expBody = experiments.length
    ? el('div', { class: 'vb-exp-grid' }, experiments.slice().reverse().map(experimentCard))
    : note('empty', {
        label: 'No experiments yet',
        detail: 'Generations appear here as the proposer forms and runs each hypothesis.',
      });
  host.appendChild(section('Experiments', expBody, {
    sub: 'Each generation, newest first — its bet and how the deltas moved. Open one to read the full entry.',
  }));
}

registerBView('epoch', renderEpoch);
