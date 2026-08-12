// test/figure_sizing.test.mjs — THE FIGURE-WIDTH CONTRACT (console review #207
// items 1, 5, 6 + the layout rule).
//
// A figure's width is its INTRINSIC content width, capped; full width belongs
// to tables and timelines. An SVG at `width:100%` scales its own coordinate
// system, so every mark, radius and `<text>` magnifies with the pane — the
// calibration trend drew its captions at ~3× on a wide screen. The other shape
// of the same defect is a `preserveAspectRatio:'none'` figure stretched across
// a `1fr` grid lane: a horizontal-only scale that flattens slopes and smears
// dots into ellipses (the per-judge trend).
//
// Pins:
//   * svg.js exposes ONE intrinsic-sizing mechanism, and the only builders that
//     may go full-width (`applyResponsive`) are those with a matched
//     `svg.dn-*-hero` max-width cap in console.css — an uncapped hero is the
//     bug, so the sweep is a test, not a comment;
//   * calibrationTrend renders at a card-scale INTRINSIC width (no 100%, no
//     aspect-ratio hero, no hero class) and still paints its end label + caption;
//   * sparkline({intrinsic}) pins its px width + max-width:100%; the default and
//     the responsive hero paths are UNCHANGED (back-compat);
//   * the per-judge trend rows carry an intrinsic 280px spark and a right-
//     aligned value column;
//   * the epoch view packs heatmap | per-judge trend | calibration into ONE
//     Measurement band (a wrapping grid), and the heatmap's full-width panel
//     is gone;
//   * a no-op heartbeat over the epoch view churns NO DOM (digest-gated).

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const {
  router, data, freshState, allByClass, readCss, svgsByClass, installFixtureMap,
} = await import('./fixtures.mjs');
const svg = await import('../js/svg.js');
const epochView = await import('../js/views/epoch.js');

const CSS = readCss();

function attr(node, name) { return (node && node.getAttribute && node.getAttribute(name)) || ''; }
function styleOf(node) { return attr(node, 'style'); }
function classOf(node) { return attr(node, 'class'); }
function mountInto(node) { const h = document.createElement('div'); if (node) h.appendChild(node); return h; }
function textNodes(host) {
  return host.querySelectorAll('[class]').filter((n) => n.localName === 'text');
}

// ── 1. the CLASS sweep: every full-width hero carries a width cap ───────────
//
// `applyResponsive(…, heroClass)` is what makes a figure scale with the pane.
// That is only legitimate when console.css caps the class — otherwise the
// figure (and its text) balloons without bound. This is the exact defect the
// calibration trend shipped with: it emitted `dn-caltrend-hero`, and no such
// rule existed. Read the hero classes svg.js emits out of the source and
// require a cap for each.
test('CLASS: every responsive hero class svg.js emits has a max-width cap in console.css', async () => {
  const fs = await import('node:fs');
  const src = fs.readFileSync(new URL('../js/svg.js', import.meta.url), 'utf8');
  const emitted = new Set();
  for (const m of src.matchAll(/'(dn-[a-z0-9]+-hero)'/g)) emitted.add(m[1]);
  assert(emitted.size >= 8, 'the sweep actually found the hero classes (' + emitted.size + ')');
  const uncapped = [...emitted].filter((cls) => !new RegExp(
    '\\.' + cls + '\\b[^{]*\\{[^}]*max-width', 's').test(CSS)
    && !new RegExp('\\.' + cls + ',[^{]*\\{[^}]*max-width', 's').test(CSS));
  // a hero class may be capped in a grouped selector; fall back to a direct
  // "is it mentioned in a rule that sets max-width" scan for those.
  const stillUncapped = uncapped.filter((cls) => {
    for (const rule of CSS.split('}')) {
      if (rule.includes('.' + cls) && /max-width/.test(rule)) return false;
    }
    return true;
  });
  assertEqual(stillUncapped.join(', '), '',
    'an uncapped full-width hero magnifies its own text without bound');
});

// ── 2. calibrationTrend: intrinsic, card-scale ─────────────────────────────
function calibFixture(overrides) {
  return Object.assign({
    points: [
      { generation_id: 'v0', score_fraction: 0.5, total_claims: 2, decision: 'promoted' },
      { generation_id: 'v1', score_fraction: null, total_claims: 0, decision: 'rejected' },
      { generation_id: 'v2', score_fraction: 0.75, total_claims: 4, decision: 'promoted' },
    ],
    rolling_mean: 0.62, trend_sign: 1, latest_fraction: 0.75, n_scored: 2,
  }, overrides || {});
}

