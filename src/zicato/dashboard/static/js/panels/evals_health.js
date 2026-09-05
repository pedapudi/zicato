// js/panels/evals_health.js — the instrument-health panel of the evals page
// (EVAL-VIEW.md §5).
//
// The board read as a MEASURING DEVICE, live: the measured A/A noise floor + the
// live MDE ladder (§4.3), and the ranked instrument-quality findings — noisiest
// evals, dead channels (with the minimum-comparisons honesty threshold), runtime
// cost, the holdout-ladder budget, rotation cadence, and redundancy clusters
// (only when a reflection is already built; else a deferred pointer at `reflect`).
//
// A panel rather than a view: it has no route of its own. The evals view
// (views/evals.js) owns two host containers — a strip ABOVE the matrix and a
// section BELOW it — and calls `mount({ strip, section }, matrixPayload,
// { navigate, href })` on every render.
//
// `mount` FETCHES its own /api/epoch/{id}/eval-health payload (the matrix payload
// only supplies `epoch_id`), builds the model, and paints the strip + section
// digest-gated. A failed or empty read degrades to honest-empty hosts (the
// matrix renders fine with nothing extra). The lower-level `evalHealthModel` /
// `evalHealthDigest` / `renderEvalHealthStrip` / `renderEvalHealthSection`
// exports are pure and unit-tested; a caller wanting a single combined node
// uses `renderEvalHealth`.
//
// The panel derives every field DEFENSIVELY from the build_eval_health reader,
// degrades to honest empties (never a fabricated number — §4), and is
// RECOMMEND-ONLY: a finding points into reflect or the builder and takes no
// action itself.

import { el } from '../core/dom.js';
import * as svg from '../svg.js';
import { section, empty, stat, dataTable, truncate, gatedSwap, fmtPercent, fmtDurationMs } from '../ui.js';

// ---- model ----------------------------------------------------------
//
// Pure, defensive normalization of the /api/epoch/{id}/eval-health payload into
// the render-friendly shape. Every field is type-guarded: a missing block reads
// as the honest "nothing yet" shape, never a throw.

function arr(v) { return Array.isArray(v) ? v : []; }
function str(v) { return typeof v === 'string' && v ? v : null; }
function num(v) { return svg.isNum(v) ? v : null; }
function intOf(v) { return Number.isInteger(v) ? v : null; }

function mdeModel(m) {
  const o = m && typeof m === 'object' ? m : {};
  return {
    floorMeasured: o.floor_measured === true,
    floor: num(o.floor),
    floorStatistic: str(o.floor_statistic),
    replicates: intOf(o.replicates) != null ? o.replicates : 0,
    replicatesSource: str(o.replicates_source),
    usable: o.usable === true,
    formulaN: intOf(o.formula_n),
    df: intOf(o.df),
    mde: num(o.mde),
    mdeRelaxed: num(o.mde_relaxed),
    alpha: num(o.alpha),
    alphaRelaxed: num(o.alpha_relaxed),
    power: num(o.power),
    formula: str(o.formula),
    note: str(o.note),
  };
}

function rotationModel(r) {
  const o = r && typeof r === 'object' ? r : {};
  return {
    rotateHoldout: o.rotate_holdout === true,
    ceiling: intOf(o.max_generations_per_contract),
    evaluated: intOf(o.evaluated_generations) != null ? o.evaluated_generations : 0,
    refreshRecommended: o.refresh_recommended === true,
    recommendation: str(o.recommendation),
  };
}

function holdoutModel(h) {
  if (!h || typeof h !== 'object') return null;
  return {
    generationId: str(h.generation_id),
    confirmed: typeof h.confirmed === 'boolean' ? h.confirmed : null,
    released: h.ladder_released === true,
    budgetTotal: intOf(h.ladder_budget_total),
    budgetRemaining: intOf(h.ladder_budget_remaining),
    threshold: num(h.threshold),
  };
}

