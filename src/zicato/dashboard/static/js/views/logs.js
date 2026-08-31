// js/views/logs.js — the operator-log pane (LOGGING.md §5).
//
// A WORKSPACE-LEVEL surface (a peer of builder / settings, outside any epoch —
// the structured log streams are per-invocation): one JSONL stream per evolve
// / reflect invocation, tailed through the SAME query-layer reader the CLI and
// `/api/logs` share. The pane renders the tail as quiet mono rows, level-
// coloured via the existing --v2 tone tokens, with level-filter chips and an
// invocation picker — no decorative chrome.
//
// RENDER DISCIPLINE. The view is fetch-then-gatedSwap: it folds its records
// into a content digest and repaints via gatedSwap, so a no-op SSE beat
// (identical digest) rebuilds ZERO DOM (the house rule). An empty / no-logs
// workspace shows an honest empty state and degrades — never an error.
//
// SERVER AUTHORITY. The reader owns the tail, the level filter, and the
// cursor; the view only renders + colours. It derives no log content.

import { el } from '../core/dom.js';
import * as D from '../data.js';
import { section, empty, gatedSwap } from '../ui.js';

// Module-level UI state — the selected level filter + invocation. Persists
// across the shell's SSE-driven re-dispatch so a steady beat keeps the
// operator's chosen filter rather than resetting it each tick.
const LEVELS = ['ALL', 'DEBUG', 'INFO', 'WARNING', 'ERROR'];
let _level = 'ALL';
let _invocation = 'latest';

// Append accumulator (LOGGING.md §5). The first paint of a given
// stream+level does a full bounded tail; every SSE-driven beat AFTER that
// fetches only `after=<byte cursor>` and appends the delta — instead of
// re-pulling limit-500 every tick. `_rows` is the accumulated tail we
// render; `_cursor` is the byte offset to resume from; `_reqInv`/`_reqLevel`
// are the request params the accumulator is keyed to; `_acctInv` is the
// resolved stream id it holds (so a picker switch, a level change, a pruned
// stream, or `latest` rolling to a new invocation all reset cleanly).
const _APPEND_CAP = 2000;
let _rows = [];
let _cursor = null;
let _reqInv = null;
let _reqLevel = null;
let _acctInv = null;

// Level → tone class (styled in console.css via the --v2 tokens).
function levelTone(level) {
  const l = String(level || '').toUpperCase();
  if (l === 'ERROR' || l === 'CRITICAL') return 'bad';
  if (l === 'WARNING') return 'warn';
  if (l === 'DEBUG') return 'faint';
  return 'info';
}

export async function render(host, ctx, _params, _route) {
  if (!host) return;
  const levelParam = _level === 'ALL' ? null : _level;

  // Append only when the request params match what the accumulator holds AND
  // we have a cursor; otherwise this is a fresh tail (first paint, or the
  // operator switched invocation/level). `after` is a BYTE offset.
  const sameReq = _invocation === _reqInv && _level === _reqLevel;
  let after = (sameReq && _cursor != null) ? _cursor : null;
  let view = await D.logs({ invocation: _invocation, level: levelParam, limit: 500, after });
  let resolvedInv = (view && view.invocation) || null;

  // If we sent an append cursor but `latest` rolled to a DIFFERENT stream
  // (or the pinned one was pruned away), that byte offset is meaningless
  // against the new file — refetch a full tail once and reset.
  if (after != null && resolvedInv !== _acctInv) {
    view = await D.logs({ invocation: _invocation, level: levelParam, limit: 500 });
    resolvedInv = (view && view.invocation) || null;
    after = null;
  }

  const fetched = (view && Array.isArray(view.records)) ? view.records : [];
  if (after != null && resolvedInv && resolvedInv === _acctInv) {
    // Genuine append to the same stream — take only records past the cursor
    // (dedup guard: the reader already returns only the delta) and cap growth.
    const add = fetched.filter((r) => typeof r.cursor !== 'number' || _cursor == null || r.cursor > _cursor);
    if (add.length) {
      _rows = _rows.concat(add);
      if (_rows.length > _APPEND_CAP) _rows = _rows.slice(-_APPEND_CAP);
    }
  } else {
    // Fresh tail (or a no-logs / transport-null view → empty accumulator).
    _rows = fetched;
  }
  if (view && view.cursor != null) _cursor = view.cursor;
  _reqInv = _invocation;
  _reqLevel = _level;
  _acctInv = resolvedInv;

  // A null payload is a transport failure (server down mid-view); an empty
  // records list with no invocation is the honest no-logs state.
  const records = _rows;
  const invocations = (view && Array.isArray(view.invocations)) ? view.invocations : [];

  const digest = JSON.stringify({
    ok: !!view,
    level: _level,
    inv: resolvedInv,
    invs: invocations.map((i) => i.id),
    rows: records.map((r) => [r.cursor, r.level, r.component, r.message, r.epoch_id, r.generation_id, r.run_id]),
  });

  gatedSwap(host, digest, () => build(host, ctx, view, records, invocations, resolvedInv));
}

