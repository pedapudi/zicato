// variants/T/tree.js — the data-model TREE sidebar (Console III's headline).
//
// A persistent, collapsible LEFT tree that mirrors the real zicato hierarchy:
//
//   Environment (workspace)
//   └─ Epoch <id>                       (one node per epoch — multi-epoch nav)
//      ├─ Generations
//      │  └─ <gen> (champion / rejected)
//      ├─ Boards
//      │  └─ <entry>
//      ├─ Mutation surface
//      └─ Publication
//
// Selecting ANY node drives the single detail pane (via ctx.navigate). The
// selection is derived ENTIRELY from the parsed route, so a cold deep-link
// hydrates the open branches + the highlighted node with no extra state. The
// expand/collapse set is the union of (the route's open path) ∪ (the user's
// manual toggles) — so the active branch is always visible, and the user can
// open siblings without losing their place.
//
// Render discipline: the tree is digest-gated like every pane. A heartbeat
// that changes no structural data (epochs, generations, board entries, or the
// selected node) writes ZERO DOM.

import { el, clearChildren } from '../../core/dom.js';
import { CROWN } from './svg.js';

// `data` is the structural model the shell assembles once per dispatch:
//   { epochs:[{id, current}], expanded:{ epochs:{<id>:{gens, boards}} },
//     byEpoch:{ <id>:{ gens:[{id, promoted, parent}], boards:[{id, kind}] } } }
// `route` is the parsed route (drives the highlighted node + open path).
// `ctx`  carries navigate(view, params) + href(view, params).
// `toggles` is a Set of manually-opened node keys (owned by the shell).
// `live` is the LIVE-ACTIVITY set — the gen ids and/or board-entry ids that
// currently have an in-flight run (derived from state.activeRuns). It drives a
// subtle pulsing badge on the running rows. It is folded into the digest as a
// SORTED set so the badge re-stamps when a gen/entry ENTERS or LEAVES the set,
// but a steady heartbeat with the SAME set is a no-op (the pulse is CSS-animated,
// so an unchanged set needs no DOM). A null/absent set is treated as empty.
export function treeDigest(model, route, toggles, live) {
  const p = (route && route.params) || {};
  const liveSet = live instanceof Set ? [...live].map(String).sort()
    : (Array.isArray(live) ? live.map(String).sort() : []);
  return JSON.stringify({
    epochs: model.epochs.map((e) => [e.id, !!e.current]),
    by: model.epochs.map((e) => {
      const b = model.byEpoch[e.id] || { gens: [], boards: [] };
      // include the current/former champion split so the badge re-stamps when
      // the crown moves (a steady heartbeat with the same crown is a no-op).
      // the round grouping (Task 5) folds in so the tree re-stamps when a gen's
      // birth-round / a round's gate outcome changes, but stays stable on a beat.
      const rounds = Array.isArray(b.rounds) ? b.rounds.map((r) => [
        r.round_index, r.championId, r.championEvalMode || '',
        r.gateOutcome ? r.gateOutcome.kind + ':' + (r.gateOutcome.gen || '') : '',
        (Array.isArray(r.challengers) ? r.challengers : []).map((g) => g && g.id),
      ]) : [];
      return [e.id, b.gens.map((g) => [g.id, !!g.promoted, !!g.currentChampion, !!g.formerChampion, !!g.orphan, Number.isInteger(g.round_index) ? g.round_index : null]), b.boards.map((x) => x.id), rounds];
    }),
    sel: [route ? route.view : 'home', p.epochId || '', p.gen || '', p.entry || '', p.mutId || '', p.gen2 || ''],
    open: [...toggles].sort(),
    live: liveSet,
  });
}

// The set of node-keys the route implies must be OPEN (so the active node is
// always visible regardless of manual toggles).
export function routeOpenKeys(route) {
  const p = (route && route.params) || {};
  const keys = new Set();
  if (!p.epochId) return keys;
  const e = 'e:' + p.epochId;
  keys.add(e);
  const v = route.view;
  if (v === 'gens' || v === 'candidate' || v === 'diff') keys.add(e + '/gens');
  // a round-scoped gens view (or a round leaf) opens its round node.
  if (v === 'gens' && p.round != null) keys.add(e + '/gens/r' + p.round);
  if (v === 'boards' || v === 'board') keys.add(e + '/boards');
  return keys;
}

