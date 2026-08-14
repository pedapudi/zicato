//! Diff-containment attestation — "are mutations confined to the mutation
//! sites?"
//!
//! INTEGRITY NOTARY — record #2 (the headline). For each materialised child
//! generation the supervisor independently recomputes the on-disk diff of the
//! child snapshot against its PARENT snapshot and asserts that every file
//! OUTSIDE the registered mutable surface is byte-identical parent↔child. A
//! file changed, added, or deleted outside that surface is an out-of-bounds
//! mutation — a proposer (or a bug) escaping its sandbox — and is flagged.
//!
//! Layout the check relies on (already walked by `reader::build_lineage_view`
//! and parsed by `epoch::build_epoch_view`):
//!
//! * each generation's snapshot lives at
//!   `epochs/{epoch_id}/generations/{generation_id}/snapshot/`;
//! * a snapshot copies every registered mutable tree under its BASENAME, so
//!   inside a snapshot root the mutable surface is `snapshot/<basename>` for
//!   each `harness.mutable_trees` entry (per
//!   `zicato.adapters.adk.ADKHarnessAdapter.mutable_subpaths`).
//!
//! v1 is the COARSE check: containment at file granularity (any out-of-bounds
//! file that differs is a violation). Line-range tightening inside a site is a
//! documented FOLLOWUP, not v1.
//!
//! FAIL-OPEN-TO-ALARM: an unreadable snapshot, a missing parent, or any I/O
//! surprise yields *no violation* (the attestation simply cannot be made), not
//! a false quarantine. The check only ever ALARMS on a positive, observed
//! out-of-bounds difference — it never fails closed and never blocks a
//! promotion (alarm-only / read-only in v1).

use crate::sha256;
use serde::Serialize;
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use walkdir::WalkDir;

/// How an out-of-bounds file differs between parent and child.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum DiffKind {
    /// Present in both, but the content hash differs.
    Changed,
    /// Present in the child snapshot, absent in the parent.
    Added,
    /// Present in the parent snapshot, absent in the child.
    Deleted,
}

impl DiffKind {
    pub fn as_str(self) -> &'static str {
        match self {
            DiffKind::Changed => "changed",
            DiffKind::Added => "added",
            DiffKind::Deleted => "deleted",
        }
    }
}

/// One out-of-bounds file difference: a mutation that escaped the sandbox.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Violation {
    /// The differing file's path RELATIVE to the snapshot root (stable,
    /// host-independent, and the same key in parent and child).
    pub path: String,
    pub kind: DiffKind,
}

/// The attestation for one parent→child generation pair.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize)]
pub struct Attestation {
    pub epoch_id: String,
    pub generation_id: String,
    pub parent_generation_id: String,
    /// `true` when every out-of-bounds file is byte-identical parent↔child.
    pub contained: bool,
    /// The out-of-bounds differences, empty when `contained`.
    pub violations: Vec<Violation>,
    /// Why the attestation could not be made, when it could not (a missing
    /// snapshot / parent). `None` on a clean attestation. A skipped
    /// attestation is NOT a violation — fail-open-to-alarm.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub skipped_reason: Option<String>,
}

/// Compute the set of mutable-surface relative roots inside a snapshot from
/// the registered `mutable_trees`.
///
/// A snapshot copies each registered tree under its basename, so the in-bounds
/// surface keyed against snapshot-relative paths is the set of those
/// basenames. An entry with an empty basename is ignored (it cannot name a
/// real subtree). When `mutable_trees` is empty the surface is the WHOLE
/// snapshot (mirroring `mutable_subpaths`' fallback): every file is in-bounds,
/// so nothing can be out-of-bounds — the attestation is trivially contained.
pub fn mutable_basenames(mutable_trees: &[String]) -> BTreeSet<String> {
    mutable_trees
        .iter()
        .filter_map(|t| {
            Path::new(t)
                .file_name()
                .and_then(|n| n.to_str())
                .filter(|n| !n.is_empty())
                .map(str::to_string)
        })
        .collect()
}

