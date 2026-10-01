// test/interface_rules.test.mjs — the console's three interface rules, checked
// against the stylesheet and the rendered chrome.
//
//   1. Sans for prose and controls. Mono is reserved for data, code and key
//      names; chrome set entirely in mono is rejected.
//   2. Never an accent left rail, for containers or for selection. A selected
//      item renders its name in the accent colour.
//   3. No pill or tag chips. A state reads as plain text in its semantic
//      colour, led by a drawn mark from js/icons.js where one exists.
//
// The top bar also holds one line at desktop widths: an id in the breadcrumb
// never wraps inside itself, and a long crumb truncates with an ellipsis while
// its full value stays in the DOM text and a hovercard.
//
// The stylesheet checks are conservative matchers over css/console.css and the
// inline styles in js/**. Each allowlist below names why an entry is allowed.

import { installDom, test, run, assert, assertEqual } from './harness.mjs';

installDom();

const fs = await import('node:fs');
const path = await import('node:path');
const { readCss, allByClass, freshState, installFetch, mountLiveShell, ui, dag } = await import('./fixtures.mjs');
const hovercard = await import('../js/hovercard.js');
const TYPEFACE = await import('../js/typefacedropdown.js');

// ---- stylesheet helpers ------------------------------------------------

// Every innermost rule as { selectors: [..], decls: [[prop, value], ..] }.
// The sheet nests its rules one level under `#console-root { … }`, so an
// innermost block's selector text can follow the parent's own declarations;
// the selector is the text after the last `;`.
function rules(css) {
  const src = css.replace(/\/\*[\s\S]*?\*\//g, '');
  const out = [];
  for (const m of src.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const head = m[1].split(';').pop().trim();
    if (!head || head.startsWith('@')) continue;
    const decls = m[2].split(';').map((d) => {
      const i = d.indexOf(':');
      return i < 0 ? null : [d.slice(0, i).trim().toLowerCase(), d.slice(i + 1).trim()];
    }).filter(Boolean);
    out.push({ selectors: head.split(',').map((s) => s.trim()).filter(Boolean), decls });
  }
  return out;
}

// The last compound selector (the element the rule styles).
function subject(sel) {
  return sel.split(/[\s>+~]+/).filter(Boolean).pop() || '';
}

function classesOf(compound) {
  return [...compound.matchAll(/\.([A-Za-z0-9_-]+)/g)].map((m) => m[1]);
}

const CSS = readCss();
const RULES = rules(CSS);

function listJs(dir) {
  const out = [];
  for (const name of fs.readdirSync(dir)) {
    const p = path.join(dir, name);
    if (fs.statSync(p).isDirectory()) out.push(...listJs(p));
    else if (name.endsWith('.js')) out.push(p);
  }
  return out;
}
const JS_DIR = new URL('../js/', import.meta.url).pathname;
const JS_SOURCES = [...listJs(JS_DIR), new URL('../console.js', import.meta.url).pathname]
  .map((p) => ({ file: path.relative(JS_DIR, p), text: fs.readFileSync(p, 'utf8') }));

// ---- rule 2: no accent left rail -----------------------------------------

// A neutral hairline divider is structure, not a rail: 1px in the rule colour.
// The execution outline's tree connectors, the side-by-side diff's column
// divider, the settings drawer's edge, the research-preview divider and the
// text-size segment divider are all of this kind.
const NEUTRAL_EDGE = /^(none|0)$|^1px\s+(solid|dashed)\s+var\(--v2-rule(-soft)?\)$|^var\(--v2-rule(-soft)?\)$|^(solid|dashed|dotted)$/;
const TONE_TOKEN = /var\(--v2-(accent|good|bad|caution|flat)\b/;

test('no rule draws a left-edge rail: left borders are neutral hairlines or absent', () => {
  const bad = [];
  for (const r of RULES) {
    for (const [prop, value] of r.decls) {
      if (/^border-(left|inline-start)(-color|-width|-style)?$/.test(prop)) {
        if (!NEUTRAL_EDGE.test(value.replace(/\s+/g, ' ').trim())) bad.push(r.selectors.join(', ') + ' { ' + prop + ': ' + value + ' }');
      }
      if (prop === 'box-shadow' && /inset\s+-?[1-9]/.test(value)) bad.push(r.selectors.join(', ') + ' { box-shadow: ' + value + ' }');
      if (/^background(-image)?$/.test(prop) && /linear-gradient\(\s*(90deg|to right|to left|270deg)/.test(value)) {
        bad.push(r.selectors.join(', ') + ' { ' + prop + ': ' + value + ' }');
      }
    }
  }
  assertEqual(bad.join('\n'), '', 'every left edge is a neutral hairline or absent');
});

test('no ::before / ::after bar is painted in the accent or a tone colour', () => {
  // The sidebar resize handle draws a 1px splitter line in a neutral ink on
  // hover; that is a drag affordance on the rail's right edge, not a rail on
  // a container, and it never takes a tone colour.
  const bad = [];
  for (const r of RULES) {
    if (!r.selectors.some((s) => /::?(before|after)/.test(s))) continue;
    for (const [prop, value] of r.decls) {
      if (/^(background|background-color|border-color|border-left|border)$/.test(prop) && TONE_TOKEN.test(value)) {
        bad.push(r.selectors.join(', ') + ' { ' + prop + ': ' + value + ' }');
      }
    }
  }
  assertEqual(bad.join('\n'), '', 'no pseudo-element paints a tone-coloured bar');
});

test('no inline style in js/** draws a left border or an inset edge shadow', () => {
  const bad = [];
  for (const { file, text } of JS_SOURCES) {
    const code = text.replace(/^\s*\/\/.*$/gm, '');
    for (const m of code.matchAll(/border-left|borderLeft|border-inline-start|borderInlineStart|inset\s+[1-9]px\s+0/g)) {
      bad.push(file + ': ' + m[0]);
    }
  }
  assertEqual(bad.join('\n'), '', 'no inline left-edge rail');
});

test('a selected tree row, matrix row and trace episode read through their names in the accent colour', () => {
  const colorOf = (selector) => {
    const hit = RULES.filter((r) => r.selectors.includes(selector));
    const decl = hit.flatMap((r) => r.decls).filter(([p]) => p === 'color').pop();
    return decl ? decl[1] : null;
  };
  assertEqual(colorOf('.dt-tree .dt-node.dt-sel .dt-text'), 'var(--v2-accent)', 'the tree');
  assertEqual(colorOf('.dn-mtx-row.dn-mtx-pinned .dn-mtx-file'), 'var(--v2-accent)', 'the mutation matrix');
  assertEqual(colorOf('.dn-trace-ep-on .dn-trace-ep-sum'), 'var(--v2-accent)', 'the trace episode list');
  const sel = RULES.filter((r) => r.selectors.some((s) => /\.dt-sel\b/.test(s)));
  assert(sel.every((r) => !r.decls.some(([p, v]) => /^background/.test(p) && !/transparent|none/.test(v))),
    'a selected tree row carries no fill');
});

// ---- rule 3: no pill or tag chips ------------------------------------------

const CHIP_WORD = /^(pill|pills|chip|chips|tag|tags|badge|badges)$/;
function chipClasses(tokens) {
  return [...new Set(tokens.filter((t) => /^(dn|dt|ezn)-/.test(t) && t.split('-').some((seg) => CHIP_WORD.test(seg))))];
}

test('no class names a pill, chip, tag or badge, in the stylesheet or in js/**', () => {
  const cssTokens = RULES.flatMap((r) => r.selectors.flatMap((s) => classesOf(s)));
  assertEqual(chipClasses(cssTokens).join(' '), '', 'no such class in console.css');
  const jsTokens = JS_SOURCES.flatMap(({ text }) => [...text.matchAll(/\b(?:dn|dt|ezn)-[a-z0-9_-]+/g)].map((m) => m[0]));
  assertEqual(chipClasses(jsTokens).join(' '), '', 'no such class in js/**');
});

// A rounded box with a visible fill or border and inner padding is a chip
// when it holds a label. The ones below hold content or take input: each is a
// container (a panel, a card, a popover) or a control (a button, a field).
const ROUNDED_BOX_ALLOW = new Map([
  ['dn-panel', 'container'], ['dn-trellis-cell', 'container'], ['dn-measure-card', 'container'],
  ['dn-decomp-side', 'container'], ['dn-decomp-banner', 'container'], ['dn-paper', 'container'],
  ['dn-paper-fig', 'container'], ['dn-hovercard', 'container'], ['dn-xscript-col', 'container'],
  ['dn-instr-turn', 'container'], ['dn-instr-cmcell', 'container'], ['dn-trace-ep', 'container'],
  ['dn-patch-block', 'container'], ['dn-rungprog-strip', 'container'], ['dt-live-hero-panel', 'container'],
  ['dt-cd-list', 'container'], ['dt-tf-pop', 'container'], ['dn-md-body', 'container (code and pre blocks)'],
  ['dn-fleet-card', 'container'], ['dn-racing-affordance', 'container'], ['dn-prop-row', 'container'],
  ['dn-set-kvrow', 'container'], ['dn-set-approw', 'container'], ['dn-set-railitem', 'control'],
  ['dn-instr-span', 'highlight inside prose'],
  ['dn-linkbtn', 'control'], ['dt-back', 'control'], ['dt-loopctl-btn', 'control'], ['dt-cd-trigger', 'control'],
  ['dt-nav-logs', 'control'], ['dt-nav-build', 'control'], ['dt-exec-link', 'control'], ['dn-ovr-arm', 'control'],
  ['dn-ovr-confirm', 'control'], ['dn-ovr-cancel', 'control'], ['dn-ovr-reason', 'control'], ['dn-figcap-more', 'control'],
  ['dn-convo-pin', 'control'], ['dn-sxs-xbtn', 'control'], ['dt-cmp-select', 'control'], ['dt-cmp-clear', 'control'],
  ['dt-live-follow', 'control'], ['dn-set-reset', 'control'], ['dt-drawer-x', 'control'], ['dn-instr-apply', 'control'],
  ['dt-logs-inv', 'control'], ['dt-tf-sizeseg-wrap', 'control'],
]);

test('no rounded, filled or bordered box wraps a label: every such rule styles a container or a control', () => {
  const bad = [];
  for (const r of RULES) {
    const get = (re) => r.decls.filter(([p]) => re.test(p)).map(([, v]) => v);
    const radius = get(/^border-radius$/).pop();
    if (!radius || /^0(px)?$/.test(radius)) continue;
    const pad = get(/^padding$/).pop();
    if (!pad || /^0(px)?(\s+0(px)?)*$/.test(pad)) continue;
    const filled = get(/^background(-color)?$/).some((v) => !/^(transparent|none)$/.test(v));
    const bordered = get(/^border$/).some((v) => !/^(0|none)$/.test(v));
    if (!filled && !bordered) continue;
    for (const s of r.selectors) {
      // an element subject (`.dn-md-body code`) is judged by its context.
      const cls = classesOf(subject(s)).length ? classesOf(subject(s)) : classesOf(s);
      if (!cls.some((c) => ROUNDED_BOX_ALLOW.has(c))) bad.push(s);
    }
  }
  assertEqual(bad.join('\n'), '', 'no chip-shaped label');
});

test('a verdict reads as plain text in its colour, led by its drawn mark', () => {
  const promoted = ui.verdictLabel('promoted');
  assertEqual(promoted.getAttribute('class'), 'dn-state dn-promoted', 'the promoted verdict class');
  assertEqual(promoted.textContent, 'promoted', 'the word');
  const mark = allByClass(promoted, 'zi')[0];
  assert(mark && mark.getAttribute('data-icon') === 'up', 'a promoted verdict is led by the up mark');
  assertEqual(allByClass(ui.verdictLabel('rejected'), 'zi')[0].getAttribute('data-icon'), 'fail', 'a rejected verdict is led by the fail mark');
  assertEqual(allByClass(ui.verdictLabel('baseline', { label: 'seed (v0)' }), 'zi').length, 0, 'the seed baseline reads as its word alone');
  const state = RULES.filter((r) => r.selectors.some((s) => /^\.dn-state(\.|$)/.test(s)));
  assert(state.length > 0, 'the state-word rules exist');
  for (const r of state) {
    for (const [p, v] of r.decls) {
      assert(!/^(border|border-radius|padding)$/.test(p) && !(/^background/.test(p) && !/transparent|none/.test(v)),
        r.selectors.join(', ') + ' draws no box (' + p + ')');
    }
  }
});

// ---- rule 1: sans for prose and controls, mono for data -------------------

// The chrome: the top bar, the tree, the buttons and the headings. None of
// them may resolve to the mono token (an id inside them may, through its own
// data class such as `.dt-leaf[data-kind^="gen"] .dt-text`).
const CHROME = [
  'dt-topbar', 'dt-crumbs', 'dt-crumb', 'dt-back', 'dt-brand-sub', 'dt-respreview', 'dt-nav-logs', 'dt-nav-build',
  'dt-exec-link', 'dt-cd-trigger', 'dt-status', 'dt-run-state', 'dt-loopctl-btn', 'dt-label', 'dt-sub', 'dt-role',
  'dn-linkbtn', 'dn-subhead', 'dn-h1', 'dn-measure-head', 'dt-drawer-title', 'dn-hovercard', 'dn-evals-filter',
  'dt-logs-level', 'dn-state', 'dn-flag', 'dt-structure-label', 'dn-roundtl-eplabel', 'dn-roundtl-fanlab',
  'dt-cmp-picker-lab', 'dt-live-hero-meta', 'dt-live-band', 'dn-verdictline',
  // sentences and status words outside the top bar: the live activity feed and
  // the execution outline (whose tool and agent names keep the mono).
  'dt-ticker-row', 'dn-execution', 'dn-exec-unresolved-title',
];

test('no chrome rule sets the mono token: the top bar, the tree, buttons and headings resolve to sans', () => {
  const bad = [];
  for (const r of RULES) {
    const mono = r.decls.some(([p, v]) => /^font(-family)?$/.test(p) && /var\(--v2-mono\)/.test(v));
    if (!mono) continue;
    for (const s of r.selectors) {
      const cls = classesOf(subject(s));
      if (cls.some((c) => CHROME.includes(c))) bad.push(s);
    }
  }
  assertEqual(bad.join('\n'), '', 'no chrome element is set in mono');
  assert(/#console-root\s*\{\s*font-family:\s*var\(--v2-sans\);/.test(CSS), 'the console root is set in the sans token');
});

// Figures follow the same split: captions, axis titles, column heads,
// legends, gate labels and sentences are sans; tick values, losses, deltas
// and ids are mono. A figure text class named for a caption role must not set
// the mono, and the named label classes below (whose names do not say so)
// must not either.
const CAPTION_ROLE = /-(axis|axislab|cap|zonecap|aggcap|legendcap|legendlab|head|title|gatelab|bench|benchlab|cutlab|dir|ctx|tollab|thrlab|key|col)$/;
const FIGURE_LABELS = [
  'dn-lane-label', 'ezn-col-head', 'ezn-dag-key', 'ezn-rungprog-label', 'ezn-board-run-rung', 'ezn-node-sub',
  'dn-funnel-sub', 'dn-metaledger-openlbl', 'dn-metaledger-rolllbl', 'dn-metaledger-softlbl',
  'dn-metaledger-bandsub', 'dn-metaledger-bandopen', 'dn-metaledger-champlbl', 'dn-metaledger-rowlbl',
  'dn-metaledger-cellmark-soft', 'dn-swissover-round', 'dn-swissover-verdict', 'dn-dot-missing', 'dn-bt-unfit',
  'dn-strip-sig-label', 'dn-meanscore', 'dn-facets-head',
];
// Classes that hold numbers or ids keep the mono; the heatmap's column heads
// are candidate ids, so they are data despite their caption-like name.
const FIGURE_DATA = ['dn-roundtl-loss', 'dn-waterfall-floor', 'dn-fieldbars-val', 'dn-funnel-name', 'dn-duelflow-delta', 'dn-hm-col'];

test('figure captions, axis titles, legends and sentences are sans; figure numbers and ids are mono', () => {
  const monoSubjects = new Set();
  for (const r of RULES) {
    if (!r.decls.some(([p, v]) => /^font(-family)?$/.test(p) && /var\(--v2-mono\)/.test(v))) continue;
    for (const s of r.selectors) for (const c of classesOf(subject(s))) monoSubjects.add(c);
  }
  const bad = [...monoSubjects].filter((c) => (/^(dn|ezn)-/.test(c) && CAPTION_ROLE.test(c) && !FIGURE_DATA.includes(c))
    || FIGURE_LABELS.includes(c));
  assertEqual(bad.sort().join(' '), '', 'no figure label class is set in mono');
  for (const c of FIGURE_DATA) assert(monoSubjects.has(c), c + ' keeps the mono for its numbers or ids');
  const svgSrc = JS_SOURCES.find((x) => x.file === 'svg.js').text;
  assert(!/'font-family': 'var\(--v2-mono\)'/.test(svgSrc), 'no inline figure text sets the mono');
});

test('a displayed value led by a word takes the sans; a number, an id or a dash keeps the mono', () => {
  for (const v of ['open', 'IDLE', 'provisional · 1 game', 'single-turn', 'PATCH', 'Σ Δ score']) {
    assertEqual(ui.valueFace(v), ' dn-wordval', JSON.stringify(v) + ' is prose');
  }
  for (const v of ['1.0', '-2.4', 'v2', '—', '1788 · 27 games', '2/3', '']) {
    assertEqual(ui.valueFace(v), '', JSON.stringify(v) + ' is data');
  }
  assertEqual(allByClass(ui.stat('open', 'state'), 'dn-wordval').length, 1, 'stat() marks a word value');
  const node = dag.lifecycleDag({ genId: 'v1', parentId: 'v0', entries: [{ entry_id: 'b1', drift_loss: 1, pass_fail: true }], decision: 'promoted' });
  const nodeIds = allByClass(node, 'ezn-node-id');
  assert(nodeIds.some((t) => t.textContent === 'v0' && !/dn-wordval/.test(t.getAttribute('class'))), 'a node named by an id keeps the mono');
  assert(nodeIds.some((t) => /dn-wordval/.test(t.getAttribute('class'))), 'a node named by a word takes the sans');
  const sansWord = RULES.find((r) => r.selectors.includes('.dn-stat .v.dn-wordval'));
  assert(sansWord && sansWord.decls.some(([p, v]) => p === 'font-family' && v === 'var(--v2-sans)'), 'a word value is set in the sans');
});

// The primary family of a stack and its generic fallback.
function stackParts(stack) {
  const fams = String(stack).split(',').map((s) => s.trim().replace(/^['"]|['"]$/g, ''));
  return { first: fams[0], generic: fams[fams.length - 1] };
}
const MONO_FACES = /mono|inconsolata|code pro|courier|consolas|menlo/i;

test('every typeface option keeps prose and controls in a sans and data in a monospace', () => {
  const block = (id) => {
    const m = CSS.match(new RegExp('#console-root\\[data-t-type="' + id + '"\\]\\s*\\{([^}]*)\\}'));
    assert(m, 'the ' + id + ' block exists');
    return m[1];
  };
  const decl = (b, name) => ((b.match(new RegExp('--' + name + '\\s*:\\s*([^;]+);')) || [])[1] || '').trim();
  const base = CSS.match(/#console-root\s*\{([^}]*--v2-sans[^}]*)\}/)[1];
  for (const [id, b] of [['(default)', base], ...ui.TYPE_OPTIONS.map((o) => [o.id, block(o.id)])]) {
    const sans = stackParts(decl(b, 'v2-sans'));
    assertEqual(sans.generic, 'sans-serif', id + ': --v2-sans ends in sans-serif');
    assert(!MONO_FACES.test(sans.first), id + ': --v2-sans is not led by a monospace face (' + sans.first + ')');
    assertEqual(stackParts(decl(b, 'v2-mono')).generic, 'monospace', id + ': --v2-mono ends in monospace');
    assert(stackParts(decl(b, 'n-font-head')).generic !== 'monospace', id + ': headings are never monospace');
  }
  for (const o of ui.TYPE_OPTIONS) {
    assertEqual(stackParts(o.prose).generic, 'sans-serif', o.id + ': the JS prose stack is a sans');
    assertEqual(stackParts(o.data).generic, 'monospace', o.id + ': the JS data stack is a monospace');
    assert(stackParts(o.head).generic !== 'monospace', o.id + ': the JS heading stack is not monospace');
  }
});

// ---- the top bar holds one line -------------------------------------------

test('the breadcrumb never wraps an id: crumbs hold one line and truncate with an ellipsis', async () => {
  // the first declaration: the desktop rule precedes the phone-width @media
  // override, which lets the bar wrap to two lines.
  const decl = (selector, prop) => {
    const d = RULES.filter((r) => r.selectors.includes(selector)).flatMap((r) => r.decls).find(([p]) => p === prop);
    return d ? d[1] : null;
  };
  assertEqual(decl('.dt-topbar', 'flex-wrap'), 'nowrap', 'the top bar never wraps at desktop widths');
  assertEqual(decl('.dt-crumb', 'white-space'), 'nowrap', 'a crumb never breaks inside');
  assertEqual(decl('.dt-crumb', 'text-overflow'), 'ellipsis', 'a crumb short of room truncates with an ellipsis');
  assertEqual(decl('.dt-crumb', 'overflow'), 'hidden', 'and clips to its box');
  assertEqual(decl('.dt-crumbs', 'min-width'), '0', 'the breadcrumb may shrink below its content width');

  const EP = '2026-09-27_e0-a-long-epoch-identifier';
  freshState(); installFetch();
  const root = mountLiveShell('#/e/' + EP + '/gen/v1');
  await new Promise((r) => setTimeout(r, 0));
  const crumbs = allByClass(root, 'dt-crumb');
  const epochCrumb = crumbs.find((c) => c.textContent === EP);
  assert(epochCrumb, 'the epoch crumb holds the full id as one text');
  assert(!/[​­]/.test(epochCrumb.textContent), 'no break opportunity is inserted into the id');
  assertEqual(epochCrumb.childNodes.length, 1, 'the id is one text node, never split');
  assert(hovercard.hasHovercard(epochCrumb), 'a long crumb shows its full value on hover');
});

// ---- the typeface control fits its Settings row ----------------------------

test('the typeface control stays inside its row: the pairing name wraps between faces, never past the column', () => {
  const decls = (selector) => RULES.filter((r) => r.selectors.includes(selector)).flatMap((r) => r.decls);
  const has = (selector, prop, value) => decls(selector).some(([p, v]) => p === prop && v === value);
  assert(has('.dt-cd.dt-tf', 'max-width', '100%') && has('.dt-cd.dt-tf', 'min-width', '0'), 'the picker is bounded by its column');
  assert(has('.dt-tf .dt-tf-trigger', 'max-width', '100%') && has('.dt-tf .dt-tf-trigger', 'min-width', '0'), 'the trigger is bounded by the picker');
  assert(has('.dt-tf .dt-tf-trigger .dt-cd-name', 'white-space', 'normal'), 'the pairing name may wrap');
  assert(has('.dt-tf .dt-tf-trigger .dt-tf-face', 'white-space', 'nowrap'), 'a face name never breaks inside');
  assert(/@media \(max-width: 560px\)\s*\{[^}]*\}[^@]*?\.dn-set-approw\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\)/.test(CSS)
    || /\.dn-set-approw\s*\{\s*grid-template-columns:\s*minmax\(0, 1fr\)/.test(CSS), 'a phone-width row stacks its label over its control');
  const { buildTypefaceDropdown } = TYPEFACE;
  const dd = buildTypefaceDropdown('google-sans-mono', () => {});
  const faces = allByClass(dd.node, 'dt-tf-face').map((n) => n.textContent);
  assertEqual(faces.join(' | '), 'Open Sans | Google Sans Mono', 'the trigger name holds one unbreakable span per face');
  assertEqual(allByClass(dd.node, 'dt-tf-trigger')[0].textContent.includes('Open Sans + Google Sans Mono'), true, 'and reads as the full label');
  dd.setValue('fraunces');
  assertEqual(allByClass(dd.node, 'dt-tf-face').map((n) => n.textContent).join(' | '), 'Fraunces', 'a single-face label is one span');
});

// ---- the link button styles a <button> as well as an <a> -------------------

test('the link-button style targets the class, so a <button> such as "open round" gets it', () => {
  const r = RULES.find((x) => x.selectors.includes('.dn-linkbtn'));
  assert(r, 'a .dn-linkbtn rule with no element qualifier exists');
  const get = (p) => (r.decls.find(([q]) => q === p) || [])[1];
  assertEqual(get('font-family'), 'var(--v2-sans)', 'the link button is set in the sans');
  assertEqual(get('background'), 'transparent', 'it clears the native button face');
  assertEqual(get('cursor'), 'pointer', 'and reads as clickable');
  assert(!RULES.some((x) => x.selectors.some((s) => /(^|\s)a\.dn-linkbtn/.test(s))), 'no rule is limited to <a> link buttons');
});

await run();
