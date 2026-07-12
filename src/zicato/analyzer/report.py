"""Comprehensive, academic-paper-style epoch analysis report.

This is the redesigned ``analysis.md`` / ``analysis.html`` artifact: a
detailed, cogent, ACM-journal-style narrative of one improvement
campaign (one epoch). It is **regenerated after every generation** —
wired into the orchestrator's per-round flow alongside the existing
decision-telemetry analyzer — so it is always current; by epoch close
it reads as a complete write-up.

Report structure (the section vocabulary an operator can rely on):

* Title + metadata
* Abstract
* Introduction
* Methodology
* Approach & Implementation
* Experimental Results
* Analysis — What Worked and What Didn't
* Threats to Validity & Limitations
* Conclusion & Next Directions

The markdown source carries headings WITHOUT explicit section numbers;
the HTML renderer auto-numbers ``h2 / h3 / h4`` (1, 1.1, 1.1.1) so the
report is consistently numbered regardless of which sections happen to
be present. Tables and figures are auto-numbered the same way.

Hybrid generation, for correctness:

* The deterministic, data-bearing sections (Methodology, every
  Experimental Results table, the score trajectory, Threats) are
  templated directly from the structured workspace data by
  :mod:`zicato.analyzer.report_sections` — exact by construction.
* The figures (inline SVG: score trajectory, drift-kind movements,
  per-board heatmap, lineage diagram, mutation surface) are produced
  by :mod:`zicato.analyzer.report_figures` from the same structured
  view; the deterministic sections drop ``<!-- FIGURE:NAME -->``
  markers and the HTML renderer substitutes the SVG at render time.
* The prose sections (Abstract, Introduction, the Analysis
  interpretation, Conclusion) are written by ONE bounded auxiliary-LLM
  call, given the structured data and the deterministic sections as
  context (:mod:`zicato.analyzer.report_prompts`).

The whole document is regenerated each round — not appended — which
keeps it internally coherent. The pass is strictly best-effort: any
failure (LLM timeout, LLM error, render error) substitutes a
placeholder and still writes a file. Internal failures inside this
module never propagate; the orchestrator wraps the call in a
``try / except`` regardless.

The report is written to ``epochs/{epoch_id}/analysis.md`` and a
rendered ``analysis.html`` so the existing
``/api/epoch/{epoch}/analysis.html`` dashboard endpoint serves the
latest version. This artifact is distinct from the per-round
``insights/round_NNNN.md`` proposer-feedback files, which are
untouched.
"""

from __future__ import annotations

import asyncio
import html as _html
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from zicato.analyzer.report_data import EpochReportData, gather_epoch_report_data
from zicato.analyzer.report_figures import render_figure
from zicato.analyzer.report_prompts import (
    PROSE_BLOCK_LABELS,
    REPORT_SYSTEM_PROMPT,
    parse_prose_blocks,
    render_report_user_prompt,
)
from zicato.analyzer.report_sections import (
    render_approach_section,
    render_methodology_section,
    render_proposer_analytics_section,
    render_results_section,
    render_statistical_integrity_section,
    render_threats_section,
    render_title_block,
)
from zicato.aux_timeout import aux_call_timeout_s
from zicato.core.workspace import analysis_path

log = logging.getLogger("zicato.analyzer.report")

# A goldfive-compatible auxiliary call_llm: (system, user, model) -> str.
_AuxCallLLM = Callable[[str, str, str], Awaitable[str]]

# Placeholder prose used when the LLM omits a block or the call fails.
_MISSING_PROSE = "_(prose section unavailable — the auxiliary LLM did not return it this round.)_"


# Explicit HTML-comment fences bracket each LLM-authored prose block in an
# assembled document (same invisible-marker scheme as ``<!-- FIGURE:... -->``
# / ``<!-- META -->``). The deterministic refresh re-lifts prose by these
# ANCHOR-EXACT fences, so LLM prose containing a ``---`` rule, an embedded
# ``## heading``, or any other structural line survives verbatim — the old
# "stop at the first ``## ``/``---``" heuristic silently truncated it. The
# fence lines render to nothing (HTML comments); see the renderer's skip
# branch. The suffix is spaced to match the label form (``<!-- PROSE:X -->``).
_PROSE_FENCE_OPEN_PREFIX = "<!-- PROSE:"
_PROSE_FENCE_CLOSE_PREFIX = "<!-- /PROSE:"
_PROSE_FENCE_SUFFIX = " -->"


def _prose_fence_open(label: str) -> str:
    """The opening fence line for a prose block, e.g. ``<!-- PROSE:ABSTRACT -->``."""
    return f"{_PROSE_FENCE_OPEN_PREFIX}{label}{_PROSE_FENCE_SUFFIX}"


def _prose_fence_close(label: str) -> str:
    """The closing fence line for a prose block, e.g. ``<!-- /PROSE:ABSTRACT -->``."""
    return f"{_PROSE_FENCE_CLOSE_PREFIX}{label}{_PROSE_FENCE_SUFFIX}"


def _is_prose_fence_line(stripped: str) -> bool:
    """True for either the open or close fence of any prose block."""
    return (
        stripped.startswith(_PROSE_FENCE_OPEN_PREFIX)
        or stripped.startswith(_PROSE_FENCE_CLOSE_PREFIX)
    ) and stripped.endswith(_PROSE_FENCE_SUFFIX.strip())


# ---------------------------------------------------------------------------
# Markdown assembly
# ---------------------------------------------------------------------------


def _placeholder_blocks() -> dict[str, str]:
    """Prose blocks substituted when the auxiliary LLM call fails entirely."""
    return {label: _MISSING_PROSE for label in PROSE_BLOCK_LABELS}


def assemble_report_markdown(
    data: EpochReportData,
    prose: dict[str, str],
    deterministic_sections: str,
) -> str:
    """Stitch the deterministic sections and the LLM prose into one document.

    The section order is fixed: Title, Abstract, Introduction,
    Methodology, Approach, Results, Analysis, Threats, Conclusion. The
    ``prose`` dict supplies the four interpretive sections keyed by the
    labels in :data:`PROSE_BLOCK_LABELS`; a missing key falls back to a
    placeholder so the document always carries every section.
    """
    abstract = prose.get("ABSTRACT", _MISSING_PROSE).strip() or _MISSING_PROSE
    introduction = prose.get("INTRODUCTION", _MISSING_PROSE).strip() or _MISSING_PROSE
    analysis = prose.get("ANALYSIS", _MISSING_PROSE).strip() or _MISSING_PROSE
    conclusion = prose.get("CONCLUSION", _MISSING_PROSE).strip() or _MISSING_PROSE

    def _prose_block(label: str, body: str) -> None:
        # Bracket every prose block in anchor-exact fences so a later
        # deterministic refresh re-lifts it verbatim (see
        # :func:`parse_prose_from_markdown`), even when the body carries a
        # ``---`` rule or an embedded ``## heading``.
        parts.append(_prose_fence_open(label))
        parts.append(body)
        parts.append(_prose_fence_close(label))

    parts: list[str] = []
    parts.append(render_title_block(data))
    parts.append("")
    parts.append("## Abstract")
    parts.append("")
    _prose_block("ABSTRACT", abstract)
    parts.append("")
    parts.append("## Introduction")
    parts.append("")
    _prose_block("INTRODUCTION", introduction)
    parts.append("")
    parts.append(deterministic_sections.strip())
    parts.append("")
    parts.append("## Analysis — What Worked and What Didn't")
    parts.append("")
    _prose_block("ANALYSIS", analysis)
    parts.append("")
    parts.append(render_threats_section(data))
    parts.append("")
    parts.append("## Conclusion & Next Directions")
    parts.append("")
    _prose_block("CONCLUSION", conclusion)
    parts.append("")
    parts.append("---")
    parts.append("")
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts.append(
        f"_Regenerated by zicato at {now} from `board.jsonl`, `scoring.json`, "
        f"`mutations.json`, `generations/*/experiment.json`, and `journal.md` "
        f"under epoch `{data.epoch_id}`. The data-bearing sections are "
        f"templated exactly from those artifacts; the prose sections are "
        f"LLM-authored. Section, table, and figure numbers are assigned by "
        f"the renderer._"
    )
    parts.append("")
    return "\n".join(parts)