function redundancyModel(r) {
  const o = r && typeof r === 'object' ? r : {};
  return {
    available: o.available === true,
    reflectionId: str(o.reflection_id),
    note: str(o.note),
    clusters: arr(o.clusters).map((c) => ({
      judgeName: str(c && c.judge_name) || '(unnamed judge)',
      redundantWith: arr(c && c.redundant_with).filter((x) => typeof x === 'string'),
    })).filter((c) => c.redundantWith.length > 0),
  };
}

export function evalHealthModel(payload) {
  const p = payload && typeof payload === 'object' ? payload : {};
  return {
    found: p.found === true,
    mde: mdeModel(p.mde),
    noisiest: arr(p.noisiest).map((r) => ({
      entryId: str(r && r.entry_id) || '',
      flipRate: num(r && r.flip_rate),
      slice: str(r && r.slice) || 'train',
      calibrationRuns: intOf(r && r.calibration_runs) != null ? r.calibration_runs : 0,
    })),
    dead: arr(p.dead).map((r) => ({
      entryId: str(r && r.entry_id) || '',
      pairs: intOf(r && r.discrimination_pairs) != null ? r.discrimination_pairs : 0,
      slice: str(r && r.slice) || 'train',
    })),
    insufficient: arr(p.insufficient).map((r) => ({
      entryId: str(r && r.entry_id) || '',
      pairs: intOf(r && r.discrimination_pairs) != null ? r.discrimination_pairs : 0,
      slice: str(r && r.slice) || 'train',
    })),
    runtimeCost: arr(p.runtime_cost).map((r) => ({
      entryId: str(r && r.entry_id) || '',
      runtimeMsMean: num(r && r.runtime_ms_mean),
      replicateTotal: intOf(r && r.replicate_total) != null ? r.replicate_total : 0,
      slice: str(r && r.slice) || 'train',
    })),
    holdoutBudget: holdoutModel(p.holdout_budget),
    rotation: rotationModel(p.rotation),
    redundancy: redundancyModel(p.redundancy),
  };
}

// ---- digest (digest-gated render — a no-op beat rebuilds nothing) --------

export function evalHealthDigest(model) {
  const m = model || {};
  const mde = m.mde || {};
  const rot = m.rotation || {};
  const hb = m.holdoutBudget || null;
  const red = m.redundancy || {};
  const f4 = (v) => (svg.isNum(v) ? v.toFixed(4) : null);
  return JSON.stringify({
    found: m.found,
    mde: [mde.floorMeasured, f4(mde.floor), mde.floorStatistic, mde.replicates,
      mde.replicatesSource, mde.usable, mde.formulaN, f4(mde.mde), f4(mde.mdeRelaxed), mde.note],
    noisiest: arr(m.noisiest).map((r) => [r.entryId, f4(r.flipRate), r.slice]),
    dead: arr(m.dead).map((r) => [r.entryId, r.pairs, r.slice]),
    insufficient: arr(m.insufficient).map((r) => [r.entryId, r.pairs, r.slice]),
    runtimeCost: arr(m.runtimeCost).map((r) => [r.entryId, f4(r.runtimeMsMean), r.replicateTotal]),
    holdoutBudget: hb ? [hb.generationId, hb.confirmed, hb.released, hb.budgetTotal,
      hb.budgetRemaining, f4(hb.threshold)] : null,
    rotation: [rot.rotateHoldout, rot.ceiling, rot.evaluated, rot.refreshRecommended],
    redundancy: [red.available, red.note,
      arr(red.clusters).map((c) => [c.judgeName, c.redundantWith.join(',')])],
  });
}

// ---- render ---------------------------------------------------------
//
// Returns ONE <section>. Quiet-precision register: the floor/MDE strip is mono,
// the ranked lists ride the dataTable idiom (no chip vocabulary — §6). Every
// finding is recommend-only; `opts.onEntry` / `opts.reflectHref` /
// `opts.builderHref` wire the (optional) pointers.

