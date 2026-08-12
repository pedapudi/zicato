// test/judges_panel.test.mjs — the board page's JUDGES panel (#194 §5).
//
// The panel joins two payloads: the entry's own judges off the epoch payload's
// additive `board_judges` map (authored), and the built-in roster after
// `disable_drift` suppression off /api/epoch/{id}/judge-roster (derived).
//
// Covers:
//   * the suppression sentence — including the case the panel exists for: a
//     `disable_drift` kind NO built-in judge emits suppresses nothing, and the
//     line says so by name rather than leaving silence to imply a disarm;
//   * a suppressed built-in staying ON the strip, struck and captioned (a
//     shorter list would show the header's effect by omission, i.e. not at all);
//   * the custom-judge table — mode, severity chip, the weight beside the name,
//     a python judge's dotted path shown and an inline judge's PROMPT never;
//   * the scorecard link (epoch → reflection → judge) and its absence;
//   * the degrades: goldfive absent, and no judges at all ("predicate/rubric
//     only — no judges configured", which is information, not absence);
//   * the digest no-op gate;
//   * full render: the panel mounts in the CONTRACT region (before the results),
//     survives a no-op beat with zero DOM churn, and is absent entirely on a
//     pre-feature server (no roster, no board_judges) so the page reads
//     byte-identical to before the feature.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const board = await import('../js/views/board.js');
const router = await import('../js/router.js');
const { state } = await import('../js/core/state.js');
const data = await import('../js/data.js');

// ---- fixtures --------------------------------------------------------

// The June target_1 shape, plus a genuinely-suppressed built-in so both halves
// of the suppression story are on one payload.
const ROSTER = {
  epoch_id: 'e4',
  builtins: [
    { name: 'reasoning_drift', suppressed: false, suppressed_by: [] },
    { name: 'goal_drift', suppressed: false, suppressed_by: [] },
    { name: 'tool_error', suppressed: true, suppressed_by: ['tool_error'] },
    { name: 'refusal', suppressed: false, suppressed_by: [] },
  ],
  builtins_note: null,
  disable_drift: ['tool_error', 'user_steer'],
  unmapped_drift_kinds: ['user_steer'],
  per_judge_weights: { file_findability: 2.0, tool_error: 0.75 },
  default_judge_weight: 1.0,
  scorecards: { file_findability: 'refl_2' },
};

const ENTRY_JUDGES = [
  { name: 'audience_appropriate', mode: 'inline', severity: 'warning' },
  { name: 'file_findability', mode: 'python', severity: 'critical', path: 'pkg.judges:FileFindabilityJudge' },
];

// goldfive is an optional extra; without it the built-in set cannot be
// enumerated at all. The suppression MAPPING needs no goldfive, so the
// unmapped-kind analysis survives.
const ROSTER_NO_GOLDFIVE = {
  epoch_id: 'e4', builtins: [], builtins_note: 'built-in roster unavailable (goldfive not installed)',
  disable_drift: ['user_steer'], unmapped_drift_kinds: ['user_steer'],
  per_judge_weights: {}, default_judge_weight: null, scorecards: {},
};

// ---- DOM helpers -----------------------------------------------------

function ctxReal() { return { navigate() {}, href: router.href }; }

function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).includes(cls));
}
function allTags(node, tag) {
  const out = [];
  const t = tag.toUpperCase();
  const walk = (n) => { for (const c of (n.children || [])) { if (c.tagName === t) out.push(c); walk(c); } };
  walk(node);
  return out;
}
function textOf(node) {
  let s = '';
  const walk = (n) => { for (const c of (n.childNodes || [])) { if (c.nodeType === 3) s += c.textContent; else walk(c); } };
  walk(node);
  return s;
}
function panel(roster, entryJudges) {
  const host = document.createElement('div');
  host.appendChild(board.judgesPanel(roster, entryJudges, ctxReal(), 'e4'));
  return host;
}
// one row of the custom-judge table, as its cell texts.
function judgeRows(host) {
  return allTags(host, 'tbody').flatMap((b) => allTags(b, 'tr')).map((tr) => allTags(tr, 'td').map(textOf));
}

// ════════════════════════════════════════════════════════════════════
// 1 — the suppression sentence
// ════════════════════════════════════════════════════════════════════

