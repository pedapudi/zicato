//! Independent byte-range verification of Python's canonical mutation spans.
//!
//! Python owns mutation semantics. This verifier owns source inventory, byte
//! coverage, captured-policy binding, and patch-record binding. Selected source
//! roots and lineage coordinates come from the workspace. Missing or
//! inconsistent evidence is unverified, never contained.

use crate::sha256::hex_digest;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Component, Path};
use std::sync::OnceLock;
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
    Violated,
    Unverified,
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
}

impl Attestation {
    fn finding(&mut self, code: &str, path: &str, detail: &str) {
        self.findings.push(Finding {
            code: code.into(),
            path: path.into(),
            detail: detail.into(),
        });
        self.status = if self.findings.iter().all(|f| {
            matches!(
                f.code.as_str(),
                "outside_mutation" | "forbidden" | "metadata" | "file_set"
            )
        }) {
            Status::Violated
        } else {
            Status::Unverified
        };
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

fn bound_bytes(path: &Path, expected_sha256: &str) -> Result<Vec<u8>, String> {
    if !digest(expected_sha256) {
        return Err("invalid captured digest".into());
    }
    if !std::fs::symlink_metadata(path)
        .map_err(|error| error.to_string())?
        .is_file()
        || path.canonicalize().map_err(|error| error.to_string())? != path
    {
        return Err("captured record is not a regular file at its canonical location".into());
    }
    let bytes = std::fs::read(path).map_err(|error| error.to_string())?;
    if hex_digest(&bytes) != expected_sha256 {
        return Err(format!("captured bytes differ: {}", path.display()));
    }
    Ok(bytes)
}

/// Bind declarations to the policy captured before proposal, without interpreting markers.
fn verify_policy(
    root: &Path,
    parent: &Path,
    epoch: &str,
    parent_id: &str,
    manifest: &Manifest,
    source: &BTreeMap<String, SourceFile>,
) -> Result<(), String> {
    if !digest(&manifest.policy_sha256) {
        return Err("invalid mutation policy digest".into());
    }
    let epoch_dir = root.join("epochs").join(epoch);
    let bytes = bound_bytes(
        &epoch_dir
            .join("generations")
            .join(parent_id)
            .join("mutation-policies")
            .join(format!("{}.json", manifest.policy_sha256)),
        &manifest.policy_sha256,
    )?;
    let policy: MutationPolicy =
        serde_json::from_slice(&bytes).map_err(|error| error.to_string())?;
    if policy.format_version != 1
        || policy.epoch_id != epoch
        || policy.parent_generation_id != parent_id
        || policy.parent_source != manifest.parent_source
    {
        return Err("mutation policy does not name the selected parent source".into());
    }
    bound_bytes(&epoch_dir.join("brief.md"), &policy.brief_sha256)?;
    bound_bytes(&epoch_dir.join("scoring.json"), &policy.scoring_sha256)?;
    let mut paths = BTreeSet::new();
    for file in &policy.source_files {
        if !safe_relative(&file.path)
            || !paths.insert(file.path.as_str())
            || !source.get(&file.path).is_some_and(|observed| {
                observed.sha256 == file.sha256 && observed.executable == file.executable
            })
        {
            return Err("mutation policy source inventory differs from observed source".into());
        }
    }
    if paths.len() != source.len() {
        return Err("mutation policy does not cover the complete parent inventory".into());
    }
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
    let mut result = Attestation {
        epoch_id: epoch.into(),
        parent_generation_id: parent_id.into(),
        generation_id: child_id.into(),
        status: Status::Contained,
        findings: Vec::new(),
    };
    if ![epoch, parent_id, child_id]
        .iter()
        .all(|id| safe_relative(id) && !id.contains('/'))
    {
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
    let canonical = workspace.canonicalize().and_then(|root| {
        Ok((
            root,
            parent_root.canonicalize()?,
            child_root.canonicalize()?,
        ))
    });
    let (root, parent, child) = match canonical {
        Ok(value) => value,
        Err(error) => {
            result.finding("source_unreadable", "", &error.to_string());
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
        (Err(error), _) | (_, Err(error)) => {
            result.finding("source_unreadable", "", &error);
            return result;
        }
    };
    if let Err(error) = verify_policy(&root, &parent, epoch, parent_id, &manifest, &left) {
        result.finding("mutation_policy", "", &error);
        return result;
    }
    let expected: BTreeSet<_> = left
        .keys()
        .chain(right.keys())
        .map(String::as_str)
        .collect();
    let recorded: BTreeSet<_> = manifest
        .files
        .iter()
        .map(|file| file.path.as_str())
        .collect();
    if expected != recorded || recorded.len() != manifest.files.len() {
        result.finding(
            "source_inventory",
            "",
            "manifest does not cover the complete observed file inventory",
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
        if old.map(|f| &f.sha256) != file.parent_sha256.as_ref()
            || new.map(|f| &f.sha256) != file.child_sha256.as_ref()
            || old.map(|f| f.executable) != file.parent_executable
            || new.map(|f| f.executable) != file.child_executable
        {
            result.finding(
                "source_binding",
                &file.path,
                "observed source hashes or executable state differ",
            );
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
        for change in &file.changes {
            if !(parent_offset <= change.parent_start
                && change.parent_start <= change.parent_end
                && change.parent_end <= before.len()
                && child_offset <= change.child_start
                && change.child_start <= change.child_end
                && change.child_end <= after.len())
            {
                result.finding(
                    "byte_coverage",
                    &file.path,
                    "invalid or overlapping change coordinates",
                );
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
        if before[parent_offset..] != after[child_offset..] {
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

/// Resolve source locations from the configured generation store, not the manifest.
pub fn attest_generation(
    paths: &crate::reader::WorkspacePaths,
    epoch: &str,
    parent: &str,
    child: &str,
) -> Attestation {
    let config: serde_json::Value = std::fs::read(paths.workspace.join("config.json"))
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or_default();
    let source = |generation: &str| match config["generation_source_backend"].as_str() {
        Some("git") => Some(
            paths
                .workspace
                .join("repo-worktrees")
                .join(epoch)
                .join(generation),
        ),
        Some("directory") => Some(
            paths
                .epochs
                .join(epoch)
                .join("generations")
                .join(generation)
                .join("snapshot"),
        ),
        _ => None,
    };
    if [epoch, parent, child]
        .iter()
        .all(|id| safe_relative(id) && !id.contains('/'))
    {
        if let (Some(parent_root), Some(child_root)) = (source(parent), source(child)) {
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
    let mut result = Attestation {
        epoch_id: epoch.into(),
        parent_generation_id: parent.into(),
        generation_id: child.into(),
        status: Status::Contained,
        findings: Vec::new(),
    };
    result.finding(
        "source_store",
        "",
        "selected source store or generation coordinates are unavailable",
    );
    result
}

/// Persist an alarm or an unverified result without changing promotion policy.
pub fn write_finding(paths: &crate::reader::WorkspacePaths, result: &Attestation) {
    if ![result.epoch_id.as_str(), result.generation_id.as_str()]
        .iter()
        .all(|id| safe_relative(id) && !id.contains('/'))
    {
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
        }
    }
}
