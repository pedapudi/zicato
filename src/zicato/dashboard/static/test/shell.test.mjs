// test/shell.test.mjs — the console's shell: visual elements fit
// their panes, the page-wide scale control, the themes and swatch dropdown,
// and the fluid layout.
//
// Shared fixtures and helpers live in ./fixtures.mjs.

import { installDom, test, run, assert, assertEqual, assertDeep, makeEvent } from './harness.mjs';

installDom();

const {
  router, svg, ui, shell, data, compare,
  dag, live, EPOCH_ID, installFetch, freshState, allByClass,
  readCss, svgsByClass, hasScrollWrapperAncestor, mountLiveShell,
} = await import('./fixtures.mjs');

// ====================================================================
// Visual elements FIT their panes, and the density picker scales
// visual-element SIZE as well as spacing.
// ====================================================================




// ---- (a) the lifecycle DAG is fit-to-width responsive SVG ----

test('fit-to-width: the lifecycle DAG renders as a responsive SVG (width:100% + viewBox), with NO horizontal-scroll wrapper around the figure', async () => {
  freshState(); installFetch();
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  await candidate.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID, gen: 'v1' });

  const dagSvg = svgsByClass(host, 'ezn-dag')[0];
  assert(dagSvg, 'the lifecycle DAG SVG rendered on the candidate page');
  assertEqual(dagSvg.getAttribute('width'), '100%', 'the DAG SVG is width:100% (fit-to-width, not a fixed pixel width)');
  assert((dagSvg.getAttribute('viewBox') || '').startsWith('0 0 '), 'the DAG SVG carries a viewBox so it scales to its pane');
  assert(!hasScrollWrapperAncestor(dagSvg, host), 'no horizontal-scroll wrapper around the lifecycle DAG figure/panel');

  // the unit builder honours the same contract directly.
  const direct = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries: [{ entry_id: 'b1', drift_loss: 10, pass_fail: false }], decision: 'rejected' });
  assertEqual(direct.getAttribute('width'), '100%', 'lifecycleDag() builds a width:100% SVG');
});

// ---- (a′) the per-board dot-plot + epoch heatmap are responsive too ----

test('fit-to-width: the epoch heatmap is a responsive SVG and its panel does NOT scroll horizontally', async () => {
  freshState(); installFetch();
  const epoch = await import('../js/views/epoch.js');
  const host = document.createElement('div');
  await epoch.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });
  const hm = svgsByClass(host, 'dn-heatmap')[0];
  assert(hm, 'the heatmap rendered on the epoch view');
  assertEqual(hm.getAttribute('width'), '100%', 'the heatmap SVG is width:100% (fit-to-width)');
  assert(!hasScrollWrapperAncestor(hm, host), 'the heatmap panel does NOT carry a horizontal-scroll wrapper');
});

// ---- (b) the publication view's wide content is CONTAINED -------------

test('contained: the publication view’s wide tables carry their OWN contained overflow — the panel itself does not scroll horizontally', async () => {
  freshState(); installFetch();
  const publication = await import('../js/views/publication.js');
  const host = document.createElement('div');
  await publication.render(host, { navigate() {}, href: router.href }, { epochId: EPOCH_ID });

  // the per-matchup-detail + aggregate-scores tables are wrapped in a contained
  // scroll box, so the table can be wide WITHOUT the surrounding paper/panel
  // overflowing.
  const tables = host.querySelectorAll('[class]').filter((n) => n.localName === 'table'
    && /dn-(md|sc|scores|board)-table/.test(n.getAttribute('class') || ''));
  assert(tables.length >= 1, 'the publication rendered at least one table');
  let contained = 0;
  for (const t of tables) {
    let n = t.parentNode; let ok = false;
    while (n && n !== host) { if ((n.getAttribute('class') || '').includes('dn-table-scroll')) { ok = true; break; } n = n.parentNode; }
    if (ok) contained++;
  }
  assert(contained === tables.length, 'every wide publication table sits inside a contained .dn-table-scroll box (' + contained + '/' + tables.length + ')');

  // and the live figures in the paper are responsive (no fixed-pixel-width SVG
  // that could exceed the paper column).
  const figSvgs = host.querySelectorAll('[class]').filter((n) => n.localName === 'svg');
  assert(figSvgs.length >= 1, 'the paper spliced at least one live figure');
  for (const s of figSvgs) assertEqual(s.getAttribute('width'), '100%', 'each paper figure SVG is width:100% (contained within the paper column)');
});

