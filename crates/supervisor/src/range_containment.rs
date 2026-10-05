//! Independent byte-range verification of Python's canonical mutation spans.
//!
//! Python owns mutation semantics. This verifier owns source inventory, byte
//! coverage, captured-policy binding, and patch-record binding. Selected source
//! roots and lineage coordinates come from the workspace. Missing evidence is
//! unverified, evidence that contradicts the observed files is an evidence
//! mismatch, and neither is ever contained.

use crate::reader::{LineageView, WorkspacePaths};
use crate::sha256::hex_digest;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::path::{Component, Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use walkdir::WalkDir;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ByteChange {
    parent_start: usize,
    parent_end: usize,
    child_start: usize,
    child_end: usize,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct MutationSpan {
    id: String,
    kind: String,
    op: String,
    start: usize,
    end: usize,
    source_sha256: String,
    child_start: Option<usize>,
    child_end: Option<usize>,
    child_source_sha256: Option<String>,
    content_hash: String,
    metadata: Vec<[String; 2]>,
    forbidden: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct FileContainment {
    path: String,
    parent_sha256: Option<String>,
    child_sha256: Option<String>,
    parent_executable: Option<bool>,
    child_executable: Option<bool>,
    spans: Vec<MutationSpan>,
    changes: Vec<ByteChange>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PatchBinding {
    id: String,
    sha256: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct Manifest {
    format_version: u32,
    epoch_id: String,
    parent_generation_id: String,
    generation_id: String,
    parent_source: String,
    child_source: String,
    policy_sha256: String,
    patches: Vec<PatchBinding>,
    files: Vec<FileContainment>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PolicyFile {
    path: String,
    sha256: String,
    executable: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PolicyPoint {
    path: String,
    id: String,
    kind: String,
    op: String,
    start: usize,
    end: usize,
    source_sha256: String,
    content_hash: String,
    metadata: Vec<[String; 2]>,
    forbidden: bool,
}

impl PolicyPoint {
    fn matches(&self, path: &str, span: &MutationSpan) -> bool {
        self.path == path
            && self.id == span.id
            && self.kind == span.kind
            && self.op == span.op
            && self.start == span.start
            && self.end == span.end
            && self.source_sha256 == span.source_sha256
            && self.content_hash == span.content_hash
            && self.metadata == span.metadata
            && self.forbidden == span.forbidden
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct MutationPolicy {
    format_version: u32,
    epoch_id: String,
    parent_generation_id: String,
    parent_source: String,
    source_files: Vec<PolicyFile>,
    enumeration_roots: Vec<String>,
    brief_sha256: String,
    scoring_sha256: String,
    points: Vec<PolicyPoint>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    Contained,
    /// Verified evidence shows a change no mutation unit authorizes.
    Violated,
    /// Recorded evidence contradicts the observed source files.
    EvidenceMismatch,
    /// Evidence is missing, malformed, or bound to other records.
    Unverified,
}

/// Findings that are violations once every other binding holds.
const VIOLATION_CODES: [&str; 5] = [
    "outside_mutation",
    "forbidden",
    "metadata",
    "file_set",
    "artifact_present",
];

/// Findings where a record's claim about source bytes differs from the files.
const CONTRADICTION_CODES: [&str; 6] = [
    "source_binding",
    "source_inventory",
    "byte_coverage",
    "policy_inventory",
    "parent_binding",
    "parent_inventory",
];

/// Findings about the parent tree, which an alarmed parent pair explains.
const PARENT_CODES: [&str; 4] = [
    "policy_inventory",
    "parent_binding",
    "parent_inventory",
    "parent_unreadable",
];

/// Findings about the child's own files, which no ancestor explains.
const OWN_CODES: [&str; 3] = ["source_binding", "source_inventory", "byte_coverage"];

/// A violation needs every finding to be one; a contradiction outranks missing evidence.
fn status_of(findings: &[Finding]) -> Status {
    let has = |codes: &[&str], finding: &Finding| codes.contains(&finding.code.as_str());
    if findings.is_empty() {
        Status::Contained
    } else if findings.iter().all(|f| has(&VIOLATION_CODES, f)) {
        Status::Violated
    } else if findings.iter().any(|f| has(&CONTRADICTION_CODES, f)) {
        Status::EvidenceMismatch
    } else {
        Status::Unverified
    }
}

#[derive(Debug, Clone, Serialize)]
pub struct Finding {
    pub code: String,
    pub path: String,
    pub detail: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct Attestation {
    pub epoch_id: String,
    pub parent_generation_id: String,
    pub generation_id: String,
    pub status: Status,
    pub findings: Vec<Finding>,
    /// The ancestor whose own source change explains this pair's evidence
    /// mismatch: the parent's tree changed after this child's policy captured
    /// it. Absent when the pair's finding is its own or no ancestor explains it.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub introduced_by: Option<String>,
    /// Set on an unverified pair with a recorded decision whose evidence
    /// verified on an earlier scan: the evidence was removed or made unreadable.
    #[serde(skip_serializing_if = "std::ops::Not::not")]
    pub evidence_withdrawn: bool,
    /// The epoch evidence input (`brief.md` or `scoring.json`) whose change
    /// explains this pair's `contract_binding` finding. The input alarms once
    /// for the epoch, so an otherwise unverified pair stays silent.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub introduced_by_input: Option<String>,
}

impl Attestation {
    fn new(epoch: &str, parent: &str, child: &str) -> Self {
        Self {
            epoch_id: epoch.into(),
            parent_generation_id: parent.into(),
            generation_id: child.into(),
            status: Status::Contained,
            findings: Vec::new(),
            introduced_by: None,
            evidence_withdrawn: false,
            introduced_by_input: None,
        }
    }

    fn finding(&mut self, code: &str, path: &str, detail: &str) {
        self.findings.push(Finding {
            code: code.into(),
            path: path.into(),
            detail: detail.into(),
        });
        self.status = status_of(&self.findings);
    }
}

#[derive(Deserialize)]
struct SourceScope {
    artifact_names: Vec<String>,
    artifact_suffixes: Vec<String>,
}

fn scope() -> &'static SourceScope {
    static SCOPE: OnceLock<SourceScope> = OnceLock::new();
    SCOPE.get_or_init(|| {
        serde_json::from_str(include_str!("../../../src/zicato/epoch/source_scope.json"))
            .expect("packaged source scope is valid")
    })
}

fn artifact(path: &Path) -> bool {
    let Some(name) = path.file_name().and_then(|v| v.to_str()) else {
        return false;
    };
    scope().artifact_names.iter().any(|v| v == name)
        || scope().artifact_suffixes.iter().any(|v| name.ends_with(v))
}

fn safe_relative(value: &str) -> bool {
    !value.is_empty()
        && !value.contains(['\\', '\0'])
        && value
            .split('/')
            .all(|part| !matches!(part, "" | "." | ".."))
        && Path::new(value)
            .components()
            .all(|part| matches!(part, Component::Normal(_)))
}

fn digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
}

struct SourceFile {
    bytes: Vec<u8>,
    sha256: String,
    executable: bool,
}

fn read_tree(root: &Path) -> Result<BTreeMap<String, SourceFile>, String> {
    if !root.is_dir() {
        return Err(format!("source directory is missing: {}", root.display()));
    }
    let mut result = BTreeMap::new();
    for item in WalkDir::new(root)
        .follow_links(false)
        .into_iter()
        .filter_entry(|entry| entry.depth() == 0 || !artifact(entry.path()))
    {
        let entry = item.map_err(|e| e.to_string())?;
        if entry.file_type().is_dir() {
            continue;
        }
        if !entry.file_type().is_file() {
            return Err(format!(
                "unsupported source file type: {}",
                entry.path().display()
            ));
        }
        let path = entry
            .path()
            .strip_prefix(root)
            .map_err(|e| e.to_string())?
            .to_str()
            .ok_or("source path is not UTF-8")?
            .to_string();
        let bytes = std::fs::read(entry.path()).map_err(|e| e.to_string())?;
        #[cfg(unix)]
        let executable = {
            use std::os::unix::fs::PermissionsExt;
            entry
                .metadata()
                .map_err(|e| e.to_string())?
                .permissions()
                .mode()
                & 0o111
                != 0
        };
        #[cfg(not(unix))]
        let executable = false;
        result.insert(
            path,
            SourceFile {
                sha256: hex_digest(&bytes),
                bytes,
                executable,
            },
        );
    }
    Ok(result)
}

/// Files under `root` whose suffix names an interpreter-loadable build artifact.
///
/// Every store excludes these when it writes a generation's tree, and the
/// source inventory skips them, so one present in a canonical tree is a file
/// no patch authorized. A `__pycache__` entry can be imported in place of its
/// source and a sourceless `.pyc` imports as a module. Artifact directories
/// such as caches are descended into, never skipped.
fn loadable_artifacts(root: &Path) -> Vec<String> {
    WalkDir::new(root)
        .follow_links(false)
        .sort_by_file_name()
        .into_iter()
        .flatten()
        .filter(|entry| !entry.file_type().is_dir())
        .filter(|entry| {
            entry.file_name().to_str().is_some_and(|name| {
                scope()
                    .artifact_suffixes
                    .iter()
                    .any(|suffix| name.ends_with(suffix))
            })
        })
        .filter_map(|entry| {
            let path = entry.path().strip_prefix(root).ok()?.to_str()?;
            Some(path.to_string())
        })
        .collect()
}

/// Why a captured record could not be bound: its bytes differ from the
/// recorded digest, or it could not be read as a regular file.
enum Unbound {
    Differs(String),
    Unreadable(String),
}

impl From<Unbound> for String {
    fn from(error: Unbound) -> Self {
        match error {
            Unbound::Differs(detail) | Unbound::Unreadable(detail) => detail,
        }
    }
}

fn bound_bytes(path: &Path, expected_sha256: &str) -> Result<Vec<u8>, Unbound> {
    let unreadable = |error: std::io::Error| Unbound::Unreadable(error.to_string());
    if !digest(expected_sha256) {
        return Err(Unbound::Unreadable("invalid captured digest".into()));
    }
    if !std::fs::symlink_metadata(path)
        .map_err(unreadable)?
        .is_file()
        || path.canonicalize().map_err(unreadable)? != path
    {
        return Err(Unbound::Unreadable(
            "captured record is not a regular file at its canonical location".into(),
        ));
    }
    let bytes = std::fs::read(path).map_err(unreadable)?;
    if hex_digest(&bytes) != expected_sha256 {
        return Err(Unbound::Differs(format!(
            "captured bytes differ: {}",
            path.display()
        )));
    }
    Ok(bytes)
}

/// A policy finding: code, path, and detail.
type PolicyFinding = (&'static str, String, String);

fn policy_binding(detail: impl Into<String>) -> PolicyFinding {
    ("policy_binding", String::new(), detail.into())
}

/// Bind declarations to the policy captured before proposal, without interpreting markers.
///
/// Errors carry their finding code: `contract_binding` (path names the epoch
/// input) when a frozen brief or scoring file differs from the captured
/// digest, `policy_inventory` when the captured parent inventory differs from
/// the observed parent, else `policy_binding`.
fn verify_policy(
    root: &Path,
    parent: &Path,
    epoch: &str,
    parent_id: &str,
    manifest: &Manifest,
    source: &BTreeMap<String, SourceFile>,
) -> Result<(), PolicyFinding> {
    let inventory = |detail: &str| ("policy_inventory", String::new(), detail.to_string());
    let policy = verify_policy_records(root, parent, epoch, parent_id, manifest)?;
    let mut paths = BTreeSet::new();
    for file in &policy.source_files {
        if !safe_relative(&file.path) || !paths.insert(file.path.as_str()) {
            return Err(policy_binding(
                "mutation policy names an invalid or repeated source path",
            ));
        }
        if !source.get(&file.path).is_some_and(|observed| {
            observed.sha256 == file.sha256 && observed.executable == file.executable
        }) {
            return Err(inventory(
                "mutation policy source inventory differs from observed source",
            ));
        }
    }
    if paths.len() != source.len() {
        return Err(inventory(
            "mutation policy does not cover the complete parent inventory",
        ));
    }
    Ok(())
}

/// Check the captured policy's own records and its binding to the manifest.
fn verify_policy_records(
    root: &Path,
    parent: &Path,
    epoch: &str,
    parent_id: &str,
    manifest: &Manifest,
) -> Result<MutationPolicy, PolicyFinding> {
    if !digest(&manifest.policy_sha256) {
        return Err(policy_binding("invalid mutation policy digest"));
    }
    let epoch_dir = root.join("epochs").join(epoch);
    let bytes = bound_bytes(
        &epoch_dir
            .join("generations")
            .join(parent_id)
            .join("mutation-policies")
            .join(format!("{}.json", manifest.policy_sha256)),
        &manifest.policy_sha256,
    )
    .map_err(policy_binding)?;
    let policy: MutationPolicy =
        serde_json::from_slice(&bytes).map_err(|error| policy_binding(error.to_string()))?;
    if policy.format_version != 1
        || policy.epoch_id != epoch
        || policy.parent_generation_id != parent_id
        || policy.parent_source != manifest.parent_source
    {
        return Err(policy_binding(
            "mutation policy does not name the selected parent source",
        ));
    }
    for (name, expected) in [
        ("brief.md", &policy.brief_sha256),
        ("scoring.json", &policy.scoring_sha256),
    ] {
        bound_bytes(&epoch_dir.join(name), expected).map_err(|error| match error {
            Unbound::Differs(detail) => ("contract_binding", name.to_string(), detail),
            Unbound::Unreadable(detail) => policy_binding(detail),
        })?;
    }
    verify_declarations(parent, manifest, &policy).map_err(policy_binding)?;
    Ok(policy)
}

/// Check the policy's enumeration roots and bind the manifest's parent
/// declarations to the policy's points.
fn verify_declarations(
    parent: &Path,
    manifest: &Manifest,
    policy: &MutationPolicy,
) -> Result<(), String> {
    let mut roots = BTreeSet::new();
    for value in &policy.enumeration_roots {
        if !(value == "." || safe_relative(value)) || !roots.insert(value.as_str()) {
            return Err("invalid or repeated mutation enumeration root".into());
        }
        let path = if value == "." {
            parent.to_path_buf()
        } else {
            parent.join(value)
        };
        if path.canonicalize().map_err(|error| error.to_string())? != path {
            return Err("mutation enumeration root resolves through a link".into());
        }
    }
    if roots.is_empty() {
        return Err("mutation enumeration roots are missing".into());
    }
    let mut points = BTreeMap::new();
    for point in &policy.points {
        if !safe_relative(&point.path)
            || !roots
                .iter()
                .any(|root| *root == "." || Path::new(&point.path).starts_with(root))
            || points
                .insert((point.id.as_str(), point.op.as_str()), point)
                .is_some()
        {
            return Err("mutation policy contains invalid or repeated declarations".into());
        }
    }
    let mut observed = BTreeSet::new();
    for file in &manifest.files {
        for span in &file.spans {
            let key = (span.id.as_str(), span.op.as_str());
            if !observed.insert(key)
                || !points
                    .get(&key)
                    .is_some_and(|point| point.matches(&file.path, span))
            {
                return Err("manifest parent declarations differ from the captured policy".into());
            }
        }
    }
    if observed.len() != points.len() {
        return Err("manifest omits captured mutation declarations".into());
    }
    Ok(())
}

/// Check one manifest against independently selected coordinates and source roots.
pub fn attest(
    workspace: &Path,
    epoch: &str,
    parent_id: &str,
    child_id: &str,
    parent_root: &Path,
    child_root: &Path,
    generation_dir: &Path,
) -> Attestation {
    let mut result = Attestation::new(epoch, parent_id, child_id);
    if !generation_ids_valid(&[epoch, parent_id, child_id]) {
        result.finding(
            "manifest_coordinates",
            "",
            "invalid selected generation coordinates",
        );
        return result;
    }
    let manifest: Manifest = match std::fs::read(generation_dir.join("containment.json")) {
        Ok(bytes) => match serde_json::from_slice(&bytes) {
            Ok(record) => record,
            Err(error) => {
                result.finding("manifest_shape", "", &error.to_string());
                return result;
            }
        },
        Err(error) => {
            result.finding("manifest_missing", "", &error.to_string());
            return result;
        }
    };
    if manifest.format_version != 1 {
        result.finding("manifest_version", "", "unsupported version");
        return result;
    }
    if manifest.patches.is_empty() || !manifest.files.iter().any(|file| !file.spans.is_empty()) {
        result.finding(
            "mutation_snapshot",
            "",
            "accepted patches and their mutation snapshot are required",
        );
    }
    if (
        manifest.epoch_id.as_str(),
        manifest.parent_generation_id.as_str(),
        manifest.generation_id.as_str(),
    ) != (epoch, parent_id, child_id)
    {
        result.finding(
            "manifest_coordinates",
            "",
            "manifest does not name the selected parent and child",
        );
    }
    let canonical = [
        ("source_unreadable", workspace),
        ("parent_unreadable", parent_root),
        ("source_unreadable", child_root),
    ]
    .map(|(code, path)| {
        path.canonicalize()
            .map_err(|error| (code, error.to_string()))
    });
    let [root, parent, child] = match canonical {
        [Ok(root), Ok(parent), Ok(child)] => [root, parent, child],
        [Err((code, error)), ..] | [_, Err((code, error)), _] | [.., Err((code, error))] => {
            result.finding(code, "", &error);
            return result;
        }
    };
    if !safe_relative(&manifest.parent_source)
        || !safe_relative(&manifest.child_source)
        || parent.strip_prefix(&root).ok() != Some(Path::new(&manifest.parent_source))
        || child.strip_prefix(&root).ok() != Some(Path::new(&manifest.child_source))
    {
        result.finding(
            "manifest_source",
            "",
            "source locations differ from the selected generation store",
        );
        return result;
    }
    let (left, right) = match (read_tree(&parent), read_tree(&child)) {
        (Ok(left), Ok(right)) => (left, right),
        (Err(error), _) => {
            result.finding("parent_unreadable", "", &error);
            return result;
        }
        (_, Err(error)) => {
            result.finding("source_unreadable", "", &error);
            return result;
        }
    };
    for path in loadable_artifacts(&child) {
        result.finding(
            "artifact_present",
            &path,
            "a canonical source tree holds no interpreter-loadable build artifacts",
        );
    }
    // A policy finding does not stop the remaining checks, so a contradiction
    // in the child's own files is still reported beside it.
    if let Err((code, path, error)) =
        verify_policy(&root, &parent, epoch, parent_id, &manifest, &left)
    {
        result.finding(code, &path, &error);
    }
    let recorded: BTreeSet<_> = manifest
        .files
        .iter()
        .map(|file| file.path.as_str())
        .collect();
    if recorded.len() != manifest.files.len()
        || right.keys().any(|path| !recorded.contains(path.as_str()))
    {
        result.finding(
            "source_inventory",
            "",
            "manifest does not cover the complete observed child inventory",
        );
    }
    if left.keys().any(|path| !recorded.contains(path.as_str())) {
        result.finding(
            "parent_inventory",
            "",
            "manifest does not cover the complete observed parent inventory",
        );
    }
    let mut operation_ids = BTreeSet::new();
    let mut child_operation_ids = BTreeSet::new();
    let mut forbidden_ids = BTreeSet::new();
    for file in &manifest.files {
        if !safe_relative(&file.path) {
            result.finding("manifest_path", &file.path, "invalid source path");
            continue;
        }
        let old = left.get(&file.path);
        let new = right.get(&file.path);
        let parent_bound = old.map(|f| &f.sha256) == file.parent_sha256.as_ref()
            && old.map(|f| f.executable) == file.parent_executable;
        let child_bound = new.map(|f| &f.sha256) == file.child_sha256.as_ref()
            && new.map(|f| f.executable) == file.child_executable;
        if !parent_bound {
            result.finding(
                "parent_binding",
                &file.path,
                "observed parent hash or executable state differs",
            );
        }
        if !child_bound {
            result.finding(
                "source_binding",
                &file.path,
                "observed child hash or executable state differs",
            );
        }
        if !(parent_bound && child_bound) {
            continue;
        }
        if old.is_none() || new.is_none() {
            result.finding(
                "file_set",
                &file.path,
                "patches do not authorize file creation or deletion",
            );
        }
        if file.parent_executable != file.child_executable {
            result.finding(
                "metadata",
                &file.path,
                "patches do not authorize executable state changes",
            );
        }
        let before = old.map(|f| f.bytes.as_slice()).unwrap_or_default();
        let after = new.map(|f| f.bytes.as_slice()).unwrap_or_default();
        let mut spans_valid = true;
        for span in &file.spans {
            if !operation_ids.insert((span.id.as_str(), span.op.as_str()))
                || span.id.is_empty()
                || !matches!(span.kind.as_str(), "span" | "file" | "code")
                || !matches!(span.op.as_str(), "replace" | "set_numeric" | "set_enum")
                || span.start > span.end
                || span.end > before.len()
                || !digest(&span.content_hash)
                || hex_digest(&before[span.start.min(before.len())..span.end.min(before.len())])
                    != span.source_sha256
                || !match (span.child_start, span.child_end, &span.child_source_sha256) {
                    (None, None, None) => true,
                    (Some(start), Some(end), Some(hash)) => {
                        start <= end
                            && end <= after.len()
                            && hex_digest(&after[start..end]) == *hash
                    }
                    _ => false,
                }
            {
                result.finding(
                    "mutation_snapshot",
                    &file.path,
                    "invalid, repeated, or mismatched mutation span",
                );
                spans_valid = false;
            }
            if span.child_start.is_some() {
                child_operation_ids.insert((span.id.as_str(), span.op.as_str()));
            }
            if span.forbidden {
                forbidden_ids.insert(span.id.as_str());
            }
        }
        if !spans_valid {
            continue;
        }
        if file.spans.iter().any(|span| {
            span.forbidden && Some(&span.source_sha256) != span.child_source_sha256.as_ref()
        }) {
            result.finding(
                "forbidden",
                &file.path,
                "a forbidden mutation unit's bytes changed",
            );
        }
        let (mut parent_offset, mut child_offset) = (0, 0);
        let mut coordinates_valid = true;
        for change in &file.changes {
            if !(parent_offset <= change.parent_start
                && change.parent_start <= change.parent_end
                && change.parent_end <= before.len()
                && child_offset <= change.child_start
                && change.child_start <= change.child_end
                && change.child_end <= after.len())
            {
                result.finding(
                    "manifest_shape",
                    &file.path,
                    "invalid or overlapping change coordinates",
                );
                coordinates_valid = false;
                break;
            }
            if before[parent_offset..change.parent_start] != after[child_offset..change.child_start]
            {
                result.finding(
                    "byte_coverage",
                    &file.path,
                    "unrecorded source bytes changed",
                );
            }
            if !file.spans.iter().any(|span| !span.forbidden && span.start <= change.parent_start && change.parent_end <= span.end
                && matches!((span.child_start, span.child_end), (Some(start), Some(end)) if start <= change.child_start && change.child_end <= end)) {
                result.finding("outside_mutation", &file.path, "changed bytes are outside allowed mutation units");
            }
            if file.spans.iter().any(|span| {
                span.forbidden
                    && if change.parent_start == change.parent_end {
                        (span.start < change.parent_start && change.parent_start < span.end)
                            || (span.start == span.end && span.start == change.parent_start)
                    } else {
                        change.parent_start < span.end && span.start < change.parent_end
                    }
            }) {
                result.finding(
                    "forbidden",
                    &file.path,
                    "changed bytes overlap a forbidden mutation unit",
                );
            }
            parent_offset = change.parent_end;
            child_offset = change.child_end;
        }
        // Malformed coordinates leave no recorded suffix to compare.
        if coordinates_valid && before[parent_offset..] != after[child_offset..] {
            result.finding(
                "byte_coverage",
                &file.path,
                "unrecorded source suffix changed",
            );
        }
    }
    verify_patches(
        generation_dir,
        &manifest,
        &operation_ids,
        &child_operation_ids,
        &forbidden_ids,
        &mut result,
    );
    result
}

fn verify_patches(
    generation_dir: &Path,
    manifest: &Manifest,
    operation_ids: &BTreeSet<(&str, &str)>,
    child_operation_ids: &BTreeSet<(&str, &str)>,
    forbidden_ids: &BTreeSet<&str>,
    result: &mut Attestation,
) {
    let experiment: serde_json::Value = match std::fs::read(generation_dir.join("experiment.json"))
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
    {
        Some(body) => body,
        None => {
            result.finding(
                "patch_binding",
                "",
                "experiment record is missing or malformed",
            );
            return;
        }
    };
    if experiment["epoch_id"] != result.epoch_id
        || experiment["generation_id"] != result.generation_id
        || experiment["parent_generation_id"] != result.parent_generation_id
    {
        result.finding(
            "manifest_coordinates",
            "",
            "experiment does not name the selected parent and child",
        );
    }
    let ids: Vec<_> = manifest
        .patches
        .iter()
        .map(|patch| patch.id.as_str())
        .collect();
    if experiment["patch_ids"] != serde_json::json!(ids)
        || ids.iter().collect::<BTreeSet<_>>().len() != ids.len()
    {
        result.finding(
            "patch_binding",
            "",
            "patch identities differ from the experiment",
        );
    }
    for patch in &manifest.patches {
        if !safe_relative(&patch.id) || patch.id.contains('/') || !digest(&patch.sha256) {
            result.finding("patch_binding", "", "invalid patch binding");
            continue;
        }
        let bytes = match std::fs::read(
            generation_dir
                .join("patches")
                .join(format!("{}.json", patch.id)),
        ) {
            Ok(bytes) => bytes,
            Err(_) => {
                result.finding("patch_binding", "", "patch record is missing");
                continue;
            }
        };
        if hex_digest(&bytes) != patch.sha256 {
            result.finding(
                "patch_binding",
                "",
                "patch record differs from the manifest",
            );
        }
        let body: serde_json::Value = match serde_json::from_slice(&bytes) {
            Ok(body) => body,
            Err(_) => {
                result.finding("patch_binding", "", "patch record is malformed");
                continue;
            }
        };
        let target = body["mutation_id"].as_str().unwrap_or("");
        let operation = body["op"].as_str().unwrap_or("");
        if body["id"] != patch.id || !operation_ids.contains(&(target, operation)) {
            result.finding(
                "patch_binding",
                "",
                "patch does not resolve against the mutation snapshot",
            );
        } else if !child_operation_ids.contains(&(target, operation)) {
            result.finding(
                "mutation_snapshot",
                "",
                "patched operation no longer resolves in the child",
            );
        } else if forbidden_ids.contains(target) {
            result.finding("forbidden", "", "patch targets a forbidden mutation unit");
        }
    }
}

fn generation_ids_valid(ids: &[&str]) -> bool {
    ids.iter().all(|id| safe_relative(id) && !id.contains('/'))
}

/// The configured generation store's name (`generation_source_backend`).
fn store_backend(paths: &WorkspacePaths) -> Option<String> {
    let bytes = std::fs::read(paths.workspace.join("config.json")).ok()?;
    let config: serde_json::Value = serde_json::from_slice(&bytes).ok()?;
    config["generation_source_backend"]
        .as_str()
        .map(str::to_string)
}

/// Where the configured store materializes one generation's source tree.
fn source_root(
    paths: &WorkspacePaths,
    backend: Option<&str>,
    epoch: &str,
    generation: &str,
) -> Option<PathBuf> {
    match backend? {
        "git" => Some(
            paths
                .workspace
                .join("repo-worktrees")
                .join(epoch)
                .join(generation),
        ),
        "directory" => Some(
            paths
                .epochs
                .join(epoch)
                .join("generations")
                .join(generation)
                .join("snapshot"),
        ),
        _ => None,
    }
}

/// Resolve source locations from the configured generation store, not the manifest.
pub fn attest_generation(
    paths: &WorkspacePaths,
    epoch: &str,
    parent: &str,
    child: &str,
) -> Attestation {
    attest_in_store(paths, store_backend(paths).as_deref(), epoch, parent, child)
}

fn attest_in_store(
    paths: &WorkspacePaths,
    backend: Option<&str>,
    epoch: &str,
    parent: &str,
    child: &str,
) -> Attestation {
    if generation_ids_valid(&[epoch, parent, child]) {
        if let (Some(parent_root), Some(child_root)) = (
            source_root(paths, backend, epoch, parent),
            source_root(paths, backend, epoch, child),
        ) {
            return attest(
                &paths.workspace,
                epoch,
                parent,
                child,
                &parent_root,
                &child_root,
                &paths.epochs.join(epoch).join("generations").join(child),
            );
        }
    }
    let mut result = Attestation::new(epoch, parent, child);
    result.finding(
        "source_store",
        "",
        "selected source store or generation coordinates are unavailable",
    );
    result
}

/// Append one path's identity, size, mode, and change times to `out`.
///
/// Any write, rename, or permission change moves the status-change time,
/// which a writer cannot set back, so equal bytes mean an unchanged entry.
fn push_metadata(out: &mut Vec<u8>, label: &[u8], path: &Path) {
    use std::os::unix::fs::MetadataExt;
    out.extend_from_slice(label);
    out.push(0);
    match std::fs::symlink_metadata(path) {
        Ok(meta) => {
            for value in [
                meta.ino() as i64,
                meta.mode() as i64,
                meta.size() as i64,
                meta.mtime(),
                meta.mtime_nsec(),
                meta.ctime(),
                meta.ctime_nsec(),
            ] {
                out.extend_from_slice(&value.to_le_bytes());
            }
        }
        Err(_) => out.push(b'!'),
    }
}

/// Append the metadata of every entry under `root`, artifacts included,
/// because the verifier reports artifact files as well as source.
fn push_tree(out: &mut Vec<u8>, root: &Path) {
    use std::os::unix::ffi::OsStrExt;
    for item in WalkDir::new(root).follow_links(false).sort_by_file_name() {
        match item {
            Ok(entry) => {
                let label = entry.path().strip_prefix(root).unwrap_or(entry.path());
                push_metadata(out, label.as_os_str().as_bytes(), entry.path());
            }
            Err(_) => out.push(b'!'),
        }
    }
}

/// Coordinates of one parent-to-child pair: epoch, parent, child.
type Pair = (String, String, String);

/// A pair's result as the previous scan, or its health file, left it.
#[derive(Clone, Deserialize)]
struct Prior {
    status: Status,
    #[serde(default)]
    evidence_withdrawn: bool,
    #[serde(default)]
    introduced_by: Option<String>,
    #[serde(default)]
    introduced_by_input: Option<String>,
}

impl Prior {
    fn of(attestation: &Attestation) -> Self {
        Self {
            status: attestation.status,
            evidence_withdrawn: attestation.evidence_withdrawn,
            introduced_by: attestation.introduced_by.clone(),
            introduced_by_input: attestation.introduced_by_input.clone(),
        }
    }

    /// The pair's own alarm. A pair attributed to an ancestor, or an
    /// unverified pair explained by a changed epoch input, has none.
    fn alarm(&self) -> Option<Alarm> {
        if self.introduced_by.is_some() {
            return None;
        }
        match self.status {
            Status::Violated => Some(Alarm::Violated),
            Status::EvidenceMismatch => Some(Alarm::EvidenceMismatch),
            Status::Unverified if self.evidence_withdrawn && self.introduced_by_input.is_none() => {
                Some(Alarm::EvidenceWithdrawn)
            }
            _ => None,
        }
    }
}

/// A pair's own alarm: the condition its alert reports.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Alarm {
    Violated,
    EvidenceMismatch,
    EvidenceWithdrawn,
}

impl Alarm {
    const ALL: [Self; 3] = [
        Self::Violated,
        Self::EvidenceMismatch,
        Self::EvidenceWithdrawn,
    ];

    fn as_str(self) -> &'static str {
        match self {
            Self::Violated => "violated",
            Self::EvidenceMismatch => "evidence_mismatch",
            Self::EvidenceWithdrawn => "evidence_withdrawn",
        }
    }

    fn parse(value: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|alarm| alarm.as_str() == value)
    }
}

/// Verifies lineage pairs, remembers each pair's result, and alerts on alarms.
///
/// A finished pair (one with a recorded decision) reuses its result while one
/// digest over the store selection, the metadata of every entry in both
/// source trees, the manifest, experiment and patch records, the parent's
/// captured policies, and the frozen brief and scoring is unchanged. Entry
/// metadata includes the status-change time, which any write moves and no
/// writer can set back. A rewrite that keeps size and inode and lands within
/// the file system's timestamp granularity of the previous change can keep
/// the digest, so such a change is found only when another input changes.
/// Generations still in flight are verified on every scan.
///
/// A pair first seen in this process takes its prior result from its health
/// file, so evidence withdrawn while the supervisor was stopped is still
/// detected.
#[derive(Default)]
pub struct ContainmentAudit {
    reusable: HashMap<Pair, (String, Attestation)>,
    priors: HashMap<(String, String), Prior>,
    alarms: HashMap<(String, String), Alarm>,
    /// Epoch evidence inputs reported as changed, keyed by epoch and input.
    input_alarms: BTreeSet<(String, String)>,
}

impl ContainmentAudit {
    pub fn new() -> Self {
        Self::default()
    }

    /// An audit whose standing alarms are the ones the ledger records, so a
    /// restarted loop records none of them a second time.
    pub fn from_alarms(recorded: crate::ledger::ContainmentAlarms) -> Self {
        let alarms = recorded
            .pairs
            .into_iter()
            .filter_map(|(pair, alarm)| Some((pair, Alarm::parse(&alarm)?)))
            .collect();
        Self {
            alarms,
            input_alarms: recorded.inputs.into_iter().collect(),
            ..Self::default()
        }
    }

    /// Verify every lineage pair, persist each result, and alert on new alarms.
    pub fn tick(
        &mut self,
        paths: &WorkspacePaths,
        lineage: &LineageView,
        ledger: Option<&crate::ledger::AuditLedger>,
    ) -> ContainmentView {
        let view = self.scan(paths, lineage);
        for attestation in &view.attestations {
            write_finding(paths, attestation);
        }
        self.alert(&view, ledger);
        view
    }

    pub fn scan(&mut self, paths: &WorkspacePaths, lineage: &LineageView) -> ContainmentView {
        let backend = store_backend(paths);
        let mut trees: HashMap<(&str, &str), Vec<u8>> = HashMap::new();
        let mut reusable = HashMap::new();
        let mut priors = HashMap::new();
        let mut view = ContainmentView {
            scanned: true,
            ..Default::default()
        };
        for generation in &lineage.generations {
            let Some(parent) = generation.parent_generation_id.as_deref() else {
                continue;
            };
            let (epoch, child) = (
                generation.epoch_id.as_str(),
                generation.generation_id.as_str(),
            );
            let pair = (epoch.to_string(), parent.to_string(), child.to_string());
            let key = generation.promoted.is_some().then(|| {
                let mut input = backend.clone().unwrap_or_default().into_bytes();
                for id in [parent, child] {
                    let tree = trees.entry((epoch, id)).or_insert_with(|| {
                        let mut tree = Vec::new();
                        if let Some(root) = source_root(paths, backend.as_deref(), epoch, id) {
                            push_tree(&mut tree, &root);
                        }
                        tree
                    });
                    input.push(0);
                    input.extend_from_slice(id.as_bytes());
                    input.extend_from_slice(tree);
                }
                let epoch_dir = paths.epochs.join(epoch);
                let generation_dir = epoch_dir.join("generations").join(child);
                for name in ["containment.json", "experiment.json"] {
                    push_metadata(&mut input, name.as_bytes(), &generation_dir.join(name));
                }
                push_tree(&mut input, &generation_dir.join("patches"));
                push_tree(
                    &mut input,
                    &epoch_dir
                        .join("generations")
                        .join(parent)
                        .join("mutation-policies"),
                );
                for name in ["brief.md", "scoring.json"] {
                    push_metadata(&mut input, name.as_bytes(), &epoch_dir.join(name));
                }
                hex_digest(&input)
            });
            let mut attestation = match (&key, self.reusable.remove(&pair)) {
                (Some(key), Some((previous, attestation))) if *key == previous => {
                    view.reused += 1;
                    attestation
                }
                _ => attest_in_store(paths, backend.as_deref(), epoch, parent, child),
            };
            if let Some(key) = key {
                reusable.insert(pair, (key, attestation.clone()));
            }
            let coordinates = (epoch.to_string(), child.to_string());
            let prior = self.priors.get(&coordinates).cloned().or_else(|| {
                // First sight in this process: the health file seeds the
                // standing alarms the ledger did not record, so a restart
                // without a ledger does not repeat them either.
                let prior = read_prior(paths, epoch, child)?;
                if let Some(alarm) = prior.alarm() {
                    self.alarms.entry(coordinates.clone()).or_insert(alarm);
                }
                if let Some(input) = &prior.introduced_by_input {
                    self.input_alarms.insert((epoch.to_string(), input.clone()));
                }
                Some(prior)
            });
            // Evidence verified on an earlier scan that cannot be verified now
            // was withdrawn. The exceptions are a generation still in flight,
            // whose records a resume may be discarding, and a rejected
            // generation whose source tree an operator pruned.
            let pruned = generation.promoted == Some(false)
                && source_root(paths, backend.as_deref(), epoch, child)
                    .is_none_or(|root| std::fs::symlink_metadata(root).is_err());
            attestation.evidence_withdrawn = attestation.status == Status::Unverified
                && generation.promoted.is_some()
                && !pruned
                && prior.is_some_and(|p| p.status != Status::Unverified || p.evidence_withdrawn);
            attestation.introduced_by_input = attestation
                .findings
                .iter()
                .find(|f| f.code == "contract_binding")
                .map(|f| f.path.clone());
            view.attestations.push(attestation);
        }
        self.reusable = reusable;
        attribute(&mut view.attestations);
        for attestation in &view.attestations {
            priors.insert(
                (
                    attestation.epoch_id.clone(),
                    attestation.generation_id.clone(),
                ),
                Prior::of(attestation),
            );
        }
        self.priors = priors;
        for attestation in &view.attestations {
            *match attestation.status {
                Status::Contained => &mut view.contained,
                Status::Violated => &mut view.violated,
                Status::EvidenceMismatch => &mut view.evidence_mismatch,
                Status::Unverified => &mut view.unverified,
            } += 1;
            view.evidence_withdrawn += u64::from(attestation.evidence_withdrawn);
        }
        view
    }

    /// Log and ledger each pair whose own finding moved into an alarm.
    ///
    /// The alarms are `violated`, `evidence_mismatch`, and `evidence_withdrawn`
    /// (an unverified pair whose evidence verified on an earlier scan). A pair
    /// alarms when it enters one or moves between two; a standing alarm is
    /// reported once, including across a restart through the health file. A
    /// pair whose finding is attributed to an ancestor stays silent: the
    /// ancestor alarms.
    fn alert(&mut self, view: &ContainmentView, ledger: Option<&crate::ledger::AuditLedger>) {
        let mut inputs: BTreeMap<(String, String), Vec<&str>> = BTreeMap::new();
        for attestation in &view.attestations {
            if let Some(input) = &attestation.introduced_by_input {
                inputs
                    .entry((attestation.epoch_id.clone(), input.clone()))
                    .or_default()
                    .push(&attestation.generation_id);
            }
        }
        for ((epoch, input), generations) in &inputs {
            if self.input_alarms.contains(&(epoch.clone(), input.clone())) {
                continue;
            }
            tracing::warn!(
                epoch_id = %epoch,
                input = %input,
                generations = ?generations,
                "MUTATION-CONTAINMENT ALERT: an epoch evidence input differs from its captured digest",
            );
            if let Some(ledger) = ledger {
                ledger.append(
                    crate::ledger::RecordKind::DiffContainmentAlert,
                    serde_json::json!({
                        "epoch_id": epoch,
                        "alarm": "epoch_evidence_changed",
                        "input": input,
                        "generations": generations,
                    }),
                );
            }
        }
        self.input_alarms = inputs.into_keys().collect();
        let mut alarms = HashMap::new();
        for attestation in &view.attestations {
            let Some(alarm) = Prior::of(attestation).alarm() else {
                continue;
            };
            let key = (
                attestation.epoch_id.clone(),
                attestation.generation_id.clone(),
            );
            let previous = self.alarms.get(&key).copied();
            alarms.insert(key, alarm);
            if previous == Some(alarm) {
                continue;
            }
            let status = serde_json::to_value(attestation.status).unwrap_or_default();
            tracing::warn!(
                epoch_id = %attestation.epoch_id,
                generation_id = %attestation.generation_id,
                parent = %attestation.parent_generation_id,
                alarm = alarm.as_str(),
                findings = ?attestation.findings,
                "MUTATION-CONTAINMENT ALERT: generation source differs from its mutation evidence",
            );
            if let Some(ledger) = ledger {
                ledger.append(
                    crate::ledger::RecordKind::DiffContainmentAlert,
                    serde_json::json!({
                        "epoch_id": attestation.epoch_id,
                        "generation_id": attestation.generation_id,
                        "parent_generation_id": attestation.parent_generation_id,
                        "alarm": alarm.as_str(),
                        "status": status,
                        "findings": attestation.findings,
                    }),
                );
            }
        }
        self.alarms = alarms;
    }
}

/// The result a pair's health file records, when it parses.
fn read_prior(paths: &WorkspacePaths, epoch: &str, generation: &str) -> Option<Prior> {
    if !generation_ids_valid(&[epoch, generation]) {
        return None;
    }
    let bytes = std::fs::read(
        paths
            .epoch_health_dir(epoch)
            .join(format!("mutation_containment_{generation}.json")),
    )
    .ok()?;
    serde_json::from_slice(&bytes).ok()
}

/// Attribute a child's parent-tree findings to its parent.
///
/// The orchestrator captures a child's policy from the parent tree before
/// proposal, and the child's manifest records the parent's bytes. When that
/// tree later differs from those records, or can no longer be read, and the
/// parent's own pair is violated, mismatched, or withdrawn, the parent's
/// change explains the child's finding. The child keeps its own alarm when it
/// also has a finding about its own files. A root parent has no pair, so
/// nothing corroborates a change to its tree.
fn attribute(attestations: &mut [Attestation]) {
    let alarmed: BTreeSet<(String, String)> = attestations
        .iter()
        .filter(|a| {
            matches!(a.status, Status::Violated | Status::EvidenceMismatch) || a.evidence_withdrawn
        })
        .map(|a| (a.epoch_id.clone(), a.generation_id.clone()))
        .collect();
    for attestation in attestations.iter_mut() {
        let parent = (
            attestation.epoch_id.clone(),
            attestation.parent_generation_id.clone(),
        );
        let has = |codes: &[&str]| {
            attestation
                .findings
                .iter()
                .any(|f| codes.contains(&f.code.as_str()))
        };
        let inherited = has(&PARENT_CODES) && !has(&OWN_CODES) && !has(&VIOLATION_CODES);
        attestation.introduced_by = (inherited && alarmed.contains(&parent)).then_some(parent.1);
    }
}

/// The latest containment scan, shared with `/statusz`.
#[derive(Debug, Clone, Default, Serialize)]
pub struct ContainmentView {
    /// `true` once at least one scan has run.
    pub scanned: bool,
    /// When the findings store recorded this result (RFC-3339), so a reader
    /// can tell a current result from a stale one.
    pub scanned_at: Option<String>,
    pub contained: u64,
    pub violated: u64,
    pub evidence_mismatch: u64,
    pub unverified: u64,
    /// Unverified pairs whose evidence verified on an earlier scan.
    pub evidence_withdrawn: u64,
    /// Finished pairs whose earlier result was reused because no input changed.
    pub reused: u64,
    /// One result per parent-to-child pair, including contained pairs.
    pub attestations: Vec<Attestation>,
}

/// The shared store the integrity loop writes and `/statusz` reads.
#[derive(Debug, Default)]
pub struct ContainmentFindings {
    inner: Mutex<ContainmentView>,
}

impl ContainmentFindings {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn record(&self, mut view: ContainmentView) {
        view.scanned_at = Some(chrono::Utc::now().to_rfc3339());
        *self.inner.lock().unwrap_or_else(|p| p.into_inner()) = view;
    }

    pub fn view(&self) -> ContainmentView {
        self.inner.lock().unwrap_or_else(|p| p.into_inner()).clone()
    }
}

/// Persist an alarm or an unverified result without changing promotion policy.
pub fn write_finding(paths: &WorkspacePaths, result: &Attestation) {
    if !generation_ids_valid(&[&result.epoch_id, &result.generation_id]) {
        return;
    }
    let path = paths.epoch_health_dir(&result.epoch_id).join(format!(
        "mutation_containment_{}.json",
        result.generation_id
    ));
    let Ok(bytes) = serde_json::to_vec_pretty(result) else {
        return;
    };
    if std::fs::read(&path).is_ok_and(|existing| existing == bytes) {
        return;
    }
    let Some(parent) = path.parent() else { return };
    let temporary = path.with_extension("json.tmp");
    if std::fs::create_dir_all(parent)
        .and_then(|_| std::fs::write(&temporary, &bytes))
        .and_then(|_| std::fs::rename(&temporary, &path))
        .is_err()
    {
        tracing::warn!(path=%path.display(), "could not persist mutation containment finding");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unhex(value: &str) -> Vec<u8> {
        value
            .as_bytes()
            .as_chunks::<2>()
            .0
            .iter()
            .map(|pair| u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap())
            .collect()
    }

    fn write_tree(root: &Path, files: &serde_json::Value) {
        std::fs::create_dir_all(root).unwrap();
        for (name, file) in files.as_object().unwrap() {
            let path = root.join(name);
            std::fs::create_dir_all(path.parent().unwrap()).unwrap();
            std::fs::write(&path, unhex(file["hex"].as_str().unwrap())).unwrap();
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                std::fs::set_permissions(
                    &path,
                    std::fs::Permissions::from_mode(if file["executable"].as_bool().unwrap() {
                        0o755
                    } else {
                        0o644
                    }),
                )
                .unwrap();
            }
        }
    }

    fn corpus() -> serde_json::Value {
        serde_json::from_str(include_str!(
            "../../../tests/fixtures/mutation_containment.json"
        ))
        .unwrap()
    }

    fn fixture(case: &serde_json::Value) -> tempfile::TempDir {
        let directory = tempfile::TempDir::new().unwrap();
        let root = directory.path();
        write_tree(&root.join("parent"), &case["parent"]);
        write_tree(&root.join("child"), &case["child"]);
        let epoch = root.join("epochs/epoch");
        std::fs::create_dir_all(&epoch).unwrap();
        for (key, name) in [("brief", "brief.md"), ("scoring", "scoring.json")] {
            if let Some(bytes) = case[key].as_str() {
                std::fs::write(epoch.join(name), unhex(bytes)).unwrap();
            }
        }
        if let Some(bytes) = case["policy"].as_str() {
            if let Some(hash) = case["manifest"]["policy_sha256"]
                .as_str()
                .filter(|hash| digest(hash))
            {
                let policies = epoch.join("generations/v0/mutation-policies");
                std::fs::create_dir_all(&policies).unwrap();
                std::fs::write(policies.join(format!("{hash}.json")), unhex(bytes)).unwrap();
            }
        }
        let generation = root.join("generation");
        std::fs::create_dir_all(generation.join("patches")).unwrap();
        std::fs::write(
            generation.join("experiment.json"),
            serde_json::to_vec(&case["experiment"]).unwrap(),
        )
        .unwrap();
        if !case["manifest"].is_null() {
            std::fs::write(
                generation.join("containment.json"),
                serde_json::to_vec(&case["manifest"]).unwrap(),
            )
            .unwrap();
        }
        for (id, bytes) in case["patch_records"].as_object().unwrap() {
            std::fs::write(
                generation.join("patches").join(format!("{id}.json")),
                unhex(bytes.as_str().unwrap()),
            )
            .unwrap();
        }
        directory
    }

    fn attest_fixture(root: &Path) -> Attestation {
        attest(
            root,
            "epoch",
            "v0",
            "v1",
            &root.join("parent"),
            &root.join("child"),
            &root.join("generation"),
        )
    }

    #[test]
    fn missing_captured_policy_is_unverified() {
        let mut case = corpus()["cases"][0].clone();
        case["manifest"]
            .as_object_mut()
            .unwrap()
            .remove("policy_sha256");
        let directory = fixture(&case);
        let result = attest_fixture(directory.path());
        assert_eq!(result.status, Status::Unverified, "{:?}", result.findings);
    }

    #[cfg(unix)]
    #[test]
    fn captured_record_links_are_unverified_even_with_identical_bytes() {
        use std::os::unix::fs::symlink;

        let case = &corpus()["cases"][0];
        let hash = case["manifest"]["policy_sha256"].as_str().unwrap();
        for name in [
            "brief.md".into(),
            format!("generations/v0/mutation-policies/{hash}.json"),
        ] {
            let directory = fixture(case);
            let root = directory.path();
            assert_eq!(attest_fixture(root).status, Status::Contained);
            let record = root.join("epochs/epoch").join(&name);
            let external = root.join("redirected-record");
            std::fs::rename(&record, &external).unwrap();
            symlink(external, &record).unwrap();
            let result = attest_fixture(root);
            assert_eq!(
                result.status,
                Status::Unverified,
                "{name}: {:?}",
                result.findings
            );
        }
    }

    fn pair(parent: &str, child: &str, code: Option<&str>) -> Attestation {
        let mut result = Attestation::new("epoch", parent, child);
        if let Some(code) = code {
            result.finding(code, "", "");
        }
        result
    }

    #[test]
    fn evidence_status_ranks_contradiction_above_missing_evidence() {
        let status = |codes: &[&str]| {
            let mut result = pair("v0", "v1", None);
            for code in codes {
                result.finding(code, "", "");
            }
            result.status
        };
        assert_eq!(status(&[]), Status::Contained);
        assert_eq!(status(&["outside_mutation", "file_set"]), Status::Violated);
        assert_eq!(
            status(&["source_binding", "patch_binding"]),
            Status::EvidenceMismatch
        );
        assert_eq!(
            status(&["outside_mutation", "source_inventory"]),
            Status::EvidenceMismatch
        );
        assert_eq!(status(&["policy_inventory"]), Status::EvidenceMismatch);
        assert_eq!(
            status(&["outside_mutation", "manifest_missing"]),
            Status::Unverified
        );
    }

    #[test]
    fn a_parent_tree_change_is_attributed_to_the_parent() {
        let mut pairs = vec![
            pair("v0", "v2", Some("source_binding")),
            pair("v2", "v5", Some("policy_inventory")),
            pair("v2", "v6", Some("policy_inventory")),
            // The root has no pair to corroborate a change to its tree.
            pair("v0", "v3", Some("policy_inventory")),
            // A contained parent leaves the child's contradiction its own.
            pair("v0", "v4", None),
            pair("v4", "v7", Some("policy_inventory")),
        ];
        attribute(&mut pairs);
        let introduced: Vec<_> = pairs
            .iter()
            .map(|a| (a.generation_id.as_str(), a.introduced_by.as_deref()))
            .collect();
        assert_eq!(
            introduced,
            [
                ("v2", None),
                ("v5", Some("v2")),
                ("v6", Some("v2")),
                ("v3", None),
                ("v4", None),
                ("v7", None),
            ]
        );
    }

    #[test]
    fn a_withdrawn_or_unreadable_parent_explains_its_children() {
        let mut parent = pair("v0", "v2", Some("manifest_missing"));
        parent.evidence_withdrawn = true;
        let mut pairs = vec![
            parent,
            pair("v2", "v5", Some("parent_unreadable")),
            pair("v2", "v6", Some("parent_binding")),
            // A finding about the child's own files keeps its own alarm.
            pair("v2", "v7", Some("parent_binding")),
        ];
        pairs[3].finding("source_binding", "", "");
        attribute(&mut pairs);
        let introduced: Vec<_> = pairs.iter().map(|a| a.introduced_by.as_deref()).collect();
        assert_eq!(introduced, [None, Some("v2"), Some("v2"), None]);
    }

    #[test]
    fn shared_mutation_and_applier_corpus() {
        let corpus = corpus();
        let cases = corpus["cases"].as_array().unwrap();
        assert!(!cases.is_empty());
        for case in cases {
            let directory = fixture(case);
            let result = attest_fixture(directory.path());
            let expected: Status = serde_json::from_value(case["expected_status"].clone()).unwrap();
            assert_eq!(
                result.status, expected,
                "case {}: {:?}",
                case["name"], result.findings
            );
            let codes: BTreeSet<&str> = result.findings.iter().map(|f| f.code.as_str()).collect();
            let expected: BTreeSet<&str> = case["expected_codes"]
                .as_array()
                .unwrap()
                .iter()
                .map(|code| code.as_str().unwrap())
                .collect();
            assert_eq!(
                codes, expected,
                "case {}: {:?}",
                case["name"], result.findings
            );
        }
    }
}
