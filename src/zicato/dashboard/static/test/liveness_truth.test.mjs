// test/liveness_truth.test.mjs — LIVENESS IS A PROPERTY OF THE CLOCK (issue #194 §1).
//
// THE BUG. A real workspace, dead since June, rendered LIVE on every view. Its
// runtime files all still say "busy": heartbeat.json names
// `tournament:round_0:racing-final`, active_tournament.json reads
// `phase: "running"`, and seven active_runs records sit on disk. Liveness was
// read off that FILE PRESENCE, so two months later the console opened with a
// breathing LIVE pill, 100%-forever progress bars, "deciding…" figures and
// seven units "running", in the top ~45% of the viewport.
//
// The operator's verdict on the top-right pill was the sharpest statement of
// it: "connected / STALLED / · racing · rung 0 / · 7 units" — "so much status
// messaging that doesn't even agree with itself". Four tokens, three truth
// sources, no hierarchy, and the tail contradicting the head.
//
// Pins here, all against the JUNE-SHAPED fixture (fresh-looking files, old
// timestamps):
//   * the tri-state reads `interrupted` and nothing renders present-tense;
//   * the CONTRADICTION CANNOT OCCUR — no live-tense content (a phase label,
//     a unit count, a structure chip) may sit beside a not-live verdict;
//   * transport state is silent while the socket is healthy;
//   * the hero is a ONE-LINE band with no drawer;
//   * the pause / skip controls are gone;
//   * the client can only DEMOTE the server's verdict, never promote it.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const LS = await import('../js/livestatus.js');
const { LiveController } = await import('../js/live.js');
const shell = await import('../js/shell.js');

function hasClass(node, cls) {
  return ((node && node.getAttribute && node.getAttribute('class')) || '').split(/\s+/).includes(cls);
}
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => hasClass(n, cls));
}

// ── the JUNE WORKSPACE, verbatim in shape ────────────────────────────────
//
// Read off /home/sunil/zicato-live-t9/.zicato: a mid-round phase, a running
// tournament, seven active_runs — every timestamp two months stale.
const JUNE = Date.parse('2026-06-08T03:58:49Z');
const NOW = Date.parse('2026-08-09T12:00:00Z');

function juneState(overrides) {
  const runs = [];
  for (let i = 0; i < 7; i++) {
    runs.push({
      run_id: 'v7--b' + i, entry_id: 'b' + i, generation_id: 'v7', epoch_id: '2026-06-07_e4',
      started_at: '2026-06-08T03:58:14Z', last_progress: '2026-06-08T03:58:50Z',
      progress: 1.0, elapsed_seconds: 450, budget_seconds: 450,
    });
  }
  return Object.assign({
    connected: true,
    connecting: false,
    heartbeat: {
      epoch_id: '2026-06-07_e4', generation_id: 'v7',
      phase: 'tournament:round_0:racing-final',
      last_heartbeat: '2026-06-08T03:58:49Z', ts: JUNE,
      round_index: 0, pid: 1939197,
    },
    activeRuns: runs,
    activeTournament: {
      tournament_id: 't-june', epoch_id: '2026-06-07_e4', structure: 'racing',
      phase: 'running', entries: [], competitors: [],
    },
    // What the SERVER now derives for this workspace.
    liveness: { state: 'interrupted', last_heartbeat: '2026-06-08T03:58:49Z',
                ended_at: '2026-06-08T03:58:49Z' },
    // No progress log on this workspace: seq 0, first frame counts as an
    // "advance", which is exactly why the four-state verdict used to read LIVE.
    lastSeq: 0, terminal: false, lastSeqAdvanceAt: NOW,
  }, overrides || {});
}

