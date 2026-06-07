/* ===================================================================
   EMBED MODE — shared across all 11 study pages.
   Honors URL params so the page can be composed into compose.html via
   an <iframe>. The NORMAL (no-param) view is left 100% unchanged: this
   script no-ops unless ?only / ?bare / ?theme is present.

   Contract:
     ?only=N   (1-based)  render ONLY option N (hide the others)
     ?bare=1              strip page chrome (header/footer/intro/sample/
                          index link + the option's title/rationale/legend/
                          reveal + structure pages' within-round view),
                          leaving just the option's primary figure(s).
     ?theme=NAME          set <html data-theme=NAME> at load (16 themes)

   Per-page config is provided in window.__EMBED_CFG__ (set just before
   this script runs):
     optSel        CSS selector for the option SECTIONS, in order.
     bareHide      [selectors] hidden inside a kept option in bare mode
                   (titles / rationales / legends / reveals / captions).
     viewSel       (structure pages) selector for the per-option VIEW
                   blocks (hero glance / single-round / within-round).
     bareKeepViews (structure pages) 0-based view indices to KEEP in bare
                   mode; the rest are hidden. Default keeps all.
   =================================================================== */
(function () {
  var P = new URLSearchParams(location.search);
  var only = P.get('only');
  var bare = P.get('bare') === '1' || P.get('bare') === 'true';
  var theme = P.get('theme');
  // composer override: comma-separated 0-based view indices to KEEP, replacing
  // the page's default bareKeepViews. The composer pins the single-round pane to
  // ?views=1 (the full tournament only) because the hero GLANCE (view 0) is
  // already covered by the separate hero level on the composed page.
  var viewsParam = P.get('views');
  if (only == null && !bare && !theme) return; // normal view untouched

  var CFG = window.__EMBED_CFG__ || {};
  var THEME_IDS = (window.THEME_IDS) || [];

  function apply() {
    // ---- theme (override whatever the picker defaulted to) ----
    if (theme && (!THEME_IDS.length || THEME_IDS.indexOf(theme) >= 0)) {
      document.documentElement.setAttribute('data-theme', theme);
    }

    document.documentElement.classList.add('embed');
    if (bare) document.documentElement.classList.add('embed-bare');

    var opts = CFG.optSel ? Array.prototype.slice.call(document.querySelectorAll(CFG.optSel)) : [];

    // ---- ?only=N — keep just one option ----
    var keepIdx = null;
    if (only != null) {
      keepIdx = parseInt(only, 10) - 1;
      opts.forEach(function (o, i) { if (i !== keepIdx) o.style.display = 'none'; });
    }
    var kept = (keepIdx != null && opts[keepIdx]) ? [opts[keepIdx]]
             : (opts.length ? opts : []);

    if (!bare) return;

    // ---- ?bare=1 — strip chrome ----
    var header = document.querySelector('header');
    if (header) header.style.display = 'none';
    var footer = document.querySelector('footer');
    if (footer) footer.style.display = 'none';

    // hide every direct child of <main> that is NOT an option we keep and
    // NOT the options container (kills intro lede, sample card, section h2…)
    var main = document.querySelector('main');
    var keepSet = kept;
    if (main) {
      Array.prototype.slice.call(main.children).forEach(function (child) {
        if (keepSet.indexOf(child) >= 0) return;                 // a kept option (cross-epoch: options live directly in main)
        if (child.querySelector && keepSet.some(function (k) { return child.contains(k); })) return; // contains the options host
        child.style.display = 'none';
      });
    }
    // body padding/margin trim so the iframe hugs the figure
    document.body.style.padding = '0';
    document.body.style.margin = '0';

    // within each kept option: hide titles / rationales / legends / reveals
    (CFG.bareHide || []).forEach(function (sel) {
      kept.forEach(function (k) {
        Array.prototype.slice.call(k.querySelectorAll(sel)).forEach(function (n) { n.style.display = 'none'; });
      });
    });
    // tidy the kept option container's own framing (border/padding) so the
    // figure reads as a clean composed panel, not a study card.
    kept.forEach(function (k) {
      k.style.margin = '0';
      k.style.border = 'none';
      k.style.background = 'transparent';
      k.style.padding = '6px 14px';
    });

    // structure pages: keep only selected view blocks. ?views= (composer) wins
    // over the page default bareKeepViews (which keeps hero-glance + single-round).
    var keepViews = viewsParam != null
      ? viewsParam.split(',').map(function (s) { return parseInt(s, 10); }).filter(function (n) { return !isNaN(n); })
      : CFG.bareKeepViews;
    if (CFG.viewSel && keepViews) {
      kept.forEach(function (k) {
        var views = Array.prototype.slice.call(k.querySelectorAll(CFG.viewSel));
        views.forEach(function (v, i) { if (keepViews.indexOf(i) < 0) v.style.display = 'none'; });
      });
    }
  }

  /* report content height to the composer so it can size the iframe */
  function reportHeight() {
    try {
      var h = Math.max(
        document.body.scrollHeight, document.documentElement.scrollHeight,
        document.body.offsetHeight, document.documentElement.offsetHeight
      );
      parent.postMessage({ __zEmbedHeight: h, key: P.get('lvl') || null }, '*');
    } catch (e) {}
  }

  function run() {
    apply();
    // measure after layout settles (svg sizing, fonts)
    requestAnimationFrame(function () { requestAnimationFrame(reportHeight); });
    setTimeout(reportHeight, 120);
    setTimeout(reportHeight, 400);
    window.addEventListener('resize', reportHeight);
  }

  if (document.readyState === 'complete' || document.readyState === 'interactive') run();
  else document.addEventListener('DOMContentLoaded', run);
})();
