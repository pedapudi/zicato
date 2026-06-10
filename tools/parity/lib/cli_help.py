"""Parity CLI-HELP gate helper.

Captures ``--help`` for the root ``zicato`` group AND every (sub)command,
concatenates them into one stable document, and either writes the golden
(``--update``) or asserts byte-identity against it.

The CLI surface — its command set, options, defaults, and help prose — is
part of the program's observable behavior. A behavior-preserving refactor
must not move it. Help is rendered in-process via Click's help formatter
(no subprocess), pinned to an 80-col wrap so the output is terminal-width
independent.

Run directly:  ``uv run python tools/parity/lib/cli_help.py [--update]``
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

_GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "cli_help.txt"


def _walk(cmd: click.Command, prefix: list[str]) -> list[list[str]]:
    """Return every command path (root first, then sorted descendants)."""
    paths: list[list[str]] = [prefix]
    sub = getattr(cmd, "commands", None)
    if sub:
        for name in sorted(sub):
            paths.extend(_walk(sub[name], [*prefix, name]))
    return paths


def _resolve(root: click.Group, path: list[str]) -> click.Command:
    cmd: click.Command = root
    for name in path:
        cmd = cmd.commands[name]  # type: ignore[attr-defined]
    return cmd


def render_all_help() -> str:
    """Render help for ``zicato`` and every subcommand into one document."""
    from zicato.cli.discovery import build_cli_root

    root = build_cli_root()
    chunks: list[str] = []
    for path in _walk(root, []):
        cmd = _resolve(root, path)
        info_name = "zicato" if not path else f"zicato {' '.join(path)}"
        ctx = click.Context(cmd, info_name=info_name, terminal_width=80, max_content_width=80)
        chunks.append(f"===== {info_name} --help =====\n{cmd.get_help(ctx)}\n")
    # Each chunk already ends with "\n"; the join leaves exactly one trailing
    # newline. Do NOT add another — the end-of-file-fixer pre-commit hook
    # strips a double trailing newline, which would desync the golden from
    # this renderer and break the CLI-HELP gate.
    return "\n".join(chunks)


def main(argv: list[str]) -> int:
    update = "--update" in argv
    rendered = render_all_help()
    if update:
        _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN.write_text(rendered, encoding="utf-8")
        print(f"wrote {_GOLDEN}")
        return 0
    if not _GOLDEN.exists():
        print(f"FAIL: golden missing at {_GOLDEN}", file=sys.stderr)
        return 1
    expected = _GOLDEN.read_text(encoding="utf-8")
    if rendered != expected:
        print("FAIL: CLI-HELP drift vs golden", file=sys.stderr)
        import difflib

        diff = difflib.unified_diff(
            expected.splitlines(),
            rendered.splitlines(),
            fromfile="golden",
            tofile="actual",
            lineterm="",
        )
        sys.stderr.write("\n".join(list(diff)[:60]) + "\n")
        return 1
    print("OK: CLI-HELP matches golden")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
