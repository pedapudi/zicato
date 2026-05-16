//! Build script: capture a short commit SHA so the running binary can
//! report a precise build identifier in `/api/health`.
//!
//! Best-effort: when `git` is unavailable (e.g. a source-tarball build)
//! the SHA is simply absent and the health endpoint falls back to the
//! crate version alone. The build never fails on its account.

use std::process::Command;

fn main() {
    let sha = Command::new("git")
        .args(["rev-parse", "--short=12", "HEAD"])
        .output()
        .ok()
        .filter(|o| o.status.success())
        .and_then(|o| String::from_utf8(o.stdout).ok())
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty());

    match sha {
        Some(s) => println!("cargo:rustc-env=ZICATO_GIT_SHA={s}"),
        // Emit an empty value so `option_env!` is still consistent;
        // `routes.rs` treats an empty SHA as "unknown".
        None => println!("cargo:rustc-env=ZICATO_GIT_SHA="),
    }

    // Re-run if HEAD moves so the SHA stays current.
    println!("cargo:rerun-if-changed=../.git/HEAD");
    println!("cargo:rerun-if-changed=../.git/refs");
}