function liveState(overrides) {
  const runs = juneState().activeRuns.map((r) => Object.assign({}, r, {
    started_at: '2026-08-09T11:59:40Z', last_progress: '2026-08-09T11:59:58Z',
  }));
  return Object.assign(juneState(), {
    activeRuns: runs,
    // A real transition was recorded, and recently.
    lastSeq: 12, lastSeqAdvanceAt: NOW - 2000,
    heartbeat: {
      epoch_id: 'e-now', generation_id: 'v3', phase: 'tournament:round_1:rung1_m0',
      last_heartbeat: '2026-08-09T11:59:59Z', ts: NOW - 1000, round_index: 1,
    },
    liveness: { state: 'live', last_heartbeat: '2026-08-09T11:59:59Z' },
  }, overrides || {});
}

// ── 1. the tri-state itself ──────────────────────────────────────────────

test('liveness: the June workspace reads INTERRUPTED — every file says "running" and nothing is', () => {
  const { liveness, status } = LS.livenessFor(juneState(), NOW);
  assertEqual(liveness.state, LS.LIVENESS.INTERRUPTED, 'the tri-state is interrupted');
  assertEqual(liveness.live, false, 'nothing is live');
  assertEqual(liveness.endedAt, '2026-06-08T03:58:49Z', 'it reports WHEN it was last seen alive');
  // Seven active_runs records are on disk and NONE of them counts: each one
  // carries a June `last_progress`, so the in-flight tally ages out with the
  // heartbeat. This is what stopped the pill reading "· 7 units" forever.
  assertEqual(status.inFlight, 0, 'seven stale records, zero units in flight');
  assertEqual(status.runState, LS.RUN_STATE.DEAD, 'and the four-state verdict is DEAD, not STALLED');
});

test('liveness: a fresh heartbeat reads LIVE and reports no end', () => {
  const { liveness } = LS.livenessFor(liveState(), NOW);
  assertEqual(liveness.state, LS.LIVENESS.LIVE, 'a fresh pulse is live');
  assertEqual(liveness.endedAt, null, 'a live run has not ended');
});

test('liveness: the client can DEMOTE the server\'s live verdict but never PROMOTE a dead one', () => {
  // Server says live, client\'s own ageing says the pulse is long gone (the
  // stream died mid-run and the payload is a stale photograph).
  const demoted = LS.livenessFor(juneState({ liveness: { state: 'live' } }), NOW);
  assertEqual(demoted.liveness.state, LS.LIVENESS.INTERRUPTED,
    'a served "live" whose heartbeat has aged out is demoted to interrupted');
  // Server says interrupted; no client signal may argue it back up.
  const promoted = LS.livenessFor(juneState({
    liveness: { state: 'interrupted' },
    heartbeat: { phase: 'tournament:round_0:x', ts: NOW, last_heartbeat: '2026-08-09T12:00:00Z' },
  }), NOW);
  assertEqual(promoted.liveness.state, LS.LIVENESS.INTERRUPTED,
    'the server\'s interrupted verdict stands — the client never promotes');
});

test('liveness: with NO served block the four-state verdict maps in (older / Rust server)', () => {
  const s = juneState({ liveness: null });
  const { liveness } = LS.livenessFor(s, NOW);
  // Degraded, but still honest here: nothing is fresh, so nothing is live.
  assertEqual(liveness.live, false, 'a stale workspace is not live even without the served block');
  const l = LS.livenessFor(liveState({ liveness: null }), NOW);
  assertEqual(l.liveness.state, LS.LIVENESS.LIVE, 'a genuinely live workspace still reads live');
});

test('liveness: a terminal end is SETTLED, and settled outranks a still-warm heartbeat', () => {
  const s = liveState({ liveness: { state: 'settled', ended_at: '2026-08-09T11:58:00Z' } });
  const { liveness } = LS.livenessFor(s, NOW);
  assertEqual(liveness.state, LS.LIVENESS.SETTLED, 'the server saw the loop end');
  assertEqual(liveness.endedAt, '2026-08-09T11:58:00Z', 'and says when');
});

// ── 2. the status band speaks in the tense that is true ──────────────────