export function buildTree(host, model, route, toggles, ctx, onToggle, live) {
  clearChildren(host);
  const open = new Set([...toggles, ...routeOpenKeys(route)]);
  // the live-activity set: gen / entry ids currently running (state.activeRuns).
  const liveSet = live instanceof Set ? live
    : new Set((Array.isArray(live) ? live : []).map(String));
  const isLive = (id) => id != null && liveSet.has(String(id));
  const p = (route && route.params) || {};
  const sel = route ? route.view : 'home';

  const tree = el('div', { class: 'dt-tree', role: 'tree', 'aria-label': 'Data model' });

  // ── Environment (root) ──────────────────────────────────────────────
  tree.appendChild(branchRow({
    key: 'env', depth: 0, kind: 'env', label: 'Environment',
    sub: 'workspace', expandable: true, isOpen: true, lockOpen: true,
    selected: sel === 'home',
    onSelect: () => ctx.navigate('home', {}),
    onToggle: null,
  }));

  // ── one Epoch node per epoch (multi-epoch navigation) ───────────────
  for (const epoch of model.epochs) {
    const eKey = 'e:' + epoch.id;
    const eOpen = open.has(eKey);
    tree.appendChild(branchRow({
      key: eKey, depth: 1, kind: 'epoch',
      label: epoch.id, sub: epoch.current ? 'current' : null,
      expandable: true, isOpen: eOpen, selected: sel === 'epoch' && p.epochId === epoch.id,
      onSelect: () => ctx.navigate('epoch', { epochId: epoch.id }),
      onToggle: () => onToggle(eKey),
    }));
    if (!eOpen) continue;

    const bundle = model.byEpoch[epoch.id] || { gens: [], boards: [] };

    // An epoch's direct children are ROUNDS; each round holds the generations
    // born / raced that round (epoch ⊃ rounds ⊃ generations). The group node is
    // labelled "Rounds" whenever real round structure exists (>1 round, or a
    // round_index stamp); it degrades to a flat "Generations" list only when
    // there is no round structure to show.
    const rounds = Array.isArray(bundle.rounds) ? bundle.rounds : [];
    const stamped = bundle.gens.some((g) => Number.isInteger(g.round_index));
    const showRounds = rounds.length > 1 || (stamped && rounds.length >= 1);

    // Rounds group (⊃ generations) — a flat "Generations" list when unstructured.
    const gKey = eKey + '/gens';
    const gOpen = open.has(gKey);
    tree.appendChild(branchRow({
      key: gKey, depth: 2, kind: 'group', label: showRounds ? 'Rounds' : 'Generations',
      sub: showRounds
        ? (rounds.length ? String(rounds.length) : null)
        : (bundle.gens.length ? String(bundle.gens.length) : null),
      expandable: showRounds ? rounds.length > 0 : bundle.gens.length > 0, isOpen: gOpen,
      selected: sel === 'gens' && p.epochId === epoch.id,
      onSelect: () => ctx.navigate('gens', { epochId: epoch.id }),
      onToggle: () => onToggle(gKey),
    }));
    if (gOpen) {
      // a generation LEAF row (the shared renderer for both the round-grouped
      // and the flat-list layouts). `depth` lets the round layout indent one
      // level deeper under its round node.
      const genLeaf = (g, depth) => {
        const selected = (sel === 'candidate' || sel === 'diff') && p.epochId === epoch.id && p.gen === g.id;
        // Only the CURRENT champion (the last id in champion_lineage) gets the
        // solid-crown (CROWN.current) "champion" badge; a FORMER champion (held
        // the title, then was succeeded) gets the distinct, dimmer hollow-crown
        // (CROWN.former) "former" marker. Pre-feature models without the
        // current/former split keep the solid crown for any promoted gen.
        const isCurrent = g.currentChampion === true;
        const isFormer = g.formerChampion === true;
        const legacyChamp = g.promoted === true && g.currentChampion === undefined && g.formerChampion === undefined;
        // Class B: a child with no recorded outcome (promoted == null) is still
        // racing — tag it "pending", NEVER "rejected/dead branch".
        const childTag = g.parent ? (g.promoted == null ? 'pending' : (g.promoted === false ? 'rejected' : 'promoted')) : 'seed';
        let kind = 'gen', glyph = (g.parent ? '↳' : '◆'), tag = childTag;
        if (isCurrent || legacyChamp) { kind = 'gen-champ'; glyph = CROWN.current; tag = 'champion'; }
        else if (isFormer) { kind = 'gen-former'; glyph = CROWN.former; tag = 'former champion'; }
        else if (g.orphan === true) { kind = 'gen-orphan'; glyph = '◌'; tag = 'unscored'; }
        return leafRow({
          depth, kind, label: g.id, glyph, tag,
          selected, live: isLive(g.id),
          onSelect: () => ctx.navigate('candidate', { epochId: epoch.id, gen: g.id }),
        });
      };

      // A CARRIED champion reference: the incumbent defending THIS round, born in
      // an earlier round (its full node lives there). Rendered dimmed + "↑ …
      // defends" so each round's full field is visible WITHOUT a confusing plain
      // duplicate. The link is ROUND-SCOPED: "carried" is the role, so the target
      // resolves to the champion's evaluation FOR THIS ROUND — the cached result
      // in fast mode, the fresh re-run in full mode — i.e. always the output that
      // round actually used.
      const champRefLeaf = (id, roundIndex, evalMode, depth) => leafRow({
        depth, kind: 'gen-carried', label: id, glyph: '↑',
        // the carried champion's per-round eval provenance: a fast/cached reuse
        // vs a full re-run that round — the canonical signal from the record.
        tag: (evalMode === 'fast' || evalMode === 'fast-degraded') ? 'defends · cached'
          : evalMode === 'full' ? 'defends · re-run' : 'defends',
        selected: (sel === 'candidate' || sel === 'diff') && p.epochId === epoch.id
          && p.gen === id && String(p.round) === String(roundIndex),
        live: isLive(id),
        onSelect: () => ctx.navigate('candidate', { epochId: epoch.id, gen: id, round: roundIndex }),
      });

      // ROUND GROUPING (Task 5): Epoch → Generations → Round 0 / Round 1 / …
      // → {challengers minted that round}. We show the round layer ONLY when it
      // SAYS something — there is real round structure (>1 round, OR a
      // round_index stamp). A single round with no stamp degrades to today's
      // FLAT list (no redundant "Round 0" wrapper). Each round node carries its
      // gate outcome; the carried-in champion is shown as a reference
      // (↑ from R{n-1}), not duplicated under the round.
      if (showRounds) {
        const gensById = new Map(bundle.gens.map((g) => [String(g.id), g]));
        rounds.forEach((r, ri) => {
          const rKey = gKey + '/r' + r.round_index;
          const rOpen = open.has(rKey) || routeOpenKeys(route).has(rKey)
            // the round holding the selected candidate opens automatically —
            // whether it is a minted challenger OR the round's carried champion.
            || ((sel === 'candidate' || sel === 'diff') && p.epochId === epoch.id
                && (String(r.championId) === String(p.gen)
                    || (r.challengers || []).some((g) => g && String(g.id) === String(p.gen))));
          const promoted = r.gateOutcome && r.gateOutcome.kind === 'promoted';
          // the DEFENDING champion lives in the ROUND HEADER (e.g. "v3 defends ·
          // — held" / "▲ v6 promoted"), NOT as a duplicate ↑-reference child row.
          // The champion's full node still appears under its own BIRTH round.
          const champLabel = r.championId != null ? String(r.championId) : null;
          const gateSub = champLabel
            ? (promoted ? champLabel + ' defends · ▲ ' + r.gateOutcome.gen + ' promoted' : champLabel + ' defends · — held')
            : (promoted ? '▲ ' + r.gateOutcome.gen + ' promoted' : '— held');
          tree.appendChild(branchRow({
            key: rKey, depth: 3, kind: 'round', label: 'Round ' + r.round_index, sub: gateSub,
            expandable: (r.challengers || []).length > 0, isOpen: rOpen,
            selected: sel === 'gens' && p.epochId === epoch.id && String(p.round) === String(r.round_index),
            onSelect: () => ctx.navigate('gens', { epochId: epoch.id, round: r.round_index }),
            onToggle: () => onToggle(rKey),
          }));
          if (!rOpen) return;
          // The round's DEFENDING champion heads its field: a FULL node in its
          // birth round (its origin), a dimmed CARRIED reference in every later
          // round it defends — so the round's complete competing field shows
          // (champion + challengers) without a confusing plain duplicate. Then
          // the round's MINTED challengers.
          if (r.championId != null) {
            const champ = gensById.get(String(r.championId));
            const bornHere = champ && Number.isInteger(champ.round_index)
              && champ.round_index === r.round_index;
            if (bornHere) tree.appendChild(genLeaf(champ, 4));
            else tree.appendChild(champRefLeaf(String(r.championId), r.round_index, r.championEvalMode, 4));
          }
          for (const c of (r.challengers || [])) {
            const g = gensById.get(String(c.id)) || c;
            tree.appendChild(genLeaf(g, 4));
          }
        });
        // ORPHANS / rejected proposals with no resolved birth round sit under a
        // trailing bucket so nothing is dropped from the tree.
        const placed = new Set();
        for (const r of rounds) for (const c of (r.challengers || [])) placed.add(String(c.id));
        const seedIds = new Set(rounds.map((r) => String(r.championId)));
        const unplaced = bundle.gens.filter((g) => !placed.has(String(g.id)) && !seedIds.has(String(g.id)));
        for (const g of unplaced) tree.appendChild(genLeaf(g, 4));
      } else {
        // DEGRADE: a flat list (today's layout) when there is no round structure.
        for (const g of bundle.gens) tree.appendChild(genLeaf(g, 3));
      }
    }

    // Boards group
    const bKey = eKey + '/boards';
    const bOpen = open.has(bKey);
    tree.appendChild(branchRow({
      key: bKey, depth: 2, kind: 'group', label: 'Boards',
      sub: bundle.boards.length ? String(bundle.boards.length) : null,
      expandable: bundle.boards.length > 0, isOpen: bOpen,
      selected: sel === 'boards' && p.epochId === epoch.id,
      onSelect: () => ctx.navigate('boards', { epochId: epoch.id }),
      onToggle: () => onToggle(bKey),
    }));
    if (bOpen) {
      for (const b of bundle.boards) {
        const selected = sel === 'board' && p.epochId === epoch.id && p.entry === b.id;
        tree.appendChild(leafRow({
          depth: 3, kind: 'board', label: b.id, glyph: '▦', tag: b.kindTag || null,
          selected, live: isLive(b.id),
          onSelect: () => ctx.navigate('board', { epochId: epoch.id, entry: b.id }),
        }));
      }
    }

    // Mutation surface (leaf)
    tree.appendChild(leafRow({
      depth: 2, kind: 'mutations', label: 'Mutation surface', glyph: '⌗', tag: null,
      selected: sel === 'mutations' && p.epochId === epoch.id,
      onSelect: () => ctx.navigate('mutations', { epochId: epoch.id }),
    }));

    // Publication (leaf)
    tree.appendChild(leafRow({
      depth: 2, kind: 'paper', label: 'Publication', glyph: '¶', tag: null,
      selected: sel === 'publication' && p.epochId === epoch.id,
      onSelect: () => ctx.navigate('publication', { epochId: epoch.id }),
    }));
  }

  if (!model.epochs.length) {
    tree.appendChild(el('div', { class: 'dt-tree-empty', text: 'No epochs in this workspace yet.' }));
  }

  host.appendChild(tree);
}