test('suppression: a kind NO built-in emits is named as suppressing nothing', () => {
  const only = { ...ROSTER, builtins: ROSTER.builtins.map((b) => ({ ...b, suppressed: false, suppressed_by: [] })),
    disable_drift: ['user_steer', 'user_pause'], unmapped_drift_kinds: ['user_steer', 'user_pause'] };
  const line = board.suppressionText(only);
  assert(/user_steer, user_pause/.test(line), 'the unmapped kinds are named');
  assert(/no built-in judge emits/.test(line), 'the line says no built-in emits them');
  assert(/they suppress nothing/.test(line), 'and that nothing was suppressed (plural)');
});

test('suppression: one unmapped kind reads in the singular', () => {
  const line = board.suppressionText(ROSTER_NO_GOLDFIVE);
  assert(/it suppresses nothing/.test(line), 'singular agreement for a single unmapped kind: ' + line);
});

test('suppression: a MIXED header reports both the real disarm and the no-op', () => {
  const line = board.suppressionText(ROSTER);
  assert(/disable_drift · tool_error · user_steer/.test(line), 'the header kinds lead the line');
  assert(/suppresses tool_error/.test(line), 'the built-in that really went dark is named');
  assert(/no built-in judge emits user_steer/.test(line), 'the no-op kind is named too');
});

test('suppression: no header ⇒ no line at all (nothing to explain)', () => {
  assertEqual(board.suppressionText({ ...ROSTER, disable_drift: [], unmapped_drift_kinds: [] }), null);
  assertEqual(board.suppressionText(null), null, 'a null roster is not a suppression claim');
});

// ════════════════════════════════════════════════════════════════════
// 2 — quiet weight precision
// ════════════════════════════════════════════════════════════════════

test('weights: an integral weight prints bare, a fractional one to two places', () => {
  assertEqual(board.weightText(2), '×2');
  assertEqual(board.weightText(0.75), '×0.75');
  assertEqual(board.weightText(1.5), '×1.50');
  assertEqual(board.weightText(null), null, 'no configured weight → nothing to print');
  assertEqual(board.weightText('heavy'), null, 'a non-number is not a weight');
});

// ════════════════════════════════════════════════════════════════════
// 3 — the armed built-ins strip
// ════════════════════════════════════════════════════════════════════

test('builtins: every default judge is a chip; the SUPPRESSED one stays, struck and captioned', () => {
  const host = panel(ROSTER, ENTRY_JUDGES);
  const strip = allByClass(host, 'dn-judges-strip')[0];
  assertEqual(strip.children.length, 4, 'all four built-ins render — the suppressed one is NOT filtered out');
  const off = allByClass(host, 'dn-judge-off');
  assertEqual(off.length, 1, 'exactly the suppressed built-in is marked off');
  assert(/tool_error/.test(textOf(off[0])), 'the dark judge is tool_error');
  assert(/suppressed by disable_drift/.test(textOf(off[0])),
    'the reason is IN the chip, not only in a tooltip an operator must hover to find');
  assert(allByClass(off[0], 'dn-judge-name').length === 1, 'the name carries the struck-through class');
});

test('builtins: a per_judge_weight rides the built-in chip too (the lookup keys on NAME, not on half)', () => {
  const host = panel(ROSTER, ENTRY_JUDGES);
  const off = allByClass(host, 'dn-judge-off')[0];
  assert(/×0.75/.test(textOf(off)), 'tool_error carries its configured weight: ' + textOf(off));
  const undecorated = allByClass(host, 'dn-pill').find((c) => /reasoning_drift/.test(textOf(c)));
  assert(!/×/.test(textOf(undecorated)), 'a judge with no configured weight shows no weight');
});

// ════════════════════════════════════════════════════════════════════
// 4 — this entry's custom judges
// ════════════════════════════════════════════════════════════════════

test('custom: each judge renders name / mode / severity, with the weight beside the name', () => {
  const host = panel(ROSTER, ENTRY_JUDGES);
  const rows = judgeRows(host);
  assertEqual(rows.length, 2, 'both of the entry\'s judges render');
  assert(/audience_appropriate/.test(rows[0][0]) && rows[0][1] === 'inline', 'the inline judge names its mode');
  assert(/file_findability/.test(rows[1][0]) && rows[1][1] === 'python', 'the python judge names its mode');
  assert(/×2/.test(rows[1][0]), 'the configured weight rides beside the name');
  assert(!/×/.test(rows[0][0]), 'an unweighted judge shows no weight');
  const sevs = allByClass(host, 'dn-pill').map(textOf);
  assert(sevs.includes('warning') && sevs.includes('critical'), 'severity renders as a chip');
});