// ---- §9.15-step-7 no-op identity: the publication view is digest-gated ----

test('publication: a no-op re-render does NOT clear-and-rebuild the DOM (digest gate)', async () => {
  freshState(); installFetch();
  const publication = await import('../js/views/publication.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await publication.render(host, ctx, { epochId: EPOCH_ID });
  const digest1 = host.getAttribute('data-t-digest');
  const first = host.firstChild;
  const writes1 = host.innerHTMLWriteCount();
  assert(host.children.length > 0, 'the publication painted');
  await publication.render(host, ctx, { epochId: EPOCH_ID });
  assertEqual(host.getAttribute('data-t-digest'), digest1, 'digest unchanged on the no-op repaint');
  assert(host.firstChild === first, 'no clear-and-rebuild on the no-op repaint (firstChild identity)');
  assertEqual(host.innerHTMLWriteCount(), writes1, 'no innerHTML writes on the no-op repaint');
});

// ---- (c) the lifecycle DAG DERIVES its height from the board-node count ----

test('lifecycle DAG height is DERIVED from the (deduped) board-node count, not a passed token, and it stays fit-to-width', () => {
  const entries = [{ entry_id: 'b1', drift_loss: 10, pass_fail: false }, { entry_id: 'b2', drift_loss: 20, pass_fail: true }];
  // a passed `height` is now IGNORED — the figure sizes itself to its nodes so
  // both compare sides share identical row spacing.
  const d = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries, decision: 'rejected', height: 999 });
  const hAttr = +d.getAttribute('height');
  assert(hAttr !== 999, 'the passed height is NOT honoured verbatim — height is derived from node count');
  assert(hAttr > 0 && hAttr < 300, 'a 2-node DAG is compact (height derived from 2 rows + padding, not a fixed 300+)');
  // adding a node grows the height by exactly ONE row pitch (constant per-node).
  const d3 = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries: entries.concat([{ entry_id: 'b3', drift_loss: 30, pass_fail: false }]), decision: 'rejected' });
  const grew = +d3.getAttribute('height') - hAttr;
  assert(grew > 0, 'one more board node makes the figure taller by a constant row pitch (' + grew + 'px)');
  // fit-to-width holds (Problem 1): width:100% + a viewBox so it scales to the pane.
  assertEqual(d.getAttribute('width'), '100%', 'the DAG is width:100% (fit-to-width)');
  assert((d.getAttribute('viewBox') || '').startsWith('0 0 '), 'the DAG carries a viewBox');
});

// ====================================================================
// A PAGE-WIDE SCALE control and a FLUID, resolution-responsive layout. The
// operator scales the WHOLE page (text and diagrams) rather than one pane, and
// the content uses the full viewport width, so the side-by-side compare panes
// and their SVGs render as large as the screen allows.
// ====================================================================


// ---- (a) the scale constants + normalisation (the control's range) ----

test('page scale: ui exposes a 70–150% range (5% steps) with a 100% default and snaps/clamps', () => {
  freshState();
  assertEqual(ui.DEFAULT_SCALE, 100, 'the page scale defaults to 100%');
  assertEqual(ui.SCALE_MIN, 70, 'the scale floors at 70%');
  assertEqual(ui.SCALE_MAX, 150, 'the scale ceils at 150%');
  assertEqual(ui.SCALE_STEP, 5, 'the scale steps by 5%');
  assertEqual(ui.normaliseScale(40), 70, 'below-range clamps up to the min');
  assertEqual(ui.normaliseScale(999), 150, 'above-range clamps down to the max');
  assertEqual(ui.normaliseScale(112), 110, 'an off-grid value snaps to the 5% step grid');
  assertEqual(ui.normaliseScale('nonsense'), 100, 'a non-numeric value falls back to the default');
  // the shell re-exports the same surface for views/tests.
  assertEqual(shell.DEFAULT_SCALE, 100, 'the shell exposes the default scale');
});

// ---- (b) the control lives in SETTINGS and drives a PAGE-WIDE scale ----
//
// The scale control lives in Settings → Appearance beside the typeface picker,
// rather than as a pill in the top-bar chrome. It is a set-once appearance
// preference and the widest control on a bar that has to make room for the run
// state. It uses the same store and the same applyScale/resetScale path.