function branchRow(o) {
  const row = el('div', {
    class: 'dt-node dt-branch dt-d' + o.depth + (o.selected ? ' dt-sel' : ''),
    'data-kind': o.kind, role: 'treeitem',
    'aria-expanded': o.expandable ? String(!!o.isOpen) : null,
    style: '--dt-depth:' + o.depth + ';',
  });
  const twisty = el('button', {
    class: 'dt-twisty' + (o.expandable ? '' : ' dt-twisty-leaf'),
    type: 'button', 'aria-hidden': o.expandable ? null : 'true',
    title: o.expandable ? (o.isOpen ? 'collapse' : 'expand') : '',
    text: o.expandable ? (o.isOpen ? '▾' : '▸') : '',
  });
  if (o.expandable && !o.lockOpen && o.onToggle) {
    twisty.addEventListener('click', (ev) => { ev.stopPropagation(); o.onToggle(); });
  } else {
    twisty.disabled = true;
  }
  const label = el('button', { class: 'dt-label', type: 'button' }, [
    el('span', { class: 'dt-icon dt-icon-' + o.kind, 'aria-hidden': 'true' }),
    el('span', { class: 'dt-text', text: o.label }),
    o.sub ? el('span', { class: 'dt-sub', text: o.sub }) : null,
  ].filter(Boolean));
  if (o.selected) label.setAttribute('aria-current', 'true');
  label.addEventListener('click', () => o.onSelect());
  row.appendChild(twisty);
  row.appendChild(label);
  return row;
}