/// Whether `rel` (a snapshot-relative path) lies inside the mutable surface
/// named by `basenames` — i.e. its first path component is a mutable basename.
/// When `basenames` is empty the whole snapshot is mutable (everything is
/// in-bounds).
fn is_in_bounds(rel: &Path, basenames: &BTreeSet<String>) -> bool {
    if basenames.is_empty() {
        return true;
    }
    match rel.components().next() {
        Some(std::path::Component::Normal(first)) => first
            .to_str()
            .map(|s| basenames.contains(s))
            .unwrap_or(false),
        // A path with no normal leading component (shouldn't happen for a
        // relative file path) is treated as out-of-bounds to be safe.
        _ => false,
    }
}

/// Walk `root`, returning a map from snapshot-relative path → content hash for
/// every regular file. Symlinks are not followed (the hash of a symlink target
/// path is recorded via its own dir entry only if it is a regular file).
///
/// Best-effort: an unreadable file is skipped (its absence from the map is
/// handled by the caller as fail-open), and a non-existent root yields `None`
/// so the caller can record a skip rather than a spurious all-deleted diff.
fn hash_tree(root: &Path) -> Option<BTreeMap<PathBuf, String>> {
    if !root.is_dir() {
        return None;
    }
    let mut out = BTreeMap::new();
    for entry in WalkDir::new(root).follow_links(false).into_iter().flatten() {
        if !entry.file_type().is_file() {
            continue;
        }
        let abs = entry.path();
        let rel = match abs.strip_prefix(root) {
            Ok(r) => r.to_path_buf(),
            Err(_) => continue,
        };
        match std::fs::read(abs) {
            Ok(bytes) => {
                out.insert(rel, sha256::hex_digest(&bytes));
            }
            // An unreadable file is skipped (fail-open): it simply does not
            // participate in the diff rather than manufacturing a violation.
            Err(_) => continue,
        }
    }
    Some(out)
}

/// Render a snapshot-relative path as a forward-slash string key (stable
/// across the parent/child comparison and host-independent in the finding).
fn rel_key(rel: &Path) -> String {
    rel.components()
        .filter_map(|c| match c {
            std::path::Component::Normal(s) => s.to_str(),
            _ => None,
        })
        .collect::<Vec<_>>()
        .join("/")
}

/// Compute the out-of-bounds diff between a parent and child snapshot.
///
/// `parent_root` / `child_root` are the two `.../snapshot/` directories;
/// `mutable_trees` are the registered mutable-tree paths (their basenames name
/// the in-bounds surface). Returns the [`Attestation`]: `contained` with no
/// violations on a clean pair, or the list of out-of-bounds changed/added/
/// deleted files. A missing snapshot or parent yields a `skipped_reason`
/// (fail-open-to-alarm), never a violation.
pub fn attest(
    epoch_id: &str,
    generation_id: &str,
    parent_generation_id: &str,
    parent_root: &Path,
    child_root: &Path,
    mutable_trees: &[String],
) -> Attestation {
    let base = Attestation {
        epoch_id: epoch_id.to_string(),
        generation_id: generation_id.to_string(),
        parent_generation_id: parent_generation_id.to_string(),
        contained: true,
        violations: Vec::new(),
        skipped_reason: None,
    };

    let parent_hashes = match hash_tree(parent_root) {
        Some(h) => h,
        None => {
            return Attestation {
                skipped_reason: Some(format!(
                    "parent snapshot unreadable: {}",
                    parent_root.display()
                )),
                ..base
            };
        }
    };
    let child_hashes = match hash_tree(child_root) {
        Some(h) => h,
        None => {
            return Attestation {
                skipped_reason: Some(format!(
                    "child snapshot unreadable: {}",
                    child_root.display()
                )),
                ..base
            };
        }
    };

    let basenames = mutable_basenames(mutable_trees);
    let mut violations = Vec::new();

    // Union of all relative paths across both trees, deduplicated + ordered.
    let mut all_paths: BTreeSet<&PathBuf> = BTreeSet::new();
    all_paths.extend(parent_hashes.keys());
    all_paths.extend(child_hashes.keys());

    for rel in all_paths {
        // Only OUT-OF-BOUNDS files matter: an in-bounds file may freely differ
        // (it is the mutation surface). This is the whole point of the check.
        if is_in_bounds(rel, &basenames) {
            continue;
        }
        let kind = match (parent_hashes.get(rel), child_hashes.get(rel)) {
            (Some(p), Some(c)) if p == c => continue, // byte-identical — fine.
            (Some(_), Some(_)) => DiffKind::Changed,
            (None, Some(_)) => DiffKind::Added,
            (Some(_), None) => DiffKind::Deleted,
            (None, None) => continue, // unreachable: rel came from one of them.
        };
        violations.push(Violation {
            path: rel_key(rel),
            kind,
        });
    }

    Attestation {
        contained: violations.is_empty(),
        violations,
        ..base
    }
}

