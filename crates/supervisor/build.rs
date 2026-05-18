//! Build script: capture a short commit SHA so the running binary can
//! report a precise build identifier in `/api/health`.
//!
//! Best-effort: when `git` is unavailable (e.g. a source-tarball build)
//! the SHA is simply absent and the health endpoint falls back to the
//! crate version alone. The build never fails on its account.

use std::path::Path;
use std::process::Command;

fn git(args: &[&str]) -> Option<String> {
    Command::new("git")
        .args(args)
        .output()
        .ok()
        .filter(|o| o.status.success())
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
}

fn main() {
    let sha = git(&["rev-parse", "--short=12", "HEAD"]);

    match sha {
        Some(s) => println!("cargo:rustc-env=ZICATO_GIT_SHA={s}"),
        // Emit an empty value so `option_env!` is still consistent;
        // `routes.rs` treats an empty SHA as "unknown".
        None => println!("cargo:rustc-env=ZICATO_GIT_SHA="),
    }

    // Re-run if HEAD moves so the SHA stays current. The crate lives
    // at crates/supervisor under a Cargo workspace, so a fixed
    // ../.git relative path is wrong; ask git for the real git
    // directory instead. `--git-common-dir` resolves to the shared
    // .git even from a linked worktree (where .git is a gitlink file).
    if let Some(git_dir) = git(&["rev-parse", "--git-common-dir"]) {
        let git_dir = Path::new(&git_dir);
        println!("cargo:rerun-if-changed={}", git_dir.join("HEAD").display());
        println!("cargo:rerun-if-changed={}", git_dir.join("refs").display());
    }
}
