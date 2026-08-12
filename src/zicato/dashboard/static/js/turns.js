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

import { el, clearChildren } from './core/dom.js';

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
// Appends ONLY the newly-landed turns (the rendered prefix is stable — dedup
// only folds consecutive duplicates, and turns arrive append-only), so a live
// beat adds the tail nodes without touching the existing turn DOM (no thread
// rebuild); a no-op beat writes ZERO DOM. A bottom-pinned reader keeps tailing
// new turns. When the ONLY divergence is the final rendered turn growing (a
// merged llmCall reasoning turn whose text grows across two seqs — the ROUTINE
// streaming case), just that one node is re-rendered in place, preserving every
// prefix node + the scroll position. A GENUINE prefix divergence (an earlier
// turn changed, or the list shrank — a completed run's final transcript) falls
// back to a full rebuild that still preserves the reader's scroll discipline
// (pinned stays pinned, scrolled-up keeps its offset).
//
// Returns { rendered, appended, rebuilt, pinned } — `appended` is how many turn
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
  // The FIRST index at which the rendered signatures diverge from the desired
  // (within the overlap). -1 ⇒ the rendered prefix is intact and the desired
  // list only extends it (pure append) or is identical.
  let diverge = -1;
  const overlap = Math.min(haveSig.length, wantSig.length);
  for (let i = 0; i < overlap; i += 1) { if (haveSig[i] !== wantSig[i]) { diverge = i; break; } }

  const out = { rendered: turns.length, appended: 0, rebuilt: false, pinned: true };

  if (diverge === -1 && haveSig.length === wantSig.length) {
    // No content change — ZERO DOM (scroll untouched).
    out.pinned = nearBottom(scroller);
    return out;
  }

  if (diverge === -1 && haveSig.length < wantSig.length) {
    // APPEND the tail turns only — the existing turn nodes stay in place.
    out.pinned = nearBottom(scroller);
    for (let i = haveSig.length; i < turns.length; i += 1) scroller.appendChild(buildTurnNode(turns[i], annBySeq));
    out.appended = turns.length - haveSig.length;
  } else if (diverge === haveSig.length - 1 && wantSig.length >= haveSig.length) {
    // LAST-TURN-GREW — the ONLY divergence is the final rendered turn, the
    // ROUTINE streaming case (goldfive's llmCallStart→llmCallEnd merge into ONE
    // turn whose text grows across two seqs, flipping just the last turnSig while
    // every earlier turn is byte-stable). Re-render JUST that node in place +
    // append any tail; the prefix nodes and scroll position are preserved (no
    // clamp-to-0 as the wholesale rebuild below would cause). It is the last
    // rendered node, so remove-then-append keeps document order.
    out.pinned = nearBottom(scroller);
    const idx = haveSig.length - 1;
    const oldNode = scroller.childNodes[idx];
    if (oldNode) scroller.removeChild(oldNode);
    scroller.appendChild(buildTurnNode(turns[idx], annBySeq));
    for (let i = haveSig.length; i < turns.length; i += 1) scroller.appendChild(buildTurnNode(turns[i], annBySeq));
    out.appended = turns.length - haveSig.length;
  } else {
    // GENUINE prefix divergence — an earlier turn changed, or the list shrank (a
    // completed run's final transcript). Rebuild wholesale, but preserve the
    // reader's scroll DISCIPLINE across the clear: a bottom-pinned reader stays
    // pinned (keeps live-tailing), a scrolled-up reader keeps their offset.
    out.pinned = nearBottom(scroller);
    out.rebuilt = true;
    const prevTop = typeof scroller.scrollTop === 'number' ? scroller.scrollTop : null;
    clearChildren(scroller);
    for (const t of turns) scroller.appendChild(buildTurnNode(t, annBySeq));
    scroller._turnSig = wantSig;
    if (typeof scroller.scrollHeight === 'number') {
      if (out.pinned) scroller.scrollTop = scroller.scrollHeight;
      else if (prevTop != null) scroller.scrollTop = prevTop;
    }
    return out;
  }

  scroller._turnSig = wantSig;
  if (out.pinned && typeof scroller.scrollHeight === 'number') scroller.scrollTop = scroller.scrollHeight;
  return out;
}