def _deterministic_sections(data: EpochReportData) -> str:
    """Render the contiguous deterministic block (Methodology .. Results).

    These are the sections that sit between the Introduction and the
    Analysis in the final document. The Title and Threats sections are
    rendered separately because they bracket prose sections.
    """
    return "\n\n".join(
        (
            render_methodology_section(data),
            render_approach_section(data),
            render_results_section(data),
            render_statistical_integrity_section(data),
            render_proposer_analytics_section(data),
        )
    )


# ---------------------------------------------------------------------------
# Minimal, dependency-free Markdown -> paper-styled HTML
# ---------------------------------------------------------------------------


# A marker the deterministic sections emit at the point a figure should
# appear. The renderer substitutes the inline SVG (produced by
# :mod:`zicato.analyzer.report_figures`) wrapped in a ``<figure>`` with
# an auto-numbered caption.
_FIGURE_MARKER_PREFIX = "<!-- FIGURE:"
_FIGURE_MARKER_SUFFIX = "-->"
_META_MARKER = "<!-- META -->"
_EYEBROW_MARKER = "<!-- EYEBROW -->"
_CALLOUT_MARKER_PREFIX = "<!-- CALLOUT:"
_CALLOUT_MARKER_SUFFIX = "-->"

# A caption line precedes a figure or table block. The line is the
# literal string ``Caption: <text>`` (or ``**Caption.** <text>``); the
# renderer auto-numbers it (``Figure 3: ...`` / ``Table 5: ...``)
# according to whether the next block is a figure or a table.
_CAPTION_PREFIXES = ("Caption: ", "Caption:")


def _is_caption_line(text: str) -> bool:
    return text.startswith("Caption:")


def _strip_caption(text: str) -> str:
    """Strip the ``Caption: `` prefix off a caption line."""
    if text.startswith("Caption: "):
        return text[len("Caption: ") :]
    if text.startswith("Caption:"):
        return text[len("Caption:") :]
    return text


def _inline_md_to_html(text: str) -> str:
    """Convert inline markdown (bold, code) within one line to HTML.

    Deliberately small — the report markdown only uses ``**bold**`` and
    backtick ``code`` spans inline. Everything else is HTML-escaped.
    """
    escaped = _html.escape(text, quote=False)
    out: list[str] = []
    i = 0
    n = len(escaped)
    while i < n:
        # `code`
        if escaped[i] == "`":
            end = escaped.find("`", i + 1)
            if end != -1:
                out.append(f"<code>{escaped[i + 1 : end]}</code>")
                i = end + 1
                continue
        # **bold**
        if escaped.startswith("**", i):
            end = escaped.find("**", i + 2)
            if end != -1:
                inner = escaped[i + 2 : end]
                out.append(f"<strong>{inner}</strong>")
                i = end + 2
                continue
        out.append(escaped[i])
        i += 1
    return "".join(out)


def _table_cells(line: str) -> list[str]:
    """Split one markdown pipe-table row into cell text values."""
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [c.strip() for c in stripped.split("|")]


def _cell_is_numeric(text: str) -> bool:
    """Heuristic — does a table cell look like a numeric value?

    Numeric cells get right-aligned in paper style. The check is
    deliberately liberal — backtick-wrapped numbers (e.g. ``1.5``), and
    signed deltas (``+0.080`` / ``-0.250``) both count. Empty / "—" /
    "(seed)" do not, so missing values are left default-aligned.
    """
    stripped = text.strip().strip("`").strip()
    if not stripped or stripped in ("—", "-"):
        return False
    if stripped.startswith(("+", "-")):
        stripped = stripped[1:]
    if not stripped:
        return False
    # accept digits, dots, commas, percent signs
    cleaned = stripped.replace(".", "").replace(",", "").replace("%", "")
    return cleaned.isdigit()