test('custom: severity chips reuse the shipped tone vocabulary rather than a new palette', () => {
  assertEqual(board.severityTone('critical'), 'rejected');
  assertEqual(board.severityTone('warning'), 'deferred');
  assertEqual(board.severityTone('info'), 'baseline');
  assertEqual(board.severityTone('something_else'), 'baseline', 'an unknown severity reads neutral, never alarming');
});

test('custom: a python judge shows its dotted path; an inline judge NEVER shows its prompt', () => {
  const withPrompt = [{ name: 'audience_appropriate', mode: 'inline', severity: 'warning',
    body: 'The agent keeps the explanation accessible.' }];
  const host = panel(ROSTER, withPrompt.concat(ENTRY_JUDGES[1]));
  const text = textOf(host);
  assert(/pkg.judges:FileFindabilityJudge/.test(text), 'the python judge\'s callable is identified');
  assert(!/accessible/.test(text), 'an inline criterion is prompt text and stays off screen');
});

// ════════════════════════════════════════════════════════════════════
// 5 — reflection scorecard links
// ════════════════════════════════════════════════════════════════════

test('scorecards: a scored judge links to its card in the Instrument lens; an unscored one does not', () => {
  const host = panel(ROSTER, ENTRY_JUDGES);
  const links = allTags(host, 'a');
  assertEqual(links.length, 1, 'only the judge a reflection actually scored gets a link');
  assertEqual(links[0].getAttribute('href'),
    router.href('instrument', { epochId: 'e4', reflectionId: 'refl_2', judge: 'file_findability' }),
    'the link lands on THAT judge\'s card, not the reflection front page');
  const rows = judgeRows(host);
  assertEqual(rows[0][3], '—', 'the unscored judge reads as an em-dash, never a dead link');
});

// ════════════════════════════════════════════════════════════════════
// 6 — degrades
// ════════════════════════════════════════════════════════════════════

test('degrade: no goldfive ⇒ the served REASON is shown, never a guessed roster', () => {
  const host = panel(ROSTER_NO_GOLDFIVE, ENTRY_JUDGES);
  assert(!allByClass(host, 'dn-judges-strip')[0], 'no chips are invented');
  const note = allByClass(host, 'dn-empty').map(textOf).join(' ');
  assert(/goldfive not installed/.test(note), 'the server\'s reason is surfaced verbatim');
  // The suppression analysis needs no goldfive, so it must still be there.
  assert(/no built-in judge emits user_steer/.test(textOf(host)),
    'the header\'s (non-)effect is still reported without goldfive');
});

test('degrade: an entry with no judges says so as INFORMATION, not as absence', () => {
  const host = panel(ROSTER, []);
  assert(/predicate\/rubric only — no judges configured/.test(textOf(host)),
    'the empty state states the contract fact: ' + textOf(host));
  assert(allByClass(host, 'dn-judges-strip')[0], 'the built-ins still render — they are armed regardless');
});

test('degrade: a null roster still renders the entry\'s own judges', () => {
  const host = panel(null, ENTRY_JUDGES);
  assertEqual(judgeRows(host).length, 2, 'the authored half survives a missing derived half');
  assert(!allByClass(host, 'dn-judges-strip')[0], 'and no built-in roster is fabricated');
});

// ════════════════════════════════════════════════════════════════════
// 7 — the digest (no-op gate)
// ════════════════════════════════════════════════════════════════════

test('digest: a deep-equal payload yields a byte-identical digest; any moved field flips it', () => {
  const a = board.judgesDigest(ROSTER, ENTRY_JUDGES);
  assertEqual(a, board.judgesDigest(JSON.parse(JSON.stringify(ROSTER)), JSON.parse(JSON.stringify(ENTRY_JUDGES))),
    'a re-served identical payload churns nothing');
  const rearmed = JSON.parse(JSON.stringify(ROSTER));
  rearmed.builtins[2].suppressed = false;
  assert(board.judgesDigest(rearmed, ENTRY_JUDGES) !== a, 'a built-in coming back on flips the digest');
  const reweighted = JSON.parse(JSON.stringify(ROSTER));
  reweighted.per_judge_weights.file_findability = 3;
  assert(board.judgesDigest(reweighted, ENTRY_JUDGES) !== a, 'a changed weight flips the digest');
  assert(board.judgesDigest(ROSTER, []) !== a, 'losing the entry\'s judges flips the digest');
});

// ════════════════════════════════════════════════════════════════════
// 8 — full render
// ════════════════════════════════════════════════════════════════════

