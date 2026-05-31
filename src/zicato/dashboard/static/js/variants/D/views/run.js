// variants/D/views/run.js — THEME 3: per-board scoring + drill-down.
//
// Three depths, one screen, narrowing:
//   Depth 1 (no entry param) — the per-board SCORING figure for one
//     candidate: a sorted value dot-plot of each entry's absolute drift
//     loss (lower = left = better) with a REFERENCE LINE at the champion's
//     scalar, plus a pass/fail/timeout glyph per row. Click an entry →
//   Depth 2 (entry param) — the ENTRY DETAIL small multiple: expectation
//     outcomes as pass/fail dots and the per-judge weighted losses as
//     direct-labelled bars for that single run. A deeper link →
//   Depth 3 — the TRANSCRIPT (turns + tool calls + drift annotations),
//     reconstructed from /api/conversation/{run_id}.
//
// Data: /api/epoch (candidates, champion), /api/score-trajectory,
// /api/generation/{e}/{g}/per-entry, /api/run/{e}/{g}/{entry}/expectations,
// /api/run/{e}/{g}/{entry}/per-judge, /api/conversation/{run_id}.

import { el, clearChildren } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { section, crumb, empty, loading, stat, verdictPill, normaliseDecision } from '../ui.js';

export async function render(host, ctx, params) {
  clearChildren(host);
  const genParam = params && params.gen;
  const entryParam = params && params.entry;

  const body = el('div');
  const head = el('div');
  host.appendChild(head);
  host.appendChild(body);
  body.appendChild(loading('Reading scoring…'));

  const [ep, traj] = await Promise.all([D.epoch(), D.scoreTrajectory()]);
  if (!ep || ep.epoch_id == null) {
    clearChildren(body);
    head.appendChild(crumb([{ label: 'environment', view: 'environment' }, { label: 'scoring' }]));
    head.appendChild(el('h1', { class: 'd-h1', text: 'Per-board scoring' }));
    body.appendChild(empty('No current epoch.'));
    return;
  }
  const epochId = ep.epoch_id;
  const experiments = Array.isArray(ep.experiments) ? ep.experiments : [];
  const gensAll = experiments.map((x) => x.generation_id).filter(Boolean);
  const genId = genParam && gensAll.includes(genParam) ? genParam : (gensAll[gensAll.length - 1] || genParam);

  // Champion baseline = the promoted generation's scalar (reference line).
  const scalarByGen = new Map();
  if (traj && Array.isArray(traj.points)) {
    for (const p of traj.points) if (svg.isNum(p.scalar)) scalarByGen.set(p.generation_id, p.scalar);
  }
  const champExp = experiments.find((x) => normaliseDecision(x.outcome) === 'promoted')
    || experiments.find((x) => !x.parent_generation_id);
  const championId = champExp ? champExp.generation_id : null;
  const championScalar = championId ? scalarByGen.get(championId) : null;

  clearChildren(body);

  if (entryParam) {
    await renderEntryDetail(head, body, ctx, epochId, genId, entryParam, championId);
    return;
  }

  // ---- Depth 1: the per-board scoring dot-plot ----
  head.appendChild(crumb([
    { label: 'environment', view: 'environment' },
    { label: 'lifecycle', view: 'lifecycle' },
    { label: genId ? `scoring · ${genId}` : 'scoring' },
  ]));
  head.appendChild(el('h1', { class: 'd-h1', text: `Per-board scoring · ${genId || '—'}` }));
  head.appendChild(el('p', { class: 'd-lede', text: 'Each board entry’s absolute drift loss for this candidate, sorted worst-first. The reference line is the champion’s level — dots left of it beat the champion.' }));

  if (!genId) { body.appendChild(empty('No candidate selected.')); return; }

  // candidate switcher
  if (gensAll.length > 1) {
    const switcher = el('div', { class: 'd-switcher' }, [
      el('span', { class: 'd-faint', text: 'candidate:' }),
      ...gensAll.map((g) => {
        const a = el('a', {
          class: 'd-switch' + (g === genId ? ' d-active' : ''),
          href: '#/D/run/' + encodeURIComponent(g),
          text: g,
        });
        return a;
      }),
    ]);
    head.appendChild(switcher);
  }

  const pe = await D.perEntry(epochId, genId);
  const entries = (pe && Array.isArray(pe.entries)) ? pe.entries : [];
  const card = el('div', { class: 'd-panel' });
  if (entries.length) {
    const items = entries
      .filter((e) => svg.isNum(e.drift_loss))
      .sort((a, b) => b.drift_loss - a.drift_loss)
      .map((e) => ({
        label: e.entry_id, value: e.drift_loss, id: e.entry_id,
        pass: e.pass_fail, timeout: !!e.wall_clock_budget_exceeded,
      }));
    card.appendChild(svg.valueDotPlot({
      width: 560, rowHeight: 22, labelWidth: 200,
      items,
      reference: svg.isNum(championScalar) ? { value: championScalar, label: `champion ${championId}` } : null,
      onClick: (it) => ctx.navigate('run', { gen: genId, entry: it.id }),
    }));
    card.appendChild(el('div', { class: 'd-legend' }, [
      svg.isNum(championScalar)
        ? el('span', null, [el('i', { class: 'spine', style: 'border-color:var(--v2-ink-faint);border-top-style:dashed;' }), `champion ${championId} = ${svg.fmt(championScalar, 1)}`]) : null,
      el('span', null, [el('i', { class: 'dotact' }), 'pass']),
      el('span', null, [el('i', { class: 'dotpred', style: 'border-color:var(--v2-bad);' }), 'fail']),
      el('span', { class: 'd-faint', text: '⏱ timeout · click an entry → its expectations, judges & transcript' }),
    ].filter(Boolean)));
  } else {
    card.appendChild(empty('No per-entry scores for this candidate (the index may not be built).'));
  }
  body.appendChild(section('Per-entry loss · sorted, vs champion', card));

  // ---- live runs in flight (kept from the operations read) ----
  renderInFlight(body);
}