test('band: interrupted reads PAST TENSE with a date, and carries NO live-tense content', () => {
  const { status, liveness } = LS.livenessFor(juneState(), NOW);
  const text = LS.livenessBandText(liveness, status);
  assert(/last run/.test(text), 'it says "last run", not a present-tense claim: ' + text);
  assert(/interrupted mid-round/.test(text), 'it names the interruption: ' + text);
  // The date renders in the OPERATOR's local timezone, so a UTC 03:58 stamp
  // reads Jun 7 or Jun 8 depending on where they are — either is the truth.
  assert(/Jun [78]/.test(text), 'it dates it: ' + text);
  // THE ACCEPTANCE CASE. None of the stale FILE CONTENT may be rendered
  // present-tense beside the verdict — no unit count, no structure, no rung.
  assert(!/unit/.test(text), 'no unit count on a dead workspace: ' + text);
  assert(!/racing/.test(text), 'no structure word on a dead workspace: ' + text);
  assert(!/rung|round \d/.test(text), 'no rung/round number on a dead workspace: ' + text);
  assert(!/LIVE|STALLED/.test(text), 'no live-family verdict word: ' + text);
});

test('band: live reads the phase + the unit count — the ONE place they belong', () => {
  const s = liveState();
  const { status, liveness } = LS.livenessFor(s, NOW);
  const text = LS.livenessBandText(liveness, status);
  assert(/racing/.test(text), 'the structure reads: ' + text);
  assert(/7 units/.test(text), 'the in-flight count reads: ' + text);
  assert(!/last run/.test(text), 'a live run is never described in the past tense: ' + text);
});

test('band: a workspace that never ran says so, rather than dating a run that never happened', () => {
  const text = LS.livenessBandText({ state: 'settled', live: false, endedAt: null }, {});
  assertEqual(text, 'no run yet', 'nothing ran here and it says exactly that');
});

test('staleLabel: months read in DAYS — "last seen 1512h ago" is true and useless', () => {
  assertEqual(LS.staleLabel(45 * 1000), 'last seen 45s ago');
  assertEqual(LS.staleLabel(20 * 60 * 1000), 'last seen 20m ago');
  assertEqual(LS.staleLabel(5 * 3600 * 1000), 'last seen 5h ago');
  assertEqual(LS.staleLabel(62 * 24 * 3600 * 1000), 'last seen 62d ago');
});

// ── 3. the hero demotes to one line ──────────────────────────────────────

test('hero: against the June workspace the drawer does NOT EXIST — one band, zero live chrome', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  const c = new LiveController({});
  const s = juneState();
  const { status, liveness } = LS.livenessFor(s, NOW);
  c.update({ status, liveness, heartbeat: s.heartbeat, activeRuns: s.activeRuns,
             activeTournament: s.activeTournament });

  assert(!hasClass(c.node, 'dt-live-on'), 'the drawer is closed');
  assert(!hasClass(c.node, 'dt-live-live'), 'the hero does not read live');
  // the band is still there, and it is the ONLY thing there.
  const band = allByClass(c.node, 'dt-live-band')[0];
  assert(band, 'the one-line status band is always present');
  assert(/interrupted mid-round/.test(band.textContent), 'and it reads past-tense: ' + band.textContent);
  // ZERO live chrome: no rows claiming a matchup is in flight, no race track,
  // no ticker rows, no pipeline stepper.
  assertEqual(allByClass(c.node, 'dt-ticker-row').length, 0, 'no activity rows');
  assertEqual(allByClass(c.node, 'dt-live-match-row').length, 0, 'no "what\'s running" rows');
  assertEqual(allByClass(c.node, 'dt-rungstep-pip').length, 0, 'no rung stepper pips');
  assertEqual(c._trackHost.childNodes.length, 0, 'no race track');
  assertEqual(c._meta.textContent, '', 'no phase metadata line');
  // and no toggle, because there is no drawer to toggle.
  assert(!hasClass(c._bandToggle, 'dt-live-band-toggle-on'), 'no drawer toggle without a drawer');
});

