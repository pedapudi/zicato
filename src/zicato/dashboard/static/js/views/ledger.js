// js/views/ledger.js — THE EXPERIMENTS LEDGER (issue #194 §3).
//
// An epoch is a sequence of experiments. Each one is a proposed IDEA, applied
// at some SITES, run against the board, and settled by the gate — and until
// now reading that story meant opening candidates one at a time, because the
// epoch page carried only a COUNT of experiments.
//
// This is the roster the count stood in for: one row per experiment —
// round · generation · core idea · sites touched · decision · Δscalar ·
// the deciding rule / rejection reason — served pre-joined by
// query/ledger_view.py (`/api/epoch/{id}/experiments-ledger`), rendered here.
//
// DISCIPLINE
//   * The verdict is the SERVER-STAMPED token, rendered through the shared
//     verdictPill. Nothing is re-classified here.
//   * The rejection reason is the RECORDED field, printed verbatim. It is
//     never parsed, split, or pattern-matched for a rule name.
//   * The core idea truncates to one line and expands IN PLACE — the choice
//     is remembered across the digest-gated re-render (the proposer-brief
//     precedent in views/epoch.js), so a live beat cannot snap it shut.

import { el } from '../core/dom.js';
import * as svg from '../svg.js';
import { dataTable, decisionFor, deltaCell, empty, truncate, verdictPill } from '../ui.js';

// How much of a core idea / rejection reason rides the row before it clips.
const IDEA_CHARS = 96;
const REASON_CHARS = 64;
// How many site ids a row names before the rest collapse into a "+N more".
const SITES_SHOWN = 3;

// The operator's expand/collapse of one row's core idea, keyed
// `${epochId}|${generation_id}`. The ledger lives on a digest-gated page, so
// a beat that moves ANY epoch data rebuilds the table; without this the
// expanded idea would silently collapse under the operator.
const _ideaOpen = new Map();

// Test seam — the module-level expand memory is process-wide, so the node
// suite resets it between cases rather than leaking one test's clicks.
export function _resetLedgerExpansion() { _ideaOpen.clear(); }

// The EXPERIMENTS LEDGER panel for one `/api/epoch/{id}/experiments-ledger`
// read. Null ONLY when the read itself failed (a transport error / a backend
// that does not serve it) — every served-but-empty shape renders its own
// honest empty state, because "this epoch ran no experiments" and "we could
// not ask" are different facts.
export function buildExperimentsLedger(ledger, opts) {
  if (!ledger || typeof ledger !== 'object') return null;
  const o = opts || {};
  const rows = Array.isArray(ledger.experiments) ? ledger.experiments.filter(Boolean) : [];
  const epochId = o.epochId != null ? String(o.epochId) : (ledger.epoch_id != null ? String(ledger.epoch_id) : '');
  const card = el('div', { class: 'dn-panel dn-ledger' });

  if (!rows.length) {
    // The index is the ledger's only source, so an unbuilt index is named as
    // such — never a silent "no experiments" that reads as an empty epoch.
    card.appendChild(ledger.note
      ? empty('No experiments ledger for this epoch — ' + String(ledger.note) + '.')
      : empty('No experiments recorded for this epoch yet.'));
    return card;
  }

  card.appendChild(dataTable({
    class: 'dn-board-table dn-ledger-table',
    columns: [
      { label: 'round', class: 'dn-num' },
      { label: 'generation' },
      { label: 'core idea' },
      { label: 'sites touched' },
      { label: 'decision' },
      { label: 'Δ scalar', class: 'dn-num' },
      { label: 'deciding rule / rejection reason' },
    ],
    rows: rows.map((r) => ledgerRow(r, epochId, o)),
  }));
  card.appendChild(el('p', { class: 'dn-faint dn-ledger-cap', style: 'font-size:11px;margin:8px 0 0;', text:
    'one row per experiment, in round order — the idea, where it was applied, and how the gate answered · '
    + 'Δ scalar is the challenger minus its champion (lower = better) · click a core idea to read it in full' }));
  return card;
}

