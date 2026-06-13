//! `zicato-supervisor` entry point.

use clap::Parser;
use std::net::IpAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tokio::signal::unix::{signal, SignalKind};
use tokio::sync::broadcast;
use tracing::{error, info};
use zicato_supervisor::{action_log::WatchdogLog, log, reader, server, watchdog, watcher};

#[derive(Parser, Debug)]
#[command(
    name = "zicato-supervisor",
    version,
    about = "Watchdog + dashboard server for the zicato runtime state files."
)]
struct Cli {
    /// Path to the zicato workspace (default: ./.zicato)
    #[arg(long, default_value = ".zicato")]
    workspace: PathBuf,

    /// HTTP server port (default: 7920; +1 retry up to 7930).
    ///
    /// Distinct from the Python dashboard service's default (7892) so
    /// the watchdog's `/statusz` surface and the dashboard UI do not
    /// contend for the same port: when `zicato evolve` spawns both, a
    /// shared default would make the second binder walk `+1` and the
    /// reported URL point at the wrong server. The two walk ranges
    /// (7920-7930 here, 7892-7902 for the dashboard) are disjoint.
    #[arg(long, default_value_t = 7920)]
    port: u16,

    /// Bind address (default: 127.0.0.1)
    #[arg(long, default_value = "127.0.0.1")]
    bind: IpAddr,

    /// Disable control-file writing (POST endpoints return 403)
    #[arg(long, default_value_t = false)]
    read_only: bool,

    /// Run as the watchdog only: do not mount the dashboard UI, the
    /// analytical `/api/*` routes, the SSE stream, or the control
    /// endpoints. The watchdog's own `/statusz` surface stays available.
    #[arg(long, default_value_t = false)]
    no_dashboard: bool,

    /// Heartbeat staleness check interval (seconds)
    #[arg(long, default_value_t = 2)]
    interval: u64,

    /// Warn after this many seconds without heartbeat
    #[arg(long, default_value_t = 30)]
    heartbeat_stale_warn: u64,

    /// SIGKILL orchestrator after this many seconds without heartbeat
    #[arg(long, default_value_t = 90)]
    heartbeat_stale_kill: u64,

    /// Warn for stalled run after this many seconds
    #[arg(long, default_value_t = 30)]
    run_stale_warn: u64,

    /// Kill stalled run after this many seconds
    #[arg(long, default_value_t = 120)]
    run_stale_kill: u64,

    /// Disable per-run wall-clock deadline enforcement. Deadline killing
    /// is on by default; pass this to attach a read-only observability
    /// supervisor to a run it should not police.
    #[arg(long, default_value_t = false)]
    run_deadline_kill_disabled: bool,

    /// Grace window (seconds) between SIGTERM and SIGKILL when killing a
    /// run that has exceeded its wall-clock deadline.
    #[arg(long, default_value_t = 5)]
    run_kill_grace: u64,

    /// Hard ceiling (seconds) on a single run's enforced wall-clock window,
    /// measured from its `started_at`. The orchestrator-written per-run
    /// deadline is untrusted: a far-future value would disable the watchdog.
    /// The deadline path clamps the enforced cutoff to
    /// `started_at + max_run_seconds`, so a run is always killable. The
    /// default (6h) is well above any normal per-board budget; it only fires
    /// on an implausible deadline.
    #[arg(long, default_value_t = 6 * 3600)]
    max_run_seconds: u64,

    /// Enable diff-containment attestation (INTEGRITY NOTARY record #2).
    ///
    /// When set, each watchdog tick independently recomputes the on-disk diff
    /// of every materialised child generation snapshot against its parent and
    /// ALARMS when a file OUTSIDE the registered mutable surface differs — a
    /// mutation that escaped its sandbox. Alarm-only / read-only in v1: it
    /// writes a quarantine finding into the epoch health dir and surfaces a
    /// hard ALERT on `/statusz`, but never blocks a promotion. Off by default
    /// (the scan is purely additive — absent, the supervisor behaves exactly
    /// as before).
    #[arg(long, default_value_t = false)]
    diff_containment: bool,