// A small recommend-only pointer — an <a> when a href is supplied, else quiet
// text (never a dead link). Keeps the panel merge-safe with the parent router.
function pointer(text, href) {
  if (href) return el('a', { class: 'dn-eh-link', href, text });
  return el('span', { class: 'dn-faint', text });
}

// An entry cell that activates onEntry when supplied (into the eval dossier),
// else a plain mono id — recommend-only, no dead affordance.
function entryCell(entryId, slice, onEntry) {
  const label = truncate(entryId, 28);
  const cls = 'dn-eh-entry' + (slice === 'holdout' ? ' dn-eh-holdout' : '');
  if (typeof onEntry === 'function') {
    const a = el('button', { class: cls + ' dn-eh-entrybtn', type: 'button',
      title: `open ${entryId}` }, [el('span', { text: label })]);
    a.addEventListener('click', () => onEntry(entryId));
    return a;
  }
  return el('span', { class: cls, text: label });
}

function sliceTag(slice) {
  return slice === 'holdout'
    ? el('span', { class: 'dn-faint', text: 'holdout' })
    : el('span', { class: 'dn-faint', text: 'train' });
}

// 1 — THE STRIP: the measured floor + the live MDE ladder (§4.3). Mono + quiet;
//     it prints the formula, the floor, the n and where the n came from — never
//     a bare number.
function replicatesLabel(mde) {
  return mde.replicatesSource ? `replicates (n) · ${mde.replicatesSource}` : 'replicates (n)';
}

function floorLabel(mde) {
  return mde.floorStatistic ? `noise floor · ${mde.floorStatistic}` : 'noise floor';
}

function mdeStrip(mde) {
  const wrap = el('div', { class: 'dn-eh-strip' });
  if (!mde.usable) {
    // Honest empty: the floor is unmeasured, or n < 2 — name the reason, no bound.
    wrap.appendChild(el('div', { class: 'dn-eh-mde-head' }, [
      el('span', { class: 'dn-eh-mde-title', text: 'Minimum detectable effect' }),
      el('span', { class: 'dn-faint dn-eh-mde-note',
        text: mde.note || 'floor unmeasured' }),
    ]));
    if (svg.isNum(mde.floor)) {
      const source = mde.replicatesSource ? ` (${mde.replicatesSource})` : '';
      wrap.appendChild(el('div', { class: 'dn-eh-mono dn-faint',
        text: `floor ${svg.fmt(mde.floor, 4)} · n=${mde.replicates}${source}` }));
    }
    return wrap;
  }
  const strip = el('div', { class: 'dn-eh-stats' }, [
    stat(svg.fmt(mde.floor, 4), floorLabel(mde)),
    stat(String(mde.formulaN), replicatesLabel(mde)),
    stat(svg.fmt(mde.mde, 4), `MDE · α ${svg.fmt(mde.alpha, 2)}`),
    stat(svg.fmt(mde.mdeRelaxed, 4), `MDE · α ${svg.fmt(mde.alphaRelaxed, 2)}`),
  ]);
  wrap.appendChild(strip);
  // The formula line, faint + mono — §4.3's "state the formula + n" rule.
  wrap.appendChild(el('div', { class: 'dn-eh-mono dn-faint dn-eh-formula',
    text: `${mde.formula}  ·  df=${mde.df}, power ${fmtPercent(mde.power)}` }));
  return wrap;
}

