// js/views/traces.js — the Traces surface (imported foreign trajectories).
//
// TRAJECTORY-UI.md §2.1. The trajectory-bootstrap engine imports foreign agent
// traces, mines adverse-signal episodes from them, and drafts board entries; this
// view VISUALISES that — one information-dense trajectory strip per trace, over
// the reconstructed conversation, with the mined episodes bracketed and cross-
// linked to the suggestions they motivated. Reflection-scoped (the persisted
// `imported/*.json` + `suggestions.json` live under a mint-mode reflection dir),
// so it navigates the same reflection-picker idiom the Instrument lens speaks,
// keyed by the route depth (#/e/<epochId>/traces[/<reflectionId>[/<traceId>]]):
//
//   * LANDING (no reflection)         — a dataTable of the epoch's reflections.
//   * LIST    (reflectionId)          — one row per imported trace: the compact
//                                       strip + source/dialect caption + counts.
//   * DETAIL  (reflectionId+traceId)  — the full strip over the reconstructed
//                                       conversation, the episode anchors linking
//                                       strip spans ↔ conversation ↔ suggestions.
//
// SERVER AUTHORITY (DQ1). The reader pre-computes the strip-model (normalized
// mark/tick/budget/episode geometry); this view DRAWS it via svg.trajectoryStrip
// and derives NO domain math. Every number is the reader's.
//
// RENDER DISCIPLINE. Digest-gated via renderView: a no-op SSE beat (identical
// content digest) rebuilds ZERO DOM (the flashing-render bug class). The trace
// list + the reconstructed conversation each scroll inside their OWN
// dn-table-scroll / dn-transcript host — the page body never scrolls. The
// episode click-focus is a TRANSIENT class toggle outside the gated render (the
// hovercard idiom), never a repaint. Metadata (dialect, counts, the honest
// reconstruction note) is a dn-faint caption, never a chip (quiet precision).
//
// REUSE, DON'T FORK. The strip is svg.trajectoryStrip (shared with WS-SUGVIZ's
// provenance mini-strip); the reconstructed conversation reuses board.js's
// exported buildTurnNode (the transcript turn vocabulary).

import { el } from '../core/dom.js';
import * as D from '../data.js';
import * as svg from '../svg.js';
import { section, empty, renderView, dataTable } from '../ui.js';
import { buildTurnNode } from './board.js';

// Foreign traces carry no per-seq annotations, so the shared turn builder gets
// an empty annotation map (it reads `annBySeq.get(seq)` → undefined → no annots).
const NO_ANNOTATIONS = new Map();

// ---- entry point ----------------------------------------------------

export async function render(host, ctx, params) {
  const p = params || {};
  const routeEpoch = p.epochId || null;
  const reflectionId = p.reflectionId || null;
  const traceId = p.traceId || null;

  await renderView(host, ctx, {
    loading: 'Reading traces…',
    epoch: true, routeEpoch, title: 'Traces',
    emptyText: 'No current epoch — Traces reads one epoch’s imported foreign trajectories.',
    load: async ({ epochId }) => {
      if (reflectionId && traceId) {
        const detail = await D.trace(reflectionId, traceId);
        return { mode: 'detail', epochId, reflectionId, traceId, detail };
      }
      if (reflectionId) {
        const list = await D.traces(reflectionId);
        return { mode: 'list', epochId, reflectionId, list };
      }
      const refl = await D.reflections(epochId);
      return { mode: 'landing', epochId, list: refl };
    },
    digest: (d) => digestFor(d),
    build: (d) => buildFor(d, ctx),
  });
}

