//! Cumulative diagnostics for the canonical active-tournament JSONL fold.
//!
//! The active-tournament event log is a single-writer append-only JSONL the
//! orchestrator/runner publish onto, and the supervisor folds it on every
//! read (`reader::read_active_tournament`). Two failure modes are invisible
//! today:
//!
//!   * **Torn writes** — a line that fails to parse as JSON. The fold
//!     currently drops it silently (`filter_map(.. .ok())`), so a writer
//!     that crashed mid-line, or any corruption, leaves no trace. This is
//!     the Rust-drops-vs-Python-raises divergence: the Python reader raises
//!     on a bad line; the Rust reader swallows it. Counting it restores
//!     visibility without changing the lenient behavior.
//!   * **Non-monotonic `seq`** — each event carries a monotonically
//!     increasing `seq`. A gap (or a backwards step) means the writer lost
//!     events or republished out of order. Counting it surfaces lost-event
//!     conditions the fold otherwise hides.
//!
//! These are cumulative *process-lifetime* counters (like the watchdog
//! action log): a restart starts them at zero, which is the honest thing to
//! report. Held behind atomics and shared by `Arc`; `/statusz` reads them.

use serde::Serialize;
use std::sync::atomic::{AtomicU64, Ordering};

/// Per-fold tallies, returned by the pure fold so the caller can fold them
/// into the shared cumulative [`FoldDiagnostics`].
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct FoldStats {
    /// Lines that failed to parse as JSON (torn / corrupt writes).
    pub parse_failures: u64,
    /// Times a parsed event's `seq` was not exactly one greater than the
    /// previous event's `seq` — a gap or a backwards step (lost events /
    /// out-of-order republish). The first event of a fold is never a gap.
    pub seq_gaps: u64,
}

/// Cumulative, thread-safe diagnostics surfaced on `/statusz`.
#[derive(Debug, Default)]
pub struct FoldDiagnostics {
    parse_failures: AtomicU64,
    seq_gaps: AtomicU64,
    /// Number of folds observed (so a reader can tell "0 folds, 0 problems"
    /// apart from "many folds, 0 problems").
    folds: AtomicU64,
}

/// A serializable snapshot of the cumulative counters for `/statusz`.
#[derive(Debug, Clone, Copy, Serialize, Default, PartialEq, Eq)]
pub struct FoldDiagnosticsView {
    /// Cumulative torn-write (JSON parse) failures over the fold path.
    pub parse_failures: u64,
    /// Cumulative non-monotonic-`seq` events over the fold path.
    pub seq_gaps: u64,
    /// Number of folds observed this process lifetime.
    pub folds: u64,
}

impl FoldDiagnostics {
    pub fn new() -> Self {
        Self::default()
    }

    /// Fold one read's [`FoldStats`] into the cumulative counters. Always
    /// counts the fold itself, even when both tallies are zero.
    pub fn record(&self, stats: FoldStats) {
        self.folds.fetch_add(1, Ordering::Relaxed);
        if stats.parse_failures > 0 {
            self.parse_failures
                .fetch_add(stats.parse_failures, Ordering::Relaxed);
        }
        if stats.seq_gaps > 0 {
            self.seq_gaps.fetch_add(stats.seq_gaps, Ordering::Relaxed);
        }
    }

    /// A point-in-time snapshot for serialization.
    pub fn view(&self) -> FoldDiagnosticsView {
        FoldDiagnosticsView {
            parse_failures: self.parse_failures.load(Ordering::Relaxed),
            seq_gaps: self.seq_gaps.load(Ordering::Relaxed),
            folds: self.folds.load(Ordering::Relaxed),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn record_accumulates_across_folds() {
        let d = FoldDiagnostics::new();
        d.record(FoldStats {
            parse_failures: 2,
            seq_gaps: 1,
        });
        d.record(FoldStats {
            parse_failures: 0,
            seq_gaps: 3,
        });
        let v = d.view();
        assert_eq!(v.parse_failures, 2);
        assert_eq!(v.seq_gaps, 4);
        assert_eq!(v.folds, 2);
    }

    #[test]
    fn a_clean_fold_still_counts_the_fold() {
        let d = FoldDiagnostics::new();
        d.record(FoldStats::default());
        let v = d.view();
        assert_eq!(v.parse_failures, 0);
        assert_eq!(v.seq_gaps, 0);
        assert_eq!(v.folds, 1);
    }
}