function ledgerRow(r, epochId, o) {
  const gen = r.generation_id != null ? String(r.generation_id) : '';
  // The ONE shared classifier owns the vocabulary: the server-stamped token
  // when there is one, `baseline` for the parentless seed (which faced no
  // gate and must not read as still racing), `pending` only when a candidate
  // genuinely has not settled. Nothing is re-derived from the raw outcome.
  const decision = decisionFor({ promoted: r.promoted, parent: r.parent_generation_id, exp: r });
  const delta = svg.isNum(r.scalar_score_delta) ? r.scalar_score_delta : null;
  const reason = (typeof r.rejection_reason === 'string' && r.rejection_reason) ? r.rejection_reason : null;
  return {
    class: 'dn-ledger-row' + (r.promoted === true ? ' dn-board-champ' : ''),
    dataset: { gen },
    cells: [
      { class: 'dn-num dn-mono', text: svg.isNum(r.round_index) ? String(r.round_index) : '—' },
      { class: 'dn-mono', el: genCell(gen, r, o) },
      { class: 'dn-ledger-ideacell', el: ideaCell(r.core_idea, epochId, gen) },
      { class: 'dn-ledger-sites', el: sitesCell(r.mutation_ids) },
      { el: verdictPill(decision) },
      deltaCell(delta, { base: 'dn-num dn-mono', text: delta == null ? '—' : svg.fmtSigned(delta, 3) }),
      {
        class: 'dn-ledger-reason dn-faint',
        title: reason || '',
        text: reason ? truncate(reason, REASON_CHARS) : '—',
      },
    ],
  };
}

// The generation, linked to its dossier when the caller supplied an href
// builder (a plain mono label otherwise — the builder stays pure).
function genCell(gen, r, o) {
  if (!gen) return el('span', { class: 'dn-faint', text: '—' });
  const label = gen + (r.promoted === true ? ' ' + svg.CROWN.current : '');
  if (typeof o.hrefFor !== 'function') return el('span', { text: label });
  return el('a', { class: 'dn-linkbtn dn-mono', href: o.hrefFor(gen), text: label });
}

// The core idea: one clipped line that expands IN PLACE on click. An absent
// idea reads '—' as a plain span — there is nothing to expand, and a dead
// button would be a lie about what is clickable.
function ideaCell(rawIdea, epochId, gen) {
  const idea = (typeof rawIdea === 'string' && rawIdea.trim()) ? rawIdea.trim() : null;
  if (!idea) return el('span', { class: 'dn-faint', text: '—' });
  const key = epochId + '|' + gen;
  const clipped = truncate(idea, IDEA_CHARS);
  if (clipped === idea) return el('span', { class: 'dn-ledger-idea-flat', text: idea });

  const open = _ideaOpen.get(key) === true;
  const btn = el('button', {
    type: 'button',
    class: 'dn-ledger-idea' + (open ? ' dn-ledger-idea-open' : ''),
    title: open ? 'collapse' : idea,
    text: open ? idea : clipped,
  });
  btn.addEventListener('click', () => {
    const next = !(_ideaOpen.get(key) === true);
    _ideaOpen.set(key, next);
    btn.textContent = next ? idea : clipped;
    btn.setAttribute('title', next ? 'collapse' : idea);
    btn.setAttribute('class', 'dn-ledger-idea' + (next ? ' dn-ledger-idea-open' : ''));
  });
  return btn;
}

// The sites this experiment touched: the first few mutation ids, then a
// "+N more" carrying the rest on hover. A proposal that patched nothing
// recorded reads '—'.
function sitesCell(raw) {
  const ids = (Array.isArray(raw) ? raw : []).filter((m) => m != null && String(m)).map(String);
  if (!ids.length) return el('span', { class: 'dn-faint', text: '—' });
  const shown = ids.slice(0, SITES_SHOWN);
  const rest = ids.slice(SITES_SHOWN);
  const kids = shown.map((m) => el('span', { class: 'dn-ledger-site dn-mono', title: m, text: m }));
  if (rest.length) {
    kids.push(el('span', {
      class: 'dn-ledger-site-more dn-faint',
      title: rest.join(' · '),
      text: '+' + rest.length + ' more',
    }));
  }
  return kids;
}

// Digest fold: rounded, id-stable, timestamp-free. A no-op beat is
// byte-identical; a settled experiment (a new decision / Δ / site) flips it.
// The expand state is DELIBERATELY absent — expanding a row must not be able
// to trigger a page rebuild that recreates the row it just expanded.
export function ledgerDigest(ledger) {
  if (!ledger || typeof ledger !== 'object') return null;
  const rows = Array.isArray(ledger.experiments) ? ledger.experiments.filter(Boolean) : [];
  return [
    ledger.note || null,
    rows.map((r) => [
      r.generation_id, svg.isNum(r.round_index) ? r.round_index : null,
      (r.core_idea || '').slice(0, 120),
      (Array.isArray(r.mutation_ids) ? r.mutation_ids : []).map(String),
      r.decision || null, r.promoted === true ? 1 : (r.promoted === false ? 0 : null),
      svg.isNum(r.scalar_score_delta) ? r.scalar_score_delta.toFixed(3) : null,
      (r.rejection_reason || '').slice(0, 120),
    ]),
  ];
}
