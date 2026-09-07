"""A failed native build cannot produce a successful or stale wheel."""

from __future__ import annotations

import importlib.util
import json
import platform
import runpy
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def hook(tmp_path, monkeypatch):
    # The build backend supplies this base; behavior under test is the hook.
    # The installed-wheel CI check exercises the actual backend interface.
    for name in (
        "hatchling",
        "hatchling.builders",
        "hatchling.builders.hooks",
        "hatchling.builders.hooks.plugin",
        "hatchling.builders.hooks.plugin.interface",
    ):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    sys.modules["hatchling.builders.hooks.plugin.interface"].BuildHookInterface = object
    spec = importlib.util.spec_from_file_location("supervisor_hook", ROOT / "hatch_build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "build_identity", lambda root: "same-build-inputs")
    instance = module.SupervisorBinaryBuildHook()
    instance.root = str(tmp_path)
    instance.target_name = "wheel"
    instance.app = SimpleNamespace(
        display_info=lambda message: None, display_warning=lambda _: None
    )
    crate = tmp_path / "crates" / "supervisor"
    crate.mkdir(parents=True)
    (crate / "Cargo.toml").write_text('[package]\nname = "zicato-supervisor"\n')
    monkeypatch.setattr(module.shutil, "which", lambda name: "/tools/cargo")
    return module, instance


def test_missing_compiler_refuses_wheel(hook, monkeypatch):
    module, instance = hook
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="requires cargo"):
        instance.initialize("standard", {})


def test_missing_source_refuses_wheel(hook):
    _, instance = hook
    instance.root = str(Path(instance.root) / "absent")
    with pytest.raises(RuntimeError, match="Cargo.toml"):
        instance.initialize("standard", {})


def test_failed_build_cannot_include_stale_binary(hook, monkeypatch):
    module, instance = hook
    dest = Path(instance.root) / module._BUNDLED_REL
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"stale executable")

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(101, args[0])

    monkeypatch.setattr(module.subprocess, "run", fail)
    data = {}
    with pytest.raises(subprocess.CalledProcessError):
        instance.initialize("standard", data)
    assert not data
    assert dest.read_bytes() == b"stale executable"


@pytest.mark.parametrize("reported", [False, True], ids=["no-artifact", "missing-executable"])
def test_missing_executable_refuses_wheel(hook, monkeypatch, reported):
    module, instance = hook
    message = {
        "reason": "compiler-artifact",
        "target": {"name": "zicato-supervisor"},
        "executable": str(Path(instance.root) / "absent"),
        "fresh": True,
    }
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout=json.dumps(message) if reported else ""),
    )
    with pytest.raises(RuntimeError, match="executable"):
        instance.initialize("standard", {})


def test_reported_executable_is_bundled_with_native_metadata(hook, monkeypatch):
    module, instance = hook
    binary = Path(instance.root) / "shared-target" / "alternate-host" / "release" / "supervisor"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"verified executable")
    command = []

    def build(args, **kwargs):
        command.extend(args)
        assert kwargs["check"]
        return SimpleNamespace(
            stdout=json.dumps(
                {
                    "reason": "compiler-artifact",
                    "target": {"name": "zicato-supervisor"},
                    "executable": str(binary),
                    "fresh": True,
                }
            )
        )

    monkeypatch.setattr(module.subprocess, "run", build)
    data = {}
    instance.initialize("standard", data)
    assert "--locked" in command
    dest = Path(instance.root) / module._BUNDLED_REL
    assert dest.read_bytes() == binary.read_bytes()
    assert dest.stat().st_mode & 0o111 == 0o111
    assert data["pure_python"] is False
    assert data["infer_tag"] is True
    assert data["force_include"][str(dest)] == "zicato/_bin/zicato-supervisor"


def test_verified_cache_avoids_cargo_and_stages_the_checked_bytes(hook, monkeypatch):
    _, instance = hook
    monkeypatch.setattr(
        instance, "_compile", lambda root, cargo: (b"verified executable", "compiled")
    )
    instance.initialize("standard", {})

    def fail_if_compiled(*args):
        pytest.fail("valid executable cache invoked Cargo")

    monkeypatch.setattr(instance, "_compile", fail_if_compiled)
    instance.initialize("standard", {})
    assert (
        Path(instance.root) / "src/zicato/_bin/zicato-supervisor"
    ).read_bytes() == b"verified executable"


@pytest.mark.parametrize("damage", ["inputs", "binary", "manifest", "missing"])
def test_unusable_cache_rebuilds_and_cannot_hide_a_failed_build(hook, monkeypatch, damage):
    _, instance = hook
    monkeypatch.setattr(
        instance, "_compile", lambda root, cargo: (b"verified executable", "compiled")
    )
    instance.initialize("standard", {})
    cache = Path(instance.root) / ".supervisor-cache"
    if damage == "inputs":
        manifest = json.loads((cache / "manifest.json").read_text())
        manifest["inputs"] = "another-revision"
        (cache / "manifest.json").write_text(json.dumps(manifest))
    elif damage == "manifest":
        (cache / "manifest.json").write_text("{")
    elif damage == "missing":
        (cache / "zicato-supervisor").rename(cache / "saved-binary")
    else:
        (cache / "zicato-supervisor").write_bytes(b"damaged executable")

    def fail(*args):
        raise subprocess.CalledProcessError(101, ["cargo", "build"])

    monkeypatch.setattr(instance, "_compile", fail)
    with pytest.raises(subprocess.CalledProcessError):
        instance.initialize("standard", {})
    monkeypatch.setattr(
        instance, "_compile", lambda root, cargo: (b"rebuilt executable", "compiled")
    )
    instance.initialize("standard", {})
    assert (cache / "zicato-supervisor").read_bytes() == b"rebuilt executable"