def _render_md_table(rows: list[str], caption_html: str | None) -> str:
    """Render a contiguous block of markdown table lines as a paper-style table.

    The first row is treated as the header, the second row's alignment
    column is ignored (we infer per-column numeric alignment from the
    body cells), the remainder is the body. ``caption_html`` is the
    pre-rendered, auto-numbered caption fragment (``<figcaption>`` body)
    — passed in by the caller because numbering is sequential across
    the document.
    """
    if len(rows) < 2:
        return ""

    header = _table_cells(rows[0])
    body_rows = [_table_cells(r) for r in rows[2:]]
    n_cols = len(header)
    # Decide per-column alignment: a column is numeric when most non-
    # empty body cells look like numbers.
    align_right = [False] * n_cols
    for ci in range(n_cols):
        numeric = 0
        non_empty = 0
        for r in body_rows:
            if ci >= len(r):
                continue
            cell = r[ci]
            if cell:
                non_empty += 1
                if _cell_is_numeric(cell):
                    numeric += 1
        if non_empty and numeric >= max(1, non_empty // 2):
            align_right[ci] = True

    parts: list[str] = ['<figure class="paper-table">']
    if caption_html:
        parts.append(f"<figcaption>{caption_html}</figcaption>")
    parts.append("<table><thead><tr>")
    for ci, cell in enumerate(header):
        cls = ' class="num"' if align_right[ci] else ""
        parts.append(f"<th{cls}>{_inline_md_to_html(cell)}</th>")
    parts.append("</tr></thead><tbody>")
    for r in body_rows:
        # Decision-row highlight: any row whose cells include a bare
        # ``promoted`` / ``rejected`` / ``deferred`` / ``baseline`` token
        # picks up a matching ``row-*`` class so the same palette token
        # used everywhere else paints a thin coloured edge on the row.
        row_cls = ""
        cell_values = {c.strip().lower() for c in r}
        if "promoted" in cell_values:
            row_cls = ' class="row-promoted"'
        elif "rejected" in cell_values:
            row_cls = ' class="row-rejected"'
        elif "deferred" in cell_values:
            row_cls = ' class="row-deferred"'
        parts.append(f"<tr{row_cls}>")
        for ci in range(n_cols):
            cell = r[ci] if ci < len(r) else ""
            cls = ' class="num"' if align_right[ci] else ""
            parts.append(f"<td{cls}>{_inline_md_to_html(cell)}</td>")
        parts.append("</tr>")
    parts.append("</tbody></table></figure>")
    return "".join(parts)


def _section_numbering_label(counters: list[int], level: int) -> str:
    """Compute the dotted prefix (e.g. ``2.1.3``) for a given h-level."""
    # counters[0] is for h2, counters[1] for h3, counters[2] for h4.
    idx = level - 2
    if idx < 0:
        return ""
    # advance the counter at this level, reset deeper counters.
    counters[idx] += 1
    for k in range(idx + 1, len(counters)):
        counters[k] = 0
    return ".".join(str(c) for c in counters[: idx + 1] if c > 0)


def markdown_to_html(md: str, *, data: EpochReportData | None = None) -> str:
    """Convert the report markdown to a paper-styled HTML fragment.

    Supports the subset the report uses: ATX headings (``#``..``####``),
    bullet lists, fenced code blocks, pipe tables, horizontal rules,
    bold + code inline spans, paragraphs, ``<!-- FIGURE:NAME -->``
    figure markers, and ``Caption: ...`` caption lines preceding a
    figure or table.

    Auto-numbering:

    * ``h2`` → ``1.`` ``2.`` ...
    * ``h3`` → ``1.1`` ``1.2`` ...
    * ``h4`` → ``1.1.1`` ...
    * Tables → ``Table N:`` (across the whole document)
    * Figures → ``Figure N:`` (across the whole document)

    ``data`` is the structured epoch view. When supplied, figure markers
    are substituted with inline SVG; when absent (e.g. tests of the
    renderer alone) the markers render as empty placeholders.
    """
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)
    in_list = False
    # counters: [h2, h3, h4]
    h_counters = [0, 0, 0]
    table_counter = 0
    figure_counter = 0
    # The most recent caption text, awaiting attachment to the next
    # figure or table block.
    pending_caption: str | None = None

    def _close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    def _consume_caption() -> str | None:
        nonlocal pending_caption
        c = pending_caption
        pending_caption = None
        return c

    pending_meta = False  # next paragraph is the masthead metadata block
    pending_eyebrow = False  # next paragraph is the masthead eyebrow line
    masthead_open = False  # we have emitted an open <header class="paper-masthead">

    def _open_masthead() -> None:
        nonlocal masthead_open
        if not masthead_open:
            out.append('<header class="paper-masthead">')
            masthead_open = True

    def _close_masthead() -> None:
        nonlocal masthead_open
        if masthead_open:
            out.append("</header>")
            masthead_open = False

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Prose fence — an invisible ``<!-- PROSE:LABEL -->`` / ``<!-- /PROSE:
        # LABEL -->`` marker bracketing an LLM prose block. HTML comments
        # render to nothing; skip the line so it neither prints literally
        # nor gets swept into the following paragraph.
        if _is_prose_fence_line(stripped):
            _close_list()
            i += 1
            continue

        # Eyebrow marker — small-caps line above the title.
        if stripped == _EYEBROW_MARKER:
            _close_list()
            pending_eyebrow = True
            i += 1
            continue

        # Meta marker — the next paragraph is the masthead metadata.
        if stripped == _META_MARKER:
            _close_list()
            pending_meta = True
            i += 1
            continue

        # Callout marker: a one-paragraph margin pull-quote. The marker
        # carries a short label like ``KEY FINDING``; the next paragraph
        # is the quote body.
        if stripped.startswith(_CALLOUT_MARKER_PREFIX) and stripped.endswith(
            _CALLOUT_MARKER_SUFFIX
        ):
            _close_list()
            label = stripped[len(_CALLOUT_MARKER_PREFIX) : -len(_CALLOUT_MARKER_SUFFIX)].strip()
            # Consume the next non-blank paragraph as the callout body.
            i += 1
            while i < n and not lines[i].strip():
                i += 1
            body_lines: list[str] = []
            while i < n:
                nxt = lines[i].strip()
                if (
                    not nxt
                    or nxt.startswith(("#", "- ", "* ", "```", "---", "***", "___"))
                    or nxt.startswith(_FIGURE_MARKER_PREFIX)
                    or nxt == _META_MARKER
                    or nxt == _EYEBROW_MARKER
                    or nxt.startswith(_CALLOUT_MARKER_PREFIX)
                    or _is_prose_fence_line(nxt)
                    or _is_caption_line(nxt)
                    or "|" in nxt
                ):
                    break
                body_lines.append(nxt)
                i += 1
            body_html = " ".join(_inline_md_to_html(p) for p in body_lines)
            label_html = (
                f'<span class="callout-label">{_inline_md_to_html(label)}</span>' if label else ""
            )
            out.append(
                f'<aside class="paper-callout" role="note">' f"{label_html}{body_html}</aside>"
            )
            continue

        # Figure marker — substitute inline SVG.
        if stripped.startswith(_FIGURE_MARKER_PREFIX) and stripped.endswith(_FIGURE_MARKER_SUFFIX):
            _close_list()
            inner = stripped[len(_FIGURE_MARKER_PREFIX) : -len(_FIGURE_MARKER_SUFFIX)].strip()
            figure_counter += 1
            svg = render_figure(inner, data) if data is not None else ""
            caption_text = _consume_caption()
            caption_html = ""
            if caption_text:
                caption_html = (
                    f'<span class="figlabel">Figure {figure_counter}:</span> '
                    + _inline_md_to_html(caption_text)
                )
            elif inner:
                caption_html = (
                    f'<span class="figlabel">Figure {figure_counter}:</span> '
                    f"<em>{_html.escape(inner)}</em>"
                )
            out.append('<figure class="paper-figure">')
            if svg:
                out.append(svg)
            else:
                out.append(f'<div class="figure-placeholder">[figure: {_html.escape(inner)}]</div>')
            if caption_html:
                out.append(f"<figcaption>{caption_html}</figcaption>")
            out.append("</figure>")
            i += 1
            continue

        # Fenced code block.
        if stripped.startswith("```"):
            _close_list()
            code: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1  # consume closing fence
            body = _html.escape("\n".join(code), quote=False)
            out.append(f"<pre><code>{body}</code></pre>")
            continue

        # Pipe table — a header line followed by a delimiter line.
        if (
            "|" in stripped
            and i + 1 < n
            and set(lines[i + 1].strip()) <= set("|-: ")
            and "-" in lines[i + 1]
        ):
            _close_list()
            table_rows: list[str] = []
            while i < n and "|" in lines[i].strip():
                table_rows.append(lines[i])
                i += 1
            table_counter += 1
            caption_text = _consume_caption()
            caption_html = ""
            if caption_text:
                caption_html = (
                    f'<span class="figlabel">Table {table_counter}:</span> '
                    + _inline_md_to_html(caption_text)
                )
            out.append(_render_md_table(table_rows, caption_html or None))
            continue

        if not stripped:
            _close_list()
            i += 1
            continue

        # Caption line preceding a figure / table.
        if _is_caption_line(stripped):
            # Note: do not _close_list here — a caption is a structural
            # marker; if there is a hanging list it should still close
            # at the next non-list line. Captions never appear inside a
            # list in the deterministic sections.
            _close_list()
            pending_caption = _strip_caption(stripped)
            i += 1
            continue

        # Horizontal rule.
        if stripped in ("---", "***", "___"):
            _close_list()
            out.append('<hr class="paper-rule"/>')
            i += 1
            continue

        # ATX heading.
        if stripped.startswith("#"):
            _close_list()
            level = len(stripped) - len(stripped.lstrip("#"))
            level = min(max(level, 1), 6)
            content = stripped[level:].strip()
            # The Abstract is not numbered (academic convention); only
            # h2's that are NOT 'Abstract' get a number. Same convention
            # for h2 / h3 / h4 — only sections inside the body get
            # numbered.
            if level == 1:
                _open_masthead()
                out.append(f"<h1>{_inline_md_to_html(content)}</h1>")
            elif level == 2 and content.strip().lower() == "abstract":
                _close_masthead()
                out.append(f'<h2 class="unnumbered">{_inline_md_to_html(content)}</h2>')
            elif 2 <= level <= 4:
                _close_masthead()
                num = _section_numbering_label(h_counters, level)
                num_span = f'<span class="secnum">{num}</span> ' if num else ""
                out.append(f"<h{level}>{num_span}{_inline_md_to_html(content)}</h{level}>")
            else:
                _close_masthead()
                out.append(f"<h{level}>{_inline_md_to_html(content)}</h{level}>")
            i += 1
            continue

        # Bullet list item.
        if stripped.startswith(("- ", "* ")):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline_md_to_html(stripped[2:])}</li>")
            i += 1
            continue

        # Paragraph — accumulate consecutive non-blank, non-structural lines.
        _close_list()
        para: list[str] = [stripped]
        i += 1
        while i < n:
            nxt = lines[i].strip()
            if (
                not nxt
                or nxt.startswith(("#", "- ", "* ", "```", "---", "***", "___"))
                or nxt.startswith(_FIGURE_MARKER_PREFIX)
                or nxt == _META_MARKER
                or nxt == _EYEBROW_MARKER
                or nxt.startswith(_CALLOUT_MARKER_PREFIX)
                or _is_prose_fence_line(nxt)
                or _is_caption_line(nxt)
                or "|" in nxt
            ):
                break
            para.append(nxt)
            i += 1
        if pending_eyebrow:
            # Small-caps eyebrow line above the title (set by render_title_block).
            pending_eyebrow = False
            _open_masthead()
            out.append(
                '<div class="paper-eyebrow">'
                + " ".join(_inline_md_to_html(p) for p in para)
                + "</div>"
            )
        elif pending_meta:
            # The masthead metadata block: each paragraph-line of the
            # form ``**Label**: value`` becomes a stacked label/value
            # cell inside a CSS-grid metadata row.
            pending_meta = False
            _open_masthead()
            cells: list[str] = []
            for raw in para:
                cells.append(_format_meta_cell(raw))
            out.append('<div class="paper-meta">' + "".join(cells) + "</div>")
        else:
            out.append("<p>" + "<br/>".join(_inline_md_to_html(p) for p in para) + "</p>")

    _close_list()
    _close_masthead()
    return "\n".join(out)


