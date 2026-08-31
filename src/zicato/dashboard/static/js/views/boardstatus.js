// js/views/boardstatus.js — the BOARD-STATUS surface for one epoch.
//
// The runtime counterpart to the tournament builder's "Board & holdout"
// section: it communicates the TRAIN / HOLDOUT split and where + when each
// slice is played. Three panels, all DEFENSIVELY derived from /api/epoch so
// they degrade to honest empty states (never crash) before the overfitting
// `#2` Ladder / `#5` generalization-gap detector land:
//
//   1. THE SPLIT — one STAT LINE (train / holdout / % held out in the shipped
//      stat idiom) with the board-level facts as CHIPS beside it, over the
//      epoch's board as a strip: train (outline) vs holdout (accent fill).
//   2. WHERE / WHEN — the swatch KEY the strip cannot be read without, plus the
//      Ladder budget remaining.
//   3. GENERALIZATION GAP — train_loss vs holdout_loss across the lineage as a
//      pair of sparklines, with the reading (widening / stable) as a verdict.
//
// The panel prints FACTS at value weight and keeps every EXPLANATION one hover
// away — the "?" mark (ui.js `moreMark`, the figCaption idiom) for the
// where/when sentences and the gap explainer, a chip's own hovercard for the
// board_meta wording. Six dim prose lines at one weight was a wall the eye
// skipped; nothing was dropped, it moved behind the affordance.
//
// Per-entry + per-panel detail rides on the shared accessible HOVERCARD
// (hovercard.js — the same popover the rest of Console uses): a transient
// overlay OUTSIDE the digest-gated panel, so it never churns the DOM on a
// no-op SSE heartbeat.

import { el } from '../core/dom.js';
import * as svg from '../svg.js';
import { section, empty, truncate, hovercardBody, stat, chip, moreMark, ENTRY_KIND_LABEL } from '../ui.js';
import { attachHovercard } from '../hovercard.js';

// The doc the popovers point at for "what does this mean" detail.
const DOC_HREF = '/docs/design/OVERFITTING.md';

// ---- model ----------------------------------------------------------
//
// Pure, defensive derivation from the /api/epoch payload. Everything is
// type-guarded: a missing block reads as the honest "nothing yet" shape,
// never a throw. Returns:
//   {
//     split:   { configured, enabled, holdoutFraction, holdoutTags,
//                entries:[{entryId, slice, tag, weight, kind, tags}],
//                trainCount, holdoutCount, total },
//     meta:    { judgeOnly, disableDrift:[…] } | null,
//     ladder:  { present, confirmed, trainScalar, holdoutScalar,
//                released, budgetTotal, budgetRemaining, threshold,
//                generationId } | null,
//     gap:     { points:[{gen, train, holdout, gap}], hasAny, widening },
//   }
export function boardStatusModel(ep) {
  const e = ep && typeof ep === 'object' ? ep : {};
  return {
    split: splitModel(e),
    meta: metaModel(e),
    ladder: ladderModel(e),
    gap: gapModel(e),
  };
}

// Per-entry `kind` + `tags` indexed by entry_id off the RAW board. The
// server's board_split carries membership only (slice / why-held-out tag /
// weight), so the join is CLIENT-LOCAL — the same `ep.board` the fallback
// branch below already reads, no second fetch and no server change.
function boardIndex(ep) {
  const byId = new Map();
  const board = Array.isArray(ep.board) ? ep.board : [];
  for (const b of board) {
    if (!b || typeof b !== 'object') continue;
    const id = b.entry_id != null ? String(b.entry_id) : '';
    if (!id) continue;
    byId.set(id, {
      kind: typeof b.kind === 'string' && b.kind ? b.kind : null,
      tags: Array.isArray(b.tags) ? b.tags.filter((t) => typeof t === 'string') : [],
    });
  }
  return byId;
}