// ---- Depth 2 + 3: one entry's detail (expectations, judges, transcript)
async function renderEntryDetail(head, body, ctx, epochId, genId, entryId, championId) {
  head.appendChild(crumb([
    { label: 'environment', view: 'environment' },
    { label: 'lifecycle', view: 'lifecycle' },
    { label: `scoring · ${genId}`, view: 'run', params: { gen: genId } },
    { label: entryId },
  ]));
  head.appendChild(el('h1', { class: 'd-h1', text: `${entryId}` }));
  head.appendChild(el('p', { class: 'd-lede', text: `How ${genId} scored on this one board entry: its expectation outcome, the per-judge process drift that fed the loss, and the run transcript.` }));

  const [pe, exps, judges] = await Promise.all([
    D.perEntry(epochId, genId),
    D.expectations(epochId, genId, entryId),
    D.perJudgeForRun(epochId, genId, entryId),
  ]);
  const row = (pe && Array.isArray(pe.entries)) ? pe.entries.find((e) => e.entry_id === entryId) : null;
  const runId = row ? row.run_id : null;

  // ---- the entry's headline figures ----
  body.appendChild(el('div', { class: 'd-panel d-row' }, [
    stat(row && svg.isNum(row.drift_loss) ? svg.fmt(row.drift_loss, 1) : '—', 'drift loss'),
    stat(row ? passLabel(row.pass_fail) : '—', 'predicate'),
    stat(row && row.wall_clock_budget_exceeded ? 'timed out' : (row && svg.isNum(row.runtime_ms) ? `${(row.runtime_ms / 1000).toFixed(0)}s` : '—'), 'runtime'),
    stat(genId, 'candidate'),
  ]));

  // ---- expectation outcomes (small multiple of pass/fail dots) ----
  const outcomes = (exps && Array.isArray(exps.outcomes)) ? exps.outcomes : [];
  const expCard = el('div', { class: 'd-panel' });
  if (outcomes.length) {
    const grid = el('div', { class: 'd-expect-grid' });
    for (const o of outcomes) {
      const passed = o.passed;
      const glyphCls = passed === true ? 'd-good' : passed === false ? 'd-bad' : 'd-flat';
      grid.appendChild(el('div', { class: 'd-expect-row' }, [
        el('span', { class: 'd-expect-dot ' + glyphCls, title: passed === true ? 'passed' : passed === false ? 'failed' : 'no verdict' }),
        el('span', { class: 'd-expect-kind', text: o.kind || 'expectation' }),
        o.judge_name ? el('span', { class: 'd-faint', text: ' · ' + o.judge_name }) : null,
        svg.isNum(o.score) ? el('span', { class: 'd-faint d-mono', text: ' ' + svg.fmt(o.score, 2) }) : null,
        el('span', { class: 'd-expect-detail d-faint', text: o.detail ? ' — ' + o.detail : '' }),
      ].filter(Boolean)));
    }
    expCard.appendChild(grid);
  } else {
    expCard.appendChild(empty('No expectation recorded for this entry (no predicate / rubric).'));
  }
  body.appendChild(section('Expectation outcomes', expCard));

  // ---- per-judge weighted losses (direct-labelled bars) ----
  const jrows = (judges && Array.isArray(judges.judges)) ? judges.judges : [];
  const jCard = el('div', { class: 'd-panel' });
  if (jrows.length) {
    const items = jrows
      .filter((j) => svg.isNum(j.weighted_loss))
      .sort((a, b) => b.weighted_loss - a.weighted_loss)
      .map((j) => ({ label: j.judge_name, value: j.weighted_loss }));
    if (items.length) {
      jCard.appendChild(svg.valueBars({ width: 420, rowHeight: 20, labelWidth: 180, items }));
      jCard.appendChild(el('p', { class: 'd-faint', style: 'font-size:11px;margin:8px 0 0;',
        text: 'weighted process-judge loss feeding this run’s scalar · higher = more drift' }));
    } else {
      jCard.appendChild(empty('Judges recorded but no weighted losses.'));
    }
  } else {
    jCard.appendChild(empty(judges && judges.note ? judges.note : 'No per-judge losses for this run (index not built).'));
  }
  body.appendChild(section('Per-judge loss', jCard));

  // ---- Depth 3: the transcript (collapsible, fetched on the run id) ----
  const trDetails = el('details', { class: 'd-brief' });
  trDetails.appendChild(el('summary', null, [
    el('span', { class: 'chev', text: '▸' }), 'Run transcript',
    el('span', { class: 'd-faint', style: 'font-weight:400;font-size:11px;',
      text: runId ? `· ${runId.slice(0, 10)}…` : '· no run id' }),
  ]));
  const trBody = el('div', { class: 'd-brief-body', style: 'max-width:none;' });
  trBody.appendChild(el('p', { class: 'd-faint', text: 'Open to load the reconstructed conversation.' }));
  trDetails.appendChild(trBody);
  // Lazy-load the transcript only when the operator opens the panel.
  let loaded = false;
  trDetails.addEventListener('toggle', async () => {
    if (loaded) return;
    if (!runId) { clearChildren(trBody); trBody.appendChild(empty('No run id — the transcript is unavailable.')); loaded = true; return; }
    loaded = true;
    clearChildren(trBody);
    trBody.appendChild(loading('Reconstructing transcript…'));
    const conv = await D.conversation(runId);
    clearChildren(trBody);
    renderTranscript(trBody, conv);
  });
  body.appendChild(section('Transcript · the run itself', trDetails));
}

