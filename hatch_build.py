"""Bundle the supervisor Cargo built for these inputs into each wheel.

An executable cache is keyed by build inputs and checked against its digest.
Cargo validates dependency artifacts when the executable cache cannot be used.
Source and editable installs require Rust; installed wheels include the binary.
A failed build cannot publish a wheel containing a missing or stale supervisor.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
import tomllib
from pathlib import Path


def _require_native_target(config_roots: list[Path], compiler: str) -> None:
    targets = []
    # Cargo merges ancestors before the nearest configuration; the legacy
    # config filename takes precedence when both filenames are present.
    for directory in reversed(config_roots):
        path = directory / "config"
        if not path.is_file():
            path = directory / "config.toml"
        if path.is_file():
            target = tomllib.loads(path.read_text()).get("build", {}).get("target")
            if target is not None:
                targets = targets + target if isinstance(target, list) else [target]
    if "CARGO_BUILD_TARGET" in os.environ:
        targets = [os.environ["CARGO_BUILD_TARGET"]]
    host = next((line[6:] for line in compiler.splitlines() if line.startswith("host: ")), None)
    if any(target != host for target in targets):
        raise RuntimeError(
            "Cross-compilation is unsupported for supervisor wheels: "
            f"configured targets {targets!r} must match compiler host {host!r}"
        )


def build_identity(root: Path) -> str:
    """Hash source, embedded revision, toolchain, host, release profile and flags."""
    files = {
        path
        for pattern in (
            "Cargo.*",
            "rust-toolchain*",
            "crates/**/*",
            "hatch_build.py",
            "src/zicato/epoch/source_scope.json",
        )
        for path in root.glob(pattern)
        if path.is_file()
    }
    config_roots = [parent / ".cargo" for parent in [root, *root.parents]]
    config_roots.append(Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo")))
    compiler = subprocess.check_output(["rustc", "-Vv"], text=True)
    _require_native_target(config_roots, compiler)
    for parent in config_roots:
        for relative in ("config", "config.toml"):
            if (parent / relative).is_file():
                files.add(parent / relative)
    sources = {
        str(path.relative_to(root) if path.is_relative_to(root) else path): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(files)
    }
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True
        )
        revision = result.stdout.strip() if result.returncode == 0 else None
    except FileNotFoundError:
        revision = None
    inputs = {
        "sources": sources,
        "revision": revision,
        "rustc": compiler,
        "cargo": subprocess.check_output(["cargo", "--version"], text=True),
        "profile": "release",
        "package": "zicato-supervisor",
        "platform": platform.platform(),
        "libc": platform.libc_ver(),
        "runner_image": {key: os.environ.get(key) for key in ("ImageOS", "ImageVersion")},
        "environment": {
            key: value
            for key, value in os.environ.items()
            if key.startswith(("CARGO", "RUST", "CC", "CFLAGS", "CXX", "CMAKE"))
        },
    }
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


if __name__ == "__main__":
    print(build_identity(Path.cwd()))
    raise SystemExit(0)


from hatchling.builders.hooks.plugin.interface import BuildHookInterface  # noqa: E402

# Path of the bundled binary inside the package, relative to the repo
# root. _resolve_supervisor_binary looks here first via zicato.__file__.
_BUNDLED_REL = Path("src") / "zicato" / "_bin" / "zicato-supervisor"


class SupervisorBinaryBuildHook(BuildHookInterface):
    """Compile crates/supervisor and stage its binary into the wheel."""

    PLUGIN_NAME = "zicato-supervisor"

    def initialize(self, version: str, build_data: dict) -> None:
        # Only the wheel target ships the binary; an sdist carries the
        # crate source instead and rebuilds at wheel-build time.
        if self.target_name != "wheel":
            return

        root = Path(self.root)
        crate_dir = root / "crates" / "supervisor"
        if not (crate_dir / "Cargo.toml").is_file():
            raise RuntimeError("Building the supervisor requires crates/supervisor/Cargo.toml")

        cargo = shutil.which("cargo")
        if cargo is None:
            raise RuntimeError("Building the supervisor requires cargo on PATH")

        started = time.monotonic()
        identity = build_identity(root)
        cache = root / ".supervisor-cache"
        try:
            record = json.loads((cache / "manifest.json").read_text())
            payload = (cache / "zicato-supervisor").read_bytes()
            valid = record == {"inputs": identity, "sha256": hashlib.sha256(payload).hexdigest()}
        except (OSError, ValueError):
            valid = False
        if valid:
            state = "reused verified"
        else:
            payload, state = self._compile(root, cargo)
        if build_identity(root) != identity:
            raise RuntimeError("Supervisor build inputs changed during preparation")
        if not valid:
            cache.mkdir(exist_ok=True)
            (cache / "zicato-supervisor").write_bytes(payload)
            (cache / "manifest.json").write_text(
                json.dumps(
                    {
                        "inputs": identity,
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
                + "\n"
            )
        self.app.display_info(
            f"zicato-supervisor: {state} release executable in {time.monotonic() - started:.3f}s"
        )

        dest = root / _BUNDLED_REL
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        dest.chmod(0o755)

        artifact = str(dest)
        build_data["pure_python"] = False
        build_data["infer_tag"] = True
        build_data.setdefault("force_include", {})[artifact] = "zicato/_bin/zicato-supervisor"
        build_data.setdefault("artifacts", []).append(artifact)

    def _compile(self, root: Path, cargo: str) -> tuple[bytes, str]:
        result = subprocess.run(  # noqa: S603 — resolved Cargo executable
            [
                cargo,
                "build",
                "--locked",
                "--release",
                "-p",
                "zicato-supervisor",
                "--message-format=json-render-diagnostics",
            ],
            cwd=root,
            check=True,
            stdout=subprocess.PIPE,
            text=True,
        )
        artifacts = [
            json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
        ]
        binary = next(
            (
                item
                for item in artifacts
                if item.get("reason") == "compiler-artifact"
                and item["target"]["name"] == "zicato-supervisor"
                and item.get("executable")
            ),
            None,
        )
        if binary is None or not Path(binary["executable"]).is_file():
            raise RuntimeError("Cargo did not report an existing supervisor executable")
        built = Path(binary["executable"])
        state = "reused" if binary["fresh"] else "compiled"
        return built.read_bytes(), state