    /// Enable promotion gatekeeping (INTEGRITY NOTARY record #3).
    ///
    /// When set, each watchdog tick re-applies the gate's scalar rule to every
    /// recorded promotion in the current epoch and ALARMS when a promotion is
    /// not supported by the recorded scores (a recorded decision contradicting
    /// its own evidence). Alarm-only / read-only in v1: it surfaces on
    /// `/statusz` and (when a ledger is configured) records a hard alert, but
    /// never blocks a promotion. Off by default.
    #[arg(long, default_value_t = false)]
    promotion_gate: bool,

    /// Enable the index-vs-canonical divergence auditor (INTEGRITY NOTARY
    /// record #4).
    ///
    /// When set, each watchdog tick joins the canonical lineage / epoch config
    /// against the SQLite index and flags promoted / parent / decision
    /// divergence, contract-hash mismatch / malformed hashes, and a stuck
    /// in-flight generation (dead worker, never resolved past the age
    /// threshold). Read-only: it only reports (on `/statusz` and, when a
    /// ledger is configured, the ledger). Off by default.
    #[arg(long, default_value_t = false)]
    divergence_audit: bool,

    /// Age (seconds) past which a divergence-audit stuck in-flight generation
    /// (dead worker, unresolved) is reported. Only consulted with
    /// `--divergence-audit`.
    #[arg(long, default_value_t = 3600)]
    divergence_stuck_age_seconds: i64,

    /// Directory for the supervisor's tamper-evident audit ledger.
    ///
    /// When set, the supervisor opens (or creates) a persisted, append-only,
    /// hash-chained `audit_ledger.jsonl` under this directory and records its
    /// watchdog actions and observed promote/reject/contract-change
    /// transitions into it. The directory should live OUTSIDE the
    /// orchestrator's mutable trees (the supervisor's OWN runtime dir) so the
    /// orchestrator cannot rewrite the ledger it is being audited against.
    /// Absent (the default) → no ledger is written and the supervisor behaves
    /// exactly as before. `/statusz` and `/api/audit/verify` surface the
    /// chain's integrity when a ledger is configured.
    #[arg(long)]
    ledger_dir: Option<PathBuf>,

    /// Log level (default: info)
    #[arg(long, default_value = "info")]
    log: String,

    /// Detach and run in the background (for `zicato dashboard --daemon`)
    #[arg(long, default_value_t = false)]
    daemon: bool,
}

