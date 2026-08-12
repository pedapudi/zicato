// test/experiments_ledger.test.mjs — THE EXPERIMENTS LEDGER (issue #194 §3).
//
// The read: /api/epoch/{id}/experiments-ledger (query/ledger_view.py) — one
// pre-joined row per experiment. The renders: the ledger table on the epoch
// page, and the core-idea THREAD folded onto standings / roster rows.
//
// Pins:
//   * the table renders one row per experiment, in the SERVED order (round
//     order is the server's job — the client never re-sorts);
//   * a long core idea clips and expands IN PLACE, and the expansion SURVIVES
//     a digest-gated re-render (the brief-collapse bug class);
//   * sites truncate past three with a "+N more" carrying the rest on hover;
//   * the deciding rule / rejection reason is rendered VERBATIM (never parsed);
//   * absent fields degrade to '—' per column — a row never vanishes;
//   * the two empty states are DIFFERENT: "no experiments" vs the honest
//     "the index is not built" note;
//   * ledgerDigest: a no-op beat is byte-identical, a settling experiment
//     flips it, and EXPANDING A ROW DOES NOT (or the expand would rebuild the
//     row it just expanded);
//   * coreIdeaLine: truncates, carries the full text on hover, and renders
//     NOTHING for an absent idea (never a '—' that reads as a recorded blank).

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const ledgerMod = await import('../js/views/ledger.js');
const ui = await import('../js/ui.js');

function classOf(node) { return (node && node.getAttribute && node.getAttribute('class')) || ''; }
function hasClass(node, cls) { return classOf(node).split(/\s+/).includes(cls); }
function allByClass(host, cls) {
  return host.querySelectorAll('[class]').filter((n) => hasClass(n, cls));
}
function rowsOf(host) { return allByClass(host, 'dn-ledger-row'); }
function cellText(row, i) {
  const tds = row.children.filter((c) => c.tagName === 'TD');
  return tds[i] ? tds[i].textContent : null;
}

const LONG_IDEA = 'Name the audience in the system prompt and require an explicit slide-structure '
  + 'outline before any prose, so the agent stops drifting into narrative paragraphs';

// The served shape, keys verbatim (query/ledger_view.py build_experiments_ledger).
function ledgerFixture(overrides) {
  return Object.assign({
    epoch_id: 'e0',
    experiments: [
      {
        generation_id: 'v0', round_index: 0, core_idea: 'the seed', mutation_ids: [],
        decision: 'promoted', promoted: true, rejection_reason: null,
        scalar_score_delta: null, drift_loss_delta: null, pass_rate_delta: null,
      },
      {
        generation_id: 'v2', round_index: 1, core_idea: LONG_IDEA,
        mutation_ids: ['agent.temperature', 'prompt.audience', 'prompt.system', 'tools.search'],
        decision: 'rejected', promoted: false,
        rejection_reason: 'insufficient improvement: 0.7328 vs 0.7188 (margin 0.0200)',
        scalar_score_delta: 0.014, drift_loss_delta: 0.01, pass_rate_delta: -0.25,
      },
      {
        generation_id: 'v1', round_index: 1, core_idea: 'trim the preamble',
        mutation_ids: ['prompt.system'], decision: 'promoted', promoted: true,
        rejection_reason: null, scalar_score_delta: -0.08,
        drift_loss_delta: -0.06, pass_rate_delta: 0.1,
      },
      {
        generation_id: 'v3', round_index: 2, core_idea: null, mutation_ids: [],
        decision: null, promoted: null, rejection_reason: null,
        scalar_score_delta: null, drift_loss_delta: null, pass_rate_delta: null,
      },
    ],
  }, overrides || {});
}

function mount(ledger, opts) {
  ledgerMod._resetLedgerExpansion();
  const host = document.createElement('div');
  const panel = ledgerMod.buildExperimentsLedger(ledger, opts || { epochId: 'e0' });
  if (panel) host.appendChild(panel);
  return host;
}

// ── 1. the table: one row per experiment, in the SERVED order ───────────────
test('ledger: one row per experiment, rendered in the SERVED (round) order — the client never re-sorts', () => {
  const host = mount(ledgerFixture());
  const rows = rowsOf(host);
  assertEqual(rows.length, 4, 'four experiments → four rows');
  assertEqual(rows.map((r) => r.getAttribute('data-gen')).join(','), 'v0,v2,v1,v3',
    'the served order is preserved verbatim — round ordering is the reader’s job');
  // round · generation · idea · sites · decision · Δ · reason
  assertEqual(cellText(rows[1], 0), '1', 'the round column reads the served round_index');
  assert(cellText(rows[1], 1).includes('v2'), 'the generation column names the candidate');
});