function renderTranscript(host, conv) {
  if (!conv || conv.error) {
    host.appendChild(empty(conv && conv.error ? conv.error : 'Transcript unavailable.'));
    return;
  }
  const turns = Array.isArray(conv.turns) ? conv.turns : [];
  const anns = Array.isArray(conv.annotations) ? conv.annotations : [];
  if (!turns.length) { host.appendChild(empty('No turns reconstructed (the run may have produced no conversation).')); return; }
  const annBySeq = new Map();
  for (const a of anns) {
    const k = a.anchor_seq;
    if (!annBySeq.has(k)) annBySeq.set(k, []);
    annBySeq.get(k).push(a);
  }
  for (const t of turns) {
    const turn = el('div', { class: 'd-turn d-turn-' + (t.role || 'agent') }, [
      el('div', { class: 'd-turn-head d-faint d-mono' }, [
        el('span', { text: (t.agent || t.role || 'turn') }),
        t.kind ? el('span', { text: ' · ' + t.kind }) : null,
      ].filter(Boolean)),
      t.text ? el('div', { class: 'd-turn-text', text: t.text }) : null,
    ].filter(Boolean));
    if (Array.isArray(t.tool_calls) && t.tool_calls.length) {
      for (const tc of t.tool_calls) {
        turn.appendChild(el('div', { class: 'd-tool d-mono', text: '⚙ ' + (tc.name || tc.tool || 'tool') }));
      }
    }
    const myAnns = annBySeq.get(t.seq) || [];
    for (const a of myAnns) {
      turn.appendChild(el('div', { class: 'd-annot d-annot-' + (a.kind || 'note'), text: '◂ ' + (a.summary || a.kind) }));
    }
    host.appendChild(turn);
  }
}

function passLabel(pf) {
  if (pf === 1 || pf === true) return 'pass';
  if (pf === 0 || pf === false) return 'fail';
  return 'none';
}

// ---- live runs in flight (operations strip) ----
function renderInFlight(body) {
  // Read directly from the shared AppState the environment load populates.
  // Kept lightweight: a quiet honest state when nothing is running.
  import('../../../core/state.js').then(({ state }) => {
    const runs = Array.isArray(state.activeRuns) ? state.activeRuns : [];
    if (!runs.length) return; // nothing to show — stay quiet
    const card = el('div', { class: 'd-panel' });
    for (const r of runs) {
      card.appendChild(el('div', { class: 'd-gate', style: 'border-top:1px solid var(--v2-rule-soft);' }, [
        el('div', { class: 'd-mono', style: 'min-width:160px;', text: r.entry_id || r.run_id || r.id || 'run' }),
        el('div', { class: 'd-faint d-mono', text: svg.isNum(r.progress) ? `${(r.progress * 100).toFixed(0)}%` : 'running' }),
      ]));
    }
    body.appendChild(section('In flight', card));
  }).catch(() => {});
}