// ---- the timestamp-free content digest (the no-flash guarantee) -----
//
// Folds ONLY structural/content data. A completed reflection's imported traces
// are immutable, so a re-fetch yields an identical digest and gatedSwap writes
// nothing. The episode click-focus is a transient DOM class toggle (not folded),
// so focusing an episode never perturbs the digest / triggers a repaint.
function digestFor(d) {
  if (d.mode === 'landing') {
    const items = ((d.list && d.list.reflections) || []).map((r) => [
      r.reflection_id, r.created_at, r.mode, !!r.executed,
    ]);
    return JSON.stringify({ m: 'landing', e: d.epochId, items });
  }
  if (d.mode === 'list') {
    const l = d.list || {};
    const rows = (Array.isArray(l.traces) ? l.traces : []).map((t) => [
      t.trace_id, t.source_file, t.dialect,
      t.turn_counts && t.turn_counts.total, t.episode_count,
      (t.strip_model && Array.isArray(t.strip_model.signals)) ? t.strip_model.signals.length : 0,
      svg.trajectoryStripDigest(t.strip_model, { compact: true }),
    ]);
    return JSON.stringify({ m: 'list', id: d.reflectionId, found: !!l.found, count: l.trace_count, rows });
  }
  // detail
  const x = d.detail || {};
  const turns = (Array.isArray(x.turns) ? x.turns : []).map((t) => [t.index, t.role, (t.text || '').length, !!t.truncated]);
  const eps = (Array.isArray(x.episodes) ? x.episodes : []).map((e) => [
    e.episode_id, e.episode_type, e.tone, e.glyph,
    e.span && e.span.anchor, e.span && e.span.x0, e.span && e.span.x1,
    (e.suggestion_ids || []).join(','), e.summary,
  ]);
  return JSON.stringify({
    m: 'detail', id: d.reflectionId, trace: d.traceId, found: !!x.found,
    src: x.source_file, dialect: x.dialect,
    counts: x.signal_counts || {}, note: x.reconstruction_note,
    strip: svg.trajectoryStripDigest(x.strip_model, {}),
    turns, eps,
  });
}

function buildFor(d, ctx) {
  if (d.mode === 'landing') return buildLanding(d, ctx);
  if (d.mode === 'list') return buildList(d, ctx);
  return buildDetail(d, ctx);
}

// ====================================================================
// LANDING — the epoch's reflections (the reflection picker idiom).
// ====================================================================
function buildLanding(d, ctx) {
  const nodes = [];
  nodes.push(el('div', { class: 'dn-pagehead' }, [
    el('h1', { class: 'dn-h1', text: 'Traces · imported trajectories' }),
    el('p', { class: 'dn-lede', text: 'The trajectory-bootstrap engine imports foreign agent traces and mines adverse-signal episodes from them to draft board entries. Pick a reflection to read its imported traces as timeline strips over the reconstructed conversation.' }),
  ]));

  const items = (d.list && Array.isArray(d.list.reflections)) ? d.list.reflections : [];
  if (!items.length) {
    nodes.push(section('Reflections', el('div', { class: 'dn-panel' }, [
      empty('No reflections for this epoch yet — imported traces live under a reflection.'),
    ])));
    return nodes;
  }

  const table = dataTable({
    class: 'dn-board-table dn-trace-list',
    columns: [{ label: 'reflection' }, { label: 'created' }, { label: 'mode' }],
    rows: items.map((r) => ({
      cells: [
        { el: el('a', { class: 'dn-trace-link dn-mono', href: ctx.href('traces', { epochId: d.epochId, reflectionId: r.reflection_id }), text: r.reflection_id }) },
        { text: r.created_at || '—', class: 'dn-mono' },
        { text: r.mode || '—' },
      ],
    })),
  });
  nodes.push(section('Reflections', el('div', { class: 'dn-panel dn-table-scroll' }, [table])));
  return nodes;
}

// ====================================================================
// LIST — one row per imported trace (the strip thumbnail + caption).
// ====================================================================
function buildList(d, ctx) {
  const l = d.list || {};
  const nodes = [];
  nodes.push(el('div', { class: 'dn-pagehead' }, [
    el('h1', { class: 'dn-h1' }, ['Traces · ', el('span', { class: 'dn-mono', text: d.reflectionId })]),
    el('p', { class: 'dn-lede', text: 'Each imported foreign trajectory as an information-dense strip — the turn lane, the adverse-signal cluster, the shaded cost budget, and the mined episodes. Open a trace to read its reconstructed conversation with the episode anchors.' }),
  ]));

  const items = Array.isArray(l.traces) ? l.traces : [];
  if (!l.found || !items.length) {
    nodes.push(section('Imported traces', el('div', { class: 'dn-panel' }, [
      empty(l.found === false
        ? 'No such reflection (it may not be indexed yet).'
        : 'No imported traces for this reflection — it was created without a foreign-trace directory, or the import found none.'),
    ])));
    return nodes;
  }

  const listWrap = el('div', { class: 'dn-trace-rows' });
  for (const t of items) listWrap.appendChild(traceRow(t, d, ctx));
  nodes.push(section('Imported traces', listWrap));
  return nodes;
}