test('ledger: the promoted row wears the champion treatment and its crown', () => {
  const rows = rowsOf(mount(ledgerFixture()));
  assert(hasClass(rows[2], 'dn-board-champ'), 'a promoted experiment reads as the round’s winner');
  assert(!hasClass(rows[1], 'dn-board-champ'), 'a rejected experiment does not');
});

// ── 2. the core idea: clipped, expandable IN PLACE, expansion survives ──────
test('ledger: a long core idea clips to a button; clicking expands it IN PLACE (full text)', () => {
  const host = mount(ledgerFixture());
  const btns = allByClass(host, 'dn-ledger-idea');
  assertEqual(btns.length, 1, 'only the LONG idea earns an expander');
  const btn = btns[0];
  assert(btn.textContent.length < LONG_IDEA.length, 'the collapsed label is clipped');
  assert(btn.textContent.endsWith('…'), 'the clip is marked with an ellipsis');
  assertEqual(btn.getAttribute('title'), LONG_IDEA, 'the full idea rides the title while collapsed');

  btn.dispatchEvent({ type: 'click' });
  assertEqual(btn.textContent, LONG_IDEA, 'the click expands it to the FULL idea, in place');
  assert(hasClass(btn, 'dn-ledger-idea-open'), 'the expanded state is marked on the node');

  btn.dispatchEvent({ type: 'click' });
  assert(btn.textContent.endsWith('…'), 'clicking again collapses it');
});

test('ledger: an expanded idea SURVIVES a re-render (the digest-gated rebuild must not collapse it)', () => {
  const host = mount(ledgerFixture());
  allByClass(host, 'dn-ledger-idea')[0].dispatchEvent({ type: 'click' });

  // a live beat rebuilds the whole table (gatedSwap) — NOT a fresh module.
  const rebuilt = document.createElement('div');
  rebuilt.appendChild(ledgerMod.buildExperimentsLedger(ledgerFixture(), { epochId: 'e0' }));
  assertEqual(allByClass(rebuilt, 'dn-ledger-idea')[0].textContent, LONG_IDEA,
    'the operator’s expand is remembered across the rebuild');
});

test('ledger: a SHORT core idea is plain text — no dead expander button', () => {
  const host = mount(ledgerFixture());
  const flat = allByClass(host, 'dn-ledger-idea-flat');
  assert(flat.some((n) => n.textContent === 'trim the preamble'), 'a short idea renders as plain text');
});

// ── 3. sites, verdict, Δ, reason ────────────────────────────────────────────
test('ledger: sites name the first three and collapse the rest into "+N more" (full list on hover)', () => {
  const host = mount(ledgerFixture());
  const sites = allByClass(host, 'dn-ledger-site');
  assertEqual(sites.filter((s) => s.textContent === 'tools.search').length, 0,
    'the fourth site is NOT named inline');
  const more = allByClass(host, 'dn-ledger-site-more');
  assertEqual(more.length, 1, 'exactly one row overflows');
  assertEqual(more[0].textContent, '+1 more', 'the overflow is counted, not hidden');
  assertEqual(more[0].getAttribute('title'), 'tools.search', 'the rest ride the hover');
});

test('ledger: the rejection reason is rendered VERBATIM (truncated for width, full on hover)', () => {
  const host = mount(ledgerFixture());
  const reasons = allByClass(host, 'dn-ledger-reason');
  const rejected = reasons.find((n) => (n.getAttribute('title') || '').startsWith('insufficient'));
  assert(rejected, 'the rejected row carries its recorded reason');
  assertEqual(rejected.getAttribute('title'),
    'insufficient improvement: 0.7328 vs 0.7188 (margin 0.0200)',
    'the RECORDED string is preserved exactly — never parsed into a rule name');
});

test('ledger: the Δscalar cell is sign-toned — a regression bad, an improvement good', () => {
  const rows = rowsOf(mount(ledgerFixture()));
  const deltaCell = (row) => row.children.filter((c) => c.tagName === 'TD')[5];
  assert(hasClass(deltaCell(rows[1]), 'dn-bad-t'), 'a positive Δ (worse loss) reads bad');
  assert(hasClass(deltaCell(rows[2]), 'dn-good-t'), 'a negative Δ (better loss) reads good');
  assertEqual(deltaCell(rows[1]).textContent, '+0.014', 'the Δ is signed at ledger precision');
});

