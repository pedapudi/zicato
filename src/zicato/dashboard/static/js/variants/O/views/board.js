// variants/O/views/board.js — the NEW per-board CROSS-CANDIDATE view (FIX 7).
//
// First-class in Compass: selecting a board entry in the rail (or a sankey
// board / heatmap cell / per-board dot row) opens THIS page, keyed by the
// ENTRY ID — never an arbitrary candidate (the operator's complaint: a
// trellis click dumped them on candidate v2 "with no fidelity"). It shows,
// for ONE board entry, how EVERY candidate performed on it:
//   * the board entry's contract (kind, budget, weight, tags, input),
//   * a per-candidate loss + pass/fail/timeout comparative chart (sorted
//     bars, champion reference line),
//   * the paired (champion vs challenger) context where a matchup exists,
//   * a drill to each candidate's run/transcript FOR THIS BOARD.

import { el } from '../../../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { gatedSwap, section, empty, loading, stat } from '../ui.js';
import { loadRailModel } from '../model.js';

export async function render(host, ctx, route) {
  const entryId = route.entry;
  if (!host.firstChild) host.appendChild(loading('Reading the board entry…'));
  if (!entryId) { gatedSwap(host, 'no-entry', () => [empty('No board entry selected.')]); return; }

  const m = await loadRailModel();
  const { epochId, ordered, gens, board, tours } = m;
  const entryDef = board.find((b) => b.id === entryId) || { id: entryId };

  // Pivot: this entry's row across every generation.
  const perEntries = await Promise.all(ordered.map((g) => D.perEntry(epochId, g.generation_id)));
  const rows = [];
  ordered.forEach((g, i) => {
    const pe = perEntries[i];
    const row = (pe && Array.isArray(pe.entries)) ? pe.entries.find((e) => e.entry_id === entryId) : null;
    if (row) rows.push({
      id: g.generation_id, promoted: !!g.promoted, run_id: row.run_id,
      value: row.drift_loss, pass: row.pass_fail, timeout: !!row.wall_clock_budget_exceeded,
      runtime_ms: row.runtime_ms,
    });
  });

  // Paired context: the first matchup that scored this entry.
  const matchups = (tours && Array.isArray(tours.matchups)) ? tours.matchups : [];
  let pairSeries = null; let pairLabels = null;
  for (const mu of matchups) {
    if (!mu.champion || !mu.challenger) continue;
    const grid = await D.matchupGrid(epochId, mu.champion, mu.challenger);
    const gr = (grid && Array.isArray(grid.entry_grid)) ? grid.entry_grid.find((r) => r.entry_id === entryId) : null;
    if (gr) {
      pairSeries = [{ label: entryId, id: entryId, a: gr.parent_drift_loss, b: gr.child_drift_loss, verdict: gr.verdict }];
      pairLabels = { champion: mu.champion, challenger: mu.challenger };
      break;
    }
  }

  const champRow = rows.find((r) => r.promoted);
  const items = rows.map((r) => ({ id: r.id, label: r.id, value: r.value, pass: r.pass, timeout: r.timeout, promoted: r.promoted }))
    .sort((a, b) => (svg.isNum(a.value) ? a.value : Infinity) - (svg.isNum(b.value) ? b.value : Infinity));

  const digest = JSON.stringify({
    entryId, kind: entryDef.kind,
    rows: rows.map((r) => [r.id, fx(r.value), r.pass, r.timeout, r.run_id || null]),
    pair: pairSeries ? [pairLabels.champion, pairLabels.challenger, fx(pairSeries[0].a), fx(pairSeries[0].b)] : null,
  });

  gatedSwap(host, digest, () => {
    const out = [];
    out.push(el('div', { class: 'vo-pagehead' }, [
      el('div', { class: 'vo-pagehead-row' }, [
        el('span', { class: 'vo-eyebrow', text: 'BOARD ENTRY' }),
        el('h1', { class: 'vo-h1 vo-mono', text: entryId }),
        entryDef.kind ? el('span', { class: 'vo-kindtag', text: entryDef.kind }) : null,
      ].filter(Boolean)),
      el('p', { class: 'vo-lede', text: 'How every candidate performed on this one board entry — the cross-candidate view.' }),
    ]));

    // Contract.
    out.push(el('div', { class: 'vo-glance' }, [
      stat(entryDef.kind || '—', 'kind'),
      stat(svg.isNum(entryDef.budget_s) ? entryDef.budget_s + 's' : '—', 'budget'),
      stat(svg.isNum(entryDef.weight) ? String(entryDef.weight) : '—', 'weight'),
      stat(entryDef.expectation_kind || 'none', 'expectation'),
    ]));
    if (entryDef.input_preview) out.push(el('p', { class: 'vo-soft', text: '“' + entryDef.input_preview + '”' }));
    if (Array.isArray(entryDef.tags) && entryDef.tags.length) {
      out.push(el('div', { class: 'vo-tags' }, entryDef.tags.map((t) => el('span', { class: 'vo-tag', text: t }))));
    }

    // The comparative chart.
    if (!items.length) {
      out.push(section('Per-candidate scoring', empty('No candidate has a recorded run for this entry.')));
    } else {
      out.push(section('Per-candidate scoring · sorted best-first',
        el('div', { class: 'vo-figure' }, [
          el('div', { class: 'vo-figure-mark' }, [svg.sortedBars({
            width: 520, rowHeight: 26, labelWidth: 84, items,
            reference: champRow && svg.isNum(champRow.value) ? { label: champRow.id, value: champRow.value } : null,
            onClick: (it) => ctx.navigate('run', { gen: it.id, entry: entryId }),
          })]),
          el('figcaption', { class: 'vo-figcaption', text: 'Drift loss per candidate (lower = better); the dashed rule is the champion. Click a bar to open that candidate’s run for this board.' }),
        ])));
    }

    // Paired context.
    if (pairSeries) {
      out.push(section(`Paired duel · ${pairLabels.champion} vs ${pairLabels.challenger}`,
        el('div', { class: 'vo-panel' }, [
          svg.pairedSlopegraph({
            width: 480, height: 160, series: pairSeries,
            left: { title: pairLabels.champion }, right: { title: pairLabels.challenger },
            onClick: () => ctx.navigate('run', { gen: pairLabels.challenger, entry: entryId }),
          }),
          el('p', { class: 'vo-faint vo-fignote', text: 'the common-random-number duel for this entry · slope down = the challenger improved' }),
        ])));
    }

    // Explicit drill list — each candidate's run for THIS board.
    if (rows.length) {
      const list = el('ul', { class: 'vo-runlist' });
      for (const r of rows) {
        const li = el('li', { class: 'vo-runlist-item' + (r.run_id ? '' : ' vo-disabled'), tabindex: r.run_id ? '0' : null, role: 'button', 'data-gen': r.id }, [
          el('span', { class: 'vo-mono', text: r.id }),
          el('span', { class: 'vo-runlist-loss', text: svg.isNum(r.value) ? svg.fmt(r.value, 1) : '—' }),
          el('span', { class: 'vo-runlist-verdict vo-' + verdictCls(r), text: verdictText(r) }),
          el('span', { class: 'vo-runlist-open', text: r.run_id ? 'open run →' : 'no run' }),
        ]);
        if (r.run_id) {
          const go = () => ctx.navigate('run', { gen: r.id, entry: entryId });
          li.addEventListener('click', go);
          li.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); go(); } });
        }
        list.appendChild(li);
      }
      out.push(section('Drill to each candidate’s run', list));
    }
    return out;
  });
}

function verdictCls(r) {
  if (r.timeout) return 'caution';
  if (r.pass === 1 || r.pass === true) return 'good';
  if (r.pass === 0 || r.pass === false) return 'bad';
  return 'flat';
}
function verdictText(r) {
  if (r.timeout) return 'timed out';
  if (r.pass === 1 || r.pass === true) return 'pass';
  if (r.pass === 0 || r.pass === false) return 'fail';
  return 'no predicate';
}
function fx(v) { return svg.isNum(v) ? Number(v).toFixed(3) : null; }
