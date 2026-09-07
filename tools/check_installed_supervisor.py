"""Check supervisor resolution from an installed package, outside its checkout.

Run with the wheel environment's Python and ``-I``. Dependencies must already
be installed in that environment. An explicit expected binary binds the wheel
to the executable reported by the successful build.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def check(expected: Path) -> Path:
    import zicato  # noqa: PLC0415
    from zicato.cli.commands.evolve import _resolve_supervisor_binary  # noqa: PLC0415
    from zicato.config import IntegrationConfig  # noqa: PLC0415

    package = Path(zicato.__file__).resolve().parent
    if not package.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError(f"Expected an installed wheel, imported {package}")
    metadata = importlib.metadata.distribution("zicato").read_text("WHEEL") or ""
    if "Root-Is-Purelib: false" not in metadata or "-any\n" in metadata:
        raise RuntimeError("Supervisor wheel must declare its native platform")
    bundled = package / "_bin" / "zicato-supervisor"
    if _resolve_supervisor_binary(IntegrationConfig()) != bundled:
        raise RuntimeError("Installed wheel did not resolve its bundled supervisor")
    if not os.access(bundled, os.X_OK):
        raise RuntimeError("Bundled supervisor is not executable")
    digest = hashlib.sha256(bundled.read_bytes()).digest()
    if digest != hashlib.sha256(expected.read_bytes()).digest():
        raise RuntimeError("Bundled supervisor differs from the verified build")
    result = subprocess.run([str(bundled), "--version"], check=True, capture_output=True, text=True)
    if not result.stdout.startswith("zicato-supervisor "):
        raise RuntimeError(f"Unexpected supervisor response: {result.stdout!r}")
    print(f"Installed supervisor verified: {digest.hex()} ({result.stdout.strip()})")
    return bundled


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--build", action="store_true", help="Build and install in an isolated environment"
    )
    parser.add_argument("expected", nargs="?", type=Path)
    args = parser.parse_args()
    if args.build:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="supervisor-wheel-") as directory:
            environment = Path(directory) / "environment"
            subprocess.run(
                [
                    "uv",
                    "sync",
                    "--all-extras",
                    "--frozen",
                    "--no-editable",
                    "--reinstall-package",
                    "zicato",
                ],
                cwd=root,
                env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(environment)},
                check=True,
            )
            subprocess.run(
                [
                    str(environment / "bin/python"),
                    "-I",
                    str(Path(__file__).resolve()),
                    str(root / ".supervisor-cache/zicato-supervisor"),
                ],
                cwd=directory,
                check=True,
            )
    elif args.expected is None:
        parser.error("provide an expected executable or --build")
    else:
        check(args.expected)