const STORE = {
  epoch: {
    epoch_id: 'e4',
    board: [{ entry_id: 'transformers_lay_audience', kind: 'single_turn', weight: 1, budget_s: 60, input_preview: 'Build slides' }],
    board_judges: { transformers_lay_audience: ENTRY_JUDGES },
  },
  lineage: { generations: [{ generation_id: 'g0', parent_generation_id: null, promoted: true, epoch_id: 'e4' }] },
  traj: { points: [] },
  perEntry: { g0: { entries: [{ entry_id: 'transformers_lay_audience', drift_loss: 0.4, pass_fail: true, run_id: 'r0' }] } },
  roster: ROSTER,
  dossier: null,
};

function installFetch(store) {
  globalThis.fetch = async (path) => {
    const p = String(path);
    let body = {};
    if (p.includes('/judge-roster')) body = store.roster;
    else if (p.includes('/eval/')) body = store.dossier;
    else if (p.startsWith('/api/lineage')) body = store.lineage;
    else if (p.startsWith('/api/score-trajectory')) body = store.traj;
    else if (p.startsWith('/api/epoch')) body = store.epoch;
    else if (p.includes('/per-entry')) {
      const m = p.match(/\/api\/generation\/[^/]+\/([^/]+)\/per-entry/);
      body = (m && store.perEntry[m[1]]) || { entries: [] };
    }
    return { ok: true, async json() { return body; } };
  };
}

function resetForRender() {
  state.lastSeq = -1;
  state.activeRuns = [];
  data.invalidate();
}

const PARAMS = { epochId: 'e4', entry: 'transformers_lay_audience' };

test('render: the panel mounts in the CONTRACT region — before any result section', async () => {
  resetForRender();
  installFetch({ ...STORE, roster: ROSTER });
  const host = document.createElement('div');
  await board.render(host, ctxReal(), PARAMS);
  const heads = allTags(host, 'h2').map((h) => h.textContent);
  const judges = heads.findIndex((t) => /^Judges/.test(t));
  const loss = heads.findIndex((t) => /Per-candidate loss/.test(t));
  assert(judges >= 0, 'the Judges section rendered: ' + heads.join(' | '));
  assert(loss >= 0 && judges < loss, 'what grades a run is part of the question, so it precedes the answers');
  assert(/no built-in judge emits user_steer/.test(textOf(host)), 'the served suppression story reached the page');
});

test('render: a no-op beat leaves the panel DOM untouched (the flashing-render class)', async () => {
  resetForRender();
  installFetch({ ...STORE, roster: ROSTER });
  const host = document.createElement('div');
  const ctx = ctxReal();
  await board.render(host, ctx, PARAMS);
  const upper = host.querySelector(':scope > [data-node="board-upper"]');
  const digest = upper.getAttribute('data-t-digest');
  const strip = allByClass(upper, 'dn-judges-strip')[0];
  assert(strip, 'the built-in strip rendered');

  await board.render(host, ctx, PARAMS);
  const upper2 = host.querySelector(':scope > [data-node="board-upper"]');
  assertEqual(upper2.getAttribute('data-t-digest'), digest, 'an identical beat leaves the upper digest byte-identical');
  assert(allByClass(upper2, 'dn-judges-strip')[0] === strip, 'the chip strip node identity survives (ZERO DOM)');
});

test('render: a pre-feature server (no roster, no board_judges) draws NO panel at all', async () => {
  resetForRender();
  installFetch({ ...STORE, roster: null, epoch: { ...STORE.epoch, board_judges: undefined } });
  const host = document.createElement('div');
  await board.render(host, ctxReal(), PARAMS);
  const heads = allTags(host, 'h2').map((h) => h.textContent);
  assert(!heads.some((t) => /^Judges/.test(t)), 'nothing is known, so nothing is claimed: ' + heads.join(' | '));
  assert(!/no judges configured/.test(textOf(host)), 'and no empty-state is asserted about a contract we cannot read');
});

test('render: an entry the board_judges map omits reads as "no judges configured" beside the armed built-ins', async () => {
  resetForRender();
  installFetch({ ...STORE, epoch: { ...STORE.epoch, board_judges: { some_other_entry: ENTRY_JUDGES } } });
  const host = document.createElement('div');
  await board.render(host, ctxReal(), PARAMS);
  assert(/predicate\/rubric only — no judges configured/.test(textOf(host)),
    'the omission is a fact about THIS entry, reported as one');
  assert(allByClass(host, 'dn-judges-strip')[0], 'the built-ins are armed for it all the same');
});

await run();
