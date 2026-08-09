#!/usr/bin/env python3
"""Regenerate the deck's derived artifacts from ``slides/slide-NN.svg``.

The SVGs are the only source. Everything else here is derived:

    index.html         the twelve SVGs inlined into the viewer shell
    zicato-deck.pdf    a twelve-page vector export
    contact-sheet.png  all twelve on one 4x3 sheet

Run ``python3 docs/presentation/build.py`` to rebuild all three; pass one or
more of ``sync``/``pdf``/``sheet`` to rebuild a subset, and ``--check`` to
rebuild into a temporary directory and report whether the checked-in files
are already up to date.

The SVGs embed their own JetBrains Mono (and FreeMono for the dotless-i
wordmark) as base64 ``@font-face`` rules, so rendering does not depend on
what fonts this machine happens to have installed.
"""

from __future__ import annotations

import argparse
import filecmp
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SLIDES = sorted(HERE.glob("slides/slide-*.svg"))

SLIDE_W, SLIDE_H = 1280, 720

# The contact sheet's gutter ground: a shade under the slides' own #0E1116, so
# the twelve cells read as tiles on a page rather than one continuous field.
SHEET_BG = "#08090B"
SHEET_COLS = 4
SHEET_CELL_W, SHEET_CELL_H = 960, 540
SHEET_GAP = SHEET_PAD = 24

# Chrome writes its own name into the PDF; pypdf rewrites the trailer on merge
# and this is the only producer string the committed file carries.
PDF_PRODUCER = "pypdf"

STYLE_RE = re.compile(r"<style type=\"text/css\"><!\[CDATA\[.*?\]\]></style>", re.S)
SVG_RE = re.compile(r"<svg .*?</svg>", re.S)


def chrome() -> str:
    for name in ("google-chrome", "chromium", "chromium-browser", "google-chrome-stable"):
        path = shutil.which(name)
        if path:
            return path
    sys.exit("no chrome/chromium on PATH — needed to render the SVGs")


def run_chrome(*args: str) -> None:
    subprocess.run(
        [
            chrome(),
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


def slide_bodies() -> list[str]:
    """Each slide with its ``@font-face`` block stripped, ready to inline."""
    return [STYLE_RE.sub("", p.read_text().strip()) for p in SLIDES]


def sync(out: Path) -> None:
    """Re-inline the slides into index.html, leaving the viewer shell alone."""
    html = (HERE / "index.html").read_text()
    bodies = iter(slide_bodies())
    inlined, n = SVG_RE.subn(lambda _: next(bodies), html)
    if n != len(SLIDES):
        sys.exit(f"index.html holds {n} inlined slides, expected {len(SLIDES)}")
    (out / "index.html").write_text(inlined)


def page_html(body: str, style: str) -> str:
    return (
        '<!doctype html><html><head><meta charset="utf-8"><style>'
        f"{style}</style></head><body>{body}</body></html>"
    )


def pdf(out: Path) -> None:
    from pypdf import PdfWriter

    style = (
        f"@page{{size:{SLIDE_W}px {SLIDE_H}px;margin:0}}"
        "html,body{margin:0;padding:0}svg{display:block}"
    )
    writer = PdfWriter()
    with tempfile.TemporaryDirectory() as tmp:
        for i, (path, body) in enumerate(zip(SLIDES, slide_bodies(), strict=True), 1):
            page = Path(tmp) / f"{i:02d}.html"
            page.write_text(page_html(body, style))
            rendered = Path(tmp) / f"{i:02d}.pdf"
            run_chrome("--no-pdf-header-footer", f"--print-to-pdf={rendered}", str(page))
            writer.append(str(rendered))
            print(f"  rendered {path.name}")
        # Drop Chrome's Creator/Producer/dates; a committed artifact should not
        # carry the toolchain's name or the hour it was built.
        writer.add_metadata({"/Producer": PDF_PRODUCER})
        with (out / "zicato-deck.pdf").open("wb") as fh:
            writer.write(fh)


def sheet(out: Path) -> None:
    width = SHEET_PAD * 2 + SHEET_COLS * SHEET_CELL_W + (SHEET_COLS - 1) * SHEET_GAP
    rows = -(-len(SLIDES) // SHEET_COLS)
    height = SHEET_PAD * 2 + rows * SHEET_CELL_H + (rows - 1) * SHEET_GAP
    style = (
        f"html,body{{margin:0;padding:0;background:{SHEET_BG}}}"
        f"#g{{display:grid;grid-template-columns:repeat({SHEET_COLS},{SHEET_CELL_W}px);"
        f"gap:{SHEET_GAP}px;padding:{SHEET_PAD}px;width:max-content}}"
        f"#g svg{{display:block;width:{SHEET_CELL_W}px;height:{SHEET_CELL_H}px}}"
    )
    body = '<div id="g">' + "".join(slide_bodies()) + "</div>"
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "sheet.html"
        page.write_text(page_html(body, style))
        shot = Path(tmp) / "sheet.png"
        run_chrome(
            f"--window-size={width},{height}",
            f"--screenshot={shot}",
            str(page),
        )
        shutil.copyfile(shot, out / "contact-sheet.png")
    print(f"  contact sheet {width}x{height}")


TARGETS = {"sync": sync, "pdf": pdf, "sheet": sheet}
ARTIFACTS = {"sync": "index.html", "pdf": "zicato-deck.pdf", "sheet": "contact-sheet.png"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("targets", nargs="*", help=f"any of: {', '.join(TARGETS)} (default: all)")
    ap.add_argument(
        "--check",
        action="store_true",
        help="build into a temp dir and diff against the checked-in artifacts",
    )
    args = ap.parse_args()
    targets = args.targets or list(TARGETS)
    unknown = [t for t in targets if t not in TARGETS]
    if unknown:
        sys.exit(f"unknown target(s): {', '.join(unknown)} — choose from {', '.join(TARGETS)}")

    if not SLIDES:
        sys.exit("no slides/slide-*.svg found")

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) if args.check else HERE
        stale = []
        for name in targets:
            print(f"{name}:")
            TARGETS[name](out)
            artifact = ARTIFACTS[name]
            if args.check and not filecmp.cmp(out / artifact, HERE / artifact, shallow=False):
                stale.append(artifact)
        if args.check:
            # The PDF and PNG carry rasterizer-version noise, so a byte diff here
            # means "rebuild and look", not "the deck is broken".
            for artifact in stale:
                print(f"differs: {artifact}")
            print("up to date" if not stale else f"{len(stale)} artifact(s) differ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