function traceRow(t, d, ctx) {
  const strip = (t.strip_model && Array.isArray(t.strip_model.signals)) ? t.strip_model : {};
  const nSignals = Array.isArray(strip.signals) ? strip.signals.length : 0;
  const total = (t.turn_counts && Number.isFinite(t.turn_counts.total)) ? t.turn_counts.total : 0;
  const nEp = Number.isFinite(t.episode_count) ? t.episode_count : 0;
  const href = ctx.href('traces', { epochId: d.epochId, reflectionId: d.reflectionId, traceId: t.trace_id });

  const row = el('div', { class: 'dn-panel dn-trace-row' });
  row.appendChild(el('div', { class: 'dn-trace-row-head' }, [
    el('a', { class: 'dn-trace-link dn-mono dn-trace-row-file', href, text: t.source_file || t.trace_id }),
    // dialect + counts are CAPTIONS, never chips (quiet precision).
    el('span', { class: 'dn-faint dn-trace-row-meta', text: (t.dialect || 'dialect —') + ' · ' + plural(total, 'turn') + ' · ' + plural(nSignals, 'signal') + ' · ' + plural(nEp, 'episode') }),
  ]));
  // the compact strip — clicking an episode opens the trace detail (its
  // provenance chain is explorable there).
  row.appendChild(svg.trajectoryStrip(strip, {
    compact: true,
    onFocusEpisode: () => ctx.navigate('traces', { epochId: d.epochId, reflectionId: d.reflectionId, traceId: t.trace_id }),
  }));
  return row;
}

// ====================================================================
// DETAIL — the full strip over the reconstructed conversation, with the
// episode anchors cross-linking strip spans ↔ conversation ↔ suggestions.
// ====================================================================
function buildDetail(d, ctx) {
  const x = d.detail || {};
  const nodes = [];
  nodes.push(el('div', { class: 'dn-pagehead' }, [
    el('h1', { class: 'dn-h1' }, ['Trace · ', el('span', { class: 'dn-mono', text: d.traceId })]),
    el('p', { class: 'dn-lede', text: 'One imported foreign trajectory — the strip up top, the reconstructed conversation below, and the mined episodes that motivated the drafted board entries.' }),
  ]));
  if (!x.found) {
    nodes.push(section('Trace', el('div', { class: 'dn-panel' }, [empty('No such trace (it may not be indexed under this reflection).')])));
    return nodes;
  }

  // identity as ONE dn-faint caption (metadata is a caption, never a chip).
  const bad = Number.isFinite(x.malformed_line_count) ? x.malformed_line_count : 0;
  nodes.push(caption(
    (x.source_file || '—') + ' · ' + (x.dialect || 'dialect —') + ' · ' + plural(x.line_count || 0, 'line')
    + (bad ? ' · ' + plural(bad, 'malformed line') : ''),
  ));

  // Build the two cross-linked hosts up front so the click-focus closure can
  // reference them (transient DOM toggle, no repaint).
  const episodes = Array.isArray(x.episodes) ? x.episodes : [];
  const turns = Array.isArray(x.turns) ? x.turns : [];
  const convHost = el('div', { class: 'dn-trace-conv dn-transcript dn-xscript-scroll', 'data-scroll-side': 'left' });
  const epHost = el('div', { class: 'dn-trace-eps' });

  // The focus interaction: highlight the clicked episode's anchor row; a
  // behavioral (lane-anchored) episode highlights the whole conversation (it
  // spans the lane); a signal episode has NO honest turn span (the aggregate
  // signal has no timeline position), so it highlights its row only. Transient
  // class toggles — outside the digest-gated render (the hovercard idiom).
  const byId = new Map(episodes.map((e) => [String(e.episode_id), e]));
  const focus = (epId) => {
    const eps = epHost.querySelectorAll ? epHost.querySelectorAll('[data-episode-id]') : [];
    for (const r of eps) {
      if (r.classList) r.classList.toggle('dn-trace-ep-on', r.getAttribute('data-episode-id') === String(epId));
    }
    const e = byId.get(String(epId));
    const laneWide = !!(e && e.span && e.span.anchor === 'lane');
    if (convHost.classList) convHost.classList.toggle('dn-trace-conv-focus', laneWide);
    const active = epHost.querySelector ? epHost.querySelector('.dn-trace-ep-on') : null;
    if (active && typeof active.scrollIntoView === 'function') active.scrollIntoView({ block: 'nearest' });
  };

  // the full strip (hero size) — clicking an episode focuses it.
  nodes.push(section('Strip', el('div', { class: 'dn-panel dn-trace-strip' }, [
    svg.trajectoryStrip(x.strip_model || {}, { onFocusEpisode: (epId) => focus(epId) }),
    caption('turn lane (user above / agent below the baseline, width ∝ √text length, capped) · adverse-signal cluster (aggregate counts, not real timeline positions) · shaded cost budget · bracketed mined episodes'),
  ])));

  // the episode anchors — each links the strip span ↔ the conversation ↔ the
  // suggestions it motivated (href into the builder inbox).
  if (!episodes.length) {
    epHost.appendChild(empty('No mined episodes for this trace — the import found no adverse-signal or behavioral episode to draft from.'));
  } else {
    for (const e of episodes) epHost.appendChild(episodeAnchor(e, ctx, focus));
  }
  nodes.push(section('Episodes · trace region → episode → suggestion', epHost));

  // the reconstructed conversation (own-container scroll) + the honest note.
  nodes.push(section('Reconstructed conversation', el('div', { class: 'dn-panel dn-trace-convpanel' }, [
    turns.length
      ? convHost
      : empty('No turns reconstructed for this trace.'),
    x.reconstruction_note ? caption(x.reconstruction_note) : null,
  ].filter(Boolean))));
  if (turns.length) for (const t of turns) convHost.appendChild(buildTurnNode(t, NO_ANNOTATIONS));

  return nodes;
}