def _format_meta_cell(raw: str) -> str:
    """Render one ``**Label**: value`` masthead-metadata line as a labelled cell.

    The masthead's metadata block lays out each labelled bit as a small
    label-over-value cell (label in small caps muted, value in the body
    colour). The renderer accepts the same ``**Label**: value`` markdown
    the deterministic section emits and produces the structured cell —
    on a parse miss the line falls back to a single inline span so the
    masthead never loses information.
    """
    text = raw.strip()
    # Strip the leading ``**Label**: `` if present.
    if text.startswith("**"):
        end = text.find("**", 2)
        if end != -1:
            label = text[2:end].strip()
            rest = text[end + 2 :].lstrip(": \t")
            label_html = _inline_md_to_html(label)
            value_html = _inline_md_to_html(rest)
            return (
                f'<span class="meta-row">'
                f"<strong>{label_html}</strong>"
                f'<span class="meta-value">{value_html}</span>'
                f"</span>"
            )
    return f'<span class="meta-row">{_inline_md_to_html(text)}</span>'


# --- Academic-paper CSS ---------------------------------------------------
#
# All paper styling is scoped to ``.paper`` so the same fragment renders
# the standalone ``analysis.html`` AND, when embedded inside the dark
# dashboard chrome, the inline Analysis-section card. Two property
# blocks: paper variables (light, paper-tone defaults) and the scoped
# typography / table / figure rules.
#
# The palette is exposed via CSS custom properties on ``.paper`` so a
# downstream host can override the palette without touching typography.
# The dashboard's ``.analysis-paper-card`` wrapper uses this to render
# the same fragment in a dashboard-dark palette while preserving every
# aspect of the paper typography (serif body, justified text, table
# rules, figure layout). The variable surface is structured into:
#
#   * surface tones — ``--paper-bg``, ``--paper-text``, ``--paper-muted``
#   * rules — ``--paper-rule``, ``--paper-soft-rule``
#   * code surfaces — ``--paper-code-bg``
#   * link accent — ``--paper-accent``
#   * figure surface — ``--paper-figure-bg``, ``--paper-figure-grid``,
#     ``--paper-figure-stripe-bg``
#   * decision palette (shared with the dashboard's accent tokens) —
#     ``--paper-promoted``, ``--paper-rejected``, ``--paper-deferred``,
#     ``--paper-baseline``, ``--paper-neutral``
#   * table zebra striping — ``--paper-table-zebra``
#
# All SVG figures consume the decision palette / grid tokens via CSS
# variables (see :mod:`zicato.analyzer.report_figures`), and use
# ``currentColor`` for axis text — so a dark host palette flips the
# figure rendering with no SVG-source changes.
_PAPER_VARS = """
.paper {
  --paper-bg: #fafaf7;
  --paper-text: #1e1f22;
  --paper-muted: #5a5d63;
  --paper-rule: #c8cacd;
  --paper-soft-rule: #e6e7e9;
  --paper-hairline: #d8d6cf;
  --paper-code-bg: #eeede7;
  --paper-accent: #2b4f7a;
  --paper-promoted: #2ea043;
  --paper-rejected: #d73a49;
  --paper-deferred: #bf8700;
  --paper-incomplete: #bf8700;
  --paper-baseline: #6e7681;
  --paper-neutral: #8a8d91;
  --paper-predicted: #6e7681;
  --paper-figure-bg: transparent;
  --paper-figure-grid: #d0d7de;
  --paper-figure-stripe-bg: #eef0f3;
  --paper-table-zebra: rgba(0, 0, 0, 0.022);
  --paper-callout-bg: rgba(43, 79, 122, 0.06);
  --paper-callout-rule: var(--paper-accent);
  --paper-font-body: 'Source Serif Pro', 'Source Serif 4', 'Charter',
    'Iowan Old Style', 'Cambria', Georgia, 'Times New Roman', serif;
  --paper-font-display: 'Inter', 'IBM Plex Sans', 'Helvetica Neue',
    Helvetica, 'Segoe UI', Arial, sans-serif;
  --paper-font-mono: 'JetBrains Mono', 'Source Code Pro', ui-monospace,
    SFMono-Regular, Menlo, Consolas, monospace;
}
""".strip()