// 2 — RANKED NOISY EVALS: descending flip rate, from measurement alone.
function noisiestPanel(rows, mde, opts) {
  if (!rows.length) {
    const reason = mde.floorMeasured
      ? 'no per-entry flip rates measured for this epoch yet.'
      : 'flip rate unmeasured — run the A/A calibration to measure the noise floor.';
    return subsection('Noisiest evals', empty(reason));
  }
  const table = dataTable({
    class: 'dn-board-table dn-eh-table',
    columns: [{ label: 'entry' }, { label: 'flip rate', class: 'dn-num' }, { label: 'slice' }],
    rows: rows.map((r) => ({
      class: r.flipRate > 0 ? 'dn-eh-noisy' : null,
      cells: [
        { el: entryCell(r.entryId, r.slice, opts.onEntry) },
        { class: 'dn-num', text: fmtPercent(r.flipRate) },
        { el: sliceTag(r.slice) },
      ],
    })),
  });
  return subsection('Noisiest evals', table,
    'a high A/A flip rate means the channel disagrees with itself — its verdicts are noise, not signal.');
}

// 3 — DEAD EVALS (§2.3): zero discrimination across the reign, above the
//     minimum-comparisons honesty threshold. Below it → "insufficient", never dead.
function deadPanel(dead, insufficient, opts) {
  const nodes = [];
  if (!dead.length) {
    nodes.push(empty('no dead channels detected — every eval separated at least one pair of candidates.'));
  } else {
    nodes.push(dataTable({
      class: 'dn-board-table dn-eh-table',
      columns: [{ label: 'entry' }, { label: 'comparisons', class: 'dn-num' }, { label: 'slice' }],
      rows: dead.map((r) => ({
        class: 'dn-eh-dead',
        cells: [
          { el: entryCell(r.entryId, r.slice, opts.onEntry) },
          { class: 'dn-num', text: String(r.pairs) },
          { el: sliceTag(r.slice) },
        ],
      })),
    }));
    nodes.push(el('p', { class: 'dn-faint dn-eh-rec' }, [
      el('span', { text: 'a dead channel never separated any two candidates — consider retiring or reworking it in the ' }),
      pointer('board editor', opts.builderHref),
      el('span', { text: '.' }),
    ]));
  }
  if (insufficient.length) {
    nodes.push(el('p', { class: 'dn-faint dn-eh-rec',
      text: `${insufficient.length} eval(s) had too few both-sides matchups to judge discrimination `
        + '— insufficient comparisons, not dead.' }));
  }
  return subsection('Dead evals', ...nodes);
}

// 4 — RUNTIME COST (§2.1): descending mean runtime.
function runtimePanel(rows, opts) {
  if (!rows.length) return subsection('Runtime cost', empty('no runtimes measured for this epoch yet.'));
  const table = dataTable({
    class: 'dn-board-table dn-eh-table',
    columns: [{ label: 'entry' }, { label: 'mean runtime', class: 'dn-num' },
      { label: 'runs', class: 'dn-num' }, { label: 'slice' }],
    rows: rows.map((r) => ({
      cells: [
        { el: entryCell(r.entryId, r.slice, opts.onEntry) },
        { class: 'dn-num', text: fmtDurationMs(r.runtimeMsMean) },
        { class: 'dn-num', text: String(r.replicateTotal) },
        { el: sliceTag(r.slice) },
      ],
    })),
  });
  return subsection('Runtime cost', table,
    'the wall-clock each channel spends — the costliest evals are the first candidates to slim.');
}