function leafRow(o) {
  const row = el('div', {
    class: 'dt-node dt-leaf dt-d' + o.depth + (o.selected ? ' dt-sel' : ''),
    'data-kind': o.kind, role: 'treeitem',
    style: '--dt-depth:' + o.depth + ';',
  });
  row.appendChild(el('span', { class: 'dt-twisty dt-twisty-leaf', 'aria-hidden': 'true' }));
  const label = el('button', { class: 'dt-label', type: 'button' }, [
    el('span', { class: 'dt-glyph dt-glyph-' + o.kind, 'aria-hidden': 'true', text: o.glyph || '·' }),
    el('span', { class: 'dt-text', text: o.label }),
    // a subtle, CSS-pulsing ● clue on rows with LIVE activity (a running gen or
    // board entry). It is a clue, not a banner — reuses the dn-inflight-pulse
    // animation. Re-stamped only when the row ENTERS/LEAVES the live set (digest).
    o.live ? el('span', { class: 'dt-node-pulse dn-inflight-pulse', title: 'running', 'aria-label': 'running' }) : null,
    o.tag ? el('span', { class: 'dt-tag dt-tag-' + o.kind, text: o.tag }) : null,
  ].filter(Boolean));
  if (o.selected) label.setAttribute('aria-current', 'true');
  label.addEventListener('click', () => o.onSelect());
  row.appendChild(label);
  return row;
}