_PAPER_TYPOGRAPHY = """
.paper, .paper * { box-sizing: border-box; }
.paper {
  background: var(--paper-bg);
  color: var(--paper-text);
  font-family: var(--paper-font-body);
  line-height: 1.58;
  font-size: 16px;
  text-rendering: optimizeLegibility;
  font-feature-settings: "kern" 1, "liga" 1, "onum" 1;
  -webkit-font-smoothing: antialiased;
}
.paper-article {
  max-width: 760px;
  margin: 0 auto;
  padding: 56px 44px 72px;
  text-align: justify;
  hyphens: auto;
}
.paper h1, .paper h2, .paper h3, .paper h4 {
  font-family: var(--paper-font-display);
  color: var(--paper-text);
  text-align: left;
  line-height: 1.22;
  font-feature-settings: "kern" 1, "liga" 1, "ss01" 1, "tnum" 1;
}

/* --- Masthead: title + metadata block ----------------------------------- */
/* Multi-line composition: a small caps eyebrow naming the artifact, the
   epoch name as the title, a thin rule, then the structured metadata
   row. Reads as an actual paper cover rather than a flat heading. */
.paper .paper-masthead {
  margin: 0 0 28px;
  padding: 0 0 18px;
  border-bottom: 1px solid var(--paper-rule);
  text-align: left;
}
.paper .paper-masthead .paper-eyebrow {
  font-family: var(--paper-font-display);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--paper-muted);
  margin: 0 0 14px;
  padding-bottom: 10px;
  border-top: 1px solid var(--paper-hairline);
  padding-top: 14px;
}
.paper h1 {
  font-size: 30px;
  margin: 0 0 10px 0;
  font-weight: 700;
  letter-spacing: -0.012em;
  line-height: 1.15;
}
.paper h2 {
  font-size: 19px;
  margin: 36px 0 8px 0;
  padding-bottom: 6px;
  border-bottom: 1px solid var(--paper-hairline);
  font-weight: 600;
  letter-spacing: -0.005em;
}
.paper h2.unnumbered {
  text-align: center;
  border-bottom: none;
  margin-top: 28px;
  font-size: 12px;
  letter-spacing: 0.24em;
  text-transform: uppercase;
  color: var(--paper-muted);
  font-weight: 600;
}
.paper h3 {
  font-size: 12px;
  margin: 22px 0 6px 0;
  font-weight: 700;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  color: var(--paper-muted);
}
.paper h4 {
  font-size: 14px;
  margin: 18px 0 6px 0;
  font-weight: 600;
  color: var(--paper-text);
  font-style: normal;
  letter-spacing: 0;
}
.paper h2 .secnum, .paper h3 .secnum, .paper h4 .secnum {
  display: inline-block;
  min-width: 2.4em;
  margin-right: 0.5em;
  color: var(--paper-muted);
  font-weight: 500;
  font-feature-settings: "tnum" 1, "lnum" 1;
}
.paper h3 .secnum { color: var(--paper-baseline); }
.paper p {
  margin: 10px 0;
  orphans: 2;
  widows: 2;
  overflow-wrap: break-word;
}
.paper li { overflow-wrap: break-word; }
/* Abstract section: indented, italic, single drop cap. The drop-cap is
   conservatively scoped to the FIRST paragraph after the unnumbered
   Abstract heading, never beyond. */
.paper h2.unnumbered + p {
  margin: 14px auto 18px;
  max-width: 92%;
  text-indent: 0;
  font-size: 15.5px;
  line-height: 1.62;
  color: var(--paper-text);
}
.paper h2.unnumbered + p::first-letter {
  float: left;
  font-family: var(--paper-font-display);
  font-size: 3.4em;
  line-height: 0.92;
  font-weight: 700;
  padding: 4px 8px 0 0;
  color: var(--paper-accent);
}
/* Section openers — the first paragraph after a numbered h2 keeps a
   generous lead-in and resets text-indent so the structured layout
   reads cleanly even after a centred figure. */
.paper h2 + p { margin-top: 12px; }

.paper ul { margin: 10px 0; padding-left: 22px; text-align: left; }
.paper li { margin: 4px 0; }
.paper a { color: var(--paper-accent); text-decoration: none; }
.paper a:hover { text-decoration: underline; }
.paper code {
  font-family: var(--paper-font-mono);
  font-size: 0.86em;
  background: var(--paper-code-bg);
  padding: 1px 5px;
  border-radius: 3px;
  color: var(--paper-text);
  /* A long unbroken token in prose (a contract hash, an absolute path)
     breaks rather than pushing the article column past the page. */
  overflow-wrap: anywhere;
}
.paper pre {
  background: var(--paper-code-bg);
  border: 1px solid var(--paper-soft-rule);
  border-radius: 4px;
  padding: 10px 14px;
  overflow-x: auto;
  font-size: 13px;
  line-height: 1.45;
  text-align: left;
}
.paper pre code { background: none; padding: 0; }
.paper strong { font-weight: 600; }
.paper em { font-style: italic; }
.paper hr.paper-rule {
  border: none;
  border-top: 1px solid var(--paper-soft-rule);
  margin: 28px 0;
}

/* --- Masthead metadata row ---------------------------------------------- */
.paper .paper-meta {
  font-family: var(--paper-font-display);
  font-size: 12px;
  line-height: 1.6;
  color: var(--paper-muted);
  text-align: left;
  margin: 0;
  padding: 6px 0 0;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 4px 24px;
  font-feature-settings: "tnum" 1, "lnum" 1;
}
.paper .paper-meta strong {
  color: var(--paper-text);
  font-weight: 600;
  font-size: 10.5px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  display: block;
  color: var(--paper-muted);
}
.paper .paper-meta .meta-row {
  display: block;
  padding: 4px 0;
}
.paper .paper-meta .meta-value {
  display: block;
  color: var(--paper-text);
  font-weight: 500;
  font-size: 13px;
  line-height: 1.4;
  /* A long masthead value (a full contract hash, a long goal) breaks
     inside its grid cell rather than widening the metadata row. */
  overflow-wrap: anywhere;
  min-width: 0;
}
.paper .paper-meta .meta-row { min-width: 0; }
.paper .paper-meta code {
  background: transparent;
  padding: 0;
  font-size: 0.92em;
  color: var(--paper-text);
}

/* --- Tables (paper style: thin top + bottom rules, no chunky borders) --- */
.paper figure.paper-table {
  margin: 16px 0 22px;
  padding: 0;
  text-align: left;
  /* Wide tables (e.g. mutation surface with absolute file paths) scroll
     horizontally inside their figure rather than overflow the article
     column or the host dashboard card. */
  overflow-x: auto;
  max-width: 100%;
}
.paper figure.paper-table > figcaption {
  font-family: var(--paper-font-display);
  font-size: 12px;
  color: var(--paper-muted);
  margin: 0 0 6px;
  text-align: left;
  letter-spacing: 0.01em;
}
.paper table {
  border-collapse: collapse;
  width: 100%;
  font-family: var(--paper-font-display);
  font-size: 12.5px;
  margin: 0;
  text-align: left;
  border-top: 1.3px solid var(--paper-text);
  border-bottom: 1.3px solid var(--paper-text);
  /* Help the table wrap long content cells (paths, hashes) instead of
     widening past the figure container. ``code`` cells additionally
     opt into break-anywhere so a long path can break inside a slash
     run rather than push the column. */
  table-layout: auto;
}
/* Path-like ``<code>`` cells inside paper tables can break anywhere so
   long absolute paths do not blow out the column width. Scoped to
   table cells so prose / inline-code outside tables keeps its
   non-breaking monospaced rendering. */
.paper figure.paper-table table td code,
.paper figure.paper-table table th code {
  overflow-wrap: anywhere;
  word-break: break-word;
  white-space: normal;
}
.paper table thead tr { border-bottom: 0.7px solid var(--paper-text); }
.paper table th, .paper table td {
  padding: 6px 11px;
  border: none;
  vertical-align: top;
  line-height: 1.45;
}
.paper table th {
  font-weight: 600;
  text-align: left;
  background: transparent;
  font-size: 10.5px;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--paper-muted);
  padding-top: 4px;
  padding-bottom: 6px;
}
.paper table th.num, .paper table td.num {
  text-align: right;
  font-feature-settings: "tnum" 1, "lnum" 1;
  white-space: nowrap;
}
.paper table tbody tr:nth-child(even) {
  background: var(--paper-table-zebra);
}
/* Table cell highlights for decision-coloured rows. Marker classes are
   emitted by the section renderers; the paint flows from the same
   palette tokens every figure uses, so a row reads as "promoted" or
   "rejected" in one hue across the whole document. */
.paper table td.cell-promoted, .paper table tbody tr.row-promoted td {
  box-shadow: inset 3px 0 0 0 var(--paper-promoted);
}
.paper table td.cell-rejected, .paper table tbody tr.row-rejected td {
  box-shadow: inset 3px 0 0 0 var(--paper-rejected);
}
.paper table td.cell-deferred, .paper table tbody tr.row-deferred td {
  box-shadow: inset 3px 0 0 0 var(--paper-deferred);
}

/* --- Figures (inline SVG, caption below; borderless, consistent) ------- */
.paper figure.paper-figure {
  margin: 22px 0 24px;
  padding: 0;
  text-align: center;
}
.paper figure.paper-figure > svg {
  max-width: 100%;
  height: auto;
  display: block;
  margin: 0 auto;
}
.paper figure.paper-figure > figcaption {
  font-family: var(--paper-font-display);
  font-size: 12px;
  color: var(--paper-muted);
  margin-top: 8px;
  text-align: left;
  padding: 0 6%;
  line-height: 1.5;
}
.paper .figlabel {
  font-weight: 600;
  color: var(--paper-text);
  letter-spacing: 0.02em;
}
.paper .figure-placeholder {
  border: 1px dashed var(--paper-rule);
  padding: 18px;
  font-style: italic;
  color: var(--paper-muted);
  text-align: center;
}

/* --- Callout / pull-quote (one per Analysis section, conservative) ----- */
.paper .paper-callout {
  margin: 18px 0;
  padding: 12px 16px 12px 18px;
  background: var(--paper-callout-bg);
  border-left: 3px solid var(--paper-callout-rule);
  font-family: var(--paper-font-display);
  font-size: 13.5px;
  font-style: italic;
  color: var(--paper-text);
  line-height: 1.5;
  border-radius: 0 3px 3px 0;
}
.paper .paper-callout .callout-label {
  display: block;
  font-style: normal;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--paper-muted);
  margin-bottom: 4px;
}

/* --- SVG defaults inside the paper -------------------------------------- */
/* Figures inherit the host's foreground colour via currentColor, so a
   dark wrapper flips axis text/values to the dark-palette foreground
   automatically. Decision-coloured strokes/fills bind to the palette
   variables (``--paper-promoted`` etc.) declared above so themes can
   re-tint them too. */
.paper svg {
  color: var(--paper-text);
}
.paper svg .svg-axis {
  font-family: var(--paper-font-display);
  font-size: 10.5px;
  fill: var(--paper-muted);
}
.paper svg .svg-axislabel {
  font-size: 11px;
  font-weight: 600;
  fill: var(--paper-text);
  letter-spacing: 0.04em;
  text-transform: uppercase;
}
.paper svg .svg-value {
  font-weight: 600;
  fill: var(--paper-text);
  font-feature-settings: "tnum" 1, "lnum" 1;
}
.paper svg .svg-label {
  font-family: var(--paper-font-display);
  font-size: 11px;
  fill: var(--paper-text);
}
.paper svg .svg-legend {
  font-family: var(--paper-font-display);
  font-size: 10px;
  fill: var(--paper-muted);
  letter-spacing: 0.03em;
}
.paper svg .svg-title {
  font-family: var(--paper-font-display);
  font-size: 11px;
  font-weight: 700;
  fill: var(--paper-text);
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.paper svg .svg-mono {
  font-family: var(--paper-font-mono);
  font-size: 10.5px;
}
/* Decision colour tokens for SVG strokes/fills. Figures emit
   ``class="svg-promoted"`` (etc.) on the elements that should pick up
   the decision palette; the host paints them via these CSS vars. */
.paper svg .svg-promoted-stroke { stroke: var(--paper-promoted); }
.paper svg .svg-promoted-fill   { fill: var(--paper-promoted); }
.paper svg .svg-rejected-stroke { stroke: var(--paper-rejected); }
.paper svg .svg-rejected-fill   { fill: var(--paper-rejected); }
.paper svg .svg-deferred-stroke { stroke: var(--paper-deferred); }
.paper svg .svg-deferred-fill   { fill: var(--paper-deferred); }
.paper svg .svg-incomplete-stroke { stroke: var(--paper-incomplete); }
.paper svg .svg-incomplete-fill   { fill: var(--paper-incomplete); }
.paper svg .svg-baseline-stroke { stroke: var(--paper-baseline); }
.paper svg .svg-baseline-fill   { fill: var(--paper-baseline); }
.paper svg .svg-neutral-stroke  { stroke: var(--paper-neutral); }
.paper svg .svg-neutral-fill    { fill: var(--paper-neutral); }
.paper svg .svg-predicted-stroke { stroke: var(--paper-predicted); }
.paper svg .svg-predicted-fill   { fill: var(--paper-predicted); }
.paper svg .svg-grid-stroke     { stroke: var(--paper-figure-grid); }
.paper svg .svg-grid-fill       { fill: var(--paper-figure-grid); }
.paper svg .svg-stripe-bg       { fill: var(--paper-figure-stripe-bg); }
""".strip()

