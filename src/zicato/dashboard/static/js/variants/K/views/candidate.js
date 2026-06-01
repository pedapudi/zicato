// variants/K/views/candidate.js — CANDIDATE (one generation), live.

import { el } from '../../../core/dom.js';
import { state } from '../../../core/state.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, loading, stat, linkButton } from '../ui.js';

export async function render(host, ctx, params) {
  const genId = params && params.gen;
  const entryId = params && params.entry;
  if (!host.firstChild) host.appendChild(loading('Loading candidate…'));

  const ep = await D.epoch();
  const epochId = (ep && ep.epoch_id) || (state.epochDef && state.epochDef.epoch_id) || null;

  if (!genId || !epochId) {
    gatedSwap(host, 'no-cand', () => [
      el('h1', { class: 'vk-h1', text: 'Candidate' }),
      empty('No candidate selected — open one from the paper’s lineage or heatmap figure.'),
    ]);
    return;
  }

  const pe = await D.perEntry(epochId, genId);
  const entries = (pe && Array.isArray(pe.entries)) ? pe.entries : [];

  // Champion reference (the promoted generation's per-entry loss) for the
  // dot-plot reference rule, so each entry reads "better/worse than champ".
  const lineage = await D.lineage(epochId);
  const champ = (lineage && Array.isArray(lineage.generations)) ? lineage.generations.find((g) => g.promoted) : null;
  let champEntries = null;
  if (champ && champ.generation_id !== genId) {
    const cpe = await D.perEntry(epochId, champ.generation_id);
    champEntries = (cpe && Array.isArray(cpe.entries)) ? cpe.entries : null;
  }
  const champLoss = new Map();
  if (champEntries) for (const e of champEntries) champLoss.set(e.entry_id, e.drift_loss);

  // Entry drill-down data.
  let exp = null; let perJudge = null; let row = null;
  if (entryId) {
    row = entries.find((e) => e.entry_id === entryId) || null;
    [exp, perJudge] = await Promise.all([
      D.expectations(epochId, genId, entryId), D.perJudgeForRun(epochId, genId, entryId),
    ]);
  }

  const digest = JSON.stringify({
    genId, entryId, epochId,
    entries: entries.map((e) => [e.entry_id, e.drift_loss, e.pass_fail, e.wall_clock_budget_exceeded]),
    champ: champ ? champ.generation_id : null,
    exp: exp && Array.isArray(exp.outcomes) ? exp.outcomes.map((o) => [o.kind, o.passed, o.judge_name]) : null,
    judges: perJudge && Array.isArray(perJudge.judges) ? perJudge.judges.map((j) => [j.judge_name, j.weighted_loss]) : null,
  });

  gatedSwap(host, digest, () => {
    const nodes = [];
    nodes.push(el('div', { class: 'vk-pagehead' }, [
      el('h1', { class: 'vk-h1', text: `Candidate ${genId}` }),
      el('p', { class: 'vk-lede', text: 'How this generation scored on each board entry — drift loss, pass/fail, budget.' }),
    ]));

    const items = entries.map((e) => ({
      label: e.entry_id, id: e.entry_id, value: svg.isNum(e.drift_loss) ? e.drift_loss : null,
      pass: e.pass_fail, timeout: !!e.wall_clock_budget_exceeded, ran: e.run_id != null,
    })).sort((a, b) => (svg.isNum(a.value) ? a.value : 1e9) - (svg.isNum(b.value) ? b.value : 1e9));

    const dotCard = el('div', { class: 'vk-panel' });
    if (items.length) {
      const refVal = champ && champ.generation_id !== genId
        ? items.reduce((a, it) => a + (svg.isNum(champLoss.get(it.id)) ? champLoss.get(it.id) : 0), 0) : null;
      dotCard.appendChild(svg.valueDotPlot({
        width: 540, rowHeight: 24, labelWidth: 200, items,
        reference: refVal != null ? { label: 'champion total', value: refVal } : null,
        onClick: (it) => ctx.navigate('candidate', { gen: genId, entry: it.id }),
      }));
      dotCard.appendChild(el('p', { class: 'vk-faint vk-fignote', text: 'lower loss is better · ✓ pass · ✕ fail · ⏱ timeout · click an entry to drill in' }));
    } else {
      dotCard.appendChild(empty('No per-entry scoring recorded for this candidate.'));
    }
    nodes.push(section('Per-board scoring', dotCard));

    // Entry drill-down (depth 2 + a link to depth 3, the transcript).
    if (entryId) nodes.push(section(`Entry · ${entryId}`, entryDrill(ctx, genId, entryId, row, exp, perJudge)));

    return nodes;
  });
}

function entryDrill(ctx, genId, entryId, row, exp, perJudge) {
  const card = el('div', { class: 'vk-panel' });
  card.appendChild(el('div', { class: 'vk-row' }, [
    stat(row && svg.isNum(row.drift_loss) ? svg.fmt(row.drift_loss, 1) : '—', 'drift loss'),
    stat(row ? passLabel(row.pass_fail) : '—', 'predicate'),
    stat(row && row.wall_clock_budget_exceeded ? 'timed out' : (row && svg.isNum(row.runtime_ms) ? `${(row.runtime_ms / 1000).toFixed(0)}s` : '—'), 'runtime'),
  ]));

  const outcomes = (exp && Array.isArray(exp.outcomes)) ? exp.outcomes : [];
  if (outcomes.length) {
    card.appendChild(el('div', { class: 'vk-subhead', text: 'Expectation outcomes' }));
    const ul = el('ul', { class: 'vk-outcomes' });
    for (const o of outcomes) {
      ul.appendChild(el('li', { class: 'vk-outcome ' + (o.passed ? 'vk-good' : 'vk-bad') }, [
        el('span', { class: 'vk-outcome-kind', text: (o.kind || 'check') + (o.judge_name ? ` · ${o.judge_name}` : '') }),
        el('span', { class: 'vk-outcome-detail', text: o.detail || (o.passed ? 'passed' : 'failed') }),
      ]));
    }
    card.appendChild(ul);
  }

  const judges = (perJudge && Array.isArray(perJudge.judges)) ? perJudge.judges : [];
  if (judges.length) {
    card.appendChild(el('div', { class: 'vk-subhead', text: 'Per-judge loss' }));
    const items = judges.map((j) => ({ label: j.judge_name || 'judge', value: j.weighted_loss }));
    card.appendChild(svg.valueDotPlot({ width: 480, rowHeight: 22, labelWidth: 200, items }));
  }

  // Depth 3: a clearly-themed button to the full transcript (the E bug:
  // an unstyled anchor — here a proper themed link button).
  card.appendChild(el('div', { class: 'vk-drill-actions' }, [
    linkButton('Open the full transcript →', '#/K/run/' + encodeURIComponent(genId) + '/' + encodeURIComponent(entryId),
      () => ctx.navigate('run', { gen: genId, entry: entryId })),
  ]));
  return card;
}

function passLabel(pf) {
  if (pf === 1 || pf === true) return 'pass';
  if (pf === 0 || pf === false) return 'fail';
  return 'none';
}