#[tokio::main]
async fn main() -> std::process::ExitCode {
    let cli = Cli::parse();
    log::init(&cli.log);

    if cli.daemon {
        // Best-effort fork into background. We avoid pulling in a full
        // daemonization crate; the parent simply exits after kicking the
        // worker. Logs continue to stderr (the operator is expected to
        // redirect them).
        match daemonize() {
            Ok(true) => return std::process::ExitCode::SUCCESS,
            Ok(false) => {} // child continues
            Err(e) => {
                error!(error=%e, "daemonize failed; running in foreground");
            }
        }
    }

    let workspace = match cli.workspace.canonicalize() {
        Ok(p) => p,
        Err(_) => {
            // Workspace may not exist yet; create it.
            if let Err(e) = std::fs::create_dir_all(&cli.workspace) {
                error!(?cli.workspace, error=%e, "cannot create workspace");
                return std::process::ExitCode::FAILURE;
            }
            cli.workspace
                .canonicalize()
                .unwrap_or(cli.workspace.clone())
        }
    };

    let paths = reader::WorkspacePaths::new(workspace);
    info!(workspace=?paths.workspace, "starting zicato-supervisor");

    // Channels.
    let (watch_tx, _) = broadcast::channel::<watcher::WatchEvent>(256);
    let (shutdown_tx, _) = broadcast::channel::<()>(8);

    // Filesystem watcher (kept in scope for the program lifetime).
    let _watcher = match watcher::spawn(paths.clone(), watch_tx.clone(), Duration::from_millis(100))
    {
        Ok(w) => Some(w),
        Err(e) => {
            error!(error=%e, "failed to start filesystem watcher");
            None
        }
    };

    // Watchdog tasks.
    let thresholds = watchdog::Thresholds {
        heartbeat_stale_warn: Duration::from_secs(cli.heartbeat_stale_warn),
        heartbeat_stale_kill: Duration::from_secs(cli.heartbeat_stale_kill),
        run_stale_warn: Duration::from_secs(cli.run_stale_warn),
        run_stale_kill: Duration::from_secs(cli.run_stale_kill),
        grace: Duration::from_secs(5),
        run_kill_grace: Duration::from_secs(cli.run_kill_grace),
        run_deadline_kill_disabled: cli.run_deadline_kill_disabled,
        max_run_seconds: Duration::from_secs(cli.max_run_seconds.max(1)),
    };
    if cli.run_deadline_kill_disabled {
        info!("per-run wall-clock deadline enforcement is disabled");
    }
    if cli.no_dashboard {
        info!("watchdog-only mode: dashboard routes disabled; /statusz still served");
    }

    // Shared in-memory ring buffer: the watchdog loops record their
    // escalations here, and `/statusz` reads them back.
    let action_log = Arc::new(WatchdogLog::new());

    // Shared heartbeat seq-liveness tracker: the heartbeat loop advances it
    // each tick; `/statusz` reads (does not advance) it to report the same
    // seq-change age the watchdog is deciding on.
    let seq_liveness = Arc::new(std::sync::Mutex::new(watchdog::SeqLiveness::new()));

    // Cumulative torn-write / non-monotonic-seq counters over the canonical
    // active-tournament JSONL fold; the fold path accumulates, `/statusz`
    // surfaces.
    let fold_diagnostics = Arc::new(zicato_supervisor::fold_stats::FoldDiagnostics::new());

    // Tamper-evident audit ledger (INTEGRITY NOTARY). Opt-in: only created
    // when `--ledger-dir` is set, so the default supervisor behaves exactly
    // as before. When present, the watchdog loops record their actions into
    // it and `/statusz` + `/api/audit/verify` surface the chain's integrity.
    let ledger = cli.ledger_dir.as_ref().map(|dir| {
        let led = Arc::new(zicato_supervisor::ledger::AuditLedger::open(dir));
        info!(path=?led.path(), "tamper-evident audit ledger enabled");
        // Mark the supervisor's start as the first record of this session so
        // the chain has a fresh anchor an operator can correlate to a restart.
        led.append(
            zicato_supervisor::ledger::RecordKind::SupervisorStart,
            serde_json::json!({
                "build": zicato_supervisor::server::build_id(),
                "workspace": paths.workspace.display().to_string(),
            }),
        );
        led
    });

    // Diff-containment findings store (INTEGRITY NOTARY record #2). The runs
    // loop scans materialised generations and records the latest result here;
    // `/statusz` surfaces it. Shared regardless of the flag (the loop only
    // writes into it when `--diff-containment` is set, so it stays empty/
    // not-scanned otherwise).
    let diff_findings =
        Arc::new(zicato_supervisor::diff_containment::DiffContainmentFindings::new());
    if cli.diff_containment {
        info!("diff-containment attestation enabled (alarm-only)");
    }

    // Promotion-gatekeeping findings store (INTEGRITY NOTARY record #3).
    let promotion_gate_findings =
        Arc::new(zicato_supervisor::promotion_gate::PromotionGateFindings::new());
    if cli.promotion_gate {
        info!("promotion gatekeeping enabled (alarm-only)");
    }

    // Divergence-audit findings store (INTEGRITY NOTARY record #4).
    let divergence_findings =
        Arc::new(zicato_supervisor::divergence::DivergenceFindings::new());
    if cli.divergence_audit {
        info!("index-vs-canonical divergence auditor enabled (read-only)");
    }

    let interval = Duration::from_secs(cli.interval.max(1));
    let hb_paths = paths.clone();
    let hb_shutdown = shutdown_tx.clone();
    let hb_log = action_log.clone();
    let hb_seq = seq_liveness.clone();
    tokio::spawn(async move {
        watchdog::heartbeat_loop(hb_paths, thresholds, interval, hb_log, hb_seq, hb_shutdown).await
    });
    let run_paths = paths.clone();
    let run_shutdown = shutdown_tx.clone();
    let run_log = action_log.clone();
    let run_ledger = ledger.clone();
    let run_diff = diff_findings.clone();
    let run_gate = promotion_gate_findings.clone();
    let run_divergence = divergence_findings.clone();
    let diff_enabled = cli.diff_containment;
    let gate_enabled = cli.promotion_gate;
    let divergence_enabled = cli.divergence_audit;
    let divergence_stuck_age = cli.divergence_stuck_age_seconds;
    tokio::spawn(async move {
        watchdog::runs_loop(
            run_paths,
            thresholds,
            interval,
            run_log,
            run_ledger,
            watchdog::DiffContainmentConfig {
                enabled: diff_enabled,
                findings: run_diff,
            },
            watchdog::PromotionGateConfig {
                enabled: gate_enabled,
                findings: run_gate,
            },
            watchdog::DivergenceConfig {
                enabled: divergence_enabled,
                findings: run_divergence,
                stuck_age_seconds: divergence_stuck_age,
            },
            run_shutdown,
        )
        .await
    });

    // HTTP server.
    let handle = match server::serve(
        paths.clone(),
        cli.bind,
        cli.port,
        server::ServeOptions {
            read_only: cli.read_only,
            dashboard_disabled: cli.no_dashboard,
            heartbeat_stale_threshold_seconds: cli.heartbeat_stale_warn,
            action_log: action_log.clone(),
            seq_liveness: seq_liveness.clone(),
            fold_diagnostics: fold_diagnostics.clone(),
            ledger: ledger.clone(),
            diff_findings: diff_findings.clone(),
            promotion_gate_findings: promotion_gate_findings.clone(),
            divergence_findings: divergence_findings.clone(),
        },
        watch_tx.clone(),
        shutdown_tx.clone(),
    )
    .await
    {
        Ok(h) => h,
        Err(e) => {
            error!(error=%e, "failed to start HTTP server");
            return std::process::ExitCode::FAILURE;
        }
    };
    println!("zicato-supervisor listening on http://{}", handle.addr);

    // Wait for signals.
    let mut sigterm = match signal(SignalKind::terminate()) {
        Ok(s) => s,
        Err(e) => {
            error!(error=%e, "failed to install SIGTERM handler");
            return std::process::ExitCode::FAILURE;
        }
    };
    let mut sigint = match signal(SignalKind::interrupt()) {
        Ok(s) => s,
        Err(e) => {
            error!(error=%e, "failed to install SIGINT handler");
            return std::process::ExitCode::FAILURE;
        }
    };

    tokio::select! {
        _ = sigterm.recv() => info!("received SIGTERM; shutting down"),
        _ = sigint.recv() => info!("received SIGINT; shutting down"),
    }
    let _ = shutdown_tx.send(());
    // Give the server a moment to drain.
    tokio::time::sleep(Duration::from_millis(200)).await;
    std::process::ExitCode::SUCCESS
}

/// Best-effort daemonization. Returns `Ok(true)` in the parent (which
/// should exit immediately) and `Ok(false)` in the child.
fn daemonize() -> std::io::Result<bool> {
    // SAFETY: fork()/setsid() are required for daemonization. We do not
    // touch shared state between fork and exec; we are not threaded yet
    // when this runs.
    use std::os::unix::io::AsRawFd;
    let pid = unsafe { libc::fork() };
    if pid < 0 {
        return Err(std::io::Error::last_os_error());
    }
    if pid > 0 {
        // Parent.
        return Ok(true);
    }
    // Child: detach from controlling terminal.
    unsafe {
        if libc::setsid() < 0 {
            return Err(std::io::Error::last_os_error());
        }
        // Redirect stdin to /dev/null. Keep stdout/stderr so logs still flow.
        if let Ok(f) = std::fs::File::open("/dev/null") {
            let fd = f.as_raw_fd();
            libc::dup2(fd, libc::STDIN_FILENO);
        }
    }
    Ok(false)
}