function splitModel(ep) {
  const byId = boardIndex(ep);
  const joined = (id) => byId.get(id) || { kind: null, tags: [] };
  const bs = ep.board_split && typeof ep.board_split === 'object' ? ep.board_split : null;
  // Prefer the server-computed split; fall back to "everything is train"
  // derived from the raw board so the strip still draws on a pre-feature
  // payload (board_split absent).
  let entries = [];
  if (bs && Array.isArray(bs.entries)) {
    entries = bs.entries.map((r) => ({
      entryId: r && r.entry_id != null ? String(r.entry_id) : '',
      slice: r && r.slice === 'holdout' ? 'holdout' : 'train',
      tag: r && typeof r.tag === 'string' ? r.tag : null,
      weight: r && svg.isNum(r.weight) ? r.weight : null,
    })).filter((r) => r.entryId).map((r) => Object.assign(r, joined(r.entryId)));
  } else {
    const board = Array.isArray(ep.board) ? ep.board : [];
    entries = board.map((b) => ({
      entryId: String((b && b.entry_id != null ? b.entry_id : '') || ''),
      slice: 'train',
      tag: null,
      weight: b && svg.isNum(b.weight) ? b.weight : null,
    })).filter((r) => r.entryId).map((r) => Object.assign(r, joined(r.entryId)));
  }
  const holdoutCount = entries.filter((r) => r.slice === 'holdout').length;
  const trainCount = entries.length - holdoutCount;
  return {
    configured: !!(bs && bs.configured),
    enabled: !!(bs && bs.enabled),
    holdoutFraction: bs && svg.isNum(bs.holdout_fraction) ? bs.holdout_fraction : 0,
    holdoutTags: bs && Array.isArray(bs.holdout_tags)
      ? bs.holdout_tags.filter((t) => typeof t === 'string') : [],
    entries,
    trainCount,
    holdoutCount,
    total: entries.length,
  };
}

// The board-level `board_meta` header (BOARD-FORMAT §1.0) as the epoch view
// serves it: the drift kinds suppressed for every entry + the judge-only flag.
// Both fold into the contract hash, so they describe how THIS board is scored —
// a runtime surface that omits them draws a board the operator cannot recognise
// from the one the builder authored. Absent / fully default ⇒ null (the server
// omits the key for a default board, and there is nothing to say about it).
function metaModel(ep) {
  const m = ep.board_meta && typeof ep.board_meta === 'object' ? ep.board_meta : null;
  if (!m) return null;
  const disableDrift = Array.isArray(m.disable_drift)
    ? m.disable_drift.filter((t) => typeof t === 'string') : [];
  const judgeOnly = m.judge_only === true;
  if (!disableDrift.length && !judgeOnly) return null;
  return { judgeOnly, disableDrift };
}

function ladderModel(ep) {
  const h = ep.holdout && typeof ep.holdout === 'object' ? ep.holdout : null;
  if (!h) return null;
  return {
    present: true,
    generationId: h.generation_id != null ? String(h.generation_id) : null,
    confirmed: typeof h.confirmed === 'boolean' ? h.confirmed : null,
    trainScalar: svg.isNum(h.train_scalar) ? h.train_scalar : null,
    holdoutScalar: svg.isNum(h.holdout_scalar) ? h.holdout_scalar : null,
    released: !!h.ladder_released,
    budgetTotal: svg.isNum(h.ladder_budget_total) ? h.ladder_budget_total : null,
    budgetRemaining: svg.isNum(h.ladder_budget_remaining) ? h.ladder_budget_remaining : null,
    threshold: svg.isNum(h.threshold) ? h.threshold : null,
  };
}

// Generalization-gap series across the lineage. Each generation/experiment
// record (from `#5`) MAY carry train_loss / holdout_loss / generalization_gap;
// they are absent until the detector lands, so every read is type-guarded and
// a record with neither loss contributes a null point (the sparkline draws a
// gap rather than crashing). Ordered by generation id for a stable trend.
function gapModel(ep) {
  const exps = Array.isArray(ep.experiments) ? ep.experiments : [];
  const points = [];
  for (const x of exps) {
    if (!x || typeof x !== 'object') continue;
    const train = svg.isNum(x.train_loss) ? x.train_loss : null;
    const holdout = svg.isNum(x.holdout_loss) ? x.holdout_loss : null;
    let gap = svg.isNum(x.generalization_gap) ? x.generalization_gap : null;
    if (gap == null && train != null && holdout != null) gap = holdout - train;
    if (train == null && holdout == null && gap == null) continue;
    points.push({ gen: String(x.generation_id || ''), train, holdout, gap });
  }
  points.sort((a, b) => (a.gen < b.gen ? -1 : a.gen > b.gen ? 1 : 0));
  const hasAny = points.length > 0;
  // "Widening" = the last finite gap exceeds the first finite gap (the
  // overfitting tell). Null when fewer than two finite gaps exist.
  const gaps = points.map((p) => p.gap).filter(svg.isNum);
  const widening = gaps.length >= 2 ? gaps[gaps.length - 1] > gaps[0] : null;
  return { points, hasAny, widening };
}