// 5 — HOLDOUT BUDGET + ROTATION CADENCE. The Ladder's own accounting + the
//     served refresh-cadence signal, bound rather than re-derived.
function lifecyclePanel(hb, rot, opts) {
  const nodes = [];
  // Holdout ladder budget spent.
  if (hb && (svg.isNum(hb.budgetTotal) || svg.isNum(hb.budgetRemaining))) {
    const spent = svg.isNum(hb.budgetTotal) && svg.isNum(hb.budgetRemaining)
      ? hb.budgetTotal - hb.budgetRemaining : null;
    nodes.push(el('div', { class: 'dn-eh-stats' }, [
      stat(svg.isNum(spent) ? String(spent) : '—', 'holdout budget spent'),
      stat(svg.isNum(hb.budgetRemaining) ? String(hb.budgetRemaining) : '—', 'remaining'),
      stat(svg.isNum(hb.budgetTotal) ? String(hb.budgetTotal) : '—', 'total'),
    ].filter(Boolean)));
  } else {
    nodes.push(empty('no holdout-ladder budget spent yet — appears after the first gated confirmation.'));
  }
  // Rotation cadence status.
  const cadenceText = rot.ceiling == null
    ? `holdout rotation ${rot.rotateHoldout ? 'ON' : 'OFF'} · no cadence ceiling configured`
    : `${rot.evaluated} / ${rot.ceiling} generations mined under this contract`
      + ` · holdout rotation ${rot.rotateHoldout ? 'ON' : 'OFF'}`;
  const cadence = el('div', { class: 'dn-eh-cadence' }, [
    el('span', { class: 'dn-eh-mono', text: cadenceText }),
  ]);
  if (rot.refreshRecommended) {
    cadence.appendChild(el('div', { class: 'dn-bad-t dn-eh-rec' }, [
      el('span', { text: (rot.recommendation || 'contract mined to its cadence ceiling — consider refreshing the board') + ' ' }),
      pointer('open the board editor', opts.builderHref),
    ]));
  }
  nodes.push(cadence);
  return subsection('Holdout budget · rotation cadence', ...nodes);
}

// 6 — REDUNDANCY CLUSTERS (§2.6): from an already-built reflection, else deferred.
function redundancyPanel(red, opts) {
  if (!red.available) {
    return subsection('Redundancy clusters', el('p', { class: 'dn-faint dn-eh-rec' }, [
      el('span', { text: (red.note || 'no reflection built for this epoch') + ' — ' }),
      pointer('run reflect', opts.reflectHref),
      el('span', { text: ' to surface which evals say the same thing.' }),
    ]));
  }
  if (!red.clusters.length) {
    return subsection('Redundancy clusters',
      empty('the reflection found no redundant judges — every channel carries distinct signal.'));
  }
  const table = dataTable({
    class: 'dn-board-table dn-eh-table',
    columns: [{ label: 'judge' }, { label: 'redundant with' }],
    rows: red.clusters.map((c) => ({
      cells: [
        { text: c.judgeName },
        { text: c.redundantWith.join(', ') },
      ],
    })),
  });
  return subsection('Redundancy clusters', table,
    'judges whose verdicts track each other — a candidate for pruning the panel (see reflect).');
}

// A titled sub-block: a quiet subhead + body nodes, with an optional faint note.
function subsection(title, ...children) {
  const kids = children.filter(Boolean);
  // A trailing string arg is treated as the faint explainer note.
  const note = typeof kids[kids.length - 1] === 'string' ? kids.pop() : null;
  const block = el('div', { class: 'dn-eh-sub' }, [
    el('h3', { class: 'dn-eh-subhead', text: title }),
    ...kids,
  ]);
  if (note) block.appendChild(el('p', { class: 'dn-faint dn-eh-note', text: note }));
  return block;
}

// The floor + MDE strip alone (mounts ABOVE the matrix). Returns an empty wrap
// when there is nothing to show, so evals.js's `:empty { display:none }` hides it.
export function renderEvalHealthStrip(model, opts) {
  void opts;
  const m = model || evalHealthModel(null);
  const wrap = el('div', { class: 'dn-evalhealth-strip' });
  if (!m.found) return wrap;
  wrap.appendChild(mdeStrip(m.mde));
  return wrap;
}