# The standalone HTML's full page background uses a darker margin tone
# so the centred .paper-article reads as a real sheet sitting on a
# muted desk.
_STANDALONE_PAGE = """
html, body { margin: 0; padding: 0; }
body {
  background: #e9e7e1;
  min-height: 100vh;
  padding: 32px 0 48px;
}
body > .paper {
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08),
              0 12px 32px rgba(0, 0, 0, 0.07);
  border-radius: 2px;
  max-width: 824px;
  margin: 0 auto;
}
""".strip()


def _paper_css(*, standalone: bool) -> str:
    """Assemble the paper CSS for one rendering surface.

    The fragment used inline by the dashboard omits the page-level body
    background — it embeds inside the dashboard chrome, which already
    paints the surround.
    """
    parts = [_PAPER_VARS, _PAPER_TYPOGRAPHY]
    if standalone:
        parts.append(_STANDALONE_PAGE)
    return "\n".join(parts)


def render_report_html(
    epoch_id: str,
    report_md: str,
    *,
    data: EpochReportData | None = None,
) -> str:
    """Render the report markdown into a self-contained, paper-styled HTML document.

    Zero external resources — inline CSS, inline SVG, no web-font
    fetches — so the file renders identically over ``file://`` as it
    does through the dashboard endpoint. The standalone document reads
    as a single page with the centred paper-article block; the same
    fragment also embeds inline in the dashboard via
    :func:`render_report_html_fragment`.

    ``data`` carries the structured epoch view from which inline figure
    SVGs are produced. When absent (e.g. a renderer-only test), figure
    markers degrade to small placeholders so the structural envelope is
    still well-formed.
    """
    body = markdown_to_html(report_md, data=data)
    title = f"zicato — epoch {_html.escape(epoch_id)} analysis report"
    css = _paper_css(standalone=True)
    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />'
        f"<title>{title}</title>"
        f"<style>{css}</style>"
        "</head><body>"
        '<article class="paper"><div class="paper-article">'
        f"{body}"
        "</div></article>"
        "</body></html>"
    )


