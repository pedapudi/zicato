"""Hatchling build hook: bundle the Rust supervisor binary into the wheel.

The ``zicato evolve`` command spawns a ``zicato-supervisor`` watchdog
process. For a development checkout the binary can be found by walking
to the Cargo workspace's ``target/release/`` directory, but that path
does not exist for an installed (non-checkout) package. To make the
supervisor available to every install, this hook compiles the crate
with ``cargo build --release`` and copies the binary to
``src/zicato/_bin/zicato-supervisor`` so it ships inside the wheel.

Best-effort by design: when ``cargo`` is unavailable, or the crate is
not present (e.g. an sdist that excluded ``crates/``), the hook logs a
warning and leaves the wheel without the bundled binary. The CLI's
``_resolve_supervisor_binary`` still falls back to the
``ZICATO_SUPERVISOR_BINARY`` env override, the system ``PATH``, and —
for checkouts — the workspace ``target/`` walk.

This hook never publishes anything; it only builds locally.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

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
            self.app.display_warning(
                "zicato-supervisor build hook: crates/supervisor not "
                "found; the wheel will not bundle the supervisor binary."
            )
            return

        cargo = shutil.which("cargo")
        if cargo is None:
            self.app.display_warning(
                "zicato-supervisor build hook: `cargo` not on PATH; the "
                "wheel will not bundle the supervisor binary."
            )
            return

        # Build the release binary from the workspace root so the
        # workspace target/ directory is used.
        self.app.display_info("zicato-supervisor build hook: cargo build --release")
        try:
            subprocess.run(  # noqa: S603 — cargo path resolved via shutil.which
                [cargo, "build", "--release", "-p", "zicato-supervisor"],
                cwd=root,
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            self.app.display_warning(
                f"zicato-supervisor build hook: cargo build failed ({exc}); "
                "the wheel will not bundle the supervisor binary."
            )
            return

        built = root / "target" / "release" / "zicato-supervisor"
        if not built.is_file():
            self.app.display_warning(
                f"zicato-supervisor build hook: expected binary at {built} "
                "after a successful build, but it is missing."
            )
            return

        dest = root / _BUNDLED_REL
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(built, dest)
        dest.chmod(0o755)
        self.app.display_info(f"zicato-supervisor build hook: bundled {dest}")

        # force-include guarantees the binary lands in the wheel even
        # though it is generated (not VCS-tracked). The wheel path is
        # zicato/_bin/zicato-supervisor (src/ prefix stripped).
        artifact = str(dest)
        build_data.setdefault("force_include", {})[artifact] = "zicato/_bin/zicato-supervisor"
        build_data.setdefault("artifacts", []).append(artifact)