@pytest.mark.parametrize(
    "changed",
    [
        "crates/supervisor/src/main.rs",
        "crates/supervisor/static/index.html",
        "Cargo.lock",
        "Cargo.toml",
        ".cargo/config.toml",
        "hatch_build.py",
        "toolchain",
        "target",
        "revision",
    ],
)
def test_each_build_input_invalidates_artifact_identity(hook, monkeypatch, changed):
    _, instance = hook
    identity = runpy.run_path(str(ROOT / "hatch_build.py"))["build_identity"]
    root = Path(instance.root)
    versions = {
        "rustc": "rustc 1.0\nhost: x86_64-unknown-linux-gnu",
        "cargo": "package manager identity",
    }
    revision = ["revision-one"]
    monkeypatch.setattr(platform, "platform", lambda: "test platform")
    monkeypatch.setattr(subprocess, "check_output", lambda args, **kwargs: versions[args[0]])
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=revision[0], returncode=0)
    )
    before = identity(root)
    if changed == "toolchain":
        versions["rustc"] = "another compiler"
    elif changed == "target":
        monkeypatch.setenv("CARGO_BUILD_TARGET", "x86_64-unknown-linux-gnu")
    elif changed == "revision":
        revision[0] = "revision-two"
    else:
        path = root / changed
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# changed build input\n")
    assert identity(root) != before


@pytest.mark.parametrize("target", ["aarch64-unknown-linux-gnu", "x86_64-unknown-linux-musl"])
@pytest.mark.parametrize("source", ["environment", "configuration"])
def test_foreign_cargo_target_is_rejected_before_wheel_preparation(
    hook, monkeypatch, target, source
):
    module, instance = hook
    identity = runpy.run_path(str(ROOT / "hatch_build.py"))["build_identity"]
    monkeypatch.setattr(module, "build_identity", identity)
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda *args, **kwargs: "rustc 1.0\nhost: x86_64-unknown-linux-gnu",
    )
    monkeypatch.delenv("CARGO_BUILD_TARGET", raising=False)
    if source == "environment":
        monkeypatch.setenv("CARGO_BUILD_TARGET", target)
    else:
        path = Path(instance.root) / ".cargo/config.toml"
        path.parent.mkdir()
        path.write_text(f'[build]\ntarget = "{target}"\n')
    data = {}
    with pytest.raises(RuntimeError, match="Cross-compilation is unsupported"):
        instance.initialize("standard", data)
    assert not data
    assert not (Path(instance.root) / module._BUNDLED_REL).exists()


def test_workflows_restore_caches_before_sync_and_check_installed_wheel():
    import yaml

    action = yaml.safe_load((ROOT / ".github/actions/supervisor-cache/action.yml").read_text())
    steps = action["runs"]["steps"]
    assert [step.get("uses") for step in steps if "uses" in step] == [
        "dtolnay/rust-toolchain@stable",
        "Swatinem/rust-cache@v2",
        "actions/cache@v4",
    ]
    assert "identity.outputs.key" in steps[-1]["with"]["key"]
    for filename in ("ci.yml", "slow-tier.yml"):
        workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
        for job in workflow["jobs"].values():
            steps = job["steps"]
            syncs = [i for i, step in enumerate(steps) if "uv sync " in step.get("run", "")]
            for index in syncs:
                assert steps[index - 1]["uses"] == "./.github/actions/supervisor-cache"
                assert "--all-extras" in steps[index]["run"]
                assert "--reinstall-package zicato" in steps[index]["run"]
        if filename == "ci.yml":
            parity = workflow["jobs"]["parity"]
            assert parity["env"]["UV_NO_SYNC"] == "1"
            assert any("--no-editable" in step.get("run", "") for step in parity["steps"])
            assert any(
                command in step.get("run", "")
                for step in parity["steps"]
                for command in (
                    "check_installed_supervisor.py",
                    "tools/verify.py --only installed-supervisor --installed-wheel",
                )
            )


def test_build_script_watches_the_linked_worktrees_revision(tmp_path):
    repository = tmp_path / "repository"
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test Author",
            "-c",
            "user.email=test@example.test",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "--allow-empty",
            "-qm",
            "Seed fixture",
        ],
        cwd=repository,
        check=True,
    )
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "worktree", "add", "--detach", "-q", str(linked)], cwd=repository, check=True
    )
    executable = tmp_path / "build-script"
    subprocess.run(
        [
            "rustc",
            "--edition=2021",
            str(ROOT / "crates/supervisor/build.rs"),
            "-o",
            str(executable),
        ],
        check=True,
    )
    output = subprocess.check_output([str(executable)], cwd=linked, text=True)
    watched = [
        Path(line.split("=", 1)[1])
        for line in output.splitlines()
        if line.startswith("cargo:rerun-if-changed=")
    ]
    head = subprocess.check_output(
        ["git", "rev-parse", "--git-path", "HEAD"], cwd=linked, text=True
    ).strip()
    assert Path(head) in watched
    assert all((linked / path).exists() for path in watched)