// ---------------------------------------------------------------------------
// Workspace scan + shared findings store
// ---------------------------------------------------------------------------

use crate::reader::WorkspacePaths;
use std::sync::Mutex;

/// The latest diff-containment scan result, shared with `/statusz`.
///
/// Holds only the most recent scan's quarantine attestations (the ones with
/// violations) plus a count of pairs scanned and skipped, so `/statusz` can
/// show "N pairs scanned, M quarantined" and the offending files. A scan with
/// no violations clears the quarantine list (the previous out-of-bounds
/// condition was resolved or the generation was rematerialised).
#[derive(Debug, Default)]
pub struct DiffContainmentFindings {
    inner: Mutex<DiffContainmentView>,
}

/// A serializable snapshot of the latest diff-containment scan for `/statusz`.
#[derive(Debug, Clone, Default, Serialize)]
pub struct DiffContainmentView {
    /// `true` once at least one scan has run.
    pub scanned: bool,
    /// Number of parent→child pairs attested in the latest scan.
    pub pairs_scanned: u64,
    /// Number of pairs skipped (fail-open: missing/unreadable snapshot).
    pub pairs_skipped: u64,
    /// The quarantined attestations (those with out-of-bounds violations).
    pub quarantined: Vec<Attestation>,
}

impl DiffContainmentFindings {
    pub fn new() -> Self {
        Self::default()
    }

    /// Replace the stored view with the latest scan result.
    pub fn record(&self, view: DiffContainmentView) {
        let mut g = match self.inner.lock() {
            Ok(g) => g,
            Err(p) => p.into_inner(),
        };
        *g = view;
    }

    /// A point-in-time snapshot for serialization.
    pub fn view(&self) -> DiffContainmentView {
        match self.inner.lock() {
            Ok(g) => g.clone(),
            Err(p) => p.into_inner().clone(),
        }
    }
}

/// The snapshot subdirectory inside a generation directory.
const SNAPSHOT_DIR: &str = "snapshot";

/// The quarantine-finding filename written into the epoch health dir.
fn quarantine_filename(generation_id: &str) -> String {
    format!("diff_containment_{generation_id}.json")
}

/// Scan every materialised child generation in the workspace and attest its
/// diff-containment against its parent snapshot.
///
/// A pair is attested only when BOTH the child and its parent have a
/// materialised `snapshot/` directory and the child records a
/// `parent_generation_id`. The root `v0` (no parent) and any generation whose
/// snapshot is absent are skipped (fail-open). Returns the scan view; the
/// caller records it into the shared store and writes a quarantine finding for
/// each violating pair.
pub fn scan_workspace(paths: &WorkspacePaths) -> DiffContainmentView {
    let mutable_trees = crate::epoch::build_epoch_view(paths)
        .harness
        .map(|h| h.mutable_trees)
        .unwrap_or_default();

    let lineage = crate::reader::build_lineage_view(paths);

    // Index snapshot roots by (epoch, generation) for parent lookup.
    let snapshot_root = |epoch_id: &str, generation_id: &str| -> PathBuf {
        paths
            .epochs
            .join(epoch_id)
            .join("generations")
            .join(generation_id)
            .join(SNAPSHOT_DIR)
    };

    let mut pairs_scanned = 0u64;
    let mut pairs_skipped = 0u64;
    let mut quarantined = Vec::new();

    for gen in &lineage.generations {
        let Some(parent_id) = gen.parent_generation_id.as_deref() else {
            continue; // root / no parent — nothing to diff against.
        };
        let child_root = snapshot_root(&gen.epoch_id, &gen.generation_id);
        let parent_root = snapshot_root(&gen.epoch_id, parent_id);
        // Only attest a materialised child; a generation that has not yet been
        // copied to disk is not yet auditable.
        if !child_root.is_dir() {
            continue;
        }
        let att = attest(
            &gen.epoch_id,
            &gen.generation_id,
            parent_id,
            &parent_root,
            &child_root,
            &mutable_trees,
        );
        if att.skipped_reason.is_some() {
            pairs_skipped += 1;
            continue;
        }
        pairs_scanned += 1;
        if !att.contained {
            quarantined.push(att);
        }
    }

    DiffContainmentView {
        scanned: true,
        pairs_scanned,
        pairs_skipped,
        quarantined,
    }
}