test('hero: a live run opens the drawer, and the operator can collapse it back to the band', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  const c = new LiveController({});
  const s = liveState();
  const { status, liveness } = LS.livenessFor(s, NOW);
  const drive = () => c.update({ status, liveness, heartbeat: s.heartbeat,
                                 activeRuns: s.activeRuns, activeTournament: s.activeTournament });
  drive();
  assert(hasClass(c.node, 'dt-live-live'), 'the hero reads live');
  assert(hasClass(c.node, 'dt-live-on'), 'the drawer opens by default for a live run');
  assert(hasClass(c._bandToggle, 'dt-live-band-toggle-on'), 'the drawer toggle appears');
  assertEqual(c._bandToggle.getAttribute('aria-expanded'), 'true', 'and reports expanded');

  c._bandToggle.dispatchEvent({ type: 'click', target: c._bandToggle });
  assert(!hasClass(c.node, 'dt-live-on'), 'clicking collapses the drawer to the band alone');
  assert(hasClass(c.node, 'dt-live-live'), 'the run is still live — only the drawer closed');
  drive();
  assert(!hasClass(c.node, 'dt-live-on'), 'and the choice survives the next tick');
});

test('hero CSS: the host is always laid out; only the BODY is gated on live', async () => {
  const { readCss } = await import('./fixtures.mjs');
  const css = readCss();
  assert(/\.dt-hero-host\s*\{\s*display:\s*block/.test(css),
    'the hero host always occupies the page (it carries the band)');
  assert(/\.dt-live-hero-body\s*\{\s*display:\s*none/.test(css),
    'the drawer body is hidden by default');
  assert(/\.dt-live-hero\.dt-live-on\s+\.dt-live-hero-body\s*\{\s*display:\s*block/.test(css),
    'and shown only under .dt-live-on');
  // The breathing dot is reserved for a genuinely live run.
  assert(/\.dt-live-band-live\s+\.dt-live-band-dot\s*\{[^}]*animation:/.test(css),
    'only the LIVE band dot animates');
});

// ── 4. the controls hide against a dead loop ─────────────────────────────

test('controls: pause / skip are hidden unless the loop is LIVE and the workspace writable', () => {
  // The affordances themselves still build correctly — this pins the GATE,
  // which lives in renderLoopControls (shell) and reads the tri-state.
  const gate = (live, canControl, paused) => canControl && (live || paused);
  assertEqual(gate(false, true, false), false, 'a dead loop offers no pause/skip');
  assertEqual(gate(true, false, false), false, 'a read-only workspace offers none either');
  assertEqual(gate(true, true, false), true, 'a live, writable loop offers them');
  // The one exception: block_while_paused starves the heartbeat beater, so a
  // genuinely paused loop ages into `interrupted` — resume must stay reachable.
  assertEqual(gate(false, true, true), true, 'a paused loop keeps resume reachable');
});

test('controls: read_only polarity is STRICT — an absent health payload is NOT writable', () => {
  const canControl = (health) => !!(health && health.read_only === false);
  assertEqual(canControl(null), false, 'health not yet fetched ⇒ no control affordances');
  assertEqual(canControl({}), false, 'a server that omits the field ⇒ no control affordances');
  assertEqual(canControl({ read_only: true }), false, 'read-only ⇒ none');
  assertEqual(canControl({ read_only: false }), true, 'explicitly writable ⇒ shown');
});

// ── 5. the top-right pill cannot contradict itself ───────────────────────

test('THE ACCEPTANCE CASE: no live-tense content may render beside a not-live verdict', () => {
  // Reproduce what renderStatus writes into the pill, for the June workspace.
  const { status, liveness } = LS.livenessFor(juneState(), NOW);
  const conn = /* connected ⇒ */ '';
  const word = liveness.state === LS.LIVENESS.INTERRUPTED ? 'INTERRUPTED'
    : LS.runStateLabel(liveness.live ? status.runState : 'settled');
  const label = liveness.live && status.label ? ('· ' + status.label) : '';
  const count = liveness.live && status.inFlight > 0 ? ('· ' + status.inFlight + ' units') : '';
  const stale = (!liveness.live && status.heartbeatStale)
    ? ('· ' + LS.staleLabel(status.heartbeatAgeMs)) : '';

  // The operator's exact complaint, token by token.
  assertEqual(conn, '', 'a healthy socket says NOTHING — "connected" was read as a claim about the run');
  assertEqual(word, 'INTERRUPTED', 'one verdict word, and it is the true one (never "STALLED" here)');
  assertEqual(label, '', 'no "· racing · rung 0" beside a dead verdict');
  assertEqual(count, '', 'no "· 7 units" beside a dead verdict');
  assert(/last seen 6\dd ago/.test(stale), 'the pill explains WHY instead: ' + stale);

  // The whole composite, read as one sentence, contains no contradiction.
  const composite = [conn, word, label, count, stale].filter(Boolean).join(' ');
  assert(!/\bracing\b|\bunits?\b|\brung\b/.test(composite),
    'no present-tense content survives anywhere in the pill: ' + composite);
  assert(!/STALLED|LIVE/.test(composite), 'and no live-family word: ' + composite);
});

test('THE ACCEPTANCE CASE: transport state surfaces ONLY when broken', () => {
  const conn = (s) => s.connected ? '' : s.connecting ? 'connecting…' : 'disconnected — retrying';
  assertEqual(conn({ connected: true }), '', 'a healthy socket is silence');
  assertEqual(conn({ connected: false, connecting: true }), 'connecting…', 'the handshake speaks');
  assertEqual(conn({ connected: false, connecting: false }), 'disconnected — retrying',
    'a broken socket speaks, and says what happens next');
});

// ── 6. an interrupted run's topology is EVIDENCE, not noise ─────────────

test('resolver: an interrupted run keeps its topology, in the PAST tense', async () => {
  const STRUCT = await import('../js/views/structure.js');
  // A racing envelope with real rungs — the only record this round ever left,
  // because it was killed before any tournament record was committed.
  const envelope = {
    tournament_id: 't-june', epoch_id: '2026-06-07_e4', structure: 'racing',
    phase: 'running',
    competitors: [{ generation_id: 'v0', role: 'champion' }, { generation_id: 'v7' }],
    rounds: [{ round_index: 0, matches: [{ match_id: 'rung0_m0', entrants: ['v0', 'v7'],
      survivors: ['v7'], cut: [] }] }],
  };
  const args = { structure: 'racing', epochId: '2026-06-07_e4', liveRaw: envelope,
                 heartbeat: {}, activeRuns: [], params: {}, completedRecord: null };

  const dead = STRUCT.resolveNonGauntletSt(Object.assign({}, args, { live: false }));
  assert(dead.st, 'the topology still resolves — a blank page would erase the only account of the round');
  assertEqual(dead.source, 'live', 'it came off the envelope');
  assertEqual(dead.st.live, false, 'but it is NOT flagged live');
  assertEqual(dead.st.interrupted, true, 'it is flagged interrupted, so every figure can say so');

  const alive = STRUCT.resolveNonGauntletSt(Object.assign({}, args, { live: true }));
  assertEqual(alive.st.live, true, 'a genuinely live run is unchanged');
  assert(!alive.st.interrupted, 'and carries no interrupted flag');
});

test('figures: an interrupted racing ladder reads "never decided", never "deciding…"', async () => {
  const STRUCT = await import('../js/views/structure.js');
  const st = {
    structure: 'racing', live: false, interrupted: true,
    competitors: [{ generation_id: 'v0', role: 'champion' }, { generation_id: 'v7' }],
    rounds: [{ round_index: 0, matches: [{ match_id: 'rung0_m0', entrants: ['v0', 'v7'],
      survivors: ['v7'], cut: [] }] }],
  };
  const model = STRUCT.racingModel(st);
  assert(model, 'the racing model builds from the interrupted topology');
  assertEqual(model.gateState, 'interrupted',
    'the champion gate never committed — it is not "deciding", and not merely "pending"');
  assertEqual(model.live, false, 'and nothing about it is live');
});

test('shell exports buildLoopControls unchanged (the gate moved, the affordance did not)', () => {
  assert(typeof shell.buildLoopControls === 'function', 'the control builder is still exported');
});

run();