test('calibrationTrend: INTRINSIC px width — not 100%, no hero class, no aspect-ratio', () => {
  const fig = svg.calibrationTrend(calibFixture());
  assertEqual(String(attr(fig, 'width')), '340', 'the figure is 340px of card-scale trend');
  assert(!/100%/.test(String(attr(fig, 'width'))), 'never width:100% — that is what magnified it');
  assert(!/-hero\b/.test(classOf(fig)), 'no hero class: ' + classOf(fig));
  assert(!/aspect-ratio/.test(styleOf(fig)), 'no pinned aspect-ratio (the hero mechanism)');
  assert(/max-width:\s*100%/.test(styleOf(fig)), 'capped by its pane, so a narrow card shrinks it uniformly');
  assertEqual(attr(fig, 'preserveAspectRatio'), 'xMinYMid meet',
    'meet (uniform) — never the "none" stretch');
  assertEqual(String(attr(fig, 'viewBox')), '0 0 340 78', 'viewBox width == the px width → scale exactly 1');
});

test('calibrationTrend: an explicit responsive flag can no longer re-inflate it', () => {
  const fig = svg.calibrationTrend(Object.assign(calibFixture(), { responsive: true, fitWidth: true }));
  assertEqual(String(attr(fig, 'width')), '340', 'the compact figure has no full-width mode at all');
  assert(!/aspect-ratio/.test(styleOf(fig)), 'the hero path is gone, not merely unused');
});

test('calibrationTrend: still paints the served readouts (end label + n_scored caption)', () => {
  const host = mountInto(svg.calibrationTrend(calibFixture()));
  const texts = textNodes(host).map((n) => n.textContent);
  assert(texts.includes('75%'), 'the latest-fraction end label survives the resize: ' + texts.join(' | '));
  assert(texts.some((t) => /2 of 3 generations scored/.test(t)), 'the n_scored caption survives');
  // and both are drawn INSIDE the 340×78 frame (unscaled coordinates).
  for (const n of textNodes(host)) {
    const x = Number(attr(n, 'x'));
    const y = Number(attr(n, 'y'));
    assert(x >= 0 && x <= 340, 'text x within the frame: ' + x);
    assert(y >= 0 && y <= 78, 'text y within the frame: ' + y);
  }
});

// The caption used to be drawn at the frame's foot INSIDE the 0..1 plot, where
// a low fraction (the common case) struck through it. It now owns a gutter.
test('calibrationTrend: the n_scored caption sits BELOW the plot band, never through it', () => {
  const host = mountInto(svg.calibrationTrend(calibFixture({ latest_fraction: 0, n_scored: 2 })));
  const band = host.querySelectorAll('[class]').find((n) => classOf(n) === 'dn-spark-band');
  const cap = textNodes(host).find((n) => /generations scored/.test(n.textContent));
  assert(band && cap, 'both the band and the caption render');
  const bandBottom = Number(attr(band, 'y')) + Number(attr(band, 'height'));
  const capTop = Number(attr(cap, 'y')) - 10;   // baseline minus the cap height
  assert(capTop >= bandBottom - 1, `the caption clears the plot (band ends ${bandBottom}, caption top ${capTop})`);
});

// ── 3. sparkline: the opt-in intrinsic mode, back-compat on both other paths ─
test('sparkline({intrinsic}): pins its px width + max-width, uniform meet scale', () => {
  const fig = svg.sparkline({ width: 280, height: 26, intrinsic: true, values: [3, 2, 1] });
  assertEqual(String(attr(fig, 'width')), '280', 'exactly the width the caller asked for');
  assertEqual(String(attr(fig, 'height')), '26', 'the fixed height is kept (no aspect hero)');
  assertEqual(attr(fig, 'preserveAspectRatio'), 'xMinYMid meet', 'uniform, left-aligned');
  assert(/max-width:\s*100%/.test(styleOf(fig)), 'shrinks with a narrow card instead of overflowing');
  assert(!/-hero\b/.test(classOf(fig)), 'not a hero');
});