function build(host, ctx, view, records, invocations, resolvedInv) {
  const nodes = [];
  nodes.push(el('div', { class: 'dt-pagehead' }, [
    el('h1', { class: 'dn-h1', text: 'Operator log' }),
    el('p', { class: 'dn-lede', text: 'The structured log stream for one evolve / reflect invocation — captured under .zicato/logs/, read back files-canonical. Observability only: nothing here feeds a score, a gate, or the journal.' }),
  ]));

  // ── toolbar: invocation picker + level filter chips ──────────────────
  const toolbar = el('div', { class: 'dt-logs-toolbar' });

  if (invocations.length) {
    const sel = el('select', { class: 'dt-logs-inv', 'aria-label': 'Invocation stream' });
    for (const inv of invocations) {
      const opt = el('option', { value: inv.id, text: inv.id });
      if (inv.id === resolvedInv) opt.selected = true;
      sel.appendChild(opt);
    }
    sel.addEventListener('change', () => {
      _invocation = sel.value || 'latest';
      render(host, ctx);
    });
    toolbar.appendChild(el('label', { class: 'dt-logs-invwrap' }, [
      el('span', { class: 'dn-faint dt-logs-invlab', text: 'invocation' }),
      sel,
    ]));
  }

  const chips = el('div', { class: 'dt-logs-chips', role: 'group', 'aria-label': 'Level filter' });
  for (const lvl of LEVELS) {
    const active = lvl === _level;
    const chip = el('button', {
      class: 'dt-logs-chip' + (active ? ' dt-logs-chip-on' : '') + (lvl !== 'ALL' ? ' dt-logs-t-' + levelTone(lvl) : ''),
      type: 'button',
      'aria-pressed': active ? 'true' : 'false',
      text: lvl.toLowerCase(),
    });
    chip.addEventListener('click', () => {
      if (_level === lvl) return;
      _level = lvl;
      render(host, ctx);
    });
    chips.appendChild(chip);
  }
  toolbar.appendChild(chips);
  nodes.push(toolbar);

  // ── the log body ─────────────────────────────────────────────────────
  if (!view) {
    nodes.push(section('Log', el('div', { class: 'dn-panel' }, [
      empty('The log service is unavailable right now.'),
    ])));
    return nodes;
  }
  if (!records.length) {
    const why = resolvedInv
      ? 'No records at this level for this invocation.'
      : 'No logs yet — this workspace has not run evolve or reflect, or the streams were pruned.';
    nodes.push(section('Log', el('div', { class: 'dn-panel' }, [empty(why)])));
    return nodes;
  }

  const body = el('div', { class: 'dt-logs-body dn-panel' });
  for (const r of records) body.appendChild(logRow(r));
  nodes.push(section('Log', body));
  return nodes;
}

function logRow(r) {
  const tone = levelTone(r.level);
  const row = el('div', { class: 'dt-logs-row dt-logs-t-' + tone });
  row.appendChild(el('span', { class: 'dt-logs-ts dn-faint', text: shortTs(r.ts) }));
  row.appendChild(el('span', { class: 'dt-logs-lvl dt-logs-t-' + tone, text: String(r.level || '') }));
  row.appendChild(el('span', { class: 'dt-logs-comp dn-faint', text: String(r.component || '') }));
  const ctxBits = [];
  for (const k of ['epoch_id', 'generation_id', 'run_id']) {
    if (r[k]) ctxBits.push(r[k]);
  }
  if (ctxBits.length) {
    row.appendChild(el('span', { class: 'dt-logs-ctx dn-faint', text: ctxBits.join(' · ') }));
  }
  row.appendChild(el('span', { class: 'dt-logs-msg', text: String(r.message || '') }));
  return row;
}

// Trim the ISO stamp to HH:MM:SS.mmm — the date is the invocation's, shown in
// the picker; the row only needs the intra-run time.
function shortTs(ts) {
  const s = String(ts || '');
  const t = s.indexOf('T');
  if (t < 0) return s;
  return s.slice(t + 1).replace('Z', '');
}