// A stable digest of the model so the host can DIGEST-GATE the render — a
// no-op SSE heartbeat that changes none of these never rebuilds the DOM.
export function boardStatusDigest(model) {
  const m = model || {};
  const s = m.split || {};
  const meta = m.meta || null;
  const l = m.ladder || null;
  const g = m.gap || {};
  return JSON.stringify({
    split: [
      s.configured, s.enabled, s.holdoutFraction, s.trainCount, s.holdoutCount,
      // `kind` + `tags` ride the per-entry leg: they are RENDERED (the entry
      // hovercard), so a board edit that retypes or retags an entry has to
      // repaint — an unfolded rendered field is the no-repaint bug class.
      (s.entries || []).map((r) => [r.entryId, r.slice, r.tag, r.weight, r.kind, r.tags || []]),
      (s.holdoutTags || []),
    ],
    // the board_meta header, likewise rendered beside the counts row.
    meta: meta ? [meta.judgeOnly, meta.disableDrift] : null,
    ladder: l ? [
      l.confirmed, l.released, l.budgetTotal, l.budgetRemaining,
      svg.isNum(l.threshold) ? l.threshold.toFixed(4) : null,
      svg.isNum(l.trainScalar) ? l.trainScalar.toFixed(4) : null,
      svg.isNum(l.holdoutScalar) ? l.holdoutScalar.toFixed(4) : null,
    ] : null,
    gap: (g.points || []).map((p) => [
      p.gen,
      svg.isNum(p.train) ? p.train.toFixed(4) : null,
      svg.isNum(p.holdout) ? p.holdout.toFixed(4) : null,
      svg.isNum(p.gap) ? p.gap.toFixed(4) : null,
    ]),
  });
}

// ---- render ---------------------------------------------------------
//
// Returns ONE <section> node. `opts.onEntry(entryId)` (optional) makes each
// chip activate to that board's cross-candidate view.
export function renderBoardStatus(model, opts) {
  const o = opts || {};
  const m = model || boardStatusModel(null);
  const card = el('div', { class: 'dn-panel dn-boardstatus' });

  card.appendChild(splitPanel(m.split, m.meta, o));
  card.appendChild(legendPanel(m.split, m.ladder));
  card.appendChild(gapPanel(m.gap));

  return section('Board status · train / holdout split', card);
}

// 1 — THE SPLIT: one stat line + the board-level fact chips, over the board as
//     a chip grid (train = outline, holdout = accent fill).
function splitPanel(split, meta, opts) {
  const wrap = el('div', { class: 'dn-bs-split' });
  const frac = split.total > 0 ? split.holdoutCount / split.total : 0;

  // The three counts an operator actually reads, in the shipped stat idiom
  // (mono value over a small key) — the same tiles the epoch header uses, so
  // the numbers carry value weight instead of hiding inside a sentence. A
  // board with no entries prints no tiles: `0 · 0 · 0%` would be three numbers
  // restating the empty state below them.
  const head = el('div', { class: 'dn-row dn-bs-statline' },
    split.total === 0 ? [] : [
      stat(String(split.trainCount), 'train'),
      stat(String(split.holdoutCount), 'holdout'),
      stat(`${(frac * 100).toFixed(0)}%`, 'held out'),
    ]);
  const facts = factChips(split, meta);
  if (facts.length) head.appendChild(el('div', { class: 'dn-bs-chips' }, facts));
  if (head.childNodes.length) wrap.appendChild(head);

  if (split.total === 0) {
    wrap.appendChild(empty('No board entries for this epoch yet.'));
    return wrap;
  }

  const grid = el('div', { class: 'dn-bs-grid' });
  for (const r of split.entries) {
    const held = r.slice === 'holdout';
    const chip = el('span', {
      class: 'dn-bs-chip' + (held ? ' dn-bs-holdout' : ' dn-bs-train'),
      tabindex: '0',
      text: truncate(r.entryId, 16),
    });
    attachHovercard(chip, () => entryCard(r));
    if (typeof opts.onEntry === 'function') {
      chip.style.cursor = 'pointer';
      chip.addEventListener('click', () => opts.onEntry(r.entryId));
      chip.addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); opts.onEntry(r.entryId); }
      });
    }
    grid.appendChild(chip);
  }
  wrap.appendChild(grid);
  return wrap;
}