test('sparkline: the DEFAULT and RESPONSIVE paths are untouched (back-compat)', () => {
  const plain = svg.sparkline({ width: 120, height: 24, values: [3, 2, 1] });
  assertEqual(String(attr(plain, 'width')), '100%', 'the default fit-to-width path is unchanged');
  assertEqual(attr(plain, 'preserveAspectRatio'), 'none', 'unchanged');
  const hero = svg.sparkline({ width: 240, height: 46, responsive: true, values: [3, 2, 1] });
  assert(/dn-spark-hero/.test(classOf(hero)), 'the aspect-locked hero still opts in');
  assert(/aspect-ratio:\s*240\s*\/\s*46/.test(styleOf(hero)), 'and still pins its aspect');
  assert(/\.dn-spark-hero\b[^}]*max-width/.test(CSS), 'the hero it opts into is capped');
});

// ── 4. the per-judge trend rows ────────────────────────────────────────────
function trendFixture() {
  return {
    epoch_id: 'e0',
    generations: ['v0', 'v1', 'v3'],
    judges: [
      { judge_name: 'coordinator', by_generation: { v0: 2.4, v1: 1.8, v3: 1.2 } },
      { judge_name: 'fact_checker', by_generation: { v0: 0.9, v3: 0.4 } },
    ],
  };
}

test('per-judge trend: each row carries an INTRINSIC 280px spark (not a stretched 1fr lane)', () => {
  const host = mountInto(epochView.buildJudgeTrendPanel(trendFixture()));
  const rows = allByClass(host, 'dn-judgetrend-row');
  assertEqual(rows.length, 2, 'one row per plottable judge');
  for (const row of rows) {
    const sparks = svgsByClass(row, 'dn-spark');
    assertEqual(sparks.length, 1, 'one spark per row');
    assertEqual(String(attr(sparks[0], 'width')), '280', 'intrinsic width, not 100%');
    assert(/max-width:\s*100%/.test(styleOf(sparks[0])), 'and capped by the card');
  }
});