def render_report_html_fragment(
    epoch_id: str,
    report_md: str,
    *,
    data: EpochReportData | None = None,
) -> str:
    """Render the report as a self-contained HTML fragment for inline embedding.

    Used by the dashboard's Epoch view to drop the paper-styled report
    inline (inside the dark dashboard chrome) without iframe-ing the
    standalone document. The fragment carries its own ``<style>`` block
    scoped to ``.paper`` so it cannot leak typography into the
    dashboard's surrounding chrome. The dashboard wraps this fragment
    in a paper card; the fragment itself is the article body.

    ``epoch_id`` is accepted for parity with :func:`render_report_html`
    (and for future use, e.g. anchor ids) — it is not embedded today.
    """
    body = markdown_to_html(report_md, data=data)
    css = _paper_css(standalone=False)
    # The ``data-epoch`` attribute is informational; the dashboard does
    # not parse it but it eases debugging the rendered fragment.
    epoch_attr = _html.escape(epoch_id, quote=True)
    return (
        f"<style>{css}</style>"
        f'<article class="paper paper-card" data-epoch="{epoch_attr}">'
        '<div class="paper-article">'
        f"{body}"
        "</div></article>"
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def restamp_masthead(report_md: str, data: EpochReportData) -> str:
    """Splice a freshly-rendered masthead over a report's title block.

    The masthead (status / goal / generation counts) is fully data-derived,
    so it can be regenerated without the auxiliary LLM. The title block runs
    from the top of the document to the first level-2 heading (the Abstract);
    everything from that heading on — the LLM narrative — is preserved
    verbatim. A no-op (returns the input unchanged) on any document that is
    not in the analyzer's ``<!-- META -->`` masthead format.
    """
    if "<!-- META -->" not in report_md:
        return report_md
    lines = report_md.split("\n")
    body_idx = next((i for i, line in enumerate(lines) if line.startswith("## ")), None)
    if body_idx is None:
        return report_md
    return render_title_block(data) + "\n\n" + "\n".join(lines[body_idx:])


#: The h2 headings that bracket the four LLM-authored prose blocks in an
#: assembled document, mapped to their :data:`PROSE_BLOCK_LABELS` key. Used
#: to lift existing prose out of a persisted report so a deterministic-only
#: refresh can re-template every data section WITHOUT discarding the prose.
_PROSE_HEADINGS: tuple[tuple[str, str], ...] = (
    ("## Abstract", "ABSTRACT"),
    ("## Introduction", "INTRODUCTION"),
    ("## Analysis", "ANALYSIS"),
    ("## Conclusion", "CONCLUSION"),
)


def _mask_regen_timestamp(report_md: str) -> str:
    """Blank the volatile ``_Regenerated by zicato at <ts>...`` footer line.

    Used by the digest gate so a refresh whose only difference is the
    regeneration timestamp reads as a content no-op.
    """
    return "\n".join(
        "" if line.startswith("_Regenerated by zicato at ") else line
        for line in report_md.replace("\r\n", "\n").split("\n")
    )


def parse_prose_from_markdown(report_md: str) -> dict[str, str]:
    """Lift the four LLM-authored prose blocks out of an assembled report.

    Returns a ``{LABEL: text}`` dict for every prose block found (keyed by
    :data:`PROSE_BLOCK_LABELS`), skipping the placeholder body so a
    never-written block is not resurrected as prose. Absent blocks are
    simply not present in the result, which the assembler then fills with a
    placeholder — so a document with no prose yet round-trips to
    placeholders, not to broken sections.

    Two parse paths:

    * **Fenced (current format).** When the document carries the explicit
      ``<!-- PROSE:LABEL -->`` … ``<!-- /PROSE:LABEL -->`` fences, each block
      is lifted ANCHOR-EXACT — the whole text between its open and close
      fence, INCLUDING any ``---`` rule or embedded ``## heading`` the LLM
      wrote. This is the fix for the silent truncation the old heuristic
      caused; the fences round-trip a block byte-identically.
    * **Legacy (unfenced) fallback.** A pre-fix document written before the
      fences existed has none, so we fall back to the old heuristic (body
      from a ``## <Heading>`` to the next ``## ``/``---``). Whatever it
      captures is spliced back verbatim and the assembler re-emits it WITH
      fences, so the document self-heals to the exact format on the first
      deterministic refresh.
    """
    text = report_md.replace("\r\n", "\n")
    # An open fence anywhere ⇒ the document is in the fenced format; parse
    # exclusively by fence (never mix the heuristic in — a fenced body may
    # legitimately contain ``## ``/``---`` lines the heuristic would trip on).
    if _PROSE_FENCE_OPEN_PREFIX in text:
        return _parse_prose_fenced(text)
    return _parse_prose_heuristic(text)


def _parse_prose_fenced(text: str) -> dict[str, str]:
    """Lift each prose block by its anchor-exact ``<!-- PROSE:LABEL -->`` fence."""
    lines = text.split("\n")
    open_to_label = {_prose_fence_open(lbl): lbl for lbl in PROSE_BLOCK_LABELS}
    out: dict[str, str] = {}
    i = 0
    n = len(lines)
    while i < n:
        label = open_to_label.get(lines[i].strip())
        if label is None:
            i += 1
            continue
        close = _prose_fence_close(label)
        body: list[str] = []
        i += 1
        # Capture verbatim to the matching close fence (or EOF). Only the
        # exact close sentinel terminates the block, so structural lines —
        # rules, headings, other blocks' fences — are preserved as body.
        while i < n and lines[i].strip() != close:
            body.append(lines[i])
            i += 1
        i += 1  # consume the close fence
        captured = "\n".join(body).strip()
        if captured and captured != _MISSING_PROSE:
            out[label] = captured
    return out


def _parse_prose_heuristic(text: str) -> dict[str, str]:
    """Legacy unfenced parse: body from a ``## <Heading>`` to the next ``## ``/``---``.

    Retained ONLY for pre-fix documents that predate the prose fences; it
    truncates a block at the first ``## ``/``---`` it contains (the very bug
    the fenced format fixes), but a legacy document is no worse off than
    under the old code, and the assembler re-emits the captured prose WITH
    fences so the document upgrades on its first refresh.
    """
    lines = text.split("\n")
    out: dict[str, str] = {}
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        label = None
        for prefix, lbl in _PROSE_HEADINGS:
            if line.startswith(prefix):
                label = lbl
                break
        if label is None:
            i += 1
            continue
        body: list[str] = []
        i += 1
        while i < n:
            nxt = lines[i]
            if nxt.startswith("## ") or nxt.strip() == "---":
                break
            body.append(nxt)
            i += 1
        captured = "\n".join(body).strip()
        if captured and captured != _MISSING_PROSE:
            out[label] = captured
    return out


def regenerate_epoch_report_deterministic(workspace_root: Path, epoch_id: str) -> bool:
    """Refresh a persisted report's DETERMINISTIC sections — no LLM call.

    The event-driven freshness path (see ``docs/design/PUBLICATION.md``):
    after each settled round the orchestrator calls this to re-template
    every data-bearing section (masthead, methodology, results, validity,
    proposer analytics, threats) from the CURRENT workspace data, while
    preserving the existing LLM-authored prose verbatim. Cost discipline —
    no auxiliary-LLM call is made; the full LLM prose render happens at
    epoch close. Mid-epoch the masthead carries the ``LIVING DRAFT`` stamp
    (data-derived: dropped once the epoch is marked closed).

    Idempotent and digest-gated: returns ``True`` only when the rewrite
    actually changed ``analysis.md`` on disk; a byte-identical regeneration
    is a no-op (``False``) and rewrites nothing, so a settled round that
    moved no data never churns the file (and the dashboard's digest
    discipline rebuilds zero DOM). Best-effort on the HTML companion — a
    render failure there never loses the refreshed markdown.
    """
    data = gather_epoch_report_data(workspace_root, epoch_id)
    deterministic = _deterministic_sections(data)

    md_path = analysis_path(workspace_root, epoch_id)
    try:
        existing = md_path.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    prose = parse_prose_from_markdown(existing) if existing else {}
    for label in PROSE_BLOCK_LABELS:
        prose.setdefault(label, _MISSING_PROSE)

    new_md = assemble_report_markdown(data, prose, deterministic)
    # Digest gate: the assembled document carries a volatile "Regenerated
    # at <now>" footer, so a raw equality check would always differ. Mask
    # that one line on both sides — when only the timestamp moved the
    # content is unchanged, so keep the existing file byte-for-byte (the
    # dashboard's digest discipline then rebuilds zero DOM).
    if _mask_regen_timestamp(new_md) == _mask_regen_timestamp(existing):
        return False

    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(new_md, encoding="utf-8")
    try:
        md_path.with_suffix(".html").write_text(
            render_report_html(epoch_id, new_md, data=data),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001 — HTML is non-critical
        log.debug("epoch report: deterministic analysis.html refresh skipped (%s)", exc)
    return True


def restamp_persisted_report(workspace_root: Path, epoch_id: str) -> bool:
    """Rewrite a persisted ``analysis.md``/``.html`` masthead from CURRENT data.

    The comprehensive report is regenerated after every round, so the copy
    on disk is frozen at the *last mid-run* pass — its masthead reads
    "in progress" with pre-close counts even after the epoch closes. This
    re-renders just the (deterministic) masthead from the epoch's current
    config + generations and rewrites both files, leaving the expensive LLM
    narrative untouched. Cheap, no LLM, idempotent. Returns ``True`` when the
    file actually changed; a no-op (``False``) when absent, already current,
    or not in masthead format.
    """
    md_path = analysis_path(workspace_root, epoch_id)
    try:
        report_md = md_path.read_text(encoding="utf-8")
    except OSError:
        return False
    data = gather_epoch_report_data(workspace_root, epoch_id)
    new_md = restamp_masthead(report_md, data)
    if new_md == report_md:
        return False
    md_path.write_text(new_md, encoding="utf-8")
    try:
        md_path.with_suffix(".html").write_text(
            render_report_html(epoch_id, new_md, data=data),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001 — HTML is non-critical
        log.debug("epoch report: analysis.html re-stamp skipped (%s)", exc)
    return True


async def generate_epoch_report(
    workspace_root: Path,
    epoch_id: str,
    aux_call_llm: _AuxCallLLM,
    model: str = "",
) -> Path:
    """Regenerate the comprehensive epoch analysis report.

    Gathers the structured workspace data, renders the deterministic
    sections, asks the auxiliary LLM for the four prose sections in one
    bounded call, assembles the full document, and writes both
    ``analysis.md`` and ``analysis.html`` under the epoch directory.

    The pass is **best-effort**. The auxiliary-LLM call is wrapped in
    :func:`asyncio.wait_for` against :func:`aux_call_timeout_s`; a
    timeout or any LLM error substitutes placeholder prose and the
    deterministic sections still ship. The file is therefore *always*
    written when this function returns normally.

    Internal failures (path math, disk write) DO raise — that is the
    correct behaviour for the orchestrator's ``try / except`` wrapper,
    which keeps a wedge here from aborting the round or the loop.

    Parameters
    ----------
    workspace_root:
        Absolute path to the ``.zicato/`` workspace root.
    epoch_id:
        The epoch whose report should be regenerated.
    aux_call_llm:
        The AUXILIARY LLM callable (see :class:`RuntimeConfig` and the
        collusion guard) — never the inner-harness callable.
    model:
        Optional model identifier forwarded verbatim to *aux_call_llm*.

    Returns
    -------
    Path
        Absolute path of the written ``analysis.md``.
    """
    data = gather_epoch_report_data(workspace_root, epoch_id)
    deterministic = _deterministic_sections(data)

    user_prompt = render_report_user_prompt(data, deterministic)
    try:
        response = await asyncio.wait_for(
            aux_call_llm(REPORT_SYSTEM_PROMPT, user_prompt, model),
            timeout=aux_call_timeout_s(),
        )
        prose = parse_prose_blocks(response)
        if not prose:
            log.debug(
                "epoch report: auxiliary LLM returned no parseable prose blocks; "
                "substituting placeholders"
            )
            prose = _placeholder_blocks()
    except TimeoutError:
        log.warning(
            "epoch report: auxiliary LLM timed out after %.1fs; "
            "writing report with placeholder prose",
            aux_call_timeout_s(),
        )
        prose = _placeholder_blocks()
    except Exception as exc:  # noqa: BLE001 — opaque LLM errors are common
        log.warning(
            "epoch report: auxiliary LLM call failed (%s: %s); "
            "writing report with placeholder prose",
            type(exc).__name__,
            exc,
        )
        prose = _placeholder_blocks()

    report_md = assemble_report_markdown(data, prose, deterministic)

    md_path = analysis_path(workspace_root, epoch_id)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(report_md, encoding="utf-8")

    # The HTML companion is best-effort within the best-effort pass —
    # the markdown is the canonical artifact, but the dashboard endpoint
    # serves the HTML, so a render failure must not lose the markdown.
    try:
        html_path = md_path.with_suffix(".html")
        html_path.write_text(
            render_report_html(epoch_id, report_md, data=data),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001 — HTML is non-critical
        log.debug("epoch report: analysis.html render skipped (%s)", exc)

    return md_path


__all__ = [
    "generate_epoch_report",
    "assemble_report_markdown",
    "markdown_to_html",
    "render_report_html",
    "render_report_html_fragment",
    "restamp_masthead",
    "restamp_persisted_report",
    "regenerate_epoch_report_deterministic",
    "parse_prose_from_markdown",
]