// The ranked-lists section alone (mounts BELOW the matrix).
export function renderEvalHealthSection(model, opts) {
  const o = opts || {};
  const m = model || evalHealthModel(null);
  const card = el('div', { class: 'dn-panel dn-evalhealth' });

  if (!m.found) {
    card.appendChild(empty('No indexed evals for this epoch yet — the instrument panel appears after the first round.'));
    return section('Instrument health · the board as a measuring device', card);
  }

  card.appendChild(noisiestPanel(m.noisiest, m.mde, o));
  card.appendChild(deadPanel(m.dead, m.insufficient, o));
  card.appendChild(runtimePanel(m.runtimeCost, o));
  card.appendChild(lifecyclePanel(m.holdoutBudget, m.rotation, o));
  card.appendChild(redundancyPanel(m.redundancy, o));

  return section('Instrument health · the board as a measuring device', card);
}

// The combined strip + section as ONE node — for a single-host mount / the tests.
export function renderEvalHealth(model, opts) {
  const wrap = el('div', { class: 'dn-evalhealth-wrap' });
  wrap.appendChild(renderEvalHealthStrip(model, opts));
  wrap.appendChild(renderEvalHealthSection(model, opts));
  return wrap;
}

// ---- mount (the WS-MATRIX seam) -------------------------------------
//
// `hosts` = { strip, section } (either optional); `matrix` is the raw
// build_eval_matrix payload (we read only `epoch_id`); `ctx` = { navigate, href }.
// Recommend-only pointers are wired best-effort from ctx.href — a route the
// router cannot build is simply omitted (never a dead link).

function _safeHref(ctx, view, params) {
  try {
    if (ctx && typeof ctx.href === 'function') {
      const h = ctx.href(view, params);
      return typeof h === 'string' && h ? h : null;
    }
  } catch (_e) { /* an unknown route degrades to no link */ }
  return null;
}

function _mountOpts(ctx, epoch) {
  let onEntry = null;
  // A ranked-list entry links into its per-entry dossier — the BOARD view
  // (WS-DOSSIER upgrades board.js), the same route the matrix cells click into:
  // ctx.navigate('board', { epochId, entry }) → #/e/<epoch>/board/<entry>.
  if (ctx && typeof ctx.navigate === 'function' && typeof epoch === 'string' && epoch) {
    onEntry = (entryId) => ctx.navigate('board', { epochId: epoch, entry: entryId });
  }
  return {
    // Reflect owns the redundancy / judge detail (instrument.js is a shipped view).
    reflectHref: _safeHref(ctx, 'instrument', { epochId: epoch }),
    // The builder's board editor is where a dead / redundant channel is reworked.
    builderHref: _safeHref(ctx, 'builder', { epochId: epoch }) || _safeHref(ctx, 'builder', {}),
    onEntry,
  };
}

async function _loadModel(epoch) {
  if (typeof epoch !== 'string' || !epoch) return evalHealthModel(null);
  try {
    const res = await fetch(`/api/epoch/${encodeURIComponent(epoch)}/eval-health`);
    if (!res || !res.ok) return evalHealthModel(null);
    return evalHealthModel(await res.json());
  } catch (_e) {
    return evalHealthModel(null);
  }
}

export async function mount(hosts, matrix, ctx) {
  if (!hosts || typeof hosts !== 'object') return;
  const epoch = matrix && typeof matrix === 'object' ? matrix.epoch_id : null;
  const opts = _mountOpts(ctx, epoch);
  const model = await _loadModel(typeof epoch === 'string' ? epoch : null);
  const digest = evalHealthDigest(model);
  // Distinct digest prefixes so the two hosts gate independently. When there are
  // no indexed evals we paint NOTHING — the hosts stay empty so evals.js's
  // `:empty { display:none }` hides both containers (no honest-empty clutter
  // above/below the matrix).
  if (hosts.strip) {
    gatedSwap(hosts.strip, 'eh-strip:' + digest, () =>
      model.found ? [renderEvalHealthStrip(model, opts)] : []);
  }
  if (hosts.section) {
    gatedSwap(hosts.section, 'eh-section:' + digest, () =>
      model.found ? [renderEvalHealthSection(model, opts)] : []);
  }
}

export default mount;