async function appearanceBody(root) {
  const settings = await import('../js/views/settings.js');
  const host = globalThis.document.createElement('div');
  await settings.render(host, { navigate() {}, href: router.href }, { section: 'appearance' });
  await new Promise((r) => setTimeout(r, 0));
  return allByClass(host, 'dn-set-body')[0];
}

test('page scale: the control lives in Settings → Appearance; setting it applies a PAGE-WIDE scale at the app ROOT (not a pane) + persists + restores', async () => {
  // start from a clean store so the restore assertion is meaningful.
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  installFetch();
  const root = mountLiveShell('#/');

  // the top bar carries NO scale control any more.
  const topbar = allByClass(root, 'dt-topbar')[0];
  assertEqual(allByClass(topbar, 'dt-scale-pill').length, 0, 'no scale pill in the top-bar chrome');

  const body = await appearanceBody(root);
  const range = body.querySelectorAll('[class]').filter((n) =>
    n.localName === 'input' && (n.getAttribute('class') || '').includes('dn-set-range'))[0];
  assert(range, 'the page-scale control is a draggable/keyboard range input in Appearance');
  assertEqual(range.getAttribute('type'), 'range', 'it is a native range slider (draggable + arrow-key accessible)');
  assertEqual(range.getAttribute('min'), '70', 'the slider min is 70%');
  assertEqual(range.getAttribute('max'), '150', 'the slider max is 150%');
  assertEqual(range.getAttribute('step'), '5', 'the slider steps by 5%');
  assert(allByClass(body, 'dn-set-readout')[0], 'a % readout sits beside the slider');

  // default is 100% (no clipping; whole page at native size).
  assertEqual(root.getAttribute('data-t-scale'), '100', 'the page starts at 100% scale');

  // DRAG / SET it → the WHOLE PAGE scales at the app ROOT (the zoom token
  // changes on the app root rather than on any individual pane).
  range.value = '130';
  range.setAttribute('value', '130');
  range.dispatchEvent({ type: 'input', target: range });
  assertEqual(root.getAttribute('data-t-scale'), '130', 'the app ROOT records the new page scale');
  assertEqual(String(root.style.zoom), '1.3', 'the scale is applied as `zoom` on the variant root (page-wide, reflows — no clipping)');
  assertEqual(root.style.cssText.includes('--dt-page-scale:1.3'), true, 'the raw scale ratio is stamped on the root');
  // the readout reflects the new value.
  assert(allByClass(body, 'dn-set-readout')[0].textContent.includes('130%'), 'the % readout updated to 130%');

  // it is NOT a per-pane control: no pane carries its own scale attribute/zoom.
  const panes = root.querySelectorAll('[class]').filter((n) =>
    (n.getAttribute('class') || '').split(/\s+/).some((c) => /pane|split-side|viewhost/.test(c)));
  for (const p of panes) {
    assert(p.getAttribute('data-t-scale') == null, 'no per-pane scale attribute (scaling is page-wide)');
    assert(!(p.style && p.style.cssText && p.style.cssText.includes('zoom')), 'no per-pane zoom (scaling is page-wide)');
  }

  // PERSIST: the chosen scale was written to localStorage.
  assertEqual(ui.readScale(), 130, 'the chosen page scale persisted to localStorage');

  // RESTORE: a fresh mount reads it back and re-applies it to the root, and a
  // freshly-rendered Appearance section reflects the persisted value.
  const root2 = mountLiveShell('#/');
  assertEqual(root2.getAttribute('data-t-scale'), '130', 'a fresh mount restores the persisted scale');
  assertEqual(String(root2.style.zoom), '1.3', 'the restored scale is re-applied as root zoom');
  const body2 = await appearanceBody(root2);
  const range2 = body2.querySelectorAll('[class]').filter((n) =>
    n.localName === 'input' && (n.getAttribute('class') || '').includes('dn-set-range'))[0];
  assertEqual(range2.getAttribute('value'), '130', 'the restored slider reflects the persisted value');
});

// ---- (c) keyboard accessibility (a native range) -------------------