function episodeAnchor(e, ctx, focus) {
  const tone = stripTone(e.tone);
  const sugs = Array.isArray(e.suggestion_ids) ? e.suggestion_ids : [];
  const anchor = e.span && e.span.anchor;
  const row = el('div', {
    class: 'dn-trace-ep dn-trace-ep-' + tone, 'data-episode-id': e.episode_id || '',
    tabindex: '0', role: 'button', title: 'focus this episode on the strip + conversation',
  });
  row.addEventListener('click', () => focus(e.episode_id));
  row.addEventListener('keydown', (ev) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault && ev.preventDefault(); focus(e.episode_id); } });

  row.appendChild(el('div', { class: 'dn-trace-ep-head' }, [
    el('span', { class: 'dn-trace-ep-glyph dn-strip-t-' + tone, 'aria-hidden': 'true', text: e.glyph || '○' }),
    el('span', { class: 'dn-trace-ep-sum', text: e.summary || (e.signal_kind || e.episode_type || 'episode') }),
  ]));

  // honest anchor note: a signal episode has no turn span (aggregate signal);
  // a behavioral episode brackets the whole conversation.
  const noteBits = [];
  if (anchor === 'lane') noteBits.push('brackets the whole conversation');
  else if (anchor === 'signal') noteBits.push('anchored to an aggregate signal — no single turn position');
  if (Number.isFinite(e.severity_rank)) noteBits.push('severity ' + e.severity_rank);
  if (noteBits.length) row.appendChild(caption(noteBits.join(' · ')));

  // the linked suggestions — inline links into the builder inbox (recommend-only:
  // every affordance terminates at a builder draft the operator seals).
  if (sugs.length) {
    const kids = ['drafted · '];
    sugs.forEach((sid, i) => {
      if (i) kids.push(' · ');
      kids.push(el('a', {
        class: 'dn-trace-ep-sug dn-mono', href: ctx.href('builder', {}),
        title: 'open the builder inbox to review this suggestion', text: sid,
      }));
    });
    row.appendChild(el('p', { class: 'dn-faint dn-trace-ep-sugs' }, kids));
  } else {
    row.appendChild(caption('no suggestion drafted from this episode'));
  }
  return row;
}

// ---- small local helpers --------------------------------------------

// The console's quiet-caption treatment — the ONE home for metadata + legends.
function caption(text) { return el('p', { class: 'dn-faint dn-trace-cap', text: String(text) }); }

// tone → the shared strip tone suffix (bad/caution/neutral/accent), the §3.5
// table's vocabulary; display colouring only.
function stripTone(tone) {
  const t = String(tone == null ? 'neutral' : tone);
  if (t === 'bad' || t === 'caution' || t === 'accent') return t;
  return 'neutral';
}

// "N thing" / "N things" — a count with its honest unit (never a bare number).
function plural(n, unit) {
  const v = Number.isFinite(n) ? n : 0;
  return v + ' ' + unit + (v === 1 ? '' : 's');
}