// The board-level FACTS as chips beside the stat line: whether a holdout is
// configured at all, and the `board_meta` header (BOARD-FORMAT §1.0) — the
// judge-only flag + the drift kinds suppressed for every entry. Both fold into
// the contract hash, so they describe how THIS board is scored; each was a
// sentence at prose weight, and a sentence per flag is what made this panel a
// wall. The chip carries the fact; the wording rides its hovercard.
function factChips(split, meta) {
  const chips = [];
  // only worth saying of a board that HAS entries — "no holdout" over an empty
  // board is a fact about nothing.
  if (split.total > 0 && !split.configured) {
    chips.push(factChip('boardfact', 'no holdout',
      'no holdout configured — every entry is train'));
  }
  if (meta && meta.judgeOnly) {
    // WORDING IS THE BUILDER'S, VERBATIM (views/builder.js's board-metadata
    // panel) — the two surfaces name the same flag, so they must not describe
    // it in two different sentences.
    chips.push(factChip('judgeonly', 'judge-only',
      'judge-only board — score on judges alone, no steering'));
  }
  const drift = (meta && meta.disableDrift) || [];
  if (drift.length) {
    chips.push(factChip('boardfact',
      'drift suppressed' + (drift.length > 1 ? ' ×' + drift.length : ''),
      'drift suppressed for every entry · ' + drift.join(', ')));
  }
  return chips;
}

// One fact chip in the shipped chip vocabulary. attachHovercard makes the chip
// focusable, so the sentence is reachable by keyboard — on this surface the
// hovercard is the ONLY copy of that wording.
function factChip(tone, word, sentence) {
  const node = chip(tone, word, 'dn-bs-fact');
  attachHovercard(node, sentence);
  return node;
}

// The per-entry hovercard content (entry id, kind, slice, weight, tags,
// why-held-out). `kind` and `tags` are the client-local join off `ep.board` —
// membership + provenance, never a per-entry SCALAR: breaking the holdout out
// by measurement on every page load would be an ungoverned query against it
// (eval_view.py excludes holdout entries from facet aggregation for exactly
// that reason).
function entryCard(r) {
  const lines = [
    el('div', { class: 'dn-hc-title', text: r.entryId }),
  ];
  if (r.kind) lines.push(el('div', { class: 'dn-hc-row', text: `kind: ${ENTRY_KIND_LABEL[r.kind] || r.kind}` }));
  lines.push(el('div', { class: 'dn-hc-row', text: r.slice === 'holdout' ? 'slice: holdout' : 'slice: train' }));
  if (svg.isNum(r.weight)) lines.push(el('div', { class: 'dn-hc-row', text: `weight: ${svg.fmt(r.weight, 2)}` }));
  if (Array.isArray(r.tags) && r.tags.length) {
    lines.push(el('div', { class: 'dn-hc-row', text: `tags: ${r.tags.join(', ')}` }));
  }
  if (r.slice === 'holdout') {
    lines.push(el('div', { class: 'dn-hc-row', text: r.tag
      ? `held out by tag “${r.tag}” — played at the gate's confirmation step only`
      : 'held out by fraction — played at the gate\'s confirmation step only' }));
  } else {
    lines.push(el('div', { class: 'dn-hc-row', text: 'played every round; the only slice the proposer sees' }));
  }
  return hovercardBody(lines);
}

// 2 — WHERE / WHEN: the swatch key + the Ladder budget remaining, on one row.
//
// The KEY stays visible and the two where/when SENTENCES collapse behind the
// "?": figCaption's own rule is that a swatch key the figure cannot be read
// without belongs beside the figure, while its explanation does not. The chip
// grid above is unreadable without knowing which fill means held out; it is
// perfectly readable without being told when each slice is played.
function legendPanel(split, ladder) {
  const wrap = el('div', { class: 'dn-bs-legend' });

  const trainSwatch = el('span', { class: 'dn-bs-sw dn-bs-train' });
  const holdSwatch = el('span', { class: 'dn-bs-sw dn-bs-holdout' });
  const key = el('div', { class: 'dn-bs-legrow' }, [
    trainSwatch, el('span', { class: 'dn-bs-legtxt', text: 'train' }),
    holdSwatch, el('span', { class: 'dn-bs-legtxt', text: 'holdout' }),
  ]);
  key.appendChild(moreMark([
    'train → every round · proposer-visible',
    'holdout → gated confirmation only · proposer never sees it',
  ], { title: 'where and when each slice is played' }));
  wrap.appendChild(key);

  // The Ladder budget readout. Graceful "—" + "after a run" when no decision
  // has recorded a holdout step yet (the `#2` Ladder absent / null).
  const hasBudget = ladder && (svg.isNum(ladder.budgetRemaining) || svg.isNum(ladder.budgetTotal));
  const budgetText = hasBudget
    ? `${svg.isNum(ladder.budgetRemaining) ? ladder.budgetRemaining : '—'}`
      + ` / ${svg.isNum(ladder.budgetTotal) ? ladder.budgetTotal : '—'}`
    : '—';
  const budgetEl = el('div', { class: 'dn-bs-ladder' }, [
    el('span', { class: 'dn-bs-ladder-lab', text: 'ladder budget remaining' }),
    el('span', { class: 'dn-bs-ladder-val', text: budgetText }),
    hasBudget
      ? null
      : el('span', { class: 'dn-faint', text: 'after a run' }),
  ].filter(Boolean));
  attachHovercard(budgetEl, () => ladderCard(ladder));
  wrap.appendChild(budgetEl);
  return wrap;
}

