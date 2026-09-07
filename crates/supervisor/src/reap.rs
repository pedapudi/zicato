//! Confined snapshot cleanup after confirmed producer death.
//!
//! Each active record identifies the process that created its worker. Unknown
//! or live producer identity refuses orphan cleanup. The watchdog acquires the
//! stable invocation writer guard before reading an orphan batch and retains
//! it through group termination, snapshot cleanup and active-record removal.
//!
//! Snapshot deletion is confined to a `ztw-snap-*` directory below the system
//! temporary directory. A malformed path cannot authorize broader deletion.

use crate::signal;
use crate::state::ActiveRun;
use std::path::{Path, PathBuf};
use tracing::{debug, warn};

/// The mkdtemp prefix every generation-store backend uses for a run's
/// ephemeral snapshot root (`tempfile.mkdtemp(prefix="ztw-snap-…")`). Must
/// match `zicato.epoch.genstore.EPHEMERAL_SNAPSHOT_PREFIX`.
pub const SNAPSHOT_PREFIX: &str = "ztw-snap-";

/// Positively establish that the producer recorded by a run no longer exists.
///
/// Missing provenance, invalid tokens and unreadable live identity retain
/// ownership. A readable replacement token proves that the recorded owner died.
pub fn producer_is_dead(run: &ActiveRun) -> bool {
    let (Some(pid), Some(expected)) = (run.producer_pid, run.producer_start_time) else {
        return false;
    };
    pid > 1
        && expected.is_finite()
        && expected >= 0.0
        && !signal::is_same_process(pid, Some(expected))
}

/// Resolve the system temp dir the runner's `tempfile.mkdtemp` would have
/// used, canonicalized when possible.
///
/// `std::env::temp_dir()` honours `$TMPDIR` on Unix exactly as Python's
/// `tempfile` does, falling back to `/tmp`. Canonicalizing resolves symlinks
/// (e.g. macOS's `/tmp` → `/private/tmp`) so the prefix comparison in
/// [`reapable_snapshot_root`] is against the same real path the recorded
/// snapshot resolves to.
fn system_temp_dir() -> PathBuf {
    let raw = std::env::temp_dir();
    raw.canonicalize().unwrap_or(raw)
}

/// The prefix-guarded `ztw-snap-*` root to remove for a recorded
/// `snapshot_path`, or `None` when the path fails the guard.
///
/// The runner records the per-run WORKING COPY (`<ztw-snap-root>/<name>`) as
/// `snapshot_path`; the directory to remove is its `ztw-snap-*` mkdtemp
/// ancestor (the parent, which also holds the run's scratch dir), mirroring
/// the Python `discard_ephemeral_parent`. This walks from the recorded
/// path up toward the temp dir to find the FIRST ancestor whose basename
/// starts with [`SNAPSHOT_PREFIX`], then enforces the rail:
///
/// * the found root MUST be a strict descendant of `temp_dir`,
/// * its basename MUST start with `ztw-snap-`.
///
/// Anything else — a path outside the temp dir, a path with no `ztw-snap-*`
/// ancestor, the temp dir itself, an empty/relative path — returns `None`,
/// so the rmtree caller can never be handed an arbitrary tree. Pure (no
/// filesystem mutation) and `temp_dir`-injected so it is unit-testable.
pub fn reapable_snapshot_root(snapshot_path: &str, temp_dir: &Path) -> Option<PathBuf> {
    if snapshot_path.is_empty() {
        return None;
    }
    let candidate = Path::new(snapshot_path);
    // A relative recorded path is suspect: the temp-dir containment check
    // below is only meaningful for an absolute path. Refuse it outright.
    if !candidate.is_absolute() {
        return None;
    }
    // Canonicalize when the path still exists so symlinked temp dirs compare
    // equal; fall back to the lexical path (a path whose target was already
    // partially removed must still be reapable).
    let resolved = candidate
        .canonicalize()
        .unwrap_or_else(|_| candidate.to_path_buf());

    // Walk ancestors (self first) for the first `ztw-snap-*` directory.
    let snap_root = resolved
        .ancestors()
        .find(|a| {
            a.file_name()
                .and_then(|n| n.to_str())
                .is_some_and(|n| n.starts_with(SNAPSHOT_PREFIX))
        })?
        .to_path_buf();

    // RAIL: the root must be a STRICT descendant of the temp dir. (A root
    // equal to the temp dir is impossible — temp_dir's own basename does not
    // start with the prefix — but `starts_with` + `!=` makes the intent
    // explicit and survives an odd `$TMPDIR` that happened to match.)
    if snap_root == temp_dir || !snap_root.starts_with(temp_dir) {
        return None;
    }
    // RAIL (belt-and-braces): the basename must carry the prefix. The
    // ancestor search already guarantees this, but re-asserting keeps the
    // guarantee local to the return.
    let basename_ok = snap_root
        .file_name()
        .and_then(|n| n.to_str())
        .is_some_and(|n| n.starts_with(SNAPSHOT_PREFIX));
    if !basename_ok {
        return None;
    }
    Some(snap_root)
}

