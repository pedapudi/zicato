// js/turns.js — the ONE transcript turn vocabulary.
//
// Three surfaces render a reconstructed conversation: the Board view's inline
// side-by-side panes, the Traces view's foreign-trajectory reconstruction, and
// the live conversation follow pane (js/convo.js). They must render a turn
// IDENTICALLY — an appended turn has to be byte-identical to a rebuilt one, or
// the append-only reconcile that keeps scroll alive would show a seam.
//
// These functions used to live inside views/board.js, with traces.js importing
// them across the view boundary. They are lifted here unchanged so the follow
// pane can reuse them without a view↔view import cycle (board.js mounts the
// follow pane, so the pane cannot import back from board.js). board.js
// re-exports them, so its existing importers and tests are untouched.

import { el } from './core/dom.js';

// Build ONE turn's DOM node — the shared turn renderer for both the initial
// fill and the live append, so an appended turn is byte-identical to a rebuilt
// one. A foreign trace turn ({role, text}) renders through this same builder
// with an empty annotation map (foreign traces carry no per-seq annotations).
export function buildTurnNode(t, annBySeq) {
  const turn = el('div', { class: 'dn-turn dn-turn-' + (t.role || 'agent') }, [
    el('div', { class: 'dn-turn-head dn-faint dn-mono' }, [
      el('span', { text: t.agent || t.role || 'turn' }),
      t.kind ? el('span', { text: ' · ' + t.kind }) : null,
    ].filter(Boolean)),
    t.text ? el('div', { class: 'dn-turn-text', text: t.text }) : null,
  ].filter(Boolean));
  if (Array.isArray(t.tool_calls)) for (const tc of t.tool_calls) {
    turn.appendChild(el('div', { class: 'dn-tool dn-mono', text: '⚙ ' + (tc.name || tc.tool || 'tool') }));
  }
  for (const a of ((annBySeq && annBySeq.get(t.seq)) || [])) {
    turn.appendChild(el('div', { class: 'dn-annot dn-annot-' + (a.kind || 'note'), text: '◂ ' + (a.summary || a.kind) }));
  }
  return turn;
}

// Drop a turn that EXACTLY repeats the one immediately before it — same role,
// identical non-empty text, neither carrying its own tool calls — folding the
// literal goal duplicate (runStarted then goalDerived) to ONE read. Genuinely-
// distinct turns (different text, a tool call, an empty turn) are kept, and only
// CONSECUTIVE duplicates fold (a later echo across intervening turns is kept).
export function dedupConsecutiveTurns(turns) {
  // A hole is dropped rather than rendered. The live pane splices turns at the
  // server's indices and heals a gap by re-reading, but a renderer that throws
  // on a sparse array turns a recoverable wire hiccup into a blank screen.
  const list = Array.isArray(turns) ? turns.filter(Boolean) : [];
  const out = [];
  for (const t of list) {
    const prev = out[out.length - 1];
    if (prev && isDuplicateTurn(prev, t)) continue;
    out.push(t);
  }
  return out;
}

function isDuplicateTurn(a, b) {
  if (!a || !b) return false;
  const aText = (a.text || '').trim();
  const bText = (b.text || '').trim();
  if (aText === '' || aText !== bText) return false;
  if ((a.role || '') !== (b.role || '')) return false;
  const aTools = Array.isArray(a.tool_calls) && a.tool_calls.length;
  const bTools = Array.isArray(b.tool_calls) && b.tool_calls.length;
  if (aTools || bTools) return false;
  return true;
}

// Per-turn content signature for the append reconcile — seq / role / text
// length / tool-call count / annotation count. Two turns with the same
// signature render identically, so an unchanged prefix is a true no-op.
export function turnSig(t, annBySeq) {
  const na = annBySeq && annBySeq.get(t.seq);
  return [t.seq, t.role, (t.text || '').length, Array.isArray(t.tool_calls) ? t.tool_calls.length : 0, na ? na.length : 0].join(':');
}

// Whether a scroller is pinned at (or within a hair of) the bottom — the
// live-tail signal. A headless test DOM without scroll metrics defaults to tail.
export function nearBottom(scroller) {
  const sh = scroller.scrollHeight, st = scroller.scrollTop, ch = scroller.clientHeight;
  if (typeof sh !== 'number' || typeof ch !== 'number' || typeof st !== 'number') return true;
  return (sh - st - ch) <= 8;
}