test('ledger: absent fields degrade to "—" PER COLUMN — an unsettled row never vanishes', () => {
  const rows = rowsOf(mount(ledgerFixture()));
  const v3 = rows[3];
  assertEqual(v3.getAttribute('data-gen'), 'v3', 'the in-flight experiment still has a row');
  assertEqual(cellText(v3, 2), '—', 'no core idea reads as a dash');
  assertEqual(cellText(v3, 3), '—', 'no sites reads as a dash');
  assertEqual(cellText(v3, 5), '—', 'no Δ reads as a dash, never a fabricated 0');
  assertEqual(cellText(v3, 6), '—', 'no reason reads as a dash');
  assert(cellText(v3, 4).includes('racing'), 'an undecided experiment reads as still racing, NOT rejected');
});

test('ledger: the generation links to its dossier when the caller supplies a route', () => {
  const host = mount(ledgerFixture(), { epochId: 'e0', hrefFor: (g) => '#/candidate/e0/' + g });
  const links = allByClass(host, 'dn-linkbtn');
  assert(links.some((a) => a.getAttribute('href') === '#/candidate/e0/v2'), 'each row opens its candidate');
});

// ── 4. the two empty states are DIFFERENT facts ─────────────────────────────
test('ledger: an empty epoch and an UNBUILT INDEX read differently — never one silence for both', () => {
  const empty = mount({ epoch_id: 'e0', experiments: [] });
  assert(empty.textContent.includes('No experiments recorded'), 'an empty epoch says so');
  assert(!empty.textContent.includes('reindex'), '...and does not blame the index');

  const noIndex = mount({ epoch_id: 'e0', experiments: [], note: 'index not built; run zicato reindex' });
  assert(noIndex.textContent.includes('index not built'), 'an unbuilt index is NAMED, not silently empty');

  assertEqual(ledgerMod.buildExperimentsLedger(null), null,
    'a failed read (null) omits the panel entirely — "could not ask" is not "nothing to show"');
});

// ── 5. the digest ───────────────────────────────────────────────────────────
test('ledgerDigest: byte-identical on a no-op beat; flips when an experiment settles', () => {
  const a = JSON.stringify(ledgerMod.ledgerDigest(ledgerFixture()));
  const b = JSON.stringify(ledgerMod.ledgerDigest(ledgerFixture()));
  assertEqual(a, b, 'a no-op heartbeat re-emits an identical digest (no repaint, no flash)');

  const settled = ledgerFixture();
  settled.experiments[3].decision = 'promoted';
  settled.experiments[3].promoted = true;
  settled.experiments[3].scalar_score_delta = -0.02;
  assert(JSON.stringify(ledgerMod.ledgerDigest(settled)) !== a, 'a settling experiment repaints');

  assertEqual(ledgerMod.ledgerDigest(null), null, 'an absent read contributes no digest');
});

test('ledgerDigest: EXPANDING a row does not change the digest (an expand must not rebuild its own row)', () => {
  const host = mount(ledgerFixture());
  const before = JSON.stringify(ledgerMod.ledgerDigest(ledgerFixture()));
  allByClass(host, 'dn-ledger-idea')[0].dispatchEvent({ type: 'click' });
  assertEqual(JSON.stringify(ledgerMod.ledgerDigest(ledgerFixture())), before,
    'the expand memory is deliberately OUTSIDE the digest');
});

test('ledgerDigest: the served note is folded — an index appearing repaints the panel', () => {
  const cold = ledgerMod.ledgerDigest({ epoch_id: 'e0', experiments: [], note: 'index not built; run zicato reindex' });
  const warm = ledgerMod.ledgerDigest({ epoch_id: 'e0', experiments: [] });
  assert(JSON.stringify(cold) !== JSON.stringify(warm), 'the note is part of what is rendered, so it gates');
});

// ── 6. the core-idea THREAD (standings / roster second line) ────────────────
test('coreIdeaLine: truncates to one line, carries the full idea on hover', () => {
  const line = ui.coreIdeaLine(LONG_IDEA);
  assert(line, 'a recorded idea earns a line');
  assert(hasClass(line, 'dn-coreidea'), 'the thread wears its own quiet class');
  assert(line.textContent.length <= 72, 'the visible text is clipped to one row-height line');
  assertEqual(line.getAttribute('title'), LONG_IDEA, 'the full idea rides the hover');
});