/// Write a quarantine finding for one violating attestation into the epoch
/// health dir (`epochs/{epoch_id}/health/diff_containment_{gen}.json`).
///
/// Best-effort and atomic-ish (`.tmp` + rename): a write failure is logged,
/// not propagated. The finding is the supervisor's durable, out-of-band record
/// that this generation escaped its mutation surface — separate from (and
/// corroborating) the in-memory `/statusz` alert.
pub fn write_quarantine_finding(paths: &WorkspacePaths, att: &Attestation) {
    use tracing::warn;
    let dir = paths.epoch_health_dir(&att.epoch_id);
    if let Err(e) = std::fs::create_dir_all(&dir) {
        warn!(?dir, error=%e, "could not create epoch health dir for quarantine finding");
        return;
    }
    let path = dir.join(quarantine_filename(&att.generation_id));
    let body = serde_json::json!({
        "kind": "diff_containment_quarantine",
        "epoch_id": att.epoch_id,
        "generation_id": att.generation_id,
        "parent_generation_id": att.parent_generation_id,
        "contained": att.contained,
        "violations": att.violations,
        "checked_at": chrono::Utc::now().to_rfc3339(),
    });
    let bytes = match serde_json::to_vec_pretty(&body) {
        Ok(b) => b,
        Err(e) => {
            warn!(error=%e, "quarantine finding serialization failed");
            return;
        }
    };
    let tmp = path.with_extension("json.tmp");
    if std::fs::write(&tmp, &bytes)
        .and_then(|_| std::fs::rename(&tmp, &path))
        .is_err()
    {
        warn!(?path, "failed to write quarantine finding");
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    fn write(root: &Path, rel: &str, contents: &[u8]) {
        let p = root.join(rel);
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(p, contents).unwrap();
    }

    #[test]
    fn mutable_basenames_takes_the_last_component() {
        let trees = vec![
            "/abs/registered/agent".to_string(),
            "relative/tools".to_string(),
        ];
        let names = mutable_basenames(&trees);
        assert!(names.contains("agent"));
        assert!(names.contains("tools"));
        assert_eq!(names.len(), 2);
    }

    #[test]
    fn in_bounds_change_is_contained() {
        // A file UNDER the mutable surface (agent/) may freely differ.
        let tmp = TempDir::new().unwrap();
        let parent = tmp.path().join("p");
        let child = tmp.path().join("c");
        write(&parent, "agent/main.py", b"x = 1\n");
        write(&parent, "support/lib.py", b"shared\n");
        write(&child, "agent/main.py", b"x = 2\n"); // changed IN the surface
        write(&child, "support/lib.py", b"shared\n"); // untouched out of bounds
        let att = attest(
            "e1",
            "v1",
            "v0",
            &parent,
            &child,
            &["/reg/agent".to_string()],
        );
        assert!(att.contained, "in-bounds change must be contained: {att:?}");
        assert!(att.violations.is_empty());
        assert!(att.skipped_reason.is_none());
    }

    #[test]
    fn out_of_bounds_change_is_a_violation() {
        let tmp = TempDir::new().unwrap();
        let parent = tmp.path().join("p");
        let child = tmp.path().join("c");
        write(&parent, "agent/main.py", b"x = 1\n");
        write(&parent, "support/lib.py", b"shared\n");
        write(&child, "agent/main.py", b"x = 2\n");
        write(&child, "support/lib.py", b"TAMPERED\n"); // out-of-bounds change
        let att = attest(
            "e1",
            "v1",
            "v0",
            &parent,
            &child,
            &["/reg/agent".to_string()],
        );
        assert!(!att.contained);
        assert_eq!(att.violations.len(), 1);
        assert_eq!(att.violations[0].path, "support/lib.py");
        assert_eq!(att.violations[0].kind, DiffKind::Changed);
    }

    #[test]
    fn out_of_bounds_add_and_delete_are_violations() {
        let tmp = TempDir::new().unwrap();
        let parent = tmp.path().join("p");
        let child = tmp.path().join("c");
        write(&parent, "agent/main.py", b"x = 1\n");
        write(&parent, "support/old.py", b"old\n"); // deleted out of bounds
        write(&child, "agent/main.py", b"x = 1\n");
        write(&child, "support/new.py", b"new\n"); // added out of bounds
        let att = attest(
            "e1",
            "v1",
            "v0",
            &parent,
            &child,
            &["/reg/agent".to_string()],
        );
        assert!(!att.contained);
        let kinds: BTreeMap<&str, DiffKind> = att
            .violations
            .iter()
            .map(|v| (v.path.as_str(), v.kind))
            .collect();
        assert_eq!(kinds.get("support/new.py"), Some(&DiffKind::Added));
        assert_eq!(kinds.get("support/old.py"), Some(&DiffKind::Deleted));
    }

    #[test]
    fn identical_trees_are_contained() {
        let tmp = TempDir::new().unwrap();
        let parent = tmp.path().join("p");
        let child = tmp.path().join("c");
        write(&parent, "agent/a.py", b"a\n");
        write(&parent, "support/b.py", b"b\n");
        write(&child, "agent/a.py", b"a\n");
        write(&child, "support/b.py", b"b\n");
        let att = attest(
            "e1",
            "v1",
            "v0",
            &parent,
            &child,
            &["/reg/agent".to_string()],
        );
        assert!(att.contained);
    }

    #[test]
    fn empty_mutable_trees_makes_everything_in_bounds() {
        // No declared mutable surface → the whole snapshot is the surface
        // (mirrors mutable_subpaths' fallback): nothing is out-of-bounds.
        let tmp = TempDir::new().unwrap();
        let parent = tmp.path().join("p");
        let child = tmp.path().join("c");
        write(&parent, "anything.py", b"1\n");
        write(&child, "anything.py", b"2\n");
        let att = attest("e1", "v1", "v0", &parent, &child, &[]);
        assert!(att.contained);
    }

    #[test]
    fn missing_parent_snapshot_is_skipped_not_a_violation() {
        // Fail-open-to-alarm: an absent parent snapshot cannot be attested, so
        // it is SKIPPED with a reason — never a false quarantine.
        let tmp = TempDir::new().unwrap();
        let parent = tmp.path().join("does-not-exist");
        let child = tmp.path().join("c");
        write(&child, "agent/a.py", b"a\n");
        let att = attest(
            "e1",
            "v1",
            "v0",
            &parent,
            &child,
            &["/reg/agent".to_string()],
        );
        assert!(att.contained, "a skip is not a violation");
        assert!(att.violations.is_empty());
        assert!(att.skipped_reason.is_some());
    }

    #[test]
    fn nested_out_of_bounds_paths_are_detected() {
        // A deeply-nested out-of-bounds file is still caught (the leading
        // component, not just the immediate parent, decides in/out of bounds).
        let tmp = TempDir::new().unwrap();
        let parent = tmp.path().join("p");
        let child = tmp.path().join("c");
        write(&parent, "vendor/deep/nested/x.py", b"v1\n");
        write(&child, "vendor/deep/nested/x.py", b"v2\n");
        let att = attest(
            "e1",
            "v1",
            "v0",
            &parent,
            &child,
            &["/reg/agent".to_string()],
        );
        assert!(!att.contained);
        assert_eq!(att.violations[0].path, "vendor/deep/nested/x.py");
    }

    // ---- workspace scan + quarantine findings -----------------------

    /// Build a minimal workspace with a config (mutable_trees) + a current
    /// epoch marker + a parent (v0) and child (v1) generation snapshot.
    fn scan_ws() -> (TempDir, WorkspacePaths) {
        let tmp = TempDir::new().unwrap();
        let ws = tmp.path().to_path_buf();
        std::fs::create_dir_all(ws.join("epochs/e1/generations")).unwrap();
        let p = WorkspacePaths::new(ws.clone());
        std::fs::write(p.current_epoch_marker(), "e1").unwrap();
        // Harness with a single mutable tree "agent".
        std::fs::write(
            ws.join("config.json"),
            serde_json::json!({"adk_entrypoint": "m:a", "mutable_trees": ["/reg/agent"]})
                .to_string(),
        )
        .unwrap();
        (tmp, p)
    }

    fn write_gen_snapshot(
        p: &WorkspacePaths,
        gen: &str,
        parent: Option<&str>,
        files: &[(&str, &[u8])],
    ) {
        let gen_dir = p.epochs.join("e1").join("generations").join(gen);
        std::fs::create_dir_all(&gen_dir).unwrap();
        if let Some(parent) = parent {
            std::fs::write(
                gen_dir.join("experiment.json"),
                serde_json::json!({"parent_generation_id": parent}).to_string(),
            )
            .unwrap();
            std::fs::write(
                p.lineage(),
                serde_json::json!({"epochs": [{"id": "e1", "generations": [{
                    "id": gen, "parent_id": parent, "promoted": false
                }]}]})
                .to_string(),
            )
            .unwrap();
        }
        let snap = gen_dir.join("snapshot");
        for (rel, contents) in files {
            write(&snap, rel, contents);
        }
    }

    #[test]
    fn scan_flags_an_out_of_bounds_child() {
        let (_t, p) = scan_ws();
        // v0 (parent) and v1 (child) both materialised; v1 tampers with an
        // out-of-bounds support file.
        write_gen_snapshot(
            &p,
            "v0",
            None,
            &[("agent/main.py", b"x=1\n"), ("support/lib.py", b"shared\n")],
        );
        write_gen_snapshot(
            &p,
            "v1",
            Some("v0"),
            &[
                ("agent/main.py", b"x=2\n"),
                ("support/lib.py", b"TAMPERED\n"),
            ],
        );
        let view = scan_workspace(&p);
        assert!(view.scanned);
        assert_eq!(view.pairs_scanned, 1, "one parent->child pair attested");
        assert_eq!(view.quarantined.len(), 1);
        assert_eq!(view.quarantined[0].generation_id, "v1");
        assert_eq!(view.quarantined[0].violations[0].path, "support/lib.py");
    }

    #[test]
    fn scan_passes_an_in_bounds_child() {
        let (_t, p) = scan_ws();
        write_gen_snapshot(
            &p,
            "v0",
            None,
            &[("agent/main.py", b"x=1\n"), ("support/lib.py", b"shared\n")],
        );
        write_gen_snapshot(
            &p,
            "v1",
            Some("v0"),
            &[("agent/main.py", b"x=2\n"), ("support/lib.py", b"shared\n")],
        );
        let view = scan_workspace(&p);
        assert_eq!(view.pairs_scanned, 1);
        assert!(
            view.quarantined.is_empty(),
            "in-bounds child must not quarantine"
        );
    }

    #[test]
    fn scan_skips_a_child_without_a_parent_snapshot() {
        let (_t, p) = scan_ws();
        // Child references v0 but v0 has no snapshot on disk → fail-open skip.
        write_gen_snapshot(&p, "v1", Some("v0"), &[("agent/main.py", b"x=2\n")]);
        let view = scan_workspace(&p);
        assert_eq!(view.pairs_scanned, 0);
        assert_eq!(view.pairs_skipped, 1);
        assert!(view.quarantined.is_empty());
    }

    #[test]
    fn write_quarantine_finding_lands_in_the_epoch_health_dir() {
        let (_t, p) = scan_ws();
        let att = Attestation {
            epoch_id: "e1".into(),
            generation_id: "v1".into(),
            parent_generation_id: "v0".into(),
            contained: false,
            violations: vec![Violation {
                path: "support/lib.py".into(),
                kind: DiffKind::Changed,
            }],
            skipped_reason: None,
        };
        write_quarantine_finding(&p, &att);
        let finding = p.epoch_health_dir("e1").join("diff_containment_v1.json");
        assert!(finding.exists(), "quarantine finding must be written");
        let body: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&finding).unwrap()).unwrap();
        assert_eq!(body["kind"], "diff_containment_quarantine");
        assert_eq!(body["generation_id"], "v1");
        assert_eq!(body["violations"][0]["path"], "support/lib.py");
    }

    #[test]
    fn findings_store_round_trips_the_latest_view() {
        let store = DiffContainmentFindings::new();
        assert!(!store.view().scanned);
        store.record(DiffContainmentView {
            scanned: true,
            pairs_scanned: 3,
            pairs_skipped: 1,
            quarantined: vec![],
        });
        let v = store.view();
        assert!(v.scanned);
        assert_eq!(v.pairs_scanned, 3);
        assert_eq!(v.pairs_skipped, 1);
    }
}