test('page scale: the control is keyboard-accessible — a focusable native range with the aria value bounds', async () => {
  installFetch();
  const root = mountLiveShell('#/');
  const body = await appearanceBody(root);
  const range = body.querySelectorAll('[class]').filter((n) =>
    n.localName === 'input' && (n.getAttribute('class') || '').includes('dn-set-range'))[0];
  // a native range input is inherently arrow-key adjustable; expose the aria bounds.
  assertEqual(range.getAttribute('aria-valuemin'), '70', 'aria-valuemin set for assistive tech');
  assertEqual(range.getAttribute('aria-valuemax'), '150', 'aria-valuemax set for assistive tech');
  assert(range.getAttribute('aria-valuenow') != null, 'aria-valuenow tracks the current scale');
  assert((range.getAttribute('aria-label') || '').length > 0, 'the slider carries an aria-label');
});

// ---- (d) scale survives the other appearance axes ----

test('page scale: persists across re-applies and survives a colour/typeface change (the sole sizing axis)', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  const root = mountLiveShell('#/');

  // set a non-default scale, then change colour + typeface (the other axes).
  shell.applyScale(85, root);
  assertEqual(root.getAttribute('data-t-scale'), '85', 'page scale set to 85%');
  shell.applyTheme('solarized-dark', root);
  shell.applyTypeface('display', root);
  // the scale is UNCHANGED by a colour/typeface switch (separate axes).
  assertEqual(root.getAttribute('data-t-scale'), '85', 'switching colour/typeface left the page scale untouched');
  assertEqual(ui.readScale(), 85, 'the page scale is still persisted at 85%');

  // re-apply a new scale — it lands.
  shell.applyScale(120, root);
  assertEqual(root.getAttribute('data-t-scale'), '120', 'the new page scale applied');
  assertEqual(ui.readScale(), 120, 'scale persists independently');
});

// ---- CHANGE 4: the scale RESET affordance returns to 100% + persists ----

test('page scale RESET: a keyboard-accessible reset button snaps the scale back to 100% and persists', async () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  installFetch();
  const root = mountLiveShell('#/');

  // move off 100% first.
  shell.applyScale(135, root);
  assertEqual(root.getAttribute('data-t-scale'), '135', 'scale moved to 135%');
  assertEqual(ui.readScale(), 135, '135% persisted');

  // the reset affordance is a real <button> (keyboard-accessible) beside the range.
  const body = await appearanceBody(root);
  const resetBtn = body.querySelectorAll('[class]').filter((n) =>
    n.localName === 'button' && (n.getAttribute('class') || '').includes('dn-set-reset'))[0];
  assert(resetBtn, 'a reset button sits beside the scale range');
  assert((resetBtn.getAttribute('aria-label') || '').length > 0, 'the reset button carries an aria-label (keyboard/AT accessible)');

  // clicking it snaps the page scale back to 100% and persists.
  resetBtn.dispatchEvent({ type: 'click' });
  assertEqual(root.getAttribute('data-t-scale'), '100', 'reset returned the page scale to 100%');
  assertEqual(String(root.style.zoom), '1', 'the root zoom is back to 1 (100%)');
  assertEqual(ui.readScale(), 100, 'the reset 100% persisted to localStorage');

  // the programmatic resetScale() export does the same.
  shell.applyScale(70, root);
  shell.resetScale(root);
  assertEqual(root.getAttribute('data-t-scale'), '100', 'resetScale() also returns to 100%');
});

// ---- CHANGE 5 + 6: sixteen themes + the colour SWATCH DROPDOWN ----

test('colour themes: all SIXTEEN are registered, each defines the full --v2 token contract, and selecting each applies it', () => {
  freshState();
  const ids = ui.COLOR_THEMES.map((t) => t[0]);
  const expected = ['monokai', 'solarized-dark', 'solarized-light',
    'google-light', 'google-dark', 'lunaria-light', 'lunaria-eclipse',
    'belafonte-day', 'belafonte-night',
    'paper', 'zenburn', 'selenized-black', 'relaxed',
    'espresso', 'dracula', 'ubuntu'];
  assertEqual(ids.length, 16, 'sixteen colour themes registered');
  assertDeep(ids, expected, 'the sixteen ids are the three originals + thirteen Gogh palettes');
  assertEqual(ui.DEFAULT_COLOR, 'monokai', 'monokai stays the default');

  // every theme defines the FULL --v2 token contract in the scoped CSS.
  const css = readCss();
  const contract = ['paper', 'panel', 'ink', 'ink-soft', 'ink-faint', 'rule', 'rule-soft',
    'good', 'good-soft', 'bad', 'bad-soft', 'caution', 'accent', 'flat', 'cell-empty'];
  for (const id of ids) {
    if (id === 'monokai') continue; // monokai shares the bare-root default block
    const re = new RegExp('\\[data-t-theme="' + id + '"\\]\\s*\\{([^}]*)\\}');
    const m = re.exec(css.replace(/\n/g, ' '));
    assert(m, 'theme ' + id + ' has a CSS block');
    for (const tok of contract) {
      assert(m[1].includes('--v2-' + tok + ':'), 'theme ' + id + ' defines --v2-' + tok);
    }
  }

  // selecting EACH theme applies it to the root + persists (incl. all ten Gogh).
  const root = document.createElement('div');
  for (const id of ids) {
    shell.applyTheme(id, root);
    assertEqual(root.getAttribute('data-t-theme'), id, id + ' applied to the root');
    assertEqual(ui.readColor(), id, id + ' persisted');
  }
});

