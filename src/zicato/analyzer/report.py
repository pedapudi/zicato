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
* 1. Introduction
* 2. Methodology
* 3. Approach & Implementation
* 4. Experimental Results
* 5. Analysis — What Worked and What Didn't
* 6. Threats to Validity & Limitations
* 7. Conclusion & Next Directions

Hybrid generation, for correctness:

* The deterministic, data-bearing sections (Methodology, every
  Experimental Results table, the score trajectory, Threats) are
  templated directly from the structured workspace data by
  :mod:`zicato.analyzer.report_sections` — exact by construction.
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
from zicato.analyzer.report_prompts import (
    PROSE_BLOCK_LABELS,
    REPORT_SYSTEM_PROMPT,
    parse_prose_blocks,
    render_report_user_prompt,
)
from zicato.analyzer.report_sections import (
    render_approach_section,
    render_methodology_section,
    render_results_section,
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

    parts: list[str] = []
    parts.append(render_title_block(data))
    parts.append("")
    parts.append("## Abstract")
    parts.append("")
    parts.append(abstract)
    parts.append("")
    parts.append("## 1. Introduction")
    parts.append("")
    parts.append(introduction)
    parts.append("")
    parts.append(deterministic_sections.strip())
    parts.append("")
    parts.append("## 5. Analysis — What Worked and What Didn't")
    parts.append("")
    parts.append(analysis)
    parts.append("")
    parts.append(render_threats_section(data))
    parts.append("")
    parts.append("## 7. Conclusion & Next Directions")
    parts.append("")
    parts.append(conclusion)
    parts.append("")
    parts.append("---")
    parts.append("")
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts.append(
        f"_Regenerated by zicato at {now} from `board.jsonl`, `scoring.json`, "
        f"`mutations.json`, `generations/*/experiment.json`, and `journal.md` "
        f"under epoch `{data.epoch_id}`. The data-bearing sections are "
        f"templated exactly from those artifacts; the prose sections are "
        f"LLM-authored._"
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
        )
    )


# ---------------------------------------------------------------------------
# Minimal, dependency-free Markdown -> HTML
# ---------------------------------------------------------------------------


def _inline_md_to_html(text: str) -> str:
    """Convert inline markdown (bold, code, arrows) within one line to HTML.

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


def _render_md_table(rows: list[str]) -> str:
    """Render a contiguous block of markdown table lines as an HTML table."""
    if len(rows) < 2:
        return ""

    def _cells(line: str) -> list[str]:
        stripped = line.strip()
        if stripped.startswith("|"):
            stripped = stripped[1:]
        if stripped.endswith("|"):
            stripped = stripped[:-1]
        return [c.strip() for c in stripped.split("|")]

    header = _cells(rows[0])
    body = rows[2:]
    parts: list[str] = ["<table><thead><tr>"]
    parts.extend(f"<th>{_inline_md_to_html(c)}</th>" for c in header)
    parts.append("</tr></thead><tbody>")
    for line in body:
        parts.append("<tr>")
        parts.extend(f"<td>{_inline_md_to_html(c)}</td>" for c in _cells(line))
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def markdown_to_html(md: str) -> str:
    """Convert the report markdown to an HTML fragment (no dependencies).

    Supports the subset the report uses: ATX headings (``#``..``####``),
    bullet lists, fenced code blocks, pipe tables, horizontal rules,
    bold + code inline spans, and paragraphs. Anything unrecognised is
    rendered as an escaped paragraph. The output is a fragment; the
    document shell is supplied by :func:`render_report_html`.
    """
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)
    in_list = False

    def _close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    while i < n:
        line = lines[i]
        stripped = line.strip()

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
            out.append(_render_md_table(table_rows))
            continue

        if not stripped:
            _close_list()
            i += 1
            continue

        # Horizontal rule.
        if stripped in ("---", "***", "___"):
            _close_list()
            out.append("<hr/>")
            i += 1
            continue

        # ATX heading.
        if stripped.startswith("#"):
            _close_list()
            level = len(stripped) - len(stripped.lstrip("#"))
            level = min(max(level, 1), 6)
            content = stripped[level:].strip()
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
                or "|" in nxt
            ):
                break
            para.append(nxt)
            i += 1
        out.append("<p>" + "<br/>".join(_inline_md_to_html(p) for p in para) + "</p>")

    _close_list()
    return "\n".join(out)


_HTML_CSS = """
:root {
  --bg: #ffffff; --text: #24292f; --muted: #57606a; --border: #d0d7de;
  --code-bg: #f6f8fa; --accent: #0969da;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #1a1a1a; --text: #e0e0e0; --muted: #9d9d9d; --border: #444;
    --code-bg: #2d2d2d; --accent: #58a6ff;
  }
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0; background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  line-height: 1.6; font-size: 16px;
}
main { max-width: 880px; margin: 0 auto; padding: 40px 32px 80px 32px; }
h1 { font-size: 28px; margin: 0 0 8px 0; }
h2 {
  font-size: 21px; margin: 36px 0 12px 0; padding-bottom: 6px;
  border-bottom: 1px solid var(--border);
}
h3 { font-size: 17px; margin: 24px 0 8px 0; }
h4 { font-size: 15px; margin: 18px 0 6px 0; color: var(--muted); }
p { margin: 10px 0; }
ul { margin: 10px 0; padding-left: 24px; }
li { margin: 4px 0; }
code {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 0.9em; background: var(--code-bg); padding: 1px 5px;
  border-radius: 4px;
}
pre {
  background: var(--code-bg); border: 1px solid var(--border);
  border-radius: 6px; padding: 12px 14px; overflow-x: auto; font-size: 13px;
}
pre code { background: none; padding: 0; }
table {
  border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 14px;
}
th, td {
  border: 1px solid var(--border); padding: 6px 10px; text-align: left;
}
th { background: var(--code-bg); }
hr { border: none; border-top: 1px solid var(--border); margin: 28px 0; }
""".strip()


def render_report_html(epoch_id: str, report_md: str) -> str:
    """Render the report markdown into a self-contained HTML document.

    Zero external resources — inline CSS only, dark-mode aware — so the
    file renders identically over ``file://`` and through the dashboard.
    """
    body = markdown_to_html(report_md)
    title = f"zicato — epoch {_html.escape(epoch_id)} analysis report"
    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head>'
        '<meta charset="utf-8" />'
        '<meta name="viewport" content="width=device-width, initial-scale=1" />'
        f"<title>{title}</title>"
        f"<style>{_HTML_CSS}</style>"
        "</head><body><main>"
        f"{body}"
        "</main></body></html>"
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


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
        html_path.write_text(render_report_html(epoch_id, report_md), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 — HTML is non-critical
        log.debug("epoch report: analysis.html render skipped (%s)", exc)

    return md_path


__all__ = [
    "generate_epoch_report",
    "assemble_report_markdown",
    "markdown_to_html",
    "render_report_html",
]
