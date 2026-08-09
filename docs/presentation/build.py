#!/usr/bin/env python3
"""Rebuild the deck's derived artifacts from ``slides/slide-NN.svg``.

The SVGs are the only source; ``index.html`` (the twelve inlined into the
viewer shell), ``zicato-deck.pdf`` and ``contact-sheet.png`` all come from
here. They embed their own JetBrains Mono and FreeMono as base64
``@font-face``, so rendering never depends on the host's installed fonts.

Needs headless ``google-chrome``/``chromium`` on PATH, and ``pypdf``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from pypdf import PdfWriter

HERE = Path(__file__).resolve().parent
SLIDES = sorted(HERE.glob("slides/slide-*.svg"))

# The contact sheet's geometry, measured off the committed PNG: a 4-wide grid of
# half-size slides on a ground one shade under their own #0E1116.
COLS, CELL_W, CELL_H, GUTTER, BG = 4, 960, 540, 24, "#08090B"

STYLE_RE = re.compile(r"<style type=\"text/css\"><!\[CDATA\[.*?\]\]></style>", re.S)
SVG_RE = re.compile(r"<svg .*?</svg>", re.S)


def render(*args: str) -> None:
    """Drive headless Chrome — the same engine index.html is viewed in."""
    for name in ("google-chrome", "chromium", "chromium-browser"):
        if browser := shutil.which(name):
            break
    else:
        sys.exit("no chrome/chromium on PATH")
    subprocess.run(
        [
            browser,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            "--hide-scrollbars",
            "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=10000",
            *args,
        ],
        check=True,
        capture_output=True,
    )


def bodies() -> list[str]:
    """Each slide with its ``@font-face`` block stripped, ready to inline."""
    return [STYLE_RE.sub("", p.read_text().strip()) for p in SLIDES]


def page(style: str, body: str) -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8"><style>'
        f"{style}</style></head><body>{body}</body></html>"
    )


def sync() -> None:
    """Re-inline the slides into index.html, leaving the viewer shell alone."""
    slides = iter(bodies())
    html, n = SVG_RE.subn(lambda _: next(slides), (HERE / "index.html").read_text())
    if n != len(SLIDES):
        sys.exit(f"index.html holds {n} inlined slides, expected {len(SLIDES)}")
    (HERE / "index.html").write_text(html)


def pdf(tmp: Path) -> None:
    style = "@page{size:1280px 720px;margin:0}html,body{margin:0;padding:0}svg{display:block}"
    writer = PdfWriter()
    for i, body in enumerate(bodies(), 1):
        (tmp / f"{i:02d}.html").write_text(page(style, body))
        render("--no-pdf-header-footer", f"--print-to-pdf={tmp}/{i:02d}.pdf", f"{tmp}/{i:02d}.html")
        writer.append(f"{tmp}/{i:02d}.pdf")
    # Chrome stamps its name, version and the build hour into every PDF it
    # writes; a committed artifact should carry none of that.
    writer.add_metadata({"/Producer": "pypdf"})
    with (HERE / "zicato-deck.pdf").open("wb") as fh:
        writer.write(fh)


def sheet(tmp: Path) -> None:
    rows = -(-len(SLIDES) // COLS)
    w = GUTTER * (COLS + 1) + COLS * CELL_W
    h = GUTTER * (rows + 1) + rows * CELL_H
    style = (
        f"html,body{{margin:0;background:{BG}}}"
        f"#g{{display:grid;grid-template-columns:repeat({COLS},{CELL_W}px);"
        f"gap:{GUTTER}px;padding:{GUTTER}px;width:max-content}}"
        f"#g svg{{display:block;width:{CELL_W}px;height:{CELL_H}px}}"
    )
    (tmp / "sheet.html").write_text(page(style, '<div id="g">' + "".join(bodies()) + "</div>"))
    render(f"--window-size={w},{h}", f"--screenshot={HERE}/contact-sheet.png", f"{tmp}/sheet.html")


if __name__ == "__main__":
    if not SLIDES:
        sys.exit("no slides/slide-*.svg found")
    with tempfile.TemporaryDirectory() as tmpdir:
        sync()
        pdf(Path(tmpdir))
        sheet(Path(tmpdir))
    print(f"rebuilt index.html, zicato-deck.pdf, contact-sheet.png from {len(SLIDES)} slides")