// Group a transcript's annotations by the turn seq they anchor to — the map
// buildTurnNode reads. Shared so every surface anchors notes the same way.
export function annotationsBySeq(annotations) {
  const map = new Map();
  for (const a of (Array.isArray(annotations) ? annotations : [])) {
    const k = a.anchor_seq;
    if (!map.has(k)) map.set(k, []);
    map.get(k).push(a);
  }
  return map;
}

// Reconcile a turn list into a persistent scroller. THE append-only renderer,
// shared by the board's side-by-side columns and the live follow pane.
//
// A NODE IS ONLY TOUCHED WHEN ITS OWN CONTENT CHANGED. Unchanged turns keep
// their exact DOM nodes, so scroll position, text selection and focus all
// survive; a beat that brings nothing writes ZERO DOM.
//
// This is a per-index patch rather than a prefix-diff-then-rebuild, because a
// transcript does NOT only grow at the end. Two ordinary things change an
// already-rendered turn:
//
//   * the open final turn absorbs another event and its text grows (goldfive's
//     llmCallStart → llmCallEnd merge into one turn);
//   * an ANNOTATION lands anchored to an EARLIER turn — drift detections and
//     judge verdicts anchor to the nearest preceding turn, so a note arriving
//     twenty turns later re-decorates turn 1.
//
// The second case is what makes a prefix-diff wrong here: it reads as "the
// prefix diverged", and rebuilding the thread on every late annotation is
// exactly the churn this renderer exists to avoid. Patching by index keeps
// every other node untouched and costs one rebuild of the one turn that
// genuinely changed.
//
// Returns { rendered, appended, patched, pinned } — `appended` is how many turn
// nodes newly landed and `pinned` whether the reader was tailing when they did,
// which is exactly what the follow pane's "N new turns ↓" badge counts.
export function reconcileTurns(scroller, rawTurns, annotations) {
  // DEDUP CONSECUTIVE IDENTICAL TURNS. goldfive emits the goal twice — on
  // `runStarted.goalSummary` and again on `goalDerived` (the LiteralGoalDeriver
  // echoes the same string) — so the goal reads twice; collapse the literal
  // duplicate (see dedupConsecutiveTurns).
  const turns = dedupConsecutiveTurns(Array.isArray(rawTurns) ? rawTurns : []);
  const annBySeq = annotationsBySeq(annotations);

  const wantSig = turns.map((t) => turnSig(t, annBySeq));
  const haveSig = Array.isArray(scroller._turnSig) ? scroller._turnSig : [];

  const out = { rendered: turns.length, appended: 0, patched: 0, pinned: nearBottom(scroller) };
  const overlap = Math.min(haveSig.length, wantSig.length);

  // 1 — PATCH the turns whose OWN content changed. Every other node is left
  //     untouched, which is what preserves scroll, selection and focus.
  for (let i = 0; i < overlap; i += 1) {
    if (haveSig[i] === wantSig[i]) continue;
    const oldNode = scroller.childNodes[i];
    const fresh = buildTurnNode(turns[i], annBySeq);
    if (oldNode) { scroller.insertBefore(fresh, oldNode); scroller.removeChild(oldNode); }
    else scroller.appendChild(fresh);
    out.patched += 1;
  }

  // 2 — APPEND what is new past the end.
  for (let i = overlap; i < turns.length; i += 1) {
    scroller.appendChild(buildTurnNode(turns[i], annBySeq));
    out.appended += 1;
  }

  // 3 — TRIM a list that shrank (a completed run's final, deduped transcript).
  for (let i = haveSig.length - 1; i >= overlap; i -= 1) {
    const extra = scroller.childNodes[i];
    if (extra) scroller.removeChild(extra);
  }

  // Nothing moved — ZERO DOM, and the scroll position is not even read back.
  if (out.patched === 0 && out.appended === 0 && haveSig.length === wantSig.length) return out;

  scroller._turnSig = wantSig;
  if (out.pinned && typeof scroller.scrollHeight === 'number') scroller.scrollTop = scroller.scrollHeight;
  return out;
}