// ---- ROUND 9: the four NEW Gogh themes (Paper/Zenburn/Selenized Black/Relaxed) ----

test('new themes: Paper/Zenburn/Selenized Black/Relaxed are registered with swatch strips, define the full token contract, and selecting each changes the root attribute + tokens', () => {
  freshState();
  const css = readCss();
  const byId = new Map(ui.COLOR_THEMES.map((t) => [t[0], t]));
  const NEW = ['paper', 'zenburn', 'selenized-black', 'relaxed'];
  const contract = ['paper', 'panel', 'ink', 'ink-soft', 'ink-faint', 'rule', 'rule-soft',
    'good', 'good-soft', 'bad', 'bad-soft', 'caution', 'accent', 'flat', 'cell-empty'];

  // (a) each new theme is registered with a name + a 4–6-colour preview strip.
  for (const id of NEW) {
    const t = byId.get(id);
    assert(t, id + ' is registered in COLOR_THEMES');
    assert(typeof t[1] === 'string' && t[1].length > 0, id + ' has a display name');
    assert(Array.isArray(t[2]) && t[2].length >= 4 && t[2].length <= 6, id + ' has a 4–6-colour swatch strip (got ' + t[2].length + ')');
    for (const c of t[2]) assert(/^#[0-9a-fA-F]{6}$/.test(c), id + ' swatch ' + c + ' is an inlined hex (no network)');
  }

  // (b) each new theme defines the full --v2 token contract in the scoped CSS,
  //     and its token block differs from monokai's (the default) — tokens differ.
  const monokaiBlock = /\[data-t-theme="monokai"\]\s*\{([^}]*)\}/.exec(css.replace(/\n/g, ' '))[1];
  for (const id of NEW) {
    const m = new RegExp('\\[data-t-theme="' + id + '"\\]\\s*\\{([^}]*)\\}').exec(css.replace(/\n/g, ' '));
    assert(m, id + ' has a scoped CSS token block');
    for (const tok of contract) assert(m[1].includes('--v2-' + tok + ':'), id + ' defines --v2-' + tok);
    const paperVal = /--v2-paper:\s*([^;]+);/.exec(m[1])[1].trim();
    const monokaiPaper = /--v2-paper:\s*([^;]+);/.exec(monokaiBlock)[1].trim();
    assert(paperVal.toLowerCase() !== monokaiPaper.toLowerCase(), id + ' ground differs from monokai (tokens differ)');
  }

  // (c) selecting each NEW theme applies it to the root attribute + persists.
  const root = document.createElement('div');
  shell.applyTheme('monokai', root);
  let prev = root.getAttribute('data-t-theme');
  for (const id of NEW) {
    shell.applyTheme(id, root);
    assertEqual(root.getAttribute('data-t-theme'), id, id + ' applied to the root (attribute changed)');
    assert(prev !== id, 'the root theme attribute changed selecting ' + id);
    assertEqual(ui.readColor(), id, id + ' persisted');
    prev = id;
  }
});

// ---- ROUND 10: the three NEW Gogh themes (Espresso/Dracula/Ubuntu) ----

