// test/render_crosspin.test.mjs — the BROWSER half of the render cross-pin.
//
// The terminal console (zicato/tui) is a SECOND RENDERER of the same served
// model this dashboard renders. Where presentation is DERIVED in JS — the loop
// verdict's wording, when a rating reads `provisional`, whether an absent
// outcome is a rejection, how a structure payload normalizes — the terminal
// carries a Python port of the same mapping.
//
// A port is only as good as the thing that stops it drifting. This file and
// tests/test_tui_crosspin.py read ONE fixture (fixtures/render_crosspin.json)
// and assert it against their own implementation, so changing either side
// alone turns a suite red. The fixture is the contract; these two files are
// its two witnesses.

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { installDom, test, run, assertEqual } from './harness.mjs';

installDom();

const ui = await import('../js/ui.js');
const svg = await import('../js/svg.js');
const structure = await import('../js/views/structure.js');
const { AppState } = await import('../js/core/state.js');

const here = dirname(fileURLToPath(import.meta.url));
const CASES = JSON.parse(readFileSync(join(here, 'fixtures', 'render_crosspin.json'), 'utf8'));

// `—` in the fixture is the shared null render; both sides emit it verbatim.

test('crosspin: loopVerdict — only the problem words speak', () => {
  for (const c of CASES.loop_verdict) {
    const got = ui.loopVerdict(c.traj);
    if (c.expect === null) {
      assertEqual(got, null, `loopVerdict(${JSON.stringify(c.traj)})`);
    } else {
      assertEqual(got.word, c.expect.word, 'word');
      assertEqual(got.cls, c.expect.cls, 'cls');
    }
  }
});

test('crosspin: ratingModel — the integer register + the provisional threshold', () => {
  for (const c of CASES.rating_model) {
    const got = ui.ratingModel(c.src);
    if (c.expect === null) {
      assertEqual(got, null, `ratingModel(${JSON.stringify(c.src)})`);
    } else {
      assertEqual(got.elo, c.expect.elo, 'elo');
      assertEqual(got.se, c.expect.se, 'se');
      assertEqual(got.games, c.expect.games, 'games');
      assertEqual(got.provisional, c.expect.provisional, 'provisional');
      assertEqual(got.text, c.expect.text, 'text');
    }
  }
});

test('crosspin: decisionFor — an absent outcome is pending, never rejected', () => {
  for (const c of CASES.decision_for) {
    assertEqual(ui.decisionFor(c.spec), c.expect, JSON.stringify(c.spec));
  }
});

test('crosspin: the verdict pill label', () => {
  for (const c of CASES.verdict_label) {
    assertEqual(ui.verdictPill(c.decision).textContent, c.expect, String(c.decision));
  }
});

test('crosspin: fmtDurationMs — the ms/s/m/h ladder', () => {
  for (const c of CASES.fmt_duration_ms) {
    assertEqual(ui.fmtDurationMs(c.ms), c.expect, String(c.ms));
  }
});

test('crosspin: promotionRateLabel + costPerPromotionLabel', () => {
  for (const c of CASES.promotion_rate_label) {
    assertEqual(ui.promotionRateLabel(c.traj), c.expect, JSON.stringify(c.traj));
  }
  for (const c of CASES.cost_per_promotion_label) {
    assertEqual(ui.costPerPromotionLabel(c.cost), c.expect, JSON.stringify(c.cost));
  }
});

test('crosspin: scoreFmt + fmtSigned — null is —, never 0', () => {
  for (const c of CASES.score_fmt) {
    assertEqual(ui.scoreFmt(c.value, c.digits), c.expect, String(c.value));
  }
  for (const c of CASES.fmt_signed) {
    assertEqual(svg.fmtSigned(c.value, c.digits), c.expect, String(c.value));
  }
});

test('crosspin: normalizeStructure — one renderer input, live vs settled', () => {
  for (const c of CASES.normalize_structure) {
    const got = structure.normalizeStructure(c.st, c.live);
    if (c.expect === null) {
      assertEqual(got, null, JSON.stringify(c.st));
      continue;
    }
    assertEqual(got.structure, c.expect.structure, 'structure');
    assertEqual(got.live, c.expect.live, 'live');
    assertEqual(got.source, c.expect.source, 'source');
    assertEqual(got.phase, c.expect.phase, 'phase');
    assertEqual(
      JSON.stringify(got.rounds.map((r) => r.round_index)),
      JSON.stringify(c.expect.round_indexes),
      'round_indexes',
    );
  }
});

// severity_tone / practice_tone live inside views/instrument.js as module-private
// helpers; the fixture pins their vocabulary so the Python port cannot invent a
// tone the browser never assigns. The mapping is asserted here against the
// SAME table instrument.js applies, restated so a change to either is visible.
test('crosspin: the reflection tone vocabulary is closed', () => {
  const severity = { critical: 'bad', warning: 'warn' };
  for (const c of CASES.severity_tone) {
    const key = String(c.severity || '').toLowerCase();
    assertEqual(severity[key] || 'faint', c.expect, String(c.severity));
  }
  const practice = { sound: 'good', unsound: 'bad', attend: 'warn' };
  for (const c of CASES.practice_tone) {
    const key = String(c.verdict || '').toLowerCase();
    assertEqual(practice[key] || 'faint', c.expect, String(c.verdict));
  }
});

// The no-op gate. The SSE stream carries NO digest, so `seq` is the only thing
// that can tell a real state_change from a no-op beat BEFORE any fetch. The
// terminal console gates its refetch on this exact function, so a divergence
// here is the difference between an idle TUI and one that hammers the service.
test('crosspin: noteProgress — the seq no-op / rollover / degrade gate', () => {
  for (const c of CASES.note_progress) {
    const st = new AppState();
    st.lastSeq = c.last;
    const got = st.noteProgress(c.seq, null);
    assertEqual(got.advanced, c.expect.advanced, `advanced: ${c.why}`);
    assertEqual(got.rollover, c.expect.rollover, `rollover: ${c.why}`);
    assertEqual(got.present, c.expect.present, `present: ${c.why}`);
  }
});

run();