function ladderCard(ladder) {
  const lines = [
    el('div', { class: 'dn-hc-title', text: 'Holdout ladder' }),
    el('div', { class: 'dn-hc-row', text:
      'The holdout is confirmed at the gate under a Ladder budget — each confirmation'
      + ' spends one rung, so a champion cannot be tuned against the holdout indefinitely.' }),
  ];
  if (ladder) {
    if (svg.isNum(ladder.budgetRemaining) && svg.isNum(ladder.budgetTotal)) {
      lines.push(el('div', { class: 'dn-hc-row', text:
        `budget: ${ladder.budgetRemaining} of ${ladder.budgetTotal} rungs remaining` }));
    }
    if (svg.isNum(ladder.trainScalar) || svg.isNum(ladder.holdoutScalar)) {
      lines.push(el('div', { class: 'dn-hc-row', text:
        `train ${svg.fmt(ladder.trainScalar)} · holdout ${svg.fmt(ladder.holdoutScalar)}` }));
    }
    if (svg.isNum(ladder.threshold)) {
      lines.push(el('div', { class: 'dn-hc-row', text: `threshold: ${svg.fmt(ladder.threshold)}` }));
    }
    if (ladder.confirmed != null) {
      lines.push(el('div', { class: 'dn-hc-row', text:
        ladder.confirmed ? 'last decision: confirmed on the holdout' : 'last decision: NOT confirmed' }));
    }
  } else {
    lines.push(el('div', { class: 'dn-hc-row dn-faint', text: 'No holdout step recorded yet.' }));
  }
  lines.push(el('a', { class: 'dn-hc-link', href: DOC_HREF, text: 'overfitting design →' }));
  return hovercardBody(lines);
}

// 3 — GENERALIZATION-GAP TREND: train vs holdout loss across the lineage as a
//     pair of sparklines. The numbers stay; what a widening gap MEANS collapses
//     behind the "?" — the verdict line below already reports this run's
//     reading, so the definition is reference material rather than news.
function gapPanel(gap) {
  const wrap = el('div', { class: 'dn-bs-gap' });
  const head = el('div', { class: 'dn-bs-gap-head' }, [
    el('span', { text: 'generalization gap · train vs holdout loss' }),
  ]);
  head.appendChild(moreMark(() => hovercardBody([
    el('div', { class: 'dn-hc-title', text: 'Generalization gap' }),
    el('div', { class: 'dn-hc-row', text:
      'The gap is holdout loss minus train loss. A champion that keeps improving'
      + ' on train while the holdout stalls or worsens is overfitting the train slice:'
      + ' a WIDENING gap (holdout loss pulling above train) = overfitting.' }),
    el('a', { class: 'dn-hc-link', href: DOC_HREF, text: 'overfitting design →' }),
  ]), { title: 'what a widening gap means' }));
  wrap.appendChild(head);

  if (!gap.hasAny) {
    wrap.appendChild(empty('No train / holdout loss recorded yet — the generalization-gap detector reports after a run.'));
    return wrap;
  }

  const trainVals = gap.points.map((p) => p.train);
  const holdoutVals = gap.points.map((p) => p.holdout);
  wrap.appendChild(el('div', { class: 'dn-bs-gap-rows' }, [
    gapRow('train', trainVals, 'dn-bs-line-train'),
    gapRow('holdout', holdoutVals, 'dn-bs-line-holdout'),
  ]));

  if (gap.widening != null) {
    wrap.appendChild(el('div', {
      class: 'dn-bs-verdict ' + (gap.widening ? 'dn-bad' : 'dn-good'),
      text: gap.widening
        ? 'gap is widening — watch for overfitting'
        : 'gap is stable or narrowing — generalizing well',
    }));
  }
  return wrap;
}

function gapRow(label, values, lineClass) {
  const spark = svg.sparkline({ values, goodDirection: 'down', endDot: true });
  // theme the line via the row's wrapper class (the spark inherits dn-spark-*).
  return el('div', { class: 'dn-bs-gap-row ' + lineClass }, [
    el('span', { class: 'dn-bs-gap-lab', text: label }),
    el('span', { class: 'dn-bs-gap-spark' }, [spark]),
  ]);
}