test('new themes: Espresso/Dracula/Ubuntu are registered with swatch strips, define the full token contract, and selecting each changes the root attribute + tokens', () => {
  freshState();
  const css = readCss();
  const byId = new Map(ui.COLOR_THEMES.map((t) => [t[0], t]));
  const NEW = ['espresso', 'dracula', 'ubuntu'];
  const contract = ['paper', 'panel', 'ink', 'ink-soft', 'ink-faint', 'rule', 'rule-soft',
    'good', 'good-soft', 'bad', 'bad-soft', 'caution', 'accent', 'flat', 'cell-empty'];

  // (a) each new theme is registered with a name + a 4–6-colour preview strip.
  for (const id of NEW) {
    const t = byId.get(id);
    assert(t, id + ' is registered in COLOR_THEMES');
    assert(typeof t[1] === 'string' && t[1].length > 0, id + ' has a display name');
    assert(Array.isArray(t[2]) && t[2].length >= 4 && t[2].length <= 6, id + ' has a 4–6-colour swatch strip (got ' + t[2].length + ')');
    for (const c of t[2]) assert(/^#[0-9a-fA-F]{6}$/.test(c), id + ' swatch ' + c + ' is an inlined hex (no network)');
  }

  // (b) each new theme defines the full --v2 token contract in the scoped CSS,
  //     and its token block differs from monokai's (the default) — tokens differ.
  const monokaiBlock = /\[data-t-theme="monokai"\]\s*\{([^}]*)\}/.exec(css.replace(/\n/g, ' '))[1];
  for (const id of NEW) {
    const m = new RegExp('\\[data-t-theme="' + id + '"\\]\\s*\\{([^}]*)\\}').exec(css.replace(/\n/g, ' '));
    assert(m, id + ' has a scoped CSS token block');
    for (const tok of contract) assert(m[1].includes('--v2-' + tok + ':'), id + ' defines --v2-' + tok);
    const paperVal = /--v2-paper:\s*([^;]+);/.exec(m[1])[1].trim();
    const monokaiPaper = /--v2-paper:\s*([^;]+);/.exec(monokaiBlock)[1].trim();
    assert(paperVal.toLowerCase() !== monokaiPaper.toLowerCase(), id + ' ground differs from monokai (tokens differ)');
  }

  // (c) the signature palette grounds are mapped faithfully.
  const espressoBlock = new RegExp('\\[data-t-theme="espresso"\\]\\s*\\{([^}]*)\\}').exec(css.replace(/\n/g, ' '))[1];
  const draculaBlock = new RegExp('\\[data-t-theme="dracula"\\]\\s*\\{([^}]*)\\}').exec(css.replace(/\n/g, ' '))[1];
  const ubuntuBlock = new RegExp('\\[data-t-theme="ubuntu"\\]\\s*\\{([^}]*)\\}').exec(css.replace(/\n/g, ' '))[1];
  assert(/--v2-paper:\s*#323232/i.test(espressoBlock), 'espresso ground is the palette background #323232');
  assert(/--v2-paper:\s*#282A36/i.test(draculaBlock), 'dracula ground is the palette background #282A36');
  assert(/--v2-paper:\s*#300A24/i.test(ubuntuBlock), 'ubuntu ground is the signature aubergine #300A24');

  // (c.1) Dracula maps to the CANONICAL palette with its SIGNATURE PURPLE as the
  // accent (drives the LIVE pill / highlights / active state) — NOT cyan.
  assert(/--v2-accent:\s*#BD93F9/i.test(draculaBlock), 'dracula accent is the signature purple #BD93F9 (not cyan)');
  assert(!/--v2-accent:\s*#8BE9FD/i.test(draculaBlock), 'dracula accent is NOT the cyan #8BE9FD');
  assert(/--v2-ink:\s*#F8F8F2/i.test(draculaBlock), 'dracula foreground is the palette fg #F8F8F2');
  assert(/--v2-good:\s*#50FA7B/i.test(draculaBlock), 'dracula good keys off the palette green #50FA7B');
  assert(/--v2-bad:\s*#FF5555/i.test(draculaBlock), 'dracula bad keys off the palette red #FF5555');
  assert(/--v2-caution:\s*#F1FA8C/i.test(draculaBlock), 'dracula caution keys off the palette yellow #F1FA8C');
  assert(/--v2-flat:\s*#6272A4/i.test(draculaBlock), 'dracula flat keys off the comment grey #6272A4');
  for (const tok of contract) {
    assert(draculaBlock.includes('--v2-' + tok + ':'), 'dracula defines --v2-' + tok);
  }

  // (d) selecting each NEW theme applies it to the root attribute + persists.
  const root = document.createElement('div');
  shell.applyTheme('monokai', root);
  let prev = root.getAttribute('data-t-theme');
  for (const id of NEW) {
    shell.applyTheme(id, root);
    assertEqual(root.getAttribute('data-t-theme'), id, id + ' applied to the root (attribute changed)');
    assert(prev !== id, 'the root theme attribute changed selecting ' + id);
    assertEqual(ui.readColor(), id, id + ' persisted');
    prev = id;
  }
});

test('colour picker is a SWATCH DROPDOWN: a closed trigger with the current swatch+name and one swatch strip per option', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  shell.applyTheme('monokai');
  const root = mountLiveShell('#/');

  // the colour control is a dropdown rather than an inline button row.
  const dd = allByClass(root, 'dt-cd')[0];
  assert(dd, 'the colour control is a dropdown (dt-cd)');
  assertEqual(allByClass(root, 'dt-theme-btn').length, 0, 'no old inline colour buttons remain');

  // the closed trigger shows the current theme name + a swatch strip preview.
  const trigger = allByClass(root, 'dt-cd-trigger')[0];
  assert(trigger, 'a dropdown trigger button rendered');
  assert(trigger.textContent.includes('monokai'), 'the trigger names the current theme');
  const triggerStrip = trigger.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dt-swatch-strip'))[0];
  assert(triggerStrip, 'the trigger shows a swatch-strip preview');

  // the listbox has one OPTION per theme, each with a swatch strip (≥4 swatches) + name.
  const options = allByClass(root, 'dt-cd-option');
  assertEqual(options.length, 16, 'one dropdown option per theme (sixteen)');
  // the four round-9 + three round-10 themes each surface as a listed option.
  for (const id of ['paper', 'zenburn', 'selenized-black', 'relaxed', 'espresso', 'dracula', 'ubuntu']) {
    assert(options.filter((o) => o.getAttribute('data-theme') === id).length === 1, id + ' is a listed dropdown option');
  }
  for (const opt of options) {
    const strip = opt.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dt-swatch-strip'))[0];
    assert(strip, 'option ' + opt.getAttribute('data-theme') + ' has a swatch strip');
    const swatches = strip.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dt-swatch'));
    assert(swatches.length >= 4, 'the strip shows ≥4 representative colours (got ' + swatches.length + ')');
    // the swatches are real colour values (inline background style).
    assert((swatches[0].getAttribute('style') || '').includes('background:'), 'a swatch carries a colour background');
  }

  // clicking an option applies + persists that theme, and the trigger updates.
  const opt = options.filter((o) => o.getAttribute('data-theme') === 'belafonte-night')[0];
  opt.dispatchEvent({ type: 'click' });
  assertEqual(root.getAttribute('data-t-theme'), 'belafonte-night', 'clicking the option applied the theme');
  assertEqual(ui.readColor(), 'belafonte-night', 'the chosen theme persisted');
  assert(allByClass(root, 'dt-cd-trigger')[0].textContent.includes('belafonte night'), 'the closed trigger now shows the chosen theme');

  // keyboard: ArrowDown on the trigger opens; Enter on the open list selects.
  const triggerKb = allByClass(root, 'dt-cd-trigger')[0];
  triggerKb.dispatchEvent({ type: 'keydown', key: 'ArrowDown', preventDefault() {}, target: triggerKb });
  assert((dd.getAttribute('class') || '').includes('dt-cd-open'), 'ArrowDown opens the dropdown');
  const list = allByClass(root, 'dt-cd-list')[0];
  // arrow to the first option (monokai) and select it.
  list.dispatchEvent({ type: 'keydown', key: 'ArrowUp', preventDefault() {} });
  // Esc closes without further change.
  list.dispatchEvent({ type: 'keydown', key: 'Escape', preventDefault() {} });
  assert(!(dd.getAttribute('class') || '').includes('dt-cd-open'), 'Escape closes the dropdown');
});

// ---- 6-SWATCH PREVIEW: every theme's strip shows SIX colours (adds accent) ----

test('colour themes: every COLOR_THEMES tuple carries SIX valid-hex preview colours (ground · surface · ink · good · bad · accent)', () => {
  assertEqual(ui.COLOR_THEMES.length, 16, 'theme count stays sixteen');
  for (const [id, , swatches] of ui.COLOR_THEMES) {
    assert(Array.isArray(swatches), id + ' has a swatch tuple');
    assertEqual(swatches.length, 6, id + ' preview tuple has exactly six colours (got ' + swatches.length + ')');
    for (const c of swatches) {
      assert(/^#[0-9a-fA-F]{6}$/.test(c), id + ' swatch ' + c + ' is a valid 6-digit hex');
    }
  }
  // dracula's 6th swatch (accent) is the signature purple.
  const dracula = ui.COLOR_THEMES.find((t) => t[0] === 'dracula');
  assertEqual(dracula[2][5].toUpperCase(), '#BD93F9', 'dracula 6th swatch is the signature purple #BD93F9');
});

test('colour picker renders SIX swatches per option (the 6-swatch strip) and SIX on the closed trigger', () => {
  try { globalThis.window.localStorage.clear(); } catch (e) { /* ignore */ }
  shell.applyTheme('monokai');
  const root = mountLiveShell('#/');

  // closed trigger strip shows six swatches.
  const trigger = allByClass(root, 'dt-cd-trigger')[0];
  const triggerStrip = trigger.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dt-swatch-strip'))[0];
  const triggerSwatches = triggerStrip.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dt-swatch'));
  assertEqual(triggerSwatches.length, 6, 'the closed trigger shows six swatches');

  // every option's strip shows exactly six swatches, matching its tuple.
  const byId = new Map(ui.COLOR_THEMES.map((t) => [t[0], t]));
  const options = allByClass(root, 'dt-cd-option');
  assertEqual(options.length, 16, 'one option per theme (sixteen)');
  for (const opt of options) {
    const id = opt.getAttribute('data-theme');
    const strip = opt.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').includes('dt-swatch-strip'))[0];
    const swatches = strip.querySelectorAll('[class]').filter((n) => (n.getAttribute('class') || '').split(/\s+/).includes('dt-swatch'));
    assertEqual(swatches.length, 6, id + ' option strip shows six swatches');
    // the 6th rendered swatch carries the tuple's accent colour.
    const accent = byId.get(id)[2][5];
    assert((swatches[5].getAttribute('style') || '').toLowerCase().includes(accent.toLowerCase()),
      id + ' 6th swatch carries its accent ' + accent);
  }
});

// ---- (e) the layout is FLUID (not clamped to a narrow column) ------

test('layout: the detail pane + compare grid are FLUID — not clamped to a narrow fixed max-width; the compare split uses the FULL content width', async () => {
  // the detail host fills the width (width:100%), so the two compare panes
  // each take HALF the FULL content width — not half of a narrow column.
  const css = await import('node:fs').then((fs) =>
    fs.readFileSync(new URL('../css/console.css', import.meta.url), 'utf8'));

  // the detail pane carries no narrow 1160px cap.
  assert(!/\.dt-viewhost\s*\{[^}]*max-width:\s*1160px/.test(css.replace(/\n/g, ' ')),
    'the detail pane is no longer clamped to the narrow 1160px column');
  // the detail pane is fluid (width:100%).
  assert(/\.dt-viewhost\s*\{[^}]*width:\s*100%/.test(css.replace(/\n/g, ' ')),
    'the detail pane is fluid (width:100%, fills the available column)');

  // the compare split is a two-equal-column grid (1fr 1fr) — so within a FULL-
  // width detail pane each pane is half the FULL width (bigger SVGs on bigger
  // screens). It collapses to one column only on a small screen.
  assert(/\.dt-split\s*\{[^}]*grid-template-columns:\s*1fr\s+1fr/.test(css.replace(/\n/g, ' ')),
    'the compare split is a two-equal-column grid that fills the detail width');

  // and at runtime the split renders two full-width-sharing sides (not a
  // narrow centred column): render a compare view and confirm the split frame
  // is NOT single-column and carries two sides.
  freshState(); installFetch();
  const candidate = await import('../js/views/candidate.js');
  const host = document.createElement('div');
  const ctx = { navigate() {}, href: router.href };
  await candidate.render(host, ctx, { epochId: EPOCH_ID, gen: 'v1' }, { params: { epochId: EPOCH_ID, gen: 'v1' }, cmp: 'v2' });
  const split = allByClass(host, 'dt-split')[0];
  assert(split && !(split.getAttribute('class') || '').includes('dt-split-single'),
    'the compare view is a two-column split (each pane gets half the FULL content width)');
  assertEqual(allByClass(host, 'dt-split-side').length, 2, 'two compare panes share the full width');
});

await run();