/// GC one run's orphaned ephemeral snapshot, returning `true` iff a
/// `ztw-snap-*` root was removed (or was already gone after passing the
/// guard).
///
/// The path guard ([`reapable_snapshot_root`]) is applied against the live
/// system temp dir; only a vetted `ztw-snap-*` root under it is removed. A
/// record with no `snapshot_path`, or one that fails the guard, is a no-op
/// (returns `false`) — never an error and never a deletion outside the
/// guard. Best-effort: an rmtree failure is logged, not propagated.
pub fn reap_orphaned_snapshot(run: &ActiveRun) -> bool {
    let Some(snapshot_path) = run.snapshot_path.as_deref() else {
        return false;
    };
    let temp_dir = system_temp_dir();
    let Some(root) = reapable_snapshot_root(snapshot_path, &temp_dir) else {
        warn!(
            run_id = %run.run_id,
            snapshot_path,
            "recorded snapshot_path is not a reapable ztw-snap-* root under the temp dir; refusing to remove",
        );
        return false;
    };
    match std::fs::remove_dir_all(&root) {
        Ok(()) => {
            debug!(run_id = %run.run_id, ?root, "reaped orphaned ephemeral snapshot");
            true
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            // Already gone (the runner's own finally block won the race, or a
            // prior tick reaped it). The guard passed, so count it reaped.
            true
        }
        Err(e) => {
            warn!(run_id = %run.run_id, ?root, error=%e, "failed to reap ephemeral snapshot");
            false
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn run_with_snapshot(snapshot_path: Option<&str>) -> ActiveRun {
        ActiveRun {
            run_id: "r1".into(),
            snapshot_path: snapshot_path.map(str::to_string),
            ..Default::default()
        }
    }

    #[test]
    fn producer_death_requires_valid_saved_identity() {
        let pid = std::process::id() as i32;
        let token = signal::pid_start_time(pid).unwrap();
        for run in [
            ActiveRun::default(),
            ActiveRun {
                producer_pid: Some(pid),
                ..Default::default()
            },
            ActiveRun {
                producer_pid: Some(0),
                producer_start_time: Some(token),
                ..Default::default()
            },
            ActiveRun {
                producer_pid: Some(pid),
                producer_start_time: Some(f64::NAN),
                ..Default::default()
            },
            ActiveRun {
                producer_pid: Some(pid),
                producer_start_time: Some(-1.0),
                ..Default::default()
            },
            ActiveRun {
                producer_pid: Some(pid),
                producer_start_time: Some(token),
                ..Default::default()
            },
        ] {
            assert!(!producer_is_dead(&run));
        }
        for run in [
            ActiveRun {
                producer_pid: Some(99_999_999),
                producer_start_time: Some(token),
                ..Default::default()
            },
            ActiveRun {
                producer_pid: Some(pid),
                producer_start_time: Some(token + 1.0),
                ..Default::default()
            },
        ] {
            assert!(producer_is_dead(&run));
        }
    }

    // ---- reapable_snapshot_root (the prefix-guarded path rail) -------

    #[test]
    fn refuses_a_path_outside_the_temp_dir() {
        let temp = TempDir::new().unwrap();
        let outside = "/var/lib/zicato/ztw-snap-abcd/snapshot";
        assert_eq!(reapable_snapshot_root(outside, temp.path()), None);
    }

    #[test]
    fn refuses_a_path_with_no_snap_prefix_ancestor() {
        let temp = TempDir::new().unwrap();
        let inside_but_wrong = temp.path().join("some-other-dir/snapshot");
        assert_eq!(
            reapable_snapshot_root(inside_but_wrong.to_str().unwrap(), temp.path()),
            None,
        );
    }

    #[test]
    fn refuses_the_temp_dir_itself() {
        let temp = TempDir::new().unwrap();
        assert_eq!(
            reapable_snapshot_root(temp.path().to_str().unwrap(), temp.path()),
            None,
        );
    }

    #[test]
    fn refuses_an_empty_or_relative_path() {
        let temp = TempDir::new().unwrap();
        assert_eq!(reapable_snapshot_root("", temp.path()), None);
        assert_eq!(reapable_snapshot_root("ztw-snap-x/snap", temp.path()), None);
    }

    #[test]
    fn resolves_the_snap_root_from_the_recorded_working_copy() {
        // The recorded path is the WORKING COPY (a child of the ztw-snap-*
        // mkdtemp root); the guard returns the ztw-snap-* PARENT to remove,
        // mirroring the Python discard_ephemeral_parent.
        let temp = TempDir::new().unwrap();
        let snap_root = temp.path().join("ztw-snap-run42-XXXX");
        let working_copy = snap_root.join("snapshot");
        std::fs::create_dir_all(&working_copy).unwrap();
        let got = reapable_snapshot_root(working_copy.to_str().unwrap(), temp.path());
        // Canonicalize the expected root: the function canonicalizes the
        // existing path, so compare against the canonical snap root.
        assert_eq!(got, Some(snap_root.canonicalize().unwrap()));
    }

    #[test]
    fn accepts_the_snap_root_recorded_directly() {
        // A record that points straight at the ztw-snap-* root (no working
        // copy child) is reapable as-is.
        let temp = TempDir::new().unwrap();
        let snap_root = temp.path().join("ztw-snap-direct-YYYY");
        std::fs::create_dir_all(&snap_root).unwrap();
        let got = reapable_snapshot_root(snap_root.to_str().unwrap(), temp.path());
        assert_eq!(got, Some(snap_root.canonicalize().unwrap()));
    }

    // ---- reap_orphaned_snapshot (end-to-end on a real temp tree) ----

    #[test]
    fn reaps_a_real_ephemeral_snapshot_tree() {
        // Build a ztw-snap-* tree under the SYSTEM temp dir (what the guard
        // checks against) and confirm reaping removes the whole root.
        let parent = tempfile::Builder::new()
            .prefix(SNAPSHOT_PREFIX)
            .tempdir()
            .unwrap();
        let working_copy = parent.path().join("snapshot");
        std::fs::create_dir_all(working_copy.join("src")).unwrap();
        std::fs::write(working_copy.join("src/a.py"), b"x = 1\n").unwrap();
        let root = parent.path().to_path_buf();
        assert!(root.exists());

        let run = run_with_snapshot(Some(working_copy.to_str().unwrap()));
        assert!(
            reap_orphaned_snapshot(&run),
            "should report the tree reaped"
        );
        assert!(!root.exists(), "the ztw-snap-* root must be removed");
        // Defuse the TempDir guard's own drop (the dir is already gone).
        std::mem::forget(parent);
    }

    #[test]
    fn refuses_to_reap_a_path_outside_the_temp_dir() {
        // A snapshot_path that is NOT under the temp dir is refused: the dir
        // is left intact and the function reports nothing reaped.
        let outside = TempDir::new_in(std::env::current_dir().unwrap()).unwrap();
        let bogus = outside.path().join("ztw-snap-evil/snapshot");
        std::fs::create_dir_all(&bogus).unwrap();
        let run = run_with_snapshot(Some(bogus.to_str().unwrap()));
        assert!(
            !reap_orphaned_snapshot(&run),
            "a path outside the temp dir must not be reaped",
        );
        assert!(bogus.exists(), "the out-of-temp tree must be left intact");
    }

    #[test]
    fn no_snapshot_path_is_a_noop() {
        let run = run_with_snapshot(None);
        assert!(!reap_orphaned_snapshot(&run));
    }

    #[test]
    fn already_gone_snap_root_counts_as_reaped() {
        // The guard passes (a well-formed ztw-snap-* path under temp) but the
        // tree was already removed → reaped == true, no error.
        let temp_dir = system_temp_dir();
        let phantom = temp_dir.join("ztw-snap-already-gone-ZZZZ/snapshot");
        let run = run_with_snapshot(Some(phantom.to_str().unwrap()));
        assert!(reap_orphaned_snapshot(&run));
    }
}