test('per-judge trend: the CSS lane is intrinsic (max-content) and the value stays right-aligned', () => {
  const rule = CSS.split('}').find((r) => /\.dn-judgetrend-row\s*\{/.test(r));
  assert(rule && /max-content/.test(rule),
    'the spark column sizes to the figure, never to a 1fr stretch: ' + rule);
  const lastRule = CSS.split('}').find((r) => /\.dn-judgetrend-last\s*[,{]/.test(r));
  assert(lastRule && /text-align:\s*right/.test(lastRule), 'values right-align into a column');
});

// ── 5. the calibration MINI (the band's third card) ────────────────────────
test('buildCalibrationMini: null on an absent read and on a lineage with no SCORED point', () => {
  assertEqual(epochView.buildCalibrationMini(null), null, 'absent (Rust supervisor) → no card');
  assertEqual(epochView.buildCalibrationMini({ points: [] }), null, 'no points → no card');
  assertEqual(epochView.buildCalibrationMini({
    points: [{ generation_id: 'v0', score_fraction: null, total_claims: 0 }],
  }), null, 'claims but nothing scored yet → no empty frame');
});

test('buildCalibrationMini: a band card with the compact figure + a collapsed ? caption', () => {
  const host = mountInto(epochView.buildCalibrationMini(calibFixture()));
  const cards = allByClass(host, 'dn-measure-card');
  assertEqual(cards.length, 1, 'it renders AS a band card, not a full-width panel');
  const figs = svgsByClass(host, 'dn-caltrend');
  assertEqual(figs.length, 1, 'the trend figure is mounted');
  assertEqual(String(attr(figs[0], 'width')), '340', 'at card scale');
  assert(host.textContent.includes('diagnostic'), 'the diagnostic caption leads (it never feeds the gate)');
  assertEqual(allByClass(host, 'dn-figcap-more').length, 1, 'the rest collapses behind ONE "?"');
});

// ── 6. the MEASUREMENT BAND on the epoch view ──────────────────────────────
const BAND_EPOCH = '2026-06-01_e0';

function bandFixture() {
  const gens = [
    { generation_id: 'v0', epoch_id: BAND_EPOCH, parent_generation_id: '', promoted: true },
    { generation_id: 'v1', epoch_id: BAND_EPOCH, parent_generation_id: 'v0', promoted: true },
  ];
  const F = {
    '/api/lineage': { generations: gens },
    [`/api/epoch?epoch=${BAND_EPOCH}`]: {
      epoch_id: BAND_EPOCH, closed: false, goal: 'reduce drift',
      experiments: gens.map((g) => ({ generation_id: g.generation_id,
        parent_generation_id: g.parent_generation_id, outcome: { decision: 'promoted' } })),
      board: [{ entry_id: 'waffles_single', kind: 'single_turn', budget_s: 180, weight: 1 }],
    },
    [`/api/score-trajectory?epoch=${BAND_EPOCH}`]: {
      points: gens.map((g, i) => ({ generation_id: g.generation_id, scalar: 3 - i })),
    },
    [`/api/tournaments?epoch=${BAND_EPOCH}`]: {
      epoch_id: BAND_EPOCH, champion_lineage: ['v0', 'v1'],
      matchups: [{ champion: 'v0', challenger: 'v1', decision: 'promoted', delta_scalar: -1 }],
    },
    [`/api/epoch/${BAND_EPOCH}/per-judge-trend`]: trendFixture(),
    [`/api/calibration-trend?epoch=${BAND_EPOCH}`]: calibFixture(),
    [`/api/epoch/${BAND_EPOCH}/analysis`]: { analysis_md: '' },
  };
  for (const g of gens) {
    F[`/api/generation/${BAND_EPOCH}/${g.generation_id}/per-entry`] = {
      epoch_id: BAND_EPOCH, generation_id: g.generation_id,
      entries: [{ entry_id: 'waffles_single', run_id: `r_${g.generation_id}`, drift_loss: 40, pass_fail: false }],
    };
  }
  return F;
}

test('epoch view: heatmap | per-judge trend | calibration pack into ONE Measurement band', async () => {
  freshState();
  installFixtureMap(bandFixture());
  const host = document.createElement('div');
  await epochView.render(host, { navigate() {}, href: router.href }, { epochId: BAND_EPOCH });

  const bands = allByClass(host, 'dn-measure-band');
  assertEqual(bands.length, 1, 'exactly one Measurement band');
  const cards = allByClass(bands[0], 'dn-measure-card');
  assertEqual(cards.length, 3, 'heatmap + per-judge trend + calibration share it');
  assert(host.textContent.includes('Measurement'), 'the band is titled as one section');

  // the heatmap keeps its size; its full-width PANEL is what died.
  const heat = svgsByClass(host, 'dn-heatmap');
  assertEqual(heat.length, 1, 'the heatmap still renders');
  let n = heat[0].parentNode;
  let inCard = false;
  while (n && n !== host) {
    const cls = classOf(n);
    assert(!cls.split(/\s+/).includes('dn-panel'), 'the heatmap no longer owns a full-width dn-panel');
    if (cls.split(/\s+/).includes('dn-measure-card')) inCard = true;
    n = n.parentNode;
  }
  assert(inCard, 'the heatmap lives in a band card');

  // each card keeps its own collapsed "?" explainer.
  assertEqual(allByClass(bands[0], 'dn-figcap-more').length, 3,
    'packing them together costs no explanation — all three keep a "?"');
});

// The layout rule applies to the panel too: a full-width frame around a
// card-scale figure is the same wrong allocation, one level out.
test('home: the calibration PANEL is capped as well (no full-width frame on a 340px figure)', () => {
  const rule = CSS.split('}').find((r) => /\.dn-panel\.dn-caltrend-pane\s*\{/.test(r));
  assert(rule && /max-width/.test(rule), 'the home calibration panel carries a width cap');
});

test('epoch view: the band grid WRAPS (auto-fit tracks, no fixed column count)', () => {
  const rule = CSS.split('}').find((r) => r.includes('.dn-measure-band'));
  assert(rule, 'the band has a CSS rule');
  assert(/display:\s*grid/.test(rule), 'it is a grid');
  assert(/repeat\(\s*auto-fit/.test(rule), 'auto-fit → the columns collapse on a narrow viewport: ' + rule);
  assert(/minmax\(/.test(rule), 'with a min track width so a card never crushes');
});

test('epoch view: a no-op heartbeat over the band churns NO DOM (digest-gated)', async () => {
  freshState();
  installFixtureMap(bandFixture());
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await epochView.render(host, ctx, { epochId: BAND_EPOCH });
  const band = allByClass(host, 'dn-measure-band')[0];
  const before = host.textContent;
  assert(band, 'band mounted on the first render');

  data.invalidate();
  await epochView.render(host, ctx, { epochId: BAND_EPOCH });
  const after = allByClass(host, 'dn-measure-band')[0];
  assert(after === band, 'the identical read left the band node UNTOUCHED (no rebuild)');
  assertEqual(host.textContent, before, 'and no text churn');
});

run();