test('coreIdeaLine: an ABSENT idea renders NOTHING — never a "—" that reads as a recorded blank', () => {
  assertEqual(ui.coreIdeaLine(null), null, 'null → no line');
  assertEqual(ui.coreIdeaLine(''), null, 'empty → no line');
  assertEqual(ui.coreIdeaLine('   '), null, 'whitespace-only → no line');
  assertEqual(ui.coreIdeaLine(42), null, 'a non-string → no line');
});

// ── 7. WHERE the ledger sits on the epoch page ─────────────────────────────
// A CLOSED epoch IS its ledger — the whole story, settled — so it leads. An
// OPEN epoch's live question is "what is happening now", so there it follows
// the round timeline. This is the placement rule, pinned against the real view.

const { router, lookupFixture, freshState } = await import('./fixtures.mjs');
const LED_EPOCH = '2026-08-01_ledger';

function installLedgerFetch(closed) {
  const gens = [
    { generation_id: 'v0', epoch_id: LED_EPOCH, parent_generation_id: '', promoted: false, round_index: 0 },
    { generation_id: 'v1', epoch_id: LED_EPOCH, parent_generation_id: 'v0', promoted: true, round_index: 0 },
  ];
  const F = {
    '/api/epoch': {
      epoch_id: LED_EPOCH, closed, goal: 'Ledger placement.', brief: '# Epoch\n\nshort brief',
      experiments: gens.map((g) => ({ generation_id: g.generation_id, parent_generation_id: g.parent_generation_id })),
      board: [{ entry_id: 'b1', kind: 'single_turn' }],
    },
    '/api/lineage': { generations: gens },
    '/api/tournaments': { epoch_id: LED_EPOCH, champion_lineage: ['v0', 'v1'], matchups: [], tournaments: [] },
    '/api/score-trajectory': { points: [{ generation_id: 'v0', scalar: 100 }, { generation_id: 'v1', scalar: 80 }] },
    [`/api/epoch/${LED_EPOCH}/experiments-ledger`]: ledgerFixture({ epoch_id: LED_EPOCH }),
  };
  for (const g of gens) F[`/api/generation/${LED_EPOCH}/${g.generation_id}/per-entry`] = { entries: [{ entry_id: 'b1', drift_loss: 50 }] };
  globalThis.fetch = async (path) => {
    const v = lookupFixture(F, path);
    return v !== undefined ? { ok: true, json: async () => v } : { ok: false, status: 404, json: async () => ({}) };
  };
}

// The ordinal of a section title within the page, by its <h2> text.
async function sectionOrder(closed) {
  freshState(); installLedgerFetch(closed);
  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: LED_EPOCH });
  const heads = host.querySelectorAll('[class]').filter((n) => n.tagName === 'H2').map((n) => n.textContent);
  return { heads, host };
}

test('epoch page: a CLOSED epoch LEADS with the experiments ledger — it IS the epoch’s story', async () => {
  const { heads, host } = await sectionOrder(true);
  const ledgerAt = heads.findIndex((h) => h.startsWith('Experiments ·'));
  const timelineAt = heads.findIndex((h) => h.startsWith('Round timeline'));
  assert(ledgerAt >= 0, 'the ledger section rendered');
  assertEqual(ledgerAt, 0, 'the ledger is the first section on a closed epoch');
  assert(ledgerAt < timelineAt, '...and it precedes the round timeline');
  assertEqual(rowsOf(host).length, 4, 'with a row per experiment');
});

test('epoch page: an OPEN epoch keeps the live round timeline first, ledger below it', async () => {
  const { heads } = await sectionOrder(false);
  const ledgerAt = heads.findIndex((h) => h.startsWith('Experiments ·'));
  const timelineAt = heads.findIndex((h) => h.startsWith('Round timeline'));
  assert(ledgerAt >= 0 && timelineAt >= 0, 'both sections rendered');
  assert(timelineAt < ledgerAt, 'the live round timeline still leads an open epoch');
});

test('epoch page: a backend that does not serve the ledger omits the section entirely', async () => {
  freshState(); installLedgerFetch(true);
  const inner = globalThis.fetch;
  globalThis.fetch = async (path) => (String(path).includes('experiments-ledger')
    ? { ok: false, status: 404, json: async () => ({}) } : inner(path));
  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: LED_EPOCH });
  const heads = host.querySelectorAll('[class]').filter((n) => n.tagName === 'H2').map((n) => n.textContent);
  assert(!heads.some((h) => h.startsWith('Experiments ·')), 'no ledger read → no ledger section (byte-identical to before)');
  assert(heads.some((h) => h.startsWith('Round timeline')), '...and the rest of the page is unchanged');
});

await run();
